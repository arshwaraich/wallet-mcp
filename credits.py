"""Card payments through Dodo Payments (merchant of record): prepaid pass credits.

Dodo's smallest one-time charge is $0.50, so a card can't pay $0.01 a pass the way x402 does. Instead a
caller buys a pack (PACKS) with buy_pass_credits: it returns a Dodo checkout link for the user to open, and a
secret credit_key. Dodo calls POST /api/dodo/webhook when the payment succeeds; the webhook is verified
(Standard Webhooks signature), the payment is re-read from Dodo's API, and the pack's passes are added to
that key's balance. Calls past the free tier (and batches) that send credit_key spend 1 credit per pass,
taken only once the work is built and stored, like an x402 settlement.

Off unless DODO_PAYMENTS_API_KEY, DODO_PAYMENTS_WEBHOOK_KEY and the pack product ids are set
(dodo_setup.py creates the products and the webhook and prints these). Only a hash of each credit_key is
stored, so a leaked database can't spend credits.
"""
import base64
import dataclasses
import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid

import httpx

import db

logger = logging.getLogger("wallet-mcp.credits")

API_KEY = os.environ.get("DODO_PAYMENTS_API_KEY", "")
WEBHOOK_KEY = os.environ.get("DODO_PAYMENTS_WEBHOOK_KEY", "")
API_BASE = os.environ.get("DODO_PAYMENTS_API_BASE") or (
    "https://test.dodopayments.com" if API_KEY.startswith("dodo_test_") else "https://live.dodopayments.com")
RETURN_URL = os.environ.get("WALLET_MCP_CREDITS_RETURN_URL", "https://walletmcppass.com/?credits=thanks")

# name -> (passes, price in US cents, env var holding the Dodo product id)
PACKS = {
    "100": (100, 100, "DODO_PRODUCT_100"),
    "500": (500, 500, "DODO_PRODUCT_500"),
    "2000": (2000, 2000, "DODO_PRODUCT_2000"),
}
PRODUCT_IDS = {name: os.environ.get(var, "") for name, (_, _, var) in PACKS.items()}
enabled = bool(API_KEY and WEBHOOK_KEY and all(PRODUCT_IDS.values()))

KEY_PREFIX = "wpc_"
CHECKOUTS_PER_IP_PER_DAY = int(os.environ.get("WALLET_MCP_CHECKOUT_LIMIT", 20))
WEBHOOK_TOLERANCE_SECONDS = 5 * 60
RECEIPT_TX = "dodo-credits"  # requests.payment_tx for passes paid with credits (revenue = rows x $0.01)


def pack_label(name: str) -> str:
    passes, cents, _ = PACKS[name]
    return f"{passes} passes for ${cents / 100:.0f}"


PACKS_TEXT = ", ".join(f'"{n}" ({pack_label(n)})' for n in PACKS)


class CreditError(Exception):
    """A caller-facing problem: unknown key, not enough credits, checkout limit."""


