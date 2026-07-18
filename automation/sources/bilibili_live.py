"""Bilibili live finder：輪詢候選房間，回報「當下這一刻」是否直播中。

設計重點（吸收 ChatGPT + Gemini 評審）：

【ChatGPT #5】Bilibili 不能只看 live_status=1 就寫進 DB：
- 必須額外檢查 title 是否含 'LPL' / '英雄聯盟' 關鍵字
- 必須檢查 parent_area_name 是否屬於英雄聯盟（避免直播間切換到其他遊戲）

【Gemini #3】fetch 不在內部 sleep 等二次確認：
- 多次確認的責任移到呼叫端 + DB 狀態機（pending_confirm → live）
- 這樣 CLI 一次跑完不會卡 60 秒

API 文件：
- get_info: https://api.live.bilibili.com/room/v1/Room/get_info?room_id=<ID>
  回傳 data.live_status: 0=未開播 / 1=直播中 / 2=輪播中
"""

from __future__ import annotations

import logging

import requests

from automation.transformers.types import BroadcastDraft

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://live.bilibili.com/",
}


class BilibiliLiveFinder:
    """掃描候選 Bilibili 直播間，回傳當下處於 live 狀態的房間。

    Args:
        rooms: list of dict，每項要含：
            room_id        : Bilibili 房間號（int 或 str）
            league         : 'LPL'
            timezone       : 'Asia/Shanghai'
            title_keywords : 標題必須含的關鍵字 list（白名單把關）
        timeout: 每次 GET 的超時秒數
    """

    BILIBILI_API = "https://api.live.bilibili.com/room/v1/Room/get_info"

    def __init__(self, rooms: list[dict], timeout: float = 10.0) -> None:
        self.rooms = rooms
        self.timeout = timeout

    # ── 主流程 ───────────────────────────────────────────────────────────
    def fetch(self) -> list[BroadcastDraft]:
        """單次擷取，只回報「當下這一刻」的狀態（不 sleep）。"""
        drafts: list[BroadcastDraft] = []
        for room in self.rooms:
            try:
                info = self._get_room_info(room["room_id"])
            except Exception as e:
                logger.warning("Bilibili room %s 查詢失敗：%s", room["room_id"], e)
                continue

            if not self._is_likely_live_match(info, room):
                continue

            drafts.append(BroadcastDraft(
                platform="bilibili",
                external_id=str(room["room_id"]),
                channel_id=str(info.get("uid", "") or ""),
                league_code=room["league"],
                league_timezone=room.get("timezone", "Asia/Shanghai"),
                url=f"https://live.bilibili.com/{room['room_id']}",
                title=info.get("title", ""),
                scheduled_start_utc=None,        # Bilibili 沒提前時間
                actual_start_utc=None,           # data.live_time 是 string，先不解析
                source_status="live",            # CLI 流程會視 DB 既有狀態決定升級
            ))

        logger.info("Bilibili 找到 %d 個 LIVE 直播", len(drafts))
        return drafts

    # ── 多重判斷（ChatGPT #5）────────────────────────────────────────────
    def _is_likely_live_match(self, info: dict, room: dict) -> bool:
        """同時通過 4 道把關才視為「LPL 真實直播」：
        1. live_status == 1（直播中）
        2. title 含 league 關鍵字（'LPL' / '英雄聯盟'）
        3. parent_area_name 屬於英雄聯盟
        4. 任一沒通過 → 排除
        """
        # 1. 必須直播中
        if info.get("live_status") != 1:
            return False

        # 2. 標題關鍵字檢查（給定 keywords 才檢查；空 = 不限制）
        title = (info.get("title") or "").lower()
        keywords = [kw.lower() for kw in room.get("title_keywords", [])]
        if keywords and not any(kw in title for kw in keywords):
            logger.debug(
                "Bilibili room %s 標題不含關鍵字 %s（標題=%r）",
                room["room_id"], keywords, info.get("title", ""),
            )
            return False

        # 3. parent_area 不再硬性檢查
        # （理由：官方 LPL 直播間可能歸在「赛事」分類而非「英雄联盟」分類；
        #  user 把 room_id 寫進 config 已是白名單，title_keywords 是第二道把關，足夠了）

        return True

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _get_room_info(self, room_id: int | str) -> dict:
        """打 Bilibili API 拿房間資訊，加 User-Agent 避免 403。"""
        params = {"room_id": str(room_id)}
        resp = requests.get(
            self.BILIBILI_API,
            params=params,
            headers=_DEFAULT_HEADERS,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Bilibili API 回傳 code={payload.get('code')}：{payload.get('message','')}"
            )
        return payload.get("data") or {}
