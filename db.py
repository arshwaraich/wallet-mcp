"""SQLite usage log for the wallet-mcp dashboard, plus IP/global daily rate limits.

MCP and REST API calls share one log, so the limits cover both combined."""
import datetime
import hashlib
import ipaddress
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("WALLET_MCP_DB", Path(__file__).parent / "data" / "wallet-mcp.db"))
DB_PATH.parent.mkdir(exist_ok=True)

_lock = threading.Lock()

# Free successful calls a day per caller (IP or contact_email); every call after them is paid.
# Was 1 from 2026-11-08 to 2026-11-10: usage fell ~75% and nobody paid, so it went back up to 10.
PER_IP_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_IP_LIMIT", 10))
# Exempt callers (contact_email listed in EXEMPT_FILE, one per line, not in git) keep the old free
# limit: 30 a day, dropping to 20 on IP_LIMIT_DROP_DAY (UTC). WALLET_MCP_EXEMPT_IP_LIMIT pins it.
EXEMPT_FILE = Path(os.environ.get("WALLET_MCP_EXEMPT_FILE", DB_PATH.parent / "free_exempt.txt"))
EXEMPT_IP_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_EXEMPT_IP_LIMIT", 30))
IP_LIMIT_DROP_DAY, IP_LIMIT_AFTER_DROP = "2026-11-18", 20
GLOBAL_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_GLOBAL_LIMIT", 500))
# Paid calls aren't free-limited, but still capped so no one can exhaust the box's disk or CPU.
PAID_PER_IP_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_PAID_IP_LIMIT", 1000))
# Failed calls don't use up the free pass, so they get their own cap per caller (IP or email, like the free
# pass). The most any caller had failed in a day before this was 8. Paid or not, past it the call is refused.
FAIL_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_FAIL_LIMIT", 20))
FAIL_LIMIT_ERROR = "too many failed calls today: max {} a day per caller; fix the request and try again tomorrow"
PAID_GLOBAL_DAILY_LIMIT = int(os.environ.get("WALLET_MCP_PAID_GLOBAL_LIMIT", 10000))
# The per-IP refusal. Past either limit a caller can pay per call (payments.py). The usage watcher matches both texts.
IP_LIMIT_ERROR = "rate limit exceeded: {} free {} a day per caller (IP address or contact_email), and this caller has used {} today"
# Proxy egress pools shared by many users, where one user also hops between addresses (claude.ai connectors
# from Anthropic's range, Cloudflare Workers): the IP says nothing about who's calling, so only the email counts.
SHARED_EGRESS_NETS = [ipaddress.ip_network(n) for n in os.environ.get(
    "WALLET_MCP_SHARED_EGRESS", "160.79.104.0/21 162.158.0.0/15 172.64.0.0/13 104.16.0.0/13").split()]


def is_shared_egress(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in SHARED_EGRESS_NETS)


def exemptions() -> list[dict]:
    """Lines of EXEMPT_FILE: "email" or "email until=YYYY-MM-DD" (exempt up to the end of the day before,
    UTC, so the change applies from 00:00 UTC on that date). Read on every call, so edits need no restart."""
    try:
        lines = EXEMPT_FILE.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        until = next((p[6:] for p in parts[1:] if p.startswith("until=")), None)
        out.append({"email": parts[0].lower(), "until": until,
                    "active": until is None or _today_start()[:10] < until})
    return out


def is_exempt(contact_email: str | None) -> bool:
    if not contact_email:
        return False
    email = contact_email.strip().lower()
    return any(e["email"] == email and e["active"] for e in exemptions())


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
        # New versions of passes built by a paid batch update, held here until the payment settles and
        # then swapped into `passes` in one transaction (or discarded). On disk, not in memory: a batch
        # holds up to 5000 passes of up to 10 MB. Rows outlive a call only if the process dies mid-update.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pass_staging (
                stage TEXT NOT NULL,
                serial TEXT NOT NULL,
                params TEXT NOT NULL,
                pkpass BLOB NOT NULL,
                created INTEGER NOT NULL,
                PRIMARY KEY (stage, serial)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ip ON requests(ip)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_email ON requests(LOWER(contact_email))")


