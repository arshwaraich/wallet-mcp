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

# Free calls a day per caller IP: the first pass is free, everything after it is paid (2026-11-08).
PER_IP_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_IP_LIMIT", 1))
# Exempt callers (contact_email listed in EXEMPT_FILE, one per line, not in git) keep the old free
# limit: 30 a day, dropping to 20 on IP_LIMIT_DROP_DAY (UTC). WALLET_MCP_EXEMPT_IP_LIMIT pins it.
EXEMPT_FILE = Path(os.environ.get("WALLET_MCP_EXEMPT_FILE", DB_PATH.parent / "free_exempt.txt"))
EXEMPT_IP_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_EXEMPT_IP_LIMIT", 30))
IP_LIMIT_DROP_DAY, IP_LIMIT_AFTER_DROP = "2026-11-18", 20
GLOBAL_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_GLOBAL_LIMIT", 500))
# Paid calls aren't free-limited, but still capped so no one can exhaust the box's disk or CPU.
PAID_PER_IP_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_PAID_IP_LIMIT", 1000))
PAID_GLOBAL_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_PAID_GLOBAL_LIMIT", 10000))
# The per-IP refusal. Past either limit a caller can pay per call (payments.py). The usage watcher matches both texts.
IP_LIMIT_ERROR = "rate limit exceeded: {} free {} a day per caller IP, and this IP has used {} today"


def is_exempt(contact_email: str | None) -> bool:
    """Read on every call, so editing the file takes effect without a restart."""
    if not contact_email:
        return False
    try:
        lines = EXEMPT_FILE.read_text().splitlines()
    except FileNotFoundError:
        return False
    return contact_email.strip().lower() in {l.strip().lower() for l in lines if l.strip() and not l.startswith("#")}


def per_ip_daily_limit(exempt: bool = False) -> int:
    if not exempt:
        return PER_IP_DAILY_LIMIT
    if "WALLET_MCP_EXEMPT_IP_LIMIT" not in os.environ and _today_start() >= IP_LIMIT_DROP_DAY:
        return IP_LIMIT_AFTER_DROP
    return EXEMPT_IP_DAILY_LIMIT


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
        # refused = 1 for a call turned away before any work (a limit, or a payment that failed to verify).
        if "refused" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN refused INTEGER NOT NULL DEFAULT 0")
        # contact_email and x402_ready = what the caller chose to tell us (x402_ready: 1, 0 or NULL for
        # not said). Separate from x402, which records whether this call actually sent a payment.
        if "contact_email" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN contact_email TEXT")
        if "x402_ready" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN x402_ready INTEGER")
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


# Start of the logged error for a call refused over contact_email/x402_ready. Not counted as a call.
CALLER_INFO_ERROR = "contact_email or x402_ready missing or invalid"


def check_rate_limit(ip: str, exempt: bool = False) -> str | None:
    """Returns an error string if the caller should be rejected, else None."""
    today = _today_start()
    with _lock, _conn() as conn:
        (global_count,) = conn.execute(
            # Calls paid with x402 don't use up the free daily budget, and neither do refusals, so a
            # caller retrying past its own limit can't run the shared pool dry for everyone else.
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND payment_tx IS NULL AND refused = 0", (today,)
        ).fetchone()
        if global_count >= GLOBAL_DAILY_LIMIT:
            return "service is at its daily request limit, try again tomorrow"
        (ip_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND ip = ? AND COALESCE(error, '') NOT LIKE ?",
            (today, ip, CALLER_INFO_ERROR + "%"),
        ).fetchone()
        limit = per_ip_daily_limit(exempt)
        if ip_count >= limit:
            return IP_LIMIT_ERROR.format(limit, "pass" if limit == 1 else "passes", "it" if limit == 1 else "them")
    return None


def check_paid_limit(ip: str) -> str | None:
    """Returns an error string if the caller can't buy another call today, else None."""
    today = _today_start()
    with _lock, _conn() as conn:
        (global_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND payment_tx IS NOT NULL", (today,)
        ).fetchone()
        if global_count >= PAID_GLOBAL_DAILY_LIMIT:
            return "service is at its daily request limit for paid passes too, try again tomorrow"
        (ip_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND ip = ? AND payment_tx IS NOT NULL", (today, ip)
        ).fetchone()
        if ip_count >= PAID_PER_IP_DAILY_LIMIT:
            return f"rate limit exceeded: max {PAID_PER_IP_DAILY_LIMIT} paid passes/day per caller, on top of the free ones"
    return None


