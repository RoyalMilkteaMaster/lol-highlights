"""Automation operating window and manual pause policy."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from automation.infra.config import load_config as _load_config

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PAUSE_PATH = _PROJECT_ROOT / "_tmp" / "logs" / "system_paused.lock"
_DAY_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def _parse_clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _next_window_start(now: datetime, start: time, days: set[int]) -> datetime | None:
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        candidate = datetime.combine(day, start, tzinfo=now.tzinfo)
        if candidate > now:
            return candidate
    return None


def _window_decision(now: datetime, config: dict) -> dict:
    window = config.get("automation_window", {}) or {}
    timezone_name = str(window.get("timezone", "Asia/Taipei"))
    local_now = now.astimezone(ZoneInfo(timezone_name))
    if not window.get("enabled", False):
        return {
            "allowed": True,
            "source": "always",
            "reason": "未啟用固定工作時段",
            "next_change_at": None,
            "timezone": timezone_name,
            "window_enabled": False,
        }

    try:
        start = _parse_clock(str(window.get("start", "00:00")))
        end = _parse_clock(str(window.get("end", "23:59")))
    except ValueError:
        return {
            "allowed": False,
            "source": "config_error",
            "reason": "automation_window 的 start/end 格式必須是 HH:MM",
            "next_change_at": None,
            "timezone": timezone_name,
            "window_enabled": True,
        }

    day_names = [str(day).lower() for day in window.get("days", _DAY_INDEX)]
    days = {_DAY_INDEX[day] for day in day_names if day in _DAY_INDEX}
    if not days:
        return {
            "allowed": False,
            "source": "config_error",
            "reason": "automation_window 沒有有效的 days",
            "next_change_at": None,
            "timezone": timezone_name,
            "window_enabled": True,
        }

    weekday = local_now.weekday()
    previous_weekday = (weekday - 1) % 7
    current_time = local_now.time().replace(tzinfo=None)

    if start == end:
        allowed = weekday in days
        next_change = None if allowed else _next_window_start(local_now, start, days)
    elif start < end:
        allowed = weekday in days and start <= current_time < end
        next_change = (
            datetime.combine(local_now.date(), end, tzinfo=local_now.tzinfo)
            if allowed
            else _next_window_start(local_now, start, days)
        )
    else:
        started_today = weekday in days and current_time >= start
        continued_from_yesterday = previous_weekday in days and current_time < end
        allowed = started_today or continued_from_yesterday
        if started_today:
            next_change = datetime.combine(
                local_now.date() + timedelta(days=1), end, tzinfo=local_now.tzinfo
            )
        elif continued_from_yesterday:
            next_change = datetime.combine(local_now.date(), end, tzinfo=local_now.tzinfo)
        else:
            next_change = _next_window_start(local_now, start, days)

    schedule = f"{','.join(day_names)} {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    return {
        "allowed": allowed,
        "source": "window",
        "reason": "固定工作時段內" if allowed else "固定工作時段外",
        "next_change_at": next_change.isoformat() if next_change else None,
        "timezone": timezone_name,
        "window_enabled": True,
        "schedule": schedule,
    }


def _manual_pause(now: datetime, pause_path: Path) -> dict | None:
    if not pause_path.is_file():
        return None
    try:
        raw = pause_path.read_text(encoding="utf-8").strip()
    except OSError:
        return {"reason": "手動暫停", "until": None}
    if not raw:
        return {"reason": "手動暫停", "until": None}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"reason": "手動暫停（舊版 lock）", "until": None}

    until_text = data.get("until")
    if not until_text:
        return {"reason": data.get("reason") or "手動暫停", "until": None}
    try:
        until = datetime.fromisoformat(str(until_text))
    except ValueError:
        return {"reason": "手動暫停（until 格式錯誤）", "until": None}
    if until.tzinfo is None:
        until = until.replace(tzinfo=now.tzinfo)
    if now >= until.astimezone(now.tzinfo):
        return None
    return {"reason": data.get("reason") or "手動暫停", "until": until}


def automation_status(
    *,
    now: datetime | None = None,
    config: dict | None = None,
    pause_path: Path | None = None,
) -> dict:
    config = _load_config() if config is None else config
    window = config.get("automation_window", {}) or {}
    timezone_name = str(window.get("timezone", "Asia/Taipei"))
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return {
            "allowed": False,
            "source": "config_error",
            "reason": f"unknown timezone: {timezone_name}",
            "next_change_at": None,
            "timezone": timezone_name,
            "window_enabled": bool(window.get("enabled", False)),
            "checked_at": datetime.now().astimezone().isoformat(),
        }
    current = datetime.now(timezone) if now is None else now.astimezone(timezone)
    pause_file = _PAUSE_PATH if pause_path is None else Path(pause_path)

    window_decision = _window_decision(current, config)
    manual = _manual_pause(current, pause_file)
    if manual:
        until = manual["until"]
        return {
            "allowed": False,
            "source": "manual",
            "reason": manual["reason"],
            "next_change_at": until.isoformat() if until else None,
            "timezone": timezone_name,
            "window_enabled": bool(window.get("enabled", False)),
            "checked_at": current.isoformat(),
            "schedule": window_decision.get("schedule"),
        }

    decision = window_decision
    decision["checked_at"] = current.isoformat()
    return decision


def pause_system(
    *,
    until: datetime | None = None,
    reason: str = "手動暫停",
    pause_path: Path | None = None,
) -> Path:
    path = _PAUSE_PATH if pause_path is None else Path(pause_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "until": until.isoformat() if until else None,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def resume_system(*, pause_path: Path | None = None) -> bool:
    path = _PAUSE_PATH if pause_path is None else Path(pause_path)
    if not path.exists():
        return False
    path.unlink()
    return True


def parse_pause_until(value: str, timezone_name: str = "Asia/Taipei") -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace(" ", "T"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed
