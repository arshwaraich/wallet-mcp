"""One-time (idempotent) Dodo Payments setup for card credits: creates the pass-pack products and the webhook
endpoint, and prints the env lines credits.py needs (DODO_PRODUCT_*, DODO_PAYMENTS_WEBHOOK_KEY) on stdout, for
appending to the root-only env file. Progress goes to stderr, so the webhook secret is never shown:

  sudo sh -c 'set -a; . /etc/wallet-mcp/dodo.env; .venv/bin/python dodo_setup.py >> /etc/wallet-mcp/dodo.env'

Needs DODO_PAYMENTS_API_KEY. Re-running reuses products/webhooks with the same name/url and skips lines the
env file already has."""
import os
import sys

import httpx

import credits

WEBHOOK_URL = os.environ.get("WALLET_MCP_DODO_WEBHOOK_URL", "https://walletmcppass.com/api/dodo/webhook")
EVENTS = ["payment.succeeded", "refund.succeeded", "dispute.opened"]


def api(method, path, **kw):
    r = httpx.request(method, credits.API_BASE + path, timeout=30,
                      headers={"Authorization": f"Bearer {os.environ['DODO_PAYMENTS_API_KEY']}"}, **kw)
    if r.status_code >= 400:
        sys.exit(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
    return r.json() if r.content else {}


def items(page):
    return page.get("items", page) if isinstance(page, dict) else page


def log(msg):
    print(msg, file=sys.stderr)


log(f"Dodo API: {credits.API_BASE}")
products = {p.get("name"): p for p in items(api("GET", "/products", params={"page_size": 100}))}
lines = []
for name, (passes, cents, var) in credits.PACKS.items():
    title = f"Apple Wallet passes: {passes} pack"
    if os.environ.get(var):
        log(f"{var} already set")
        continue
    p = products.get(title)
    if p is None:
        p = api("POST", "/products", json={
            "name": title,
            "description": f"{passes} Apple Wallet pass credits for walletmcppass.com ($0.01 a pass). Never expire.",
            "tax_category": "saas",
            "price": {"type": "one_time_price", "currency": "USD", "price": cents, "discount": 0,
                      "purchasing_power_parity": False},
        })
        log(f"created product {title}: {p.get('product_id')}")
    else:
        log(f"reusing product {title}: {p.get('product_id')}")
    lines.append(f"{var}={p['product_id']}")

if not os.environ.get("DODO_PAYMENTS_WEBHOOK_KEY"):
    hooks = [h for h in items(api("GET", "/webhooks")) if h.get("url") == WEBHOOK_URL]
    hook = hooks[0] if hooks else api("POST", "/webhooks", json={
        "url": WEBHOOK_URL, "description": "wallet-mcp pass credits", "filter_types": EVENTS})
    log(f"webhook {hook['id']} -> {WEBHOOK_URL} ({'reused' if hooks else 'created'})")
    secret = api("GET", f"/webhooks/{hook['id']}/secret")
    secret = secret.get("secret") if isinstance(secret, dict) else secret
    if not secret:
        sys.exit("no signing secret returned; copy it from the dashboard (Developer > Webhooks)")
    lines.append(f"DODO_PAYMENTS_WEBHOOK_KEY={secret}")
else:
    log("DODO_PAYMENTS_WEBHOOK_KEY already set")
for line in lines:
    print(line)
log(f"wrote {len(lines)} line(s)")
