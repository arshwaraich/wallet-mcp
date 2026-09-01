"""SQLite usage log for the wallet-mcp dashboard, plus IP/global daily rate limits."""
import datetime
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "wallet-mcp.db"
DB_PATH.parent.mkdir(exist_ok=True)

_lock = threading.Lock()

PER_IP_DAILY_LIMIT = 30
GLOBAL_DAILY_LIMIT = 500


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                ip TEXT,
                style TEXT,
                organization_name TEXT,
                success INTEGER NOT NULL,
                error TEXT,
                duration_ms INTEGER,
                serial_number TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ip ON requests(ip)")


def _today_start() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00")


def check_rate_limit(ip: str) -> str | None:
    """Returns an error string if the caller should be rejected, else None."""
    today = _today_start()
    with _lock, _conn() as conn:
        (global_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ?", (today,)
        ).fetchone()
        if global_count >= GLOBAL_DAILY_LIMIT:
            return "service is at its daily request limit, try again tomorrow"
        (ip_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND ip = ?", (today, ip)
        ).fetchone()
        if ip_count >= PER_IP_DAILY_LIMIT:
            return f"rate limit exceeded: max {PER_IP_DAILY_LIMIT} passes/day per caller"
    return None


def log_request(
    *, ip: str, style: str, organization_name: str, success: bool,
    error: str | None, duration_ms: int, serial_number: str | None,
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO requests (ts, ip, style, organization_name, success, error, duration_ms, serial_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                ip, style, organization_name, 1 if success else 0, error, duration_ms, serial_number,
            ),
        )


def stats() -> dict:
    today = _today_start()
    week_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat(
        timespec="seconds"
    )
    with _lock, _conn() as conn:
        total, = conn.execute("SELECT COUNT(*) FROM requests").fetchone()
        today_count, = conn.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?", (today,)).fetchone()
        today_ok, = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND success = 1", (today,)
        ).fetchone()
        week_count, = conn.execute("SELECT COUNT(*) FROM requests WHERE ts >= ?", (week_ago,)).fetchone()
        by_style = conn.execute(
            "SELECT style, COUNT(*) c FROM requests GROUP BY style ORDER BY c DESC"
        ).fetchall()
        by_day = conn.execute(
            """SELECT substr(ts, 1, 10) day, COUNT(*) c, SUM(success) ok
               FROM requests WHERE ts >= ? GROUP BY day ORDER BY day""",
            (week_ago,),
        ).fetchall()
        recent = conn.execute(
            """SELECT ts, ip, style, organization_name, success, error, duration_ms
               FROM requests ORDER BY id DESC LIMIT 25"""
        ).fetchall()
        unique_ips_total, = conn.execute("SELECT COUNT(DISTINCT ip) FROM requests").fetchone()

    return {
        "total": total,
        "today": today_count,
        "today_ok": today_ok,
        "week": week_count,
        "unique_ips_total": unique_ips_total,
        "by_style": [dict(r) for r in by_style],
        "by_day": [dict(r) for r in by_day],
        "recent": [dict(r) for r in recent],
    }