def init() -> None:
    with db._lock, db._conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS credit_accounts (
                key_hash TEXT PRIMARY KEY,
                contact_email TEXT,
                credits INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credit_purchases (
                id TEXT PRIMARY KEY,
                key_hash TEXT NOT NULL,
                pack TEXT NOT NULL,
                passes INTEGER NOT NULL,
                cents INTEGER NOT NULL,
                ip TEXT,
                checkout_session_id TEXT,
                payment_id TEXT UNIQUE,
                status TEXT NOT NULL,          -- pending / paid / refunded
                amount INTEGER, currency TEXT,  -- what Dodo actually charged
                created TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS credit_spends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT NOT NULL,
                credits INTEGER NOT NULL,
                action TEXT,
                ts TEXT NOT NULL
            );
            """
        )


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def looks_like_key(key) -> bool:
    return isinstance(key, str) and key.startswith(KEY_PREFIX) and 20 <= len(key) <= 100


def balance(key: str) -> int | None:
    """Credits left on a key, or None for an unknown key."""
    if not looks_like_key(key):
        return None
    with db._conn() as conn:
        row = conn.execute("SELECT credits FROM credit_accounts WHERE key_hash = ?", (_hash(key),)).fetchone()
    return None if row is None else row["credits"]


def account(key: str) -> dict | None:
    if balance(key) is None:
        return None
    h = _hash(key)
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT pack, passes, status, created, paid_at FROM credit_purchases WHERE key_hash = ? "
            "ORDER BY created DESC LIMIT 20", (h,)).fetchall()
        credits = conn.execute("SELECT credits FROM credit_accounts WHERE key_hash = ?", (h,)).fetchone()["credits"]
    return {"credits_left": credits, "purchases": [dict(r) for r in rows]}


# --- spending ---

@dataclasses.dataclass(frozen=True)
class Hold:
    """`count` credits on a key that had them when the call was gated; spent by spend() once the work is done."""
    key_hash: str
    count: int
    action: str


def hold(key: str, count: int, action: str) -> Hold:
    left = balance(key)
    if left is None:
        raise CreditError("unknown credit_key")
    if left < count:
        raise CreditError(f"not enough pass credits: this needs {count} and the credit_key has {left} left")
    return Hold(_hash(key), count, action)


def spend(h: Hold) -> dict:
    """Take the held credits (atomically: a concurrent call may have used them first). The receipt
    goes in the result's "payment"."""
    with db._lock, db._conn() as conn:
        cur = conn.execute("UPDATE credit_accounts SET credits = credits - ? WHERE key_hash = ? AND credits >= ?",
                           (h.count, h.key_hash, h.count))
        if cur.rowcount != 1:
            raise CreditError("not enough pass credits left (another call used them)")
        conn.execute("INSERT INTO credit_spends (key_hash, credits, action, ts) VALUES (?, ?, ?, ?)",
                     (h.key_hash, h.count, h.action, _now()))
        left = conn.execute("SELECT credits FROM credit_accounts WHERE key_hash = ?", (h.key_hash,)).fetchone()[0]
    return {"method": "pass_credits", "credits_used": h.count, "credits_left": left, "transaction": RECEIPT_TX}


# --- buying ---

def _api(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, API_BASE + path, headers={"Authorization": f"Bearer {API_KEY}"}, timeout=20, **kw)
    if r.status_code >= 400:
        logger.error("dodo %s %s -> %s %s", method, path, r.status_code, r.text[:500])
        r.raise_for_status()
    return r.json()


def _checkouts_today(ip: str) -> int:
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    with db._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM credit_purchases WHERE ip = ? AND created >= ?",
                            (ip, today)).fetchone()[0]


