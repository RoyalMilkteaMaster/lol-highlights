"""共用 dataclass / 型別。

主要：
- BroadcastDraft：YouTube / Bilibili live finder 回傳的中介格式
                   尚未寫 DB；由 broadcast_mapper 對應到 series 後才 upsert
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class BroadcastDraft:
    """單一直播實例（尚未寫 DB 的中介格式）。

    Fields:
        platform           : 'youtube' / 'bilibili'
        external_id        : YT video_id / BL room_id
        channel_id         : YT channel_id / BL room owner uid（可選）
        league_code        : 'LCK' / 'LCP' / 'LPL'
        league_timezone    : 'Asia/Seoul' / 'Asia/Taipei' / 'Asia/Shanghai'
        url                : 直播完整 URL
        title              : 直播標題（用來解析隊伍）
        scheduled_start_utc: 預定開播 UTC datetime（YouTube 有；Bilibili 無）
        actual_start_utc   : 實際開播 UTC datetime（YouTube live 有；upcoming 無）
        source_status      : 'upcoming' / 'live' / 'pending_confirm' / 'ended'
    """
    platform:            str
    external_id:         str
    league_code:         str
    league_timezone:     str
    url:                 str
    title:               str
    channel_id:          str | None      = None
    scheduled_start_utc: datetime | None = None
    actual_start_utc:    datetime | None = None
    source_status:       str             = "upcoming"

    def short_id(self) -> str:
        """產生檔名用的 short_id：YouTube 取 video_id 前 6 字、Bilibili 取 room_id。"""
        if self.platform == "youtube":
            return self.external_id[:6]
        return self.external_id

    def broadcast_date_local(self):
        """以 league_timezone 推 broadcast_date（local date）。

        YouTube：用 scheduled_start_utc 轉 league timezone 取 date
        Bilibili：用 actual_start_utc 或當下時間（fetch 時刻）取 date
        """
        from zoneinfo import ZoneInfo
        from datetime import timezone

        ref = self.scheduled_start_utc or self.actual_start_utc or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return ref.astimezone(ZoneInfo(self.league_timezone)).date()
