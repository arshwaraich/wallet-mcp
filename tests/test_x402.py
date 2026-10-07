"""Tests x402 pay-per-call past the free per-IP limit, over both REST and MCP, against a fake
facilitator running in this process (so nothing touches a chain). Payments are signed for real
by the x402 client SDK with a throwaway key. Starts a real server on a throwaway db.

With --live, also sends a real signed payment from an unfunded wallet to the real facilitator on
Base mainnet; it must come back refused for lack of funds (proving the wire format end to end,
with nothing charged).
Run: .venv/bin/python tests/test_x402.py [--live]"""
import asyncio, base64, json, os, socket, sqlite3, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2 as httpx  # the mcp SDK v2 ships httpx2
from eth_account import Account
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from x402 import x402ClientSync
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

ROOT = Path(__file__).resolve().parent.parent
NETWORK = "eip155:8453"
PAY_TO = "0x000000000000000000000000000000000000dEaD"
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))
    fails += 0 if cond else 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- fake facilitator: mode decides what /verify and /settle answer ---
mode = {"verify": True, "settle": True}
seen = []


class Facilitator(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"kinds": [{"x402Version": 2, "scheme": "exact", "network": NETWORK}],
                    "extensions": [], "signers": {}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        seen.append((self.path, body))
        if self.path.endswith("/verify"):
            self._send({"isValid": True, "payer": "0xpayer"} if mode["verify"]
                       else {"isValid": False, "invalidReason": "insufficient_funds", "payer": "0xpayer"})
        elif mode["settle"]:
            self._send({"success": True, "transaction": "0xfeed", "network": NETWORK, "payer": "0xpayer"})
        else:
            self._send({"success": False, "errorReason": "nonce_used", "transaction": "", "network": NETWORK})


def start_server(facilitator_url, ip_limit, global_limit=500, **extra_env):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    db_path = Path(tempfile.mkdtemp()) / "test.db"
    env = dict(os.environ, PORT=str(port), WALLET_MCP_DB=str(db_path), WALLET_MCP_IP_LIMIT=str(ip_limit),
               WALLET_MCP_GLOBAL_LIMIT=str(global_limit),
               WALLET_MCP_PUBLIC_URL=base, WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}",
               WALLET_MCP_X402_PAY_TO=PAY_TO, WALLET_MCP_X402_FACILITATOR=facilitator_url, **extra_env)
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/stats", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    return proc, base, db_path


payer = x402ClientSync()
payer.register(NETWORK, ExactEvmClientScheme(EthAccountSigner(Account.create())))


def sign(payment_required: dict) -> dict:
    return payer.create_payment_payload(PaymentRequired.model_validate(payment_required)).model_dump(
        by_alias=True, exclude_none=True)