def start_checkout(pack: str, contact_email: str, ip: str, key: str | None = None) -> dict:
    """A Dodo checkout link for `pack`, crediting `key` (a new key is made when None)."""
    if pack not in PACKS:
        raise CreditError(f"pack must be one of {PACKS_TEXT}")
    if key is not None and balance(key) is None:
        raise CreditError("unknown credit_key (leave it out to get a new one)")
    if _checkouts_today(ip) >= CHECKOUTS_PER_IP_PER_DAY:
        raise CreditError(f"too many checkouts started today (max {CHECKOUTS_PER_IP_PER_DAY} per caller)")
    new_key = key is None
    if new_key:
        key = KEY_PREFIX + secrets.token_urlsafe(24)
    passes, cents, _ = PACKS[pack]
    purchase_id = str(uuid.uuid4())
    session = _api("POST", "/checkouts", json={
        "product_cart": [{"product_id": PRODUCT_IDS[pack], "quantity": 1}],
        "customer": {"email": contact_email, "name": contact_email.split("@")[0]},
        "metadata": {"purchase_id": purchase_id},
        "return_url": RETURN_URL,
    })
    with db._lock, db._conn() as conn:
        if new_key:
            conn.execute("INSERT INTO credit_accounts (key_hash, contact_email, credits, created) VALUES (?, ?, 0, ?)",
                         (_hash(key), contact_email, _now()))
        conn.execute(
            "INSERT INTO credit_purchases (id, key_hash, pack, passes, cents, ip, checkout_session_id, status, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (purchase_id, _hash(key), pack, passes, cents, ip, session.get("session_id"), _now()))
    out = {"checkout_url": session["checkout_url"], "pack": pack_label(pack),
           "credits_left": balance(key)}
    if new_key:
        out = {"credit_key": key, **out}
    return out


# --- webhook ---

def verify_webhook(headers, body: bytes) -> dict:
    """Standard Webhooks signature check; returns the parsed event or raises ValueError."""
    wid, ts, sigs = (headers.get("webhook-id"), headers.get("webhook-timestamp"), headers.get("webhook-signature"))
    if not (wid and ts and sigs):
        raise ValueError("missing webhook headers")
    if abs(time.time() - int(ts)) > WEBHOOK_TOLERANCE_SECONDS:
        raise ValueError("stale webhook timestamp")
    secret = WEBHOOK_KEY.removeprefix("whsec_")
    key = base64.b64decode(secret)
    expected = base64.b64encode(hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    if not any(hmac.compare_digest(expected, s.partition(",")[2]) for s in sigs.split()):
        raise ValueError("bad webhook signature")
    return json.loads(body)


def handle_event(event: dict) -> str:
    """Apply a verified Dodo event. Idempotent (Dodo retries): a payment credits once."""
    kind = event.get("type", "")
    data = event.get("data") or {}
    payment_id = data.get("payment_id")
    if kind == "payment.succeeded" and payment_id:
        payment = _api("GET", f"/payments/{payment_id}")  # trust Dodo's API, not just the payload
        if payment.get("status") != "succeeded":
            return f"payment {payment_id} status {payment.get('status')}: ignored"
        purchase_id = (payment.get("metadata") or {}).get("purchase_id")
        with db._lock, db._conn() as conn:
            row = conn.execute("SELECT * FROM credit_purchases WHERE id = ?", (purchase_id,)).fetchone()
            if row is None:
                return f"payment {payment_id}: no purchase {purchase_id!r}"
            cur = conn.execute(
                "UPDATE credit_purchases SET status = 'paid', payment_id = ?, amount = ?, currency = ?, paid_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (payment_id, payment.get("total_amount"), payment.get("currency"), _now(), purchase_id))
            if cur.rowcount != 1:
                return f"payment {payment_id}: already applied"
            conn.execute("UPDATE credit_accounts SET credits = credits + ? WHERE key_hash = ?",
                         (row["passes"], row["key_hash"]))
        logger.info("credited %d passes for payment %s", row["passes"], payment_id)
        return f"credited {row['passes']}"
    if kind in ("refund.succeeded", "dispute.opened") and payment_id:
        with db._lock, db._conn() as conn:
            row = conn.execute("SELECT * FROM credit_purchases WHERE payment_id = ? AND status = 'paid'",
                               (payment_id,)).fetchone()
            if row is None:
                return f"{kind} {payment_id}: nothing to take back"
            conn.execute("UPDATE credit_purchases SET status = 'refunded' WHERE id = ?", (row["id"],))
            conn.execute("UPDATE credit_accounts SET credits = MAX(credits - ?, 0) WHERE key_hash = ?",
                         (row["passes"], row["key_hash"]))
        logger.warning("%s: took back up to %d credits for payment %s", kind, row["passes"], payment_id)
        return f"took back {row['passes']}"
    return f"{kind}: ignored"


def stats() -> dict:
    with db._conn() as conn:
        paid = conn.execute("SELECT COUNT(*), COALESCE(SUM(cents), 0), COALESCE(SUM(passes), 0) FROM credit_purchases "
                            "WHERE status = 'paid'").fetchone()
        pending = conn.execute("SELECT COUNT(*) FROM credit_purchases WHERE status = 'pending'").fetchone()[0]
        spent = conn.execute("SELECT COALESCE(SUM(credits), 0) FROM credit_spends").fetchone()[0]
    return {"purchases": paid[0], "revenue_usd": paid[1] / 100, "credits_sold": paid[2],
            "checkouts_pending": pending, "credits_spent": spent}
