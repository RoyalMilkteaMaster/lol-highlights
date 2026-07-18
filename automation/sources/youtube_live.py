"""YouTube live finder：找指定頻道的 upcoming / live 直播。

雙來源設計（user 已選）：
1. **Primary**：YouTube Data API v3（quota 10000/day，5 channels 一天 ~50 次掃描很夠用）
   兩段式（ChatGPT #4）：
     a. search.list(eventType=upcoming/live) → 拿 video_ids
     b. videos.list(part=snippet,liveStreamingDetails) → 拿 scheduledStartTime / actualStart

2. **Fallback**：yt-dlp `--flat-playlist <channel>/streams`（quota 用盡或沒 API key 時）

任一路徑回傳 list[BroadcastDraft]，給 pipeline 後續配對 series。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone

from automation.transformers.types import BroadcastDraft

logger = logging.getLogger(__name__)


# ── 自訂例外 ─────────────────────────────────────────────────────────────
class NoApiKey(Exception):
    """沒設 YOUTUBE_API_KEY。"""


class QuotaExceeded(Exception):
    """API 配額用盡（403 quotaExceeded）。"""


class YouTubeAPIError(Exception):
    """API 一般錯誤（網路 / 認證等）。"""


# ── Public API ────────────────────────────────────────────────────────────
class YouTubeLiveFinder:
    """找指定頻道的 upcoming + live 直播。

    Args:
        channels: list of dict，每項要含：
            channel_id : YouTube 頻道 ID（UC...）
            league     : 對應的 league code（'LCK'/'LCP'）
            timezone   : league 在地時區（'Asia/Seoul'/'Asia/Taipei'）
            priority   : (optional) 同 league 多 channel fallback 順序，數字小優先（default 999）
            name       : (optional) 顯示用，例：LCKCarry / LCKglobal
            lang       : (optional) 'zh' / 'en'，純標籤
        api_key: YouTube Data API v3 key（None 會走 yt-dlp fallback）

    同 league 多 channel fallback —
      channels 依 priority asc 排序，inner loop 用 set 跟蹤已找到結果的 league →
      LCK 先試 LCKCarry，找到任何 upcoming/live 就 skip LCKglobal；都找不到才退英文。
    """

    def __init__(self, channels: list[dict], api_key: str | None = None) -> None:
        # 依 (league, priority) 排序：同 league 內 priority 小的優先
        self.channels = sorted(channels, key=lambda c: (c.get("league", ""), c.get("priority", 999)))
        self.api_key  = api_key or os.getenv("YOUTUBE_API_KEY") or None

    # ── 主流程 ───────────────────────────────────────────────────────────
    def fetch(self, days_ahead: int = 7) -> list[BroadcastDraft]:
        """主流程：先試 API，失敗 fallback yt-dlp。

        days_ahead 暫保留給未來篩選 scheduledStartTime 用（YT API 預設不限定時間）。
        """
        try:
            return self._fetch_via_api()
        except NoApiKey:
            logger.warning("沒設 YOUTUBE_API_KEY，fallback 到 yt-dlp")
            return self._fetch_via_ytdlp()
        except QuotaExceeded:
            logger.warning("YouTube API quota 用盡，fallback 到 yt-dlp")
            return self._fetch_via_ytdlp()
        except YouTubeAPIError as e:
            logger.warning("YouTube API 錯誤：%s，fallback 到 yt-dlp", e)
            return self._fetch_via_ytdlp()

    # ── Path A：YouTube Data API v3（兩段式）─────────────────────────────
    def _fetch_via_api(self) -> list[BroadcastDraft]:
        if not self.api_key:
            raise NoApiKey

        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        try:
            service = build("youtube", "v3", developerKey=self.api_key, cache_discovery=False)
        except Exception as e:
            raise YouTubeAPIError(f"建立 YouTube service 失敗：{e}") from e

        drafts: list[BroadcastDraft] = []
        # 同 league 已抓到結果就 skip 後續 fallback channel
        leagues_with_drafts: set[str] = set()
        for ch in self.channels:
            channel_id = ch["channel_id"]
            league_code = ch["league"]
            tz_name = ch.get("timezone", "UTC")
            ch_label = ch.get("name") or channel_id[:8]

            if league_code in leagues_with_drafts:
                logger.debug("[%s fallback skip] %s 已被優先 channel 抓到", league_code, ch_label)
                continue

            try:
                video_ids = self._search_videos(service, channel_id)
            except HttpError as e:
                if self._is_quota_error(e):
                    raise QuotaExceeded from e
                logger.warning("頻道 %s search.list 失敗：%s", channel_id, e)
                continue
            except Exception as e:
                raise YouTubeAPIError(f"search.list 失敗：{e}") from e

            if not video_ids:
                logger.info("頻道 %s (%s) 目前沒有 upcoming/live", ch_label, league_code)
                continue

            try:
                details = self._get_video_details(service, video_ids)
            except HttpError as e:
                if self._is_quota_error(e):
                    raise QuotaExceeded from e
                logger.warning("videos.list 失敗：%s", e)
                continue

            before = len(drafts)
            for v in details:
                draft = self._build_draft(v, channel_id, league_code, tz_name)
                if draft:
                    drafts.append(draft)
            if len(drafts) > before:
                leagues_with_drafts.add(league_code)
                logger.info("[OK] %s 用 %s (lang=%s) 抓到 %d 直播",
                            league_code, ch_label, ch.get("lang", "?"), len(drafts) - before)

        logger.info("YouTube API 找到 %d 個直播", len(drafts))
        return drafts

    @staticmethod
    def _search_videos(service, channel_id: str) -> list[str]:
        """search.list 拿 channel 的 upcoming + live video_ids（兩次呼叫）。"""
        ids: list[str] = []
        for event_type in ("upcoming", "live"):
            resp = service.search().list(
                part="id",
                channelId=channel_id,
                eventType=event_type,
                type="video",
                maxResults=10,
            ).execute()
            for item in resp.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid:
                    ids.append(vid)
        return list(dict.fromkeys(ids))   # 去重保序

    @staticmethod
    def _get_video_details(service, video_ids: list[str]) -> list[dict]:
        """videos.list 拿 liveStreamingDetails（scheduledStartTime / actualStartTime）。"""
        if not video_ids:
            return []
        resp = service.videos().list(
            part="snippet,liveStreamingDetails,status",
            id=",".join(video_ids),
            maxResults=50,
        ).execute()
        return resp.get("items", [])

    @staticmethod
    def _is_quota_error(e: Exception) -> bool:
        msg = str(e).lower()
        return "quotaexceeded" in msg or "quota" in msg and "403" in msg

    @staticmethod
    def _build_draft(
        video: dict, channel_id: str, league_code: str, tz_name: str,
    ) -> BroadcastDraft | None:
        """videos.list 單個 item → BroadcastDraft。"""
        video_id = video.get("id")
        snippet  = video.get("snippet", {})
        details  = video.get("liveStreamingDetails", {}) or {}
        if not video_id or not snippet:
            return None

        scheduled = _parse_iso_utc(details.get("scheduledStartTime"))
        actual    = _parse_iso_utc(details.get("actualStartTime"))
        # liveBroadcastContent: 'upcoming' / 'live' / 'none'
        status = snippet.get("liveBroadcastContent", "none")

        return BroadcastDraft(
            platform="youtube",
            external_id=video_id,
            channel_id=channel_id,
            league_code=league_code,
            league_timezone=tz_name,
            url=f"https://www.youtube.com/watch?v={video_id}",
            title=snippet.get("title", ""),
            scheduled_start_utc=scheduled,
            actual_start_utc=actual,
            source_status="live" if status == "live" else ("upcoming" if status == "upcoming" else "ended"),
        )

    # ── Path B：yt-dlp fallback ─────────────────────────────────────────
    def _fetch_via_ytdlp(self) -> list[BroadcastDraft]:
        """用 yt-dlp 掃 channel/streams 頁面（沒 API key 或 quota 用盡時）。

        注意：yt-dlp 拿不到 scheduledStartTime（只能拿 upload_date / live_status），
        scheduled_start_utc 會是 None；下游 broadcast_mapper 仍能用標題 + 當日去配對。

        穩定性處理（解 yt-dlp metadata 偶發不全的問題）：
        - YouTube 後端 A/B testing 會偶爾回 lite metadata（live_status 全 None）
        - 偵測到「entries ≥10 但 live_status 全 None」→ sleep 後重試（最多 2 次）
        """
        drafts: list[BroadcastDraft] = []
        # 同 league 已抓到結果就 skip 後續 fallback channel
        leagues_with_drafts: set[str] = set()
        for ch in self.channels:
            channel_id = ch["channel_id"]
            league_code = ch["league"]
            tz_name = ch.get("timezone", "UTC")
            ch_label = ch.get("name") or channel_id[:8]
            channel_url = f"https://www.youtube.com/channel/{channel_id}/streams"

            if league_code in leagues_with_drafts:
                logger.debug("[%s fallback skip] %s 已被優先 channel 抓到", league_code, ch_label)
                continue

            try:
                items = self._run_ytdlp_with_retry(channel_url, league_code)
            except Exception as e:
                logger.warning("yt-dlp 掃 %s 失敗：%s", channel_url, e)
                continue

            logger.info("yt-dlp 掃 %s [%s] (%s) → %d entries",
                        channel_url, ch_label, league_code, len(items))
            kept = 0
            for item in items:
                vid = item.get("id")
                title = item.get("title", "")
                live_status = item.get("live_status", "")
                if not vid:
                    continue
                # yt-dlp live_status：'is_upcoming' / 'is_live' / 'was_live' / 'not_live'
                if live_status not in ("is_upcoming", "is_live"):
                    continue
                kept += 1
                drafts.append(BroadcastDraft(
                    platform="youtube",
                    external_id=vid,
                    channel_id=channel_id,
                    league_code=league_code,
                    league_timezone=tz_name,
                    url=f"https://www.youtube.com/watch?v={vid}",
                    title=title,
                    scheduled_start_utc=None,        # yt-dlp flat-playlist 拿不到
                    actual_start_utc=None,
                    source_status="live" if live_status == "is_live" else "upcoming",
                ))
            logger.info("  → %s [%s] 留下 %d 個（is_upcoming/is_live）",
                        league_code, ch_label, kept)
            if kept > 0:
                leagues_with_drafts.add(league_code)
                logger.info("[OK] %s 用 %s (lang=%s) 抓到，後續 fallback channel 不掃",
                            league_code, ch_label, ch.get("lang", "?"))

        logger.info("yt-dlp fallback 找到 %d 個直播", len(drafts))
        return drafts

    @classmethod
    def _run_ytdlp_with_retry(cls, url: str, label: str, max_retries: int = 2) -> list[dict]:
        """跑 yt-dlp，遇到 metadata 全空時重試（解 YouTube A/B testing 不穩）。

        判斷策略：
        - 如果 entries 多於 10 個但全部 live_status=None → 視為解析失敗
        - sleep 2~4s 後重試，最多 max_retries 次
        - 仍失敗就回傳當下結果（避免完全 0 個）
        """
        import time
        last_items: list[dict] = []
        for attempt in range(max_retries + 1):
            items = cls._run_ytdlp(url)
            last_items = items
            # 至少要 10 個 entries 才能判斷 metadata 是不是壞的
            if len(items) < 10:
                return items
            non_none = sum(1 for e in items if e.get("live_status") is not None)
            if non_none > 0:
                # 有任何 entry 拿到 live_status → 認為這次解析 OK
                if attempt > 0:
                    logger.info(
                        "  yt-dlp %s 第 %d 次重試成功（%d/%d 有 live_status）",
                        label, attempt, non_none, len(items),
                    )
                return items
            # 全部 None → 重試
            if attempt < max_retries:
                wait = 2 + attempt * 2   # 2s, 4s
                logger.warning(
                    "  yt-dlp %s 回 %d entries 但 live_status 全 None，sleep %ds 後重試（第 %d/%d 次）",
                    label, len(items), wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
                continue
            logger.warning(
                "  yt-dlp %s 重試 %d 次後 live_status 仍全 None，放棄重試（會少抓直播）",
                label, max_retries,
            )
        return last_items

    @staticmethod
    def _run_ytdlp(url: str) -> list[dict]:
        """跑 yt-dlp --flat-playlist --dump-single-json，回傳 entries list。

        設計：
        - 用 channel `/streams` 端點（YouTube 頻道的「直播」分頁，只含 live/upcoming/past streams）
        - 用 `python -m yt_dlp` 避免 PATH 問題
        - timeout 120s（LCP /streams 有 1397 個影片，實測完整掃要 ~20s）
        - **不**用 `--playlist-items` 限縮：它會觸發 yt-dlp fast-path，
          live_status 全變 None 無法過濾（已實測 LCK ch 用 1:20 變 None）
        - 過濾改在 Python 端做（_fetch_via_ytdlp 的 live_status 比對）
        """
        import sys
        cmd = [
            sys.executable,
            "-m", "yt_dlp",
            "--flat-playlist",
            "--dump-single-json",
            "--quiet",
            "--no-warnings",
            url,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp 失敗 (rc={result.returncode}): {result.stderr[:200]}")
        if not result.stdout.strip():
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        return data.get("entries", []) or []


# ── helper：ISO8601 → datetime(UTC) ──────────────────────────────────────
def _parse_iso_utc(iso_str: str | None) -> datetime | None:
    """YouTube API 回傳格式如 '2026-05-06T10:00:00Z'。"""
    if not iso_str:
        return None
    try:
        # Python 3.11+ 才支援 'Z' 後綴；3.10 要手動處理
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None
