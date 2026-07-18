"""Read-only local dashboard for automation health and recent work."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from automation.db.connection import mysql_conn
from automation.infra.control import automation_status
from automation.infra.log_setup import setup_rotating_log

_DASHBOARD_DIR = Path(__file__).resolve().parent
_INDEX_PATH = _DASHBOARD_DIR / "index.html"
_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"
_LOG_FILES = {
    "scheduler": _LOG_DIR / "scheduler.log",
    "clip_worker": _LOG_DIR / "clip_worker.log",
    "live_split_worker": _LOG_DIR / "live_split_worker.log",
    "lpl_downloader": _LOG_DIR / "lpl_downloader.log",
    "recorder": _LOG_DIR / "recorder.log",
    "scraper": _LOG_DIR / "scraper.log",
}
_HOST = "127.0.0.1"
_PORT = 8765

logger = setup_rotating_log("dashboard", _LOG_DIR / "dashboard.log")


def _json_default(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot encode {type(value).__name__}")


def _query(conn, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def _load_workers(conn) -> list[dict]:
    return _query(
        conn,
        """
        SELECT worker_name, host, pid, status, message, last_heartbeat_at,
               TIMESTAMPDIFF(SECOND, last_heartbeat_at, UTC_TIMESTAMP()) AS age_sec
        FROM worker_heartbeats
        ORDER BY worker_name
        """,
    )


def _load_broadcasts(conn) -> list[dict]:
    return _query(
        conn,
        """
        SELECT b.broadcast_id, b.league_code, b.platform, b.title,
               b.scheduled_start_utc, b.recording_status_v2, b.error_message,
               b.updated_at, b.raw_segments_dir, b.recording_path,
               ds.detector_status, ds.detector_phase, ds.last_run_at,
               COALESCE(g.game_count, 0) AS game_count,
               COALESCE(g.cut_count, 0) AS cut_count,
               COALESCE(g.failed_count, 0) AS failed_count
        FROM broadcasts b
        LEFT JOIN broadcast_detector_state ds ON ds.broadcast_id=b.broadcast_id
        LEFT JOIN (
            SELECT broadcast_id, COUNT(*) AS game_count,
                   SUM(status='cut') AS cut_count,
                   SUM(status='failed') AS failed_count
            FROM broadcast_games
            GROUP BY broadcast_id
        ) g ON g.broadcast_id=b.broadcast_id
        WHERE b.scheduled_start_utc BETWEEN
              DATE_SUB(UTC_TIMESTAMP(), INTERVAL 48 HOUR)
              AND DATE_ADD(UTC_TIMESTAMP(), INTERVAL 7 DAY)
           OR b.recording_status_v2 IN ('waiting_stream','recording','merging','post_recording')
        ORDER BY
            CASE WHEN b.recording_status_v2 IN
                ('waiting_stream','recording','merging','post_recording') THEN 0 ELSE 1 END,
            b.scheduled_start_utc DESC
        LIMIT 40
        """,
    )


def _load_jobs(conn) -> list[dict]:
    return _query(
        conn,
        """
        SELECT cj.job_id, cj.game_id, cj.status, cj.retry_count, cj.pid,
               cj.enqueued_at, cj.started_at, cj.ended_at, cj.error_message,
               bg.broadcast_id, bg.game_index, bg.team_a_code, bg.team_b_code
        FROM clip_jobs cj
        JOIN broadcast_games bg ON bg.game_id=cj.game_id
        WHERE cj.enqueued_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
           OR cj.status IN ('pending','running')
        ORDER BY COALESCE(cj.started_at, cj.enqueued_at) DESC
        LIMIT 30
        """,
    )


def _build_alerts(workers: list[dict], broadcasts: list[dict], jobs: list[dict]) -> list[dict]:
    alerts: list[dict] = []
    for worker in workers:
        age_value = worker.get("age_sec")
        age = int(age_value) if age_value is not None else 999999
        if age > 300:
            alerts.append({
                "level": "error",
                "message": f"{worker['worker_name']} heartbeat stopped ({age}s)",
            })
        elif age > 90:
            alerts.append({
                "level": "warn",
                "message": f"{worker['worker_name']} heartbeat delayed ({age}s)",
            })

    for broadcast in broadcasts:
        if broadcast.get("recording_status_v2") == "failed":
            alerts.append({
                "level": "error",
                "message": f"broadcast {broadcast['broadcast_id']} recording failed: "
                           f"{broadcast.get('error_message') or 'no details'}",
            })
        if broadcast.get("detector_status") == "failed":
            alerts.append({
                "level": "warn",
                "message": f"broadcast {broadcast['broadcast_id']} detector failed",
            })

    for job in jobs:
        if job.get("status") == "failed":
            alerts.append({
                "level": "error",
                "message": f"clip job {job['job_id']} failed: "
                           f"{job.get('error_message') or 'no details'}",
            })
    return alerts[:20]


def build_state() -> dict:
    workers: list[dict] = []
    broadcasts: list[dict] = []
    jobs: list[dict] = []
    db_error = None
    try:
        with mysql_conn() as conn:
            workers = _load_workers(conn)
            broadcasts = _load_broadcasts(conn)
            jobs = _load_jobs(conn)
    except Exception as exc:
        db_error = f"{type(exc).__name__}: {exc}"
        logger.exception("dashboard state query failed")

    alerts = _build_alerts(workers, broadcasts, jobs)
    if db_error:
        alerts.insert(0, {"level": "error", "message": f"database unavailable: {db_error}"})

    active_statuses = {"waiting_stream", "recording", "merging", "post_recording"}
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "automation": automation_status(),
        "summary": {
            "workers_healthy": sum(
                row.get("age_sec") is not None and int(row["age_sec"]) <= 90
                for row in workers
            ),
            "workers_total": len(workers),
            "active_broadcasts": sum(
                row.get("recording_status_v2") in active_statuses for row in broadcasts
            ),
            "pending_jobs": sum(row.get("status") == "pending" for row in jobs),
            "running_jobs": sum(row.get("status") == "running" for row in jobs),
            "alerts": len(alerts),
        },
        "workers": workers,
        "broadcasts": broadcasts,
        "jobs": jobs,
        "alerts": alerts,
    }


def _tail_log(path: Path, max_bytes: int = 128 * 1024) -> str:
    if not path.is_file():
        return "[INFO] log file does not exist yet"
    with path.open("rb") as file:
        size = file.seek(0, 2)
        file.seek(max(0, size - max_bytes))
        data = file.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = text.split("\n", 1)[-1]
    return text


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "LoLMonitor/2"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_bytes(_INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/state":
            self._send_json(build_state())
            return
        if parsed.path == "/api/logs":
            name = parse_qs(parsed.query).get("name", ["scheduler"])[0]
            path = _LOG_FILES.get(name)
            if path is None:
                self._send_json({"error": "unknown log"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"name": name, "content": _tail_log(path)})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self._send_json(
            {"error": "dashboard is read-only"},
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    def log_message(self, format_string: str, *args) -> None:
        logger.debug("http: " + format_string, *args)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            default=_json_default,
        ).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer((_HOST, _PORT), DashboardHandler)
    logger.info("dashboard listening on http://%s:%d (read-only)", _HOST, _PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("dashboard stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
