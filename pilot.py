"""Organizer pilots: one event organizer, one design, one updatable pass per attendee.

The pilot's design is a create_wallet_pass parameter set whose strings may hold {name} {ticket} {type}
{email} placeholders. Each attendee pass is stored in the ordinary `passes` table (so the PassKit web
service and APNs push work unchanged) with an expiry of event_end + 7 days, and tracked here in
pilot_passes. HTTP routes live in server.py; this module is the data layer and the admin CLI:

    .venv/bin/python pilot.py new --org ... --event ... --contact EMAIL --event-end ISO --design design.json
    .venv/bin/python pilot.py list | show ID | preview ID OUT.json
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path

import db
import palette

ID_RE = re.compile(r"^[a-z0-9]{8}$")
KEY_RE = re.compile(r"^([a-z0-9]{8})\.([A-Za-z0-9_-]{16,100})$")
PLACEHOLDER_RE = re.compile(r"\{(name|ticket|type|email)\}")
SAMPLE_ROW = {"name": "Alex Rivera", "ticket": "SAMPLE-0001", "type": "General admission", "email": ""}
GRACE_SECONDS = 7 * 24 * 3600  # passes stay installable/updatable this long after the event ends
DEFAULT_MAX_PASSES = 2000
MAX_ROWS_PER_REQUEST = 1000
UPDATE_KEYS = ("header_fields", "primary_fields", "secondary_fields", "auxiliary_fields", "back_fields",
               "relevant_date", "locations", "change_message")
FIELD_LISTS = ("header_fields", "primary_fields", "secondary_fields", "auxiliary_fields", "back_fields")
PREVIEW_DIR = Path(os.environ.get("WALLET_MCP_PREVIEWS", Path(__file__).parent / "data" / "previews"))

# Set by server.py (which owns pass building) so this module never imports it at import time.
_build = None      # (params, serial, auth_token) -> pkpass bytes
_resolve = None    # params -> params with image URLs fetched to base64
PUBLIC_URL = "https://walletmcppass.com"
ORGANIZER_URL = os.environ.get("WALLET_MCP_ORGANIZER_URL", "https://walletmcppass.com")


def configure(build, resolve, public_url: str) -> None:
    global _build, _resolve, PUBLIC_URL
    _build, _resolve, PUBLIC_URL = build, resolve, public_url


def init() -> None:
    with db._lock, db._conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS pilots (
            id TEXT PRIMARY KEY, key_hash TEXT NOT NULL, org TEXT NOT NULL, event_name TEXT NOT NULL,
            event_end TEXT NOT NULL, event_end_epoch INTEGER NOT NULL, contact_email TEXT NOT NULL,
            design TEXT NOT NULL, max_passes INTEGER NOT NULL, sample_serial TEXT, created TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pilot_passes (
            pilot_id TEXT NOT NULL, ticket TEXT NOT NULL, serial TEXT NOT NULL, name TEXT NOT NULL,
            email TEXT NOT NULL, type TEXT NOT NULL, created TEXT NOT NULL,
            PRIMARY KEY (pilot_id, ticket))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pilot_passes_serial ON pilot_passes(serial)")


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- keys ----------

def get(pilot_id: str) -> dict | None:
    if not ID_RE.match(pilot_id or ""):
        return None
    with db._lock, db._conn() as conn:
        row = conn.execute("SELECT * FROM pilots WHERE id = ?", (pilot_id,)).fetchone()
    return dict(row) if row else None


def authenticate(pilot_id: str, key) -> dict | None:
    """The pilot if key is "<id>.<token>" for this id; same answer (None) for unknown id and wrong key."""
    m = KEY_RE.match(key) if isinstance(key, str) else None
    pilot = get(pilot_id)
    stored = pilot["key_hash"] if pilot else sha("x")  # compare anyway so timing doesn't tell the two apart
    ok = secrets.compare_digest(stored, sha(m.group(2) if m else ""))
    return pilot if (ok and pilot and m and m.group(1) == pilot_id) else None


# ---------- design ----------

def fill(design, row: dict):
    """Design with {name} {ticket} {type} {email} replaced by this attendee's values (single pass, so a value
    that itself contains a placeholder is not expanded again). Image params are left alone."""
    def sub(value):
        if isinstance(value, str):
            return PLACEHOLDER_RE.sub(lambda m: str(row.get(m.group(1), "")), value) if "{" in value else value
        if isinstance(value, list):
            return [sub(v) for v in value]
        if isinstance(value, dict):
            return {k: (v if k.endswith("_png_b64") else sub(v)) for k, v in value.items()}
        return value
    return sub(design)


def normalize_design(design: dict, org: str, event: str) -> dict:
    d = dict(design)
    d.setdefault("style", "eventTicket")
    d.setdefault("organization_name", org)
    d.setdefault("description", event)
    d.setdefault("barcode_message", "{ticket}")
    return d


def repair_design_colors(design: dict) -> dict:
    if not design.get("background_color"):
        return design
    fixed = palette.repair(design["background_color"], design.get("foreground_color"), design.get("label_color"))
    return {**design, **fixed}


def parse_end(value: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def expires_epoch(pilot: dict) -> int:
    return pilot["event_end_epoch"] + GRACE_SECONDS


def _iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(timespec="seconds")


def pass_link(serial: str, auth_token: str) -> str:
    return f"{PUBLIC_URL}/pass/{serial}/{auth_token}"


# ---------- storing passes ----------

def _store(pilot: dict, row: dict) -> tuple[str, str]:
    """Build + store one attendee pass (not yet recorded in pilot_passes). Returns (serial, auth_token)."""
    serial, auth = str(uuid.uuid4()), secrets.token_urlsafe(24)
    params = fill(pilot["design_obj"], row)
    pkpass = _build(params, serial, auth)
    db.store_pass(serial, auth, sha(secrets.token_urlsafe(24)), json.dumps(params), pkpass, int(time.time()),
                  expires_epoch(pilot))
    return serial, auth


def log(pilot: dict, ip: str, *, success: bool, error: str | None, started: float, serial: str | None,
        action: str) -> None:
    db.log_request(ip=ip, style=pilot["design_obj"].get("style", "eventTicket"), organization_name=pilot["org"],
                   success=success, error=error, duration_ms=int((time.monotonic() - started) * 1000),
                   serial_number=serial, source="pilot", action=action, client=f"pilot:{pilot['id']}",
                   contact_email=pilot["contact_email"])


def with_design(pilot: dict) -> dict:
    return {**pilot, "design_obj": json.loads(pilot["design"])}


def existing_pass(pilot_id: str, ticket: str):
    """(serial, auth_token) of an issued, still-live pass for this ticket, else None."""
    with db._lock, db._conn() as conn:
        r = conn.execute("""SELECT pp.serial, p.auth_token FROM pilot_passes pp JOIN passes p ON p.serial = pp.serial
                            WHERE pp.pilot_id = ? AND pp.ticket = ? AND p.expires > ?""",
                         (pilot_id, ticket, int(time.time()))).fetchone()
    return (r["serial"], r["auth_token"]) if r else None


def issue_one(pilot: dict, row: dict, ip: str) -> dict:
    """Issue (or find) the pass for one row. Synchronous: run it in a worker thread."""
    started = time.monotonic()
    found = existing_pass(pilot["id"], row["ticket"])
    if found:
        return {**row, "link": pass_link(*found), "new": False}
    try:
        serial, auth = _store(pilot, row)
        with db._lock, db._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO pilot_passes (pilot_id, ticket, serial, name, email, type, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pilot["id"], row["ticket"], serial, row["name"], row["email"], row["type"], db._now()))
            inserted = cur.rowcount == 1
        if not inserted:  # a concurrent request issued it, or a stale row for a purged pass exists
            db.delete_pass(serial)
            with db._lock, db._conn() as conn:
                conn.execute("DELETE FROM pilot_passes WHERE pilot_id = ? AND ticket = ? AND serial NOT IN "
                             "(SELECT serial FROM passes)", (pilot["id"], row["ticket"]))
            found = existing_pass(pilot["id"], row["ticket"])
            if found:
                return {**row, "link": pass_link(*found), "new": False}
            return issue_one(pilot, row, ip)
    except Exception as e:
        log(pilot, ip, success=False, error=str(e)[:300], started=started, serial=None, action="create")
        raise
    log(pilot, ip, success=True, error=None, started=started, serial=serial, action="create")
    return {**row, "link": pass_link(serial, auth), "new": True}


def issued_count(pilot_id: str) -> int:
    with db._lock, db._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM pilot_passes WHERE pilot_id = ?", (pilot_id,)).fetchone()[0]


def issued_tickets(pilot_id: str) -> set:
    with db._lock, db._conn() as conn:
        return {r[0] for r in conn.execute("SELECT ticket FROM pilot_passes WHERE pilot_id = ?", (pilot_id,))}


def live_tickets(pilot_id: str) -> set:
    with db._lock, db._conn() as conn:
        return {r[0] for r in conn.execute(
            "SELECT pp.ticket FROM pilot_passes pp JOIN passes p ON p.serial = pp.serial "
            "WHERE pp.pilot_id = ? AND p.expires > ?", (pilot_id, int(time.time())))}


def issued_rows(pilot_id: str) -> list[dict]:
    with db._lock, db._conn() as conn:
        rows = conn.execute("""SELECT pp.name, pp.email, pp.ticket, pp.type, pp.serial, p.auth_token
                               FROM pilot_passes pp JOIN passes p ON p.serial = pp.serial
                               WHERE pp.pilot_id = ? ORDER BY pp.rowid""", (pilot_id,)).fetchall()
    return [dict(r) for r in rows]


def sample_link(pilot: dict) -> str | None:
    p = db.get_pass(pilot["sample_serial"]) if pilot.get("sample_serial") else None
    return pass_link(p["serial"], p["auth_token"]) if p else None


def is_pilot_serial(serial: str) -> bool:
    with db._lock, db._conn() as conn:
        return conn.execute("SELECT 1 FROM pilot_passes WHERE serial = ? UNION SELECT 1 FROM pilots "
                            "WHERE sample_serial = ?", (serial, serial)).fetchone() is not None


def purge_orphans() -> int:
    """Forget pilot_passes rows whose pass is gone (its update window ended and the sweep deleted it)."""
    with db._lock, db._conn() as conn:
        return conn.execute("DELETE FROM pilot_passes WHERE serial NOT IN (SELECT serial FROM passes)").rowcount


# ---------- updating ----------

def validate_changes(changes) -> dict:
    """Returns the cleaned changes or raises ValueError with a message for the organizer."""
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object")
    unknown = set(changes) - set(UPDATE_KEYS)
    if unknown:
        raise ValueError(f"unknown change(s): {', '.join(sorted(unknown))}; allowed: {', '.join(UPDATE_KEYS)}")
    out = {k: v for k, v in changes.items() if v is not None}
    if not out:
        raise ValueError("nothing to update: send at least one change")
    for k in FIELD_LISTS:
        if k in out and not (isinstance(out[k], list) and all(isinstance(f, dict) for f in out[k])):
            raise ValueError(f"{k} must be a list of field objects")
    if "locations" in out and not (isinstance(out["locations"], list) and all(isinstance(x, dict) for x in out["locations"])):
        raise ValueError("locations must be a list of objects")
    for k in ("relevant_date", "change_message"):
        if k in out and not isinstance(out[k], str):
            raise ValueError(f"{k} must be a string")
    return out


def merge_changes(design: dict, changes: dict) -> dict:
    """The design with changes merged in; change_message becomes the changeMessage of every field in the
    lists that changed (Wallet only shows it for a field whose value really changed)."""
    new = dict(design)
    message = changes.get("change_message")
    for k, v in changes.items():
        if k == "change_message":
            continue
        if k in FIELD_LISTS and message:
            v = [{**f, "changeMessage": f.get("changeMessage") or message} for f in v]
        new[k] = v
    return ensure_field_keys(new)


def ensure_field_keys(design: dict) -> dict:
    """Wallet needs a key on every field, unique across the whole pass: fields without one get a slug of
    their label (suffixed -2, -3 ... on a clash). Existing keys are kept, so a field keeps its identity
    (and its changeMessage notification) across updates."""
    out = dict(design)
    used = {f["key"] for k in FIELD_LISTS for f in out.get(k) or [] if isinstance(f.get("key"), str) and f["key"]}
    for k in FIELD_LISTS:
        if not out.get(k):
            continue
        fields = []
        for f in out[k]:
            if not (isinstance(f.get("key"), str) and f["key"]):
                base = re.sub(r"[^a-z0-9]+", "-", str(f.get("label", "")).lower()).strip("-") or k.split("_")[0]
                key, n = base, 2
                while key in used:
                    key, n = f"{base}-{n}", n + 1
                used.add(key)
                f = {**f, "key": key}
            fields.append(f)
        out[k] = fields
    return out


def save_design(pilot_id: str, design: dict) -> None:
    with db._lock, db._conn() as conn:
        conn.execute("UPDATE pilots SET design = ? WHERE id = ?", (json.dumps(design), pilot_id))


def rebuild_one(pilot: dict, row: dict, ip: str) -> bool:
    """Rebuild one attendee pass from pilot['design_obj']. True if replaced."""
    started = time.monotonic()
    stored = db.get_pass(row["serial"])
    if stored is None:
        return False
    try:
        params = fill(pilot["design_obj"], row)
        pkpass = _build(params, row["serial"], stored["auth_token"])
        db.replace_pass(row["serial"], json.dumps(params), pkpass, max(int(time.time()), stored["updated"] + 1))
    except Exception as e:
        log(pilot, ip, success=False, error=str(e)[:300], started=started, serial=row["serial"], action="update")
        raise
    log(pilot, ip, success=True, error=None, started=started, serial=row["serial"], action="update")
    return True


def all_push_tokens(serials: list[str]) -> list[str]:
    tokens = set()
    for s in serials:
        tokens.update(db.push_tokens(s))
    return sorted(tokens)


def drop_dead_tokens(dead: list[str]) -> None:
    if dead:
        with db._lock, db._conn() as conn:
            conn.executemany("DELETE FROM registrations WHERE push_token = ?", [(t,) for t in dead])


# ---------- creating (CLI) ----------

def create(org: str, event: str, contact: str, event_end: str, design: dict, max_passes: int = DEFAULT_MAX_PASSES,
           keep_colors: bool = False) -> dict:
    end = parse_end(event_end)
    if end.timestamp() + GRACE_SECONDS <= time.time():
        raise SystemExit("event end is in the past")
    design = normalize_design(_resolve(design), org, event)
    if not keep_colors:
        design = repair_design_colors(design)
    pilot_id = next(i for i in (_new_id() for _ in range(50)) if get(i) is None)
    token = secrets.token_urlsafe(24)
    pilot = {"id": pilot_id, "key_hash": sha(token), "org": org, "event_name": event,
             "event_end": end.isoformat(timespec="seconds"), "event_end_epoch": int(end.timestamp()),
             "contact_email": contact, "design": json.dumps(design), "max_passes": max_passes,
             "sample_serial": None, "created": db._now()}
    pilot = with_design(pilot)
    serial, auth = str(uuid.uuid4()), secrets.token_urlsafe(24)
    params = fill(design, SAMPLE_ROW)
    pkpass = _build(params, serial, auth)  # raises before anything is stored if the design is invalid
    db.store_pass(serial, auth, sha(secrets.token_urlsafe(24)), json.dumps(params), pkpass, int(time.time()),
                  expires_epoch(pilot))
    pilot["sample_serial"] = serial
    cols = ("id", "key_hash", "org", "event_name", "event_end", "event_end_epoch", "contact_email", "design",
            "max_passes", "sample_serial", "created")
    with db._lock, db._conn() as conn:
        conn.execute(f"INSERT INTO pilots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [pilot[c] for c in cols])
    return {"id": pilot_id, "key": f"{pilot_id}.{token}", "organizer_url": f"{ORGANIZER_URL}/?event={pilot_id}.{token}",
            "sample_url": pass_link(serial, auth), "design": design}


def _new_id() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ---------- CLI ----------

def _cli_setup():
    import server  # noqa: F401 -- importing it configures this module (server.py calls pilot.configure)
    import pilot as P  # the module server configured (this file may be running as __main__)
    return P


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pilot.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--org", required=True)
    n.add_argument("--event", required=True)
    n.add_argument("--contact", required=True)
    n.add_argument("--event-end", required=True, help="ISO date/time the event ends (UTC if no offset)")
    n.add_argument("--design", required=True, help="JSON file of create_wallet_pass params")
    n.add_argument("--max", type=int, default=DEFAULT_MAX_PASSES)
    n.add_argument("--keep-colors", action="store_true", help="skip palette repair")
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("id")
    p = sub.add_parser("preview")
    p.add_argument("id")
    p.add_argument("out")
    args = ap.parse_args(argv)
    P = _cli_setup()
    db.init()
    P.init()
    if args.cmd == "new":
        design = json.loads(Path(args.design).read_text())
        r = P.create(args.org, args.event, args.contact, args.event_end, design, args.max, args.keep_colors)
        print(f"id:            {r['id']}\nkey:           {r['key']}\norganizer URL: {r['organizer_url']}\n"
              f"sample link:   {r['sample_url']}")
        if not args.keep_colors and r["design"].get("background_color"):
            d = r["design"]
            print(f"colors:        bg {d['background_color']} fg {d['foreground_color']} label {d['label_color']}")
        return
    if args.cmd == "list":
        with db._lock, db._conn() as conn:
            rows = conn.execute("SELECT * FROM pilots ORDER BY created").fetchall()
        for r in rows:
            print(f"{r['id']}  {r['org']} / {r['event_name']}  ends {r['event_end']}  "
                  f"{P.issued_count(r['id'])}/{r['max_passes']} issued  {r['contact_email']}")
        return
    pilot = P.get(args.id)
    if not pilot:
        raise SystemExit(f"no pilot {args.id}")
    pilot = P.with_design(pilot)
    if args.cmd == "show":
        d = {k: v for k, v in pilot["design_obj"].items() if not k.endswith("_png_b64")}
        print(json.dumps({**{k: pilot[k] for k in ("id", "org", "event_name", "event_end", "contact_email",
                                                   "max_passes", "created")},
                          "issued": P.issued_count(args.id), "sample_url": P.sample_link(pilot),
                          "images": [k for k in pilot["design_obj"] if k.endswith("_png_b64")], "design": d},
                         indent=2))
        print("(the key is stored hashed; it was printed once by `new`)", file=sys.stderr)
    else:  # preview
        Path(args.out).write_text(json.dumps({"design": pilot["design_obj"], "sample": SAMPLE_ROW}))
        print(f"wrote {args.out}; render with: NODE_PATH=~/projects/cf-browse/node_modules node "
              f"~/projects/walletmcppass-landing/tools/shot.js {args.out} {PREVIEW_DIR}/{args.id}.png")


if __name__ == "__main__":
    main()
