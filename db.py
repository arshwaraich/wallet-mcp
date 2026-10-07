"""SQLite usage log for the wallet-mcp dashboard, plus IP/global daily rate limits.

MCP and REST API calls share one log, so the limits cover both combined."""
import datetime
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("WALLET_MCP_DB", Path(__file__).parent / "data" / "wallet-mcp.db"))
DB_PATH.parent.mkdir(exist_ok=True)

_lock = threading.Lock()

PER_IP_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_IP_LIMIT", 30))
GLOBAL_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_GLOBAL_LIMIT", 500))
# The per-IP refusal. Past either limit a caller can pay per call (payments.py). The usage watcher matches both texts.
IP_LIMIT_ERROR = f"rate limit exceeded: max {PER_IP_DAILY_LIMIT} passes/day per caller"


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
        # source = "mcp" or "api"; rows from before the REST API existed were all MCP.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
        if "source" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN source TEXT NOT NULL DEFAULT 'mcp'")
        # action = "create" or "update"; rows from before updates existed were all creates.
        if "action" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN action TEXT NOT NULL DEFAULT 'create'")
        # client = MCP clientInfo "name/version" or the User-Agent; x402 = 1 if the caller sent an
        # x402 payment. Both recorded to gauge payment support before pricing; old rows are NULL.
        if "client" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN client TEXT")
        if "x402" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN x402 INTEGER")
        # payment_tx = the on-chain transaction of a call paid for with x402 past the free limit.
        if "payment_tx" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN payment_tx TEXT")
        # Updatable passes only (updatable=True). Unlike the request log, this keeps the
        # pass's full contents, since an update rebuilds the pass from them.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS passes (
                serial TEXT PRIMARY KEY,
                auth_token TEXT NOT NULL,
                edit_hash TEXT NOT NULL,
                params TEXT NOT NULL,
                pkpass BLOB NOT NULL,
                created TEXT NOT NULL,
                updated INTEGER NOT NULL,
                expires INTEGER NOT NULL
            )
            """
        )
        # Passes stored before update windows existed have no expiry; purge them on the next sweep.
        pass_columns = {row["name"] for row in conn.execute("PRAGMA table_info(passes)")}
        if "expires" not in pass_columns:
            conn.execute("ALTER TABLE passes ADD COLUMN expires INTEGER NOT NULL DEFAULT 0")
        # Devices that installed an updatable pass, registered by Wallet itself.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                device TEXT NOT NULL,
                serial TEXT NOT NULL,
                push_token TEXT NOT NULL,
                PRIMARY KEY (device, serial)
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
            # Calls paid with x402 don't use up the free daily budget.
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND payment_tx IS NULL", (today,)
        ).fetchone()
        if global_count >= GLOBAL_DAILY_LIMIT:
            return "service is at its daily request limit, try again tomorrow"
        (ip_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND ip = ?", (today, ip)
        ).fetchone()
        if ip_count >= PER_IP_DAILY_LIMIT:
            return IP_LIMIT_ERROR
    return None


def log_request(
    *, ip: str, style: str, organization_name: str, success: bool,
    error: str | None, duration_ms: int, serial_number: str | None, source: str, action: str = "create",
    client: str | None = None, x402: bool = False, payment_tx: str | None = None,
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO requests (ts, ip, style, organization_name, success, error, duration_ms, serial_number,
                                     source, action, client, x402, payment_tx)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                ip, style, organization_name, 1 if success else 0, error, duration_ms, serial_number, source, action,
                client, 1 if x402 else 0, payment_tx,
            ),
        )


MAX_DEVICES_PER_PASS = 100


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def pass_exists(serial: str) -> bool:
    with _lock, _conn() as conn:
        return conn.execute("SELECT 1 FROM passes WHERE serial = ?", (serial,)).fetchone() is not None


def store_pass(serial: str, auth_token: str, edit_hash: str, params: str, pkpass: bytes, updated: int,
               expires: int) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO passes (serial, auth_token, edit_hash, params, pkpass, created, updated, expires)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (serial, auth_token, edit_hash, params, pkpass, _now(), updated, expires),
        )


def get_pass(serial: str) -> sqlite3.Row | None:
    """A stored pass still inside its update window (expired ones count as gone before the sweep)."""
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM passes WHERE serial = ? AND expires > ?", (serial, int(time.time()))
        ).fetchone()


def purge_expired_passes() -> int:
    """Delete passes whose update window has ended, with their device registrations."""
    now = int(time.time())
    with _lock, _conn() as conn:
        conn.execute(
            "DELETE FROM registrations WHERE serial IN (SELECT serial FROM passes WHERE expires <= ?)", (now,)
        )
        return conn.execute("DELETE FROM passes WHERE expires <= ?", (now,)).rowcount


def replace_pass(serial: str, params: str, pkpass: bytes, updated: int) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE passes SET params = ?, pkpass = ?, updated = ? WHERE serial = ?",
            (params, pkpass, updated, serial),
        )


def register_device(device: str, serial: str, push_token: str) -> bool | None:
    """True if newly registered, False if already was (token refreshed), None if the pass is full."""
    with _lock, _conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM registrations WHERE device = ? AND serial = ?", (device, serial)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE registrations SET push_token = ? WHERE device = ? AND serial = ?",
                (push_token, device, serial),
            )
            return False
        (count,) = conn.execute("SELECT COUNT(*) FROM registrations WHERE serial = ?", (serial,)).fetchone()
        if count >= MAX_DEVICES_PER_PASS:
            return None
        conn.execute(
            "INSERT INTO registrations (device, serial, push_token) VALUES (?, ?, ?)",
            (device, serial, push_token),
        )
        return True


def unregister_device(device: str, serial: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM registrations WHERE device = ? AND serial = ?", (device, serial))


def drop_push_tokens(serial: str, push_tokens: list[str]) -> None:
    with _lock, _conn() as conn:
        conn.executemany(
            "DELETE FROM registrations WHERE serial = ? AND push_token = ?",
            [(serial, t) for t in push_tokens],
        )


def push_tokens(serial: str) -> list[str]:
    with _lock, _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT push_token FROM registrations WHERE serial = ?", (serial,)
        )]


def device_serials(device: str, updated_since: int | None) -> list[sqlite3.Row]:
    with _lock, _conn() as conn:
        return conn.execute(
            """SELECT p.serial, p.updated FROM registrations r JOIN passes p ON p.serial = r.serial
               WHERE r.device = ? AND p.updated > ? AND p.expires > ?""",
            (device, updated_since or 0, int(time.time())),
        ).fetchall()


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
        by_source = conn.execute(
            "SELECT source, COUNT(*) c FROM requests GROUP BY source ORDER BY c DESC"
        ).fetchall()
        by_day = conn.execute(
            """SELECT substr(ts, 1, 10) day, COUNT(*) c, SUM(success) ok
               FROM requests WHERE ts >= ? GROUP BY day ORDER BY day""",
            (week_ago,),
        ).fetchall()
        recent = conn.execute(
            """SELECT ts, ip, source, style, organization_name, success, error, duration_ms
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
        "by_source": [dict(r) for r in by_source],
        "by_day": [dict(r) for r in by_day],
        "recent": [dict(r) for r in recent],
    }