def log_request(
    *, ip: str, style: str, organization_name: str, success: bool,
    error: str | None, duration_ms: int, serial_number: str | None, source: str, action: str = "create",
    client: str | None = None, x402: bool = False, payment_tx: str | None = None, refused: bool = False,
    contact_email: str | None = None, x402_ready: bool | None = None,
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO requests (ts, ip, style, organization_name, success, error, duration_ms, serial_number,
                                     source, action, client, x402, payment_tx, refused, contact_email, x402_ready)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                ip, style, organization_name, 1 if success else 0, error, duration_ms, serial_number, source, action,
                client, 1 if x402 else 0, payment_tx, 1 if refused else 0, contact_email,
                None if x402_ready is None else int(x402_ready),
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


def delete_pass(serial: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM registrations WHERE serial = ?", (serial,))
        conn.execute("DELETE FROM passes WHERE serial = ?", (serial,))


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
            """SELECT ts, ip, source, style, organization_name, success, error, duration_ms,
                      contact_email, x402_ready
               FROM requests ORDER BY id DESC LIMIT 25"""
        ).fetchall()
        # SQLite takes the bare x402_ready from the MAX(id) row, so this is each email's latest answer.
        contacts = conn.execute(
            """SELECT LOWER(contact_email) email, x402_ready, COUNT(*) calls, MAX(ts) last_ts, MAX(id) last_id
               FROM requests WHERE contact_email IS NOT NULL
               GROUP BY LOWER(contact_email) ORDER BY last_id DESC"""
        ).fetchall()
        caller_info_refused, = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE error LIKE ?", (CALLER_INFO_ERROR + "%",)
        ).fetchone()
        # By IP, so a caller that rotates IPs can read as not having got through.
        refused_ips, refused_ips_through = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(EXISTS(
                   SELECT 1 FROM requests s WHERE s.ip = f.ip AND s.success = 1 AND s.id > f.last_id
               )), 0)
               FROM (SELECT ip, MAX(id) last_id FROM requests WHERE error LIKE ? GROUP BY ip) f""",
            (CALLER_INFO_ERROR + "%",),
        ).fetchone()
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
        "caller_info": {
            "emails": len(contacts),
            "x402_ready_yes": sum(c["x402_ready"] == 1 for c in contacts),
            "x402_ready_no": sum(c["x402_ready"] == 0 for c in contacts),
            "x402_ready_unknown": sum(c["x402_ready"] is None for c in contacts),
            "refused_calls": caller_info_refused,
            "refused_ips": refused_ips,
            "refused_ips_through": refused_ips_through,
            "contacts": [dict(c) for c in contacts[:100]],
        },
    }


# ---- Dashboard: the full request log and every aggregate, for the tailnet-only dashboard ----

LOG_COLUMNS = ("id", "ts", "ip", "source", "action", "style", "organization_name", "success", "refused", "error",
               "duration_ms", "serial_number", "client", "x402", "payment_tx", "contact_email", "x402_ready")
# status filter -> SQL condition. "limit" = refused at the free limit (the 402 pay-to-continue path).
LOG_STATUS = {
    "ok": "success = 1",
    "paid": "payment_tx IS NOT NULL",
    "failed": "success = 0 AND refused = 0",
    "refused": "refused = 1",
    "limit": "refused = 1 AND (error LIKE 'rate limit exceeded%' OR error LIKE 'service is at its daily%')",
    "caller_info": "error LIKE '" + CALLER_INFO_ERROR + "%'",
}


def _log_where(status: str | None, source: str | None, action: str | None, q: str | None,
               since: str | None) -> tuple[str, list]:
    where, args = [], []
    if status in LOG_STATUS:
        where.append(LOG_STATUS[status])
    if source in ("mcp", "api"):
        where.append("source = ?"), args.append(source)
    if action in ("create", "update"):
        where.append("action = ?"), args.append(action)
    if since:
        where.append("ts >= ?"), args.append(since)
    if q:
        like = f"%{q.strip()}%"
        where.append("(ip LIKE ? OR contact_email LIKE ? OR organization_name LIKE ? OR client LIKE ? "
                     "OR serial_number LIKE ? OR error LIKE ? OR style LIKE ? OR payment_tx LIKE ?)")
        args += [like] * 8
    return (" WHERE " + " AND ".join(where)) if where else "", args


def request_log(*, before_id: int | None = None, limit: int = 100, status=None, source=None, action=None,
                q=None, since=None) -> dict:
    """Newest first, paged by id. limit=0 returns every matching row (for the CSV export)."""
    where, args = _log_where(status, source, action, q, since)
    with _lock, _conn() as conn:
        (matching,) = conn.execute(f"SELECT COUNT(*) FROM requests{where}", args).fetchone()
        page_where = where + (" AND " if where else " WHERE ") + "id < ?" if before_id else where
        page_args = [*args, before_id] if before_id else args
        sql = f"SELECT {', '.join(LOG_COLUMNS)} FROM requests{page_where} ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in conn.execute(sql, page_args)]
    return {"matching": matching, "rows": rows}


def dashboard_stats(days: int = 30) -> dict:
    today = _today_start()
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days - 1)).strftime(
        "%Y-%m-%dT00:00:00")
    lim = LOG_STATUS["limit"]
    ci = LOG_STATUS["caller_info"]
    counts = f"""COUNT(*) calls, SUM(success) ok, SUM(payment_tx IS NOT NULL) paid,
                 SUM(success = 0 AND refused = 0) failed, SUM({lim}) at_limit, SUM({ci}) caller_info,
                 COUNT(DISTINCT ip) ips, COUNT(DISTINCT LOWER(contact_email)) emails"""
    with _lock, _conn() as conn:
        def one(sql, *a):
            return dict(conn.execute(sql, a).fetchone())

        def many(sql, *a):
            return [dict(r) for r in conn.execute(sql, a)]

        out = {
            "all": one(f"SELECT {counts}, MIN(ts) first_ts FROM requests"),
            "today": one(f"SELECT {counts} FROM requests WHERE ts >= ?", today),
            "week": one(f"SELECT {counts} FROM requests WHERE ts >= ?", (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=6)).strftime("%Y-%m-%dT00:00:00")),
            "by_day": many(f"SELECT substr(ts, 1, 10) day, {counts} FROM requests WHERE ts >= ? "
                           "GROUP BY day ORDER BY day", since),
            "by_style": many("SELECT COALESCE(style, '?') k, COUNT(*) c, SUM(success) ok FROM requests "
                             "GROUP BY k ORDER BY c DESC"),
            "by_source": many("SELECT source k, COUNT(*) c, SUM(success) ok FROM requests GROUP BY k ORDER BY c DESC"),
            "by_action": many("SELECT action k, COUNT(*) c, SUM(success) ok FROM requests GROUP BY k ORDER BY c DESC"),
            "clients": many("""SELECT COALESCE(client, 'unknown') k, COUNT(*) c, SUM(success) ok,
                                      SUM(payment_tx IS NOT NULL) paid, COUNT(DISTINCT ip) ips, MAX(ts) last_ts
                               FROM requests GROUP BY k ORDER BY c DESC"""),
            "errors": many(f"""SELECT substr(error, 1, 90) k, COUNT(*) c, MAX(ts) last_ts FROM requests
                               WHERE success = 0 GROUP BY k ORDER BY c DESC"""),
            # SQLite takes the bare x402_ready from the MAX(id) row, so this is each email's latest answer.
            "contacts": many(f"""SELECT LOWER(contact_email) email, x402_ready, MAX(id) last_id, COUNT(*) calls,
                                        SUM(success) ok, SUM(payment_tx IS NOT NULL) paid, SUM({lim}) at_limit,
                                        COUNT(DISTINCT ip) ips, MIN(ts) first_ts, MAX(ts) last_ts
                                 FROM requests WHERE contact_email IS NOT NULL
                                 GROUP BY LOWER(contact_email) ORDER BY last_id DESC"""),
            "payments": many("""SELECT id, ts, ip, contact_email, client, action, style, organization_name, payment_tx
                                FROM requests WHERE payment_tx IS NOT NULL ORDER BY id DESC"""),
            "ips": many(f"""SELECT ip k, COUNT(*) c, SUM(success) ok, SUM(payment_tx IS NOT NULL) paid,
                                   SUM({lim}) at_limit, COUNT(DISTINCT LOWER(contact_email)) emails, MAX(ts) last_ts
                            FROM requests GROUP BY ip ORDER BY c DESC LIMIT 500"""),
            "updatable_passes": one("SELECT COUNT(*) stored, COALESCE(SUM(expires > strftime('%s','now')), 0) live "
                                    "FROM passes"),
            "registrations": one("SELECT COUNT(*) devices FROM registrations"),
        }
    try:
        exempt = [l.strip() for l in EXEMPT_FILE.read_text().splitlines() if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        exempt = []
    out["limits"] = {"free_per_ip": per_ip_daily_limit(), "exempt_per_ip": per_ip_daily_limit(True),
                     "global_free": GLOBAL_DAILY_LIMIT, "paid_per_ip": PAID_PER_IP_DAILY_LIMIT,
                     "paid_global": PAID_GLOBAL_DAILY_LIMIT, "exempt": exempt}
    out["days"] = days
    return out