def header(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def unheader(value: str) -> dict:
    return json.loads(base64.b64decode(value))


async def mcp_call(base, tool, args, meta=None):
    async with streamable_http_client(f"{base}/mcp") as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            return await s.call_tool(tool, args, meta=meta)


PASS = {"style": "generic", "organization_name": "x402 Test", "description": "test pass"}


def offline_tests():
    fac = ThreadingHTTPServer(("127.0.0.1", 0), Facilitator)
    threading.Thread(target=fac.serve_forever, daemon=True).start()
    proc, base, db_path = start_server(f"http://127.0.0.1:{fac.server_port}", ip_limit=2)

    def rows():
        with sqlite3.connect(db_path) as conn:
            return conn.execute("SELECT action, success, error, x402, payment_tx FROM requests ORDER BY id").fetchall()

    try:
        r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True})
        serial, edit_token = r.json()["serial_number"], r.json()["edit_token"]
        r2 = httpx.post(f"{base}/api/passes", json=PASS, headers={"payment-signature": "ignored-under-limit"})
        check("free calls under the limit work, a payment sent with one is ignored",
              r.status_code == r2.status_code == 200 and "payment" not in r2.json() and not seen, r2.text)

        # --- REST ---
        r = httpx.post(f"{base}/api/passes", json=PASS)
        body = r.json()
        check("REST past the limit -> 402", r.status_code == 402, r.text)
        check("402 body is x402 v2 PaymentRequired for $0.01 USDC on Base to PAY_TO",
              body.get("x402Version") == 2 and body["accepts"][0]["amount"] == "10000"
              and body["accepts"][0]["payTo"] == PAY_TO and body["accepts"][0]["network"] == NETWORK, body)
        check("402 error says the limit and how to pay",
              "rate limit exceeded" in body.get("error", "") and "PAYMENT-SIGNATURE" in body["error"], body)
        check("PAYMENT-REQUIRED header matches the body", unheader(r.headers["payment-required"]) == body)
        check("the refusal is logged with the limit text the usage watcher matches",
              rows()[-1][:3] == ("create", 0, "rate limit exceeded: max 2 passes/day per caller"), rows()[-1])

        r = httpx.post(f"{base}/api/passes", json=PASS, headers={"payment-signature": header(sign(body))})
        check("REST paid call -> 200 with a pass", r.status_code == 200 and "download_url" in r.json(), r.text)
        check("paid result carries the receipt", r.json().get("payment", {}).get("transaction") == "0xfeed", r.text)
        check("PAYMENT-RESPONSE header carries the receipt",
              unheader(r.headers.get("payment-response", "e30="))["transaction"] == "0xfeed", r.headers)
        check("facilitator got verify then settle", [p for p, _ in seen[-2:]] == ["/verify", "/settle"], seen)
        check("paid call logged with its transaction", rows()[-1] == ("create", 1, None, 1, "0xfeed"), rows()[-1])
        check("download link of a paid pass works", httpx.get(r.json()["download_url"]).status_code == 200)

        r = httpx.post(f"{base}/api/passes", json=PASS, headers={"x-payment": header(sign(body))})
        check("legacy X-PAYMENT header also accepted", r.status_code == 200 and "payment" in r.json(), r.text)

        r = httpx.post(f"{base}/api/passes", json=PASS, headers={"payment-signature": "garbage"})
        check("unreadable payment -> 402 saying so", r.status_code == 402 and "could not read" in r.json()["error"],
              r.text)

        mode["verify"] = False
        n = len(seen)
        r = httpx.post(f"{base}/api/passes", json=PASS, headers={"payment-signature": header(sign(body))})
        check("payment the facilitator rejects -> 402 with its reason",
              r.status_code == 402 and "insufficient_funds" in r.json()["error"], r.text)
        check("rejected payment is never settled", [p for p, _ in seen[n:]] == ["/verify"], seen[n:])
        mode["verify"] = True

        # Settlement fails after the pass is built: no pass handed out, and an update changes nothing.
        mode["settle"] = False
        auth = {"authorization": f"Bearer {edit_token}"}
        r = httpx.patch(f"{base}/api/passes/{serial}", json={"organization_name": "Changed"}, headers=auth)
        check("REST update past the limit -> 402", r.status_code == 402, r.text)
        r = httpx.patch(f"{base}/api/passes/{serial}", json={"organization_name": "Changed"},
                        headers={**auth, "payment-signature": header(sign(r.json()))})
        check("failed settlement -> 402, no pass", r.status_code == 402 and "download_url" not in r.json()
              and "nonce_used" in r.json()["error"] and "not charged" in r.json()["error"], r.text)
        with sqlite3.connect(db_path) as conn:
            params = json.loads(conn.execute("SELECT params FROM passes WHERE serial = ?", (serial,)).fetchone()[0])
        check("failed settlement leaves the stored pass unchanged", params["organization_name"] == "x402 Test", params)
        check("failed settlement logged as a failure", rows()[-1][:2] == ("update", 0) and "nonce_used" in rows()[-1][2],
              rows()[-1])
        mode["settle"] = True

        r = httpx.patch(f"{base}/api/passes/{serial}", json={"organization_name": "Changed"}, headers=auth)
        r = httpx.patch(f"{base}/api/passes/{serial}", json={"organization_name": "Changed"},
                        headers={**auth, "payment-signature": header(sign(r.json()))})
        check("REST paid update -> 200", r.status_code == 200 and r.json()["payment"]["transaction"] == "0xfeed",
              r.text)

        # --- MCP ---
        res = asyncio.run(mcp_call(base, "create_wallet_pass", PASS))
        pr = res.structured_content or {}
        check("MCP past the limit -> tool error carrying PaymentRequired",
              res.is_error and pr.get("x402Version") == 2 and pr["accepts"][0]["amount"] == "10000", res)
        check("MCP refusal names the _meta key and the tool resource",
              'x402/payment' in pr.get("error", "") and pr["resource"]["url"] == "mcp://tool/create_wallet_pass", pr)
        check("MCP refusal text is the same JSON", json.loads(res.content[0].text) == pr)

        res = asyncio.run(mcp_call(base, "create_wallet_pass", PASS, meta={"x402/payment": sign(pr)}))
        check("MCP paid call -> pass", not res.is_error and "download_url" in (res.structured_content or {}), res)
        check("MCP paid result has the receipt in _meta",
              (res.meta or {}).get("x402/payment-response", {}).get("transaction") == "0xfeed", res.meta)

        res = asyncio.run(mcp_call(base, "update_wallet_pass", {"serial_number": serial, "edit_token": edit_token,
                                                                "organization_name": "Again"}))
        pr = res.structured_content or {}
        check("MCP update past the limit -> PaymentRequired for update_wallet_pass",
              res.is_error and pr.get("resource", {}).get("url") == "mcp://tool/update_wallet_pass", res)
        res = asyncio.run(mcp_call(base, "update_wallet_pass", {"serial_number": serial, "edit_token": edit_token,
                                                                "organization_name": "Again"},
                                   meta={"x402/payment": sign(pr)}))
        check("MCP paid update -> ok", not res.is_error and res.structured_content["payment"]["transaction"] == "0xfeed",
              res)
    finally:
        proc.terminate()
        proc.wait()

    # Global cap: payment lifts it too, and paid calls don't use up the free budget.
    proc, base, db_path = start_server(f"http://127.0.0.1:{fac.server_port}", ip_limit=100, global_limit=2)
    try:
        for ip in ("198.51.100.1", "198.51.100.2"):
            httpx.post(f"{base}/api/passes", json=PASS, headers={"x-real-ip": ip})
        fresh = {"x-real-ip": "198.51.100.3"}
        r = httpx.post(f"{base}/api/passes", json=PASS, headers=fresh)
        check("global cap reached -> 402 even for a fresh IP",
              r.status_code == 402 and r.json()["error"].startswith("service is at its daily request limit"), r.text)
        r = httpx.post(f"{base}/api/passes", json=PASS, headers={**fresh, "payment-signature": header(sign(r.json()))})
        check("paid call goes through past the global cap", r.status_code == 200 and "payment" in r.json(), r.text)
        r = httpx.post(f"{base}/api/passes", json=PASS, headers={**fresh, "payment-signature": header(sign(r.json()
                       if r.status_code == 402 else httpx.post(f"{base}/api/passes", json=PASS, headers=fresh).json()))})
        check("paid calls stay uncapped", r.status_code == 200, r.text)
        with sqlite3.connect(db_path) as conn:
            free_today = conn.execute("SELECT COUNT(*) FROM requests WHERE payment_tx IS NULL").fetchone()[0]
        check("paid calls aren't counted towards the free budget", free_today == 4, free_today)  # 2 free + 2 refusals
    finally:
        proc.terminate()
        proc.wait()
        fac.shutdown()


