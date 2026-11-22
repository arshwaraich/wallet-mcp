"""Tests card payments: prepaid pass credits bought through Dodo Payments (credits.py), against a fake Dodo API
in this process. Buy -> checkout link + credit_key; signed webhook credits the key (unsigned/stale/replayed
refused or ignored); paid singles and updates spend credits; free calls don't; not enough credits ->
402 with the card offer; refunds take credits back; MCP tools. x402 stays on alongside (fake facilitator).
Run: .venv/bin/python tests/test_credits.py"""
import asyncio, base64, hashlib, hmac, json, sqlite3, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import _harness
from _harness import PAY_TO, Facilitator, Server, check, finish

SECRET = base64.b64encode(b"test-webhook-secret-32-bytes!!!!").decode()


class FakeDodo:
    def __init__(self):
        self.sessions, self.payments, self.seen = {}, {}, []
        dodo = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                dodo.seen.append((self.path, body, self.headers.get("authorization")))
                sid = "cks_" + uuid.uuid4().hex[:8]
                dodo.sessions[sid] = body
                self._send({"session_id": sid, "checkout_url": f"https://checkout.example/{sid}"})

            def do_GET(self):
                pid = self.path.rsplit("/", 1)[1]
                p = dodo.payments.get(pid)
                self._send(p or {"error": "not found"}, 200 if p else 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def pay(self, purchase_session_index=-1, status="succeeded"):
        """Mark the latest checkout paid; returns the payment_id."""
        body = list(self.sessions.values())[purchase_session_index]
        pid = "pay_" + uuid.uuid4().hex[:8]
        self.payments[pid] = {"payment_id": pid, "status": status, "metadata": body["metadata"],
                              "total_amount": 100, "currency": "USD"}
        return pid


def signed(event: dict, secret=SECRET, ts=None):
    body = json.dumps(event).encode()
    wid, ts = "msg_" + uuid.uuid4().hex[:6], str(int(ts or time.time()))
    sig = base64.b64encode(hmac.new(base64.b64decode(secret), f"{wid}.{ts}.".encode() + body,
                                    hashlib.sha256).digest()).decode()
    return body, {"webhook-id": wid, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
                  "content-type": "application/json"}


dodo = FakeDodo()
fac = Facilitator()
srv = Server({"WALLET_MCP_IP_LIMIT": "1", "WALLET_MCP_X402_PAY_TO": PAY_TO, "WALLET_MCP_X402_FACILITATOR": fac.url,
              "DODO_PAYMENTS_API_KEY": "dodo_test_fake", "DODO_PAYMENTS_WEBHOOK_KEY": "whsec_" + SECRET,
              "DODO_PAYMENTS_API_BASE": dodo.url, "DODO_PRODUCT_100": "pdt_100", "DODO_PRODUCT_500": "pdt_500",
              "DODO_PRODUCT_2000": "pdt_2000"})
base = srv.base
CALLER = {"contact_email": "buyer@realco.dev", "x402_ready": False}
PASS = {"style": "generic", "organization_name": "Card Test", "description": "test pass", **CALLER}


def hook(event, **kw):
    body, headers = signed(event, **kw)
    return httpx.post(f"{base}/api/dodo/webhook", content=body, headers=headers)


def paid_event(pid):
    return {"business_id": "bus_1", "type": "payment.succeeded", "timestamp": "now",
            "data": {"payload_type": "Payment", "payment_id": pid}}


def rows():
    with sqlite3.connect(srv.db_path) as conn:
        return conn.execute("SELECT success, refused, payment_tx FROM requests ORDER BY id").fetchall()


def sc(res):
    return res.structured_content or json.loads(res.content[0].text)


async def mcp_call(tool, args):
    async with streamable_http_client(f"{base}/mcp") as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            return tools, await s.call_tool(tool, args)


try:
    r = httpx.post(f"{base}/api/passes", json=PASS)
    check("first pass free", r.status_code == 200, r.text)
    r = httpx.post(f"{base}/api/passes", json=PASS)
    msg = r.json().get("error", "")
    check("2nd -> 402 offering x402 AND card credits", r.status_code == 402 and "buy_pass_credits" in msg
          and "x402" in msg.lower() and '"100" (100 passes for $1)' in msg, msg)
    check("402 still carries x402 requirements", "payment-required" in r.headers)

    # --- buying ---
    r = httpx.post(f"{base}/api/credits", json={"pack": "7", "contact_email": "buyer@realco.dev"})
    check("unknown pack refused", r.status_code == 400 and "pack must be one of" in r.text, r.text)
    r = httpx.post(f"{base}/api/credits", json={"pack": "100", "contact_email": "x@example.com"})
    check("placeholder email refused", r.status_code == 400, r.text)
    r = httpx.post(f"{base}/api/credits", json={"pack": "100", "contact_email": "buyer@realco.dev"})
    buy = r.json()
    key = buy.get("credit_key", "")
    check("buy -> credit_key + checkout_url, 0 credits yet", r.status_code == 200 and key.startswith("wpc_")
          and buy["checkout_url"].startswith("https://checkout.example/") and buy["credits_left"] == 0, buy)
    path, sent, auth = dodo.seen[-1]
    check("checkout sent product, email, purchase_id metadata, API key",
          path == "/checkouts" and sent["product_cart"] == [{"product_id": "pdt_100", "quantity": 1}]
          and sent["customer"]["email"] == "buyer@realco.dev" and sent["metadata"].get("purchase_id")
          and auth == "Bearer dodo_test_fake", sent)
    with sqlite3.connect(srv.db_path) as conn:
        stored = [r[0] for r in conn.execute("SELECT key_hash FROM credit_accounts")]
    check("only the key's hash is stored", key not in stored and hashlib.sha256(key.encode()).hexdigest() in stored)

    r = httpx.post(f"{base}/api/passes", json={**PASS, "credit_key": key})
    check("unpaid key: 402 says not enough credits", r.status_code == 402 and "not enough pass credits" in r.text, r.text)

    # --- webhook ---
    pid = dodo.pay()
    r = httpx.post(f"{base}/api/dodo/webhook", json=paid_event(pid))
    check("unsigned webhook -> 401", r.status_code == 401, r.text)
    r = hook(paid_event(pid), secret=base64.b64encode(b"wrong-secret-wrong-secret-123456").decode())
    check("wrong-secret webhook -> 401", r.status_code == 401)
    r = hook(paid_event(pid), ts=time.time() - 3600)
    check("stale webhook -> 401", r.status_code == 401)
    r = hook(paid_event(pid))
    check("signed payment.succeeded -> credited 100", r.status_code == 200 and r.json()["outcome"] == "credited 100", r.text)
    r = hook(paid_event(pid))
    check("replayed webhook doesn't credit twice", r.json()["outcome"].endswith("already applied"), r.text)
    fake = dodo.pay(status="failed")
    r = hook(paid_event(fake))
    check("payment not succeeded at Dodo's API -> ignored", "ignored" in r.json()["outcome"], r.text)
    r = httpx.get(f"{base}/api/credits", headers={"authorization": f"Bearer {key}"})
    check("balance 100, purchase paid", r.json()["credits_left"] == 100 and r.json()["purchases"][0]["status"] == "paid",
          r.text)
    check("bad key -> 401", httpx.get(f"{base}/api/credits", headers={"authorization": "Bearer wpc_nope"}).status_code == 401)

    # --- spending ---
    r = httpx.post(f"{base}/api/passes", json={**PASS, "credit_key": key})
    res = r.json()
    check("paid single with credit_key -> 200, 1 credit used, 99 left",
          r.status_code == 200 and res["payment"] == {"method": "pass_credits", "credits_used": 1, "credits_left": 99,
                                                      "transaction": "dodo-credits"}, res)
    check("no x402 setup hint on a credits call", "payment_setup" not in res)
    check("logged as paid (payment_tx dodo-credits)", rows()[-1] == (1, 0, "dodo-credits"), rows()[-1])
    r = httpx.post(f"{base}/api/passes", json={**PASS, "credit_key": key, "updatable_hours": 5})
    left = httpx.get(f"{base}/api/credits", headers={"authorization": f"Bearer {key}"}).json()["credits_left"]
    check("failed build spends nothing", r.status_code == 400 and left == 99, (r.text, left))

    r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True, "credit_key": key})
    serial, edit = r.json()["serial_number"], r.json()["edit_token"]
    r = httpx.patch(f"{base}/api/passes/{serial}", json={"description": "changed", "credit_key": key, **CALLER},
                    headers={"authorization": f"Bearer {edit}"})
    check("update paid with credits", r.status_code == 200 and r.json()["payment"]["credits_left"] == 97, r.text)

    # top-up the same key
    r = httpx.post(f"{base}/api/credits", json={"pack": "500", "contact_email": "buyer@realco.dev", "credit_key": key})
    check("top-up returns no new key", r.status_code == 200 and "credit_key" not in r.json(), r.text)
    pid2 = dodo.pay()
    hook(paid_event(pid2))
    left = httpx.get(f"{base}/api/credits", headers={"authorization": f"Bearer {key}"}).json()["credits_left"]
    check("top-up credited 500 -> 597", left == 597, left)

    # refund
    r = hook({"type": "refund.succeeded", "data": {"payment_id": pid2}})
    left = httpx.get(f"{base}/api/credits", headers={"authorization": f"Bearer {key}"}).json()["credits_left"]
    check("refund takes the pack back -> 97", left == 97 and "took back 500" in r.text, (left, r.text))

    # --- MCP ---
    tools, res = asyncio.run(mcp_call("check_pass_credits", {"credit_key": key}))
    check("MCP lists buy/check tools", {"buy_pass_credits", "check_pass_credits"} <= tools, tools)
    check("MCP check_pass_credits", not res.is_error and sc(res)["credits_left"] == 97, res)
    _, res = asyncio.run(mcp_call("create_wallet_pass", {**PASS, "credit_key": key}))
    check("MCP paid create with credit_key", not res.is_error and sc(res)["payment"]["credits_left"] == 96
          and not (res.meta or {}).get("x402/payment-response"), res)
    _, res = asyncio.run(mcp_call("buy_pass_credits", {"pack": "2000", "contact_email": "buyer@realco.dev"}))
    check("MCP buy_pass_credits -> new key + link", not res.is_error and
          sc(res)["credit_key"].startswith("wpc_") and "checkout_url" in sc(res), res)
finally:
    srv.stop()
    dodo.httpd.shutdown()
    fac.shutdown()
finish()