def _today_start() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00")


# Start of the logged error for a call refused over contact_email/x402_ready. Not counted as a call.
CALLER_INFO_ERROR = "contact_email or x402_ready missing or invalid"


def _caller_count(conn, today: str, ip: str, contact_email: str | None, condition: str) -> int:
    """Today's rows matching condition for this caller: max of by IP and by email (email only on shared egress).
    Organizer-pilot rows (source 'pilot') never count: pilots have their own cap (pilot.py)."""
    email = contact_email.strip().lower() if contact_email else None
    used = 0
    if not is_shared_egress(ip):
        (used,) = conn.execute(f"SELECT COUNT(*) FROM requests WHERE ts >= ? AND ip = ? AND source != 'pilot' AND {condition}",
                               (today, ip)).fetchone()
    if email:
        (by_email,) = conn.execute(
            f"SELECT COUNT(*) FROM requests WHERE ts >= ? AND LOWER(contact_email) = ? AND source != 'pilot' AND {condition}",
            (today, email)).fetchone()
        used = max(used, by_email)
    return used


def check_fail_limit(ip: str, contact_email: str | None = None) -> str | None:
    """Returns an error string if the caller has failed too many calls today, else None. Refusals don't count."""
    with _lock, _conn() as conn:
        if _caller_count(conn, _today_start(), ip, contact_email, "success = 0 AND refused = 0") >= FAIL_DAILY_LIMIT:
            return FAIL_LIMIT_ERROR.format(FAIL_DAILY_LIMIT)
    return None


def check_rate_limit(ip: str, exempt: bool = False, contact_email: str | None = None) -> str | None:
    """Returns an error string if the caller should be rejected, else None.

    A caller's free passes are counted by IP address or contact_email, whichever has used more today,
    so neither switching IPs nor switching emails gets another free pass (on shared egress IPs, only
    the email). Only successful calls count: a failed build or a refusal doesn't use up the free pass."""
    today = _today_start()
    with _lock, _conn() as conn:
        (global_count,) = conn.execute(
            # Calls paid with x402 don't use up the free daily budget, and neither do refusals, so a
            # caller retrying past its own limit can't run the shared pool dry for everyone else.
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND payment_tx IS NULL AND refused = 0 AND source != 'pilot'",
            (today,)
        ).fetchone()
        if global_count >= GLOBAL_DAILY_LIMIT:
            return "service is at its daily request limit, try again tomorrow"
        used = _caller_count(conn, today, ip, contact_email, "success = 1 AND payment_tx IS NULL")
        limit = per_ip_daily_limit(exempt)
        if used >= limit:
            return IP_LIMIT_ERROR.format(limit, "pass" if limit == 1 else "passes", "it" if limit == 1 else "them")
    return None


OWN_IPS = ("149.56.100.54",)  # the VPS itself: our own test calls


def public_stats() -> dict:
    """Aggregate counts for the public landing page: no emails, IPs or pass contents."""
    with _conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) total,
                      SUM(ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')) week,
                      MAX(ts) last
               FROM requests WHERE success = 1 AND action = 'create' AND source != 'pilot'
                 AND ip NOT IN ({','.join('?' * len(OWN_IPS))})""", OWN_IPS).fetchone()
    return {"passes": row["total"] or 0, "passes_7d": row["week"] or 0, "last_pass": row["last"]}


def free_remaining(ip: str, contact_email: str | None = None) -> int:
    """Free passes this caller has left today (same keying as check_rate_limit)."""
    with _conn() as conn:
        used = _caller_count(conn, _today_start(), ip, contact_email, "success = 1 AND payment_tx IS NULL")
    return per_ip_daily_limit(is_exempt(contact_email)) - used


def check_paid_limit(ip: str, count: int = 1) -> str | None:
    """Returns an error string if the caller can't buy `count` more passes today, else None."""
    today = _today_start()
    with _lock, _conn() as conn:
        (global_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND payment_tx IS NOT NULL", (today,)
        ).fetchone()
        if global_count + count > PAID_GLOBAL_DAILY_LIMIT:
            return "service is at its daily request limit for paid passes too, try again tomorrow"
        (ip_count,) = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND ip = ? AND payment_tx IS NOT NULL", (today, ip)
        ).fetchone()
        if ip_count + count > PAID_PER_IP_DAILY_LIMIT:
            left = max(PAID_PER_IP_DAILY_LIMIT - ip_count, 0)
            return (f"rate limit exceeded: max {PAID_PER_IP_DAILY_LIMIT} paid passes/day per caller, on top of the free ones"
                    + (f" ({left} left today, this call needs {count})" if count > 1 else ""))
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


def iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    """How secret tokens are stored (pass edit tokens, pilot and batch keys)."""
    return hashlib.sha256(token.encode()).hexdigest()


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


STAGE_MAX_AGE_SECONDS = 24 * 3600  # staged versions older than this belong to a call that died


def stage_pass(stage: str, serial: str, params: str, pkpass: bytes) -> None:
    with _lock, _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO pass_staging (stage, serial, params, pkpass, created) VALUES (?, ?, ?, ?, ?)",
                     (stage, serial, params, pkpass, int(time.time())))


def discard_stage(stage: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM pass_staging WHERE stage = ?", (stage,))


def swap_stage(stage: str, extra_sql: str | None = None, extra_args: tuple = ()) -> int:
    """In one transaction: replace every pass staged under `stage` with its staged version (bumping
    `updated` like replace_pass), run extra_sql (e.g. saving the batch's new design) and drop the stage.
    Returns how many passes were replaced. On any error nothing changes (the stage is kept)."""
    now = int(time.time())
    with _lock, _conn() as conn:
        replaced = conn.execute(
            """UPDATE passes SET
                   params = (SELECT s.params FROM pass_staging s WHERE s.stage = ? AND s.serial = passes.serial),
                   pkpass = (SELECT s.pkpass FROM pass_staging s WHERE s.stage = ? AND s.serial = passes.serial),
                   updated = MAX(?, updated + 1)
               WHERE serial IN (SELECT serial FROM pass_staging WHERE stage = ?)""",
            (stage, stage, now, stage)).rowcount
        if extra_sql:
            conn.execute(extra_sql, extra_args)
        conn.execute("DELETE FROM pass_staging WHERE stage = ?", (stage,))
    return replaced


def purge_stale_stages() -> int:
    with _lock, _conn() as conn:
        return conn.execute("DELETE FROM pass_staging WHERE created < ?",
                            (int(time.time()) - STAGE_MAX_AGE_SECONDS,)).rowcount


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
    if source in ("mcp", "api", "pilot"):
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


def _local_day_start_utc(tz_min: int, days_back: int = 0) -> str:
    """UTC timestamp of local midnight days_back days ago, local = UTC + tz_min minutes (the browser's zone)."""
    off = datetime.timedelta(minutes=tz_min)
    local = datetime.datetime.now(datetime.timezone.utc) + off
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(days=days_back)
    return (start - off).strftime("%Y-%m-%dT%H:%M:%S")


def dashboard_stats(days: int = 30, tz_min: int = 0) -> dict:
    """Day buckets and the today/week tiles follow the browser's zone (tz_min minutes east of UTC)."""
    today = _local_day_start_utc(tz_min)
    since = _local_day_start_utc(tz_min, days - 1)
    local_day = f"substr(datetime(ts, '{int(tz_min):+d} minutes'), 1, 10)"
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
            "week": one(f"SELECT {counts} FROM requests WHERE ts >= ?", _local_day_start_utc(tz_min, 6)),
            "by_day": many(f"SELECT {local_day} day, {counts} FROM requests WHERE ts >= ? "
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
    exempt = [e["email"] + (f" (until {e['until']})" if e["until"] else "") + ("" if e["active"] else " — ended")
              for e in exemptions()]
    out["limits"] = {"free_per_ip": per_ip_daily_limit(), "exempt_per_ip": per_ip_daily_limit(True),
                     "global_free": GLOBAL_DAILY_LIMIT, "fail_per_caller": FAIL_DAILY_LIMIT, "paid_per_ip": PAID_PER_IP_DAILY_LIMIT,
                     "paid_global": PAID_GLOBAL_DAILY_LIMIT, "exempt": exempt}
    out["days"] = days
    return out