def capacity_tests():
    fac = ThreadingHTTPServer(("127.0.0.1", 0), Facilitator)
    threading.Thread(target=fac.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{fac.server_port}"
    proc, base, _ = start_server(url, ip_limit=0, WALLET_MCP_PAID_IP_LIMIT="1", WALLET_MCP_PAID_GLOBAL_LIMIT="2")
    try:
        def paid(ip):
            h = {"x-real-ip": ip}
            r = httpx.post(f"{base}/api/passes", json=PASS, headers=h)
            if r.status_code != 402:
                return r
            return httpx.post(f"{base}/api/passes", json=PASS, headers={**h, "payment-signature": header(sign(r.json()))})
        check("first paid call ok", paid("198.51.100.10").status_code == 200)
        r = paid("198.51.100.10")
        check("paid per-IP cap -> 429, no payment offered",
              r.status_code == 429 and "paid passes/day per caller" in r.json()["error"], r.text)
        check("another IP can still pay", paid("198.51.100.11").status_code == 200)
        r = paid("198.51.100.12")
        check("paid global cap -> 429", r.status_code == 429 and "paid passes too" in r.json()["error"], r.text)
    finally:
        proc.terminate(); proc.wait()

    proc, base, _ = start_server(url, ip_limit=100, WALLET_MCP_MIN_FREE_DISK_GB="1000000")
    try:
        r = httpx.post(f"{base}/api/passes", json=PASS)
        check("low disk -> 503", r.status_code == 503 and "capacity" in r.json()["error"], r.text)
    finally:
        proc.terminate(); proc.wait()

    proc, base, _ = start_server(url, ip_limit=100, WALLET_MCP_MAX_PASS_KB="1")
    try:
        r = httpx.post(f"{base}/api/passes", json=PASS)
        check("oversized pass -> 400 naming the limit", r.status_code == 400 and "limit is" in r.json()["error"], r.text)
        res = asyncio.run(mcp_call(base, "create_wallet_pass", PASS))
        check("oversized pass over MCP -> tool error", res.is_error and "Use smaller images" in res.content[0].text, res)
    finally:
        proc.terminate(); proc.wait()
        fac.shutdown()


def live_test():
    proc, base, _ = start_server("https://facilitator.payai.network", ip_limit=0)
    try:
        r = httpx.post(f"{base}/api/passes", json=PASS, timeout=30)
        check("live: real facilitator -> 402 PaymentRequired", r.status_code == 402 and r.json()["accepts"], r.text)
        r = httpx.post(f"{base}/api/passes", json=PASS, headers={"payment-signature": header(sign(r.json()))},
                       timeout=60)
        check("live: unfunded signed payment is refused by the real facilitator, nothing settled",
              r.status_code == 402 and "verification failed" in r.json()["error"], r.text)
        print("     facilitator said:", r.json().get("error", "")[:200])
    finally:
        proc.terminate()
        proc.wait()


offline_tests()
capacity_tests()
if "--live" in sys.argv:
    live_test()
print("ALL PASSED" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
