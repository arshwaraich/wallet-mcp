"""Tests paid batches (create_wallet_pass_batch / update_wallet_pass_batch, /api/batches) over REST and MCP:
402 with the N-pass price, pay -> permanent links, reuse is free, adding rows, rollback on a failed build or
settlement, batch update re-signs + charges per pass, pilot routes refuse batches, paid caps. Fake facilitator,
real x402 signing with a throwaway key, real server on a throwaway db.
Run: .venv/bin/python tests/test_batch.py"""
import asyncio, atexit, base64, json, os, shutil, socket, sqlite3, subprocess, sys, tempfile, threading, time, zipfile, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2 as httpx
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
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {str(detail)[:600]}" if detail and not cond else ""))
    fails += 0 if cond else 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        self._send({"kinds": [{"x402Version": 2, "scheme": "exact", "network": NETWORK}], "extensions": [], "signers": {}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        seen.append((self.path, body))
        if self.path.endswith("/verify"):
            self._send({"isValid": True, "payer": "0xpayer"} if mode["verify"]
                       else {"isValid": False, "invalidReason": "insufficient_funds", "payer": "0xpayer"})
        elif mode["settle"]:
            self._send({"success": True, "transaction": "0xbatch", "network": NETWORK, "payer": "0xpayer"})
        else:
            self._send({"success": False, "errorReason": "nonce_used", "transaction": "", "network": NETWORK})


fac = ThreadingHTTPServer(("127.0.0.1", 0), Facilitator)
threading.Thread(target=fac.serve_forever, daemon=True).start()

port = free_port()
base = f"http://127.0.0.1:{port}"
tmp = Path(tempfile.mkdtemp())
dl = Path("/var/tmp") / f"wallet-mcp-test-batch-{tmp.name}"  # /tmp is a small tmpfs that trips the disk guard
atexit.register(shutil.rmtree, dl, True)
atexit.register(shutil.rmtree, tmp, True)
db_path = tmp / "test.db"
env = dict(os.environ, PORT=str(port), WALLET_MCP_DB=str(db_path), WALLET_MCP_IP_LIMIT="10",
           WALLET_MCP_PUBLIC_URL=base, WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}", WALLET_MCP_X402_PAY_TO=PAY_TO,
           WALLET_MCP_X402_FACILITATOR=f"http://127.0.0.1:{fac.server_port}", WALLET_MCP_DOWNLOADS=str(dl),
           WALLET_MCP_PREVIEWS=str(tmp / "previews"), WALLET_MCP_PAID_IP_LIMIT="40",
           WALLET_MCP_APNS_URL="http://127.0.0.1:9")
proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=open(tmp / "server.log", "w"))
for _ in range(100):
    try:
        httpx.get(f"{base}/api/stats", timeout=1)
        break
    except httpx.HTTPError:
        time.sleep(0.1)

payer = x402ClientSync()
payer.register(NETWORK, ExactEvmClientScheme(EthAccountSigner(Account.create())))


def sign(payment_required: dict) -> dict:
    return payer.create_payment_payload(PaymentRequired.model_validate(payment_required)).model_dump(
        by_alias=True, exclude_none=True)


def hdr(payload: dict) -> dict:
    return {"payment-signature": base64.b64encode(json.dumps(payload).encode()).decode()}


async def mcp_call(tool, args):
    async with streamable_http_client(f"{base}/mcp") as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            return await s.call_tool(tool, args)


def requests_rows():
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT source, action, success, refused, payment_tx, serial_number FROM requests ORDER BY id").fetchall()


def pass_json(pkpass: bytes) -> dict:
    return json.loads(zipfile.ZipFile(io.BytesIO(pkpass)).read("pass.json"))


CALLER = {"contact_email": "org@realevent.dev", "x402_ready": True}
END = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 86400))
DESIGN = {"organization_name": "Batch Gala", "description": "Gala night",
          "primary_fields": [{"key": "name", "label": "Guest", "value": "{name}"}],
          "secondary_fields": [{"key": "doors", "label": "Doors", "value": "7pm"}],
          "back_fields": [{"key": "t", "label": "Ticket", "value": "{ticket} / {type}"}]}
ROWS = [{"name": "Ada", "ticket": "T-1"}, {"name": "Bo", "ticket": "T-2", "type": "VIP", "email": "bo@x.io"},
        {"name": "Cy", "ticket": "T-3"}, {"name": "Ada again", "ticket": "T-1"}]

try:
    # --- REST: quote -> pay -> links ---
    body = {"design": DESIGN, "event_end": END, "rows": ROWS, **CALLER}
    r = httpx.post(f"{base}/api/batches", json=body, timeout=30)
    pr = r.json()
    check("new batch without payment -> 402 for the 3 unique tickets",
          r.status_code == 402 and pr["accepts"][0]["amount"] == "30000" and "3 new passes" in pr["error"], r.text)
    with sqlite3.connect(db_path) as c:
        check("refused quote leaves no batch behind", c.execute("SELECT COUNT(*) FROM pilots").fetchone()[0] == 0)

    # --- #4: before the 402 only cheap checks; images are fetched (and a batch stored) only once paid ---
    img = {"design": {**DESIGN, "logo_png_b64": "https://unreachable.invalid/logo.png"}, "event_end": END,
           "rows": ROWS, **CALLER}
    r = httpx.post(f"{base}/api/batches", json=img, timeout=30)
    with sqlite3.connect(db_path) as c:
        n_pilots = c.execute("SELECT COUNT(*) FROM pilots").fetchone()[0]
    check("unpaid new batch with an unreachable image URL -> 402 (no fetch), no pilots row",
          r.status_code == 402 and "3 new passes" in r.json()["error"] and n_pilots == 0, (r.text, n_pilots))
    settles = sum(1 for p_, _ in seen if p_.endswith("/settle"))
    r = httpx.post(f"{base}/api/batches", json=img, headers=hdr(sign(r.json())), timeout=30)
    with sqlite3.connect(db_path) as c:
        n_pilots = c.execute("SELECT COUNT(*) FROM pilots").fetchone()[0]
    check("paid, the image fetch fails -> 400, nothing settled, no pilots row",
          r.status_code == 400 and "nothing was charged" in r.json()["error"] and n_pilots == 0
          and sum(1 for p_, _ in seen if p_.endswith("/settle")) == settles, (r.text, n_pilots))
    r = httpx.post(f"{base}/api/batches", json=body, headers=hdr(sign(pr)), timeout=60)
    res = r.json()
    check("paid batch -> 200 with key, 4 rows, 3 created, charged $0.03",
          r.status_code == 200 and res.get("batch_key") and len(res["passes"]) == 4 and res["created"] == 3
          and res["charged"] == "$0.03" and "PAYMENT-RESPONSE" in r.headers, r.text)
    key, bid = res["batch_key"], res["batch_id"]
    links = {p["ticket"]: p["link"] for p in res["passes"]}
    check("repeated ticket gets the same link, marked not new",
          res["passes"][3]["link"] == res["passes"][0]["link"] and res["passes"][3]["new"] is False)
    pk = httpx.get(links["T-2"])
    pj = pass_json(pk.content)
    check("permanent link serves a filled-in, updatable eventTicket",
          pk.status_code == 200 and pj["eventTicket"]["primaryFields"][0]["value"] == "Bo"
          and pj["barcodes"][0]["message"] == "T-2" and pj.get("webServiceURL") == f"{base}/passkit", pj)
    paid_rows = [x for x in requests_rows() if x[2] == 1]
    check("3 success rows logged, each with the payment tx (revenue counts per pass)",
          len(paid_rows) == 3 and all(x[4] == "0xbatch" and x[0] == "api" for x in paid_rows), paid_rows)

    # --- adding rows: only new tickets charged; reuse free ---
    auth = {"authorization": f"Bearer {key}"}
    r = httpx.post(f"{base}/api/batches/{bid}/passes", json={"rows": ROWS[:2], **CALLER}, headers=auth, timeout=30)
    check("re-sending existing tickets is free, same links",
          r.status_code == 200 and r.json()["created"] == 0 and r.json()["charged"] == "$0"
          and r.json()["passes"][0]["link"] == links["T-1"], r.text)
    more = {"rows": [{"name": "Di", "ticket": "T-4"}, {"name": "Ada", "ticket": "T-1"}], **CALLER}
    r = httpx.post(f"{base}/api/batches/{bid}/passes", json=more, headers=auth, timeout=30)
    check("adding 1 new ticket -> 402 for $0.01", r.status_code == 402 and r.json()["accepts"][0]["amount"] == "10000", r.text)
    stale = sign(pr)  # signed for $0.03: must not be accepted for a $0.01 quote... nor vice versa
    r = httpx.post(f"{base}/api/batches/{bid}/passes", json=more, headers={**auth, **hdr(sign(r.json()))}, timeout=30)
    check("paid add -> 1 created", r.status_code == 200 and r.json()["created"] == 1 and r.json()["issued_total"] == 4, r.text)
    r = httpx.post(f"{base}/api/batches/{bid}/passes", json={"rows": [{"name": "E", "ticket": "T-5"}], **CALLER},
                   headers={**auth, **hdr(stale)}, timeout=30)
    check("payment for the wrong amount -> 402 naming the price", r.status_code == 402 and "$0.01" in r.json()["error"], r.text)
    r = httpx.post(f"{base}/api/batches/{bid}/passes", json=more, headers={"authorization": "Bearer nope"}, timeout=30)
    check("wrong bearer -> 401", r.status_code == 401, r.text)

    # --- GET + links.csv ---
    r = httpx.get(f"{base}/api/batches/{bid}", headers=auth)
    check("GET batch lists all 4 links", r.status_code == 200 and len(r.json()["passes"]) == 4, r.text)
    r = httpx.get(r.json()["links_csv_url"])
    check("links.csv works for a batch", r.status_code == 200 and r.text.count("/pass/") == 4, r.text)
    r = httpx.post(f"{base}/api/pilot/{bid}/issue", json={"key": key, "rows": [{"name": "F", "ticket": "F-1"}]})
    check("free pilot issue route refuses a batch", r.status_code == 403 and "paid batch" in r.text, r.text)
    r = httpx.post(f"{base}/api/pilot/{bid}/update", json={"key": key, "changes": {"change_message": "x %@"}})
    check("free pilot update route refuses a batch", r.status_code == 403, r.text)

    # --- rollback: settlement fails ---
    mode["settle"] = False
    one = {"rows": [{"name": "G", "ticket": "T-6"}], **CALLER}
    q = httpx.post(f"{base}/api/batches/{bid}/passes", json=one, headers=auth, timeout=30).json()
    r = httpx.post(f"{base}/api/batches/{bid}/passes", json=one, headers={**auth, **hdr(sign(q))}, timeout=30)
    mode["settle"] = True
    r2 = httpx.get(f"{base}/api/batches/{bid}", headers=auth).json()
    check("failed settlement -> 402, the new pass is rolled back",
          r.status_code == 402 and "not charged" in r.json()["error"] and r2["issued_total"] == 4, r.text)
    q = httpx.post(f"{base}/api/batches", json=body, timeout=30).json()
    mode["settle"] = False
    r = httpx.post(f"{base}/api/batches", json=body, headers=hdr(sign(q)), timeout=30)
    mode["settle"] = True
    with sqlite3.connect(db_path) as c:
        n_batches = c.execute("SELECT COUNT(*) FROM pilots").fetchone()[0]
    check("failed settlement of a NEW batch deletes it", r.status_code == 402 and n_batches == 1, (r.text, n_batches))

    # --- rollback: one row doesn't build (EAN13 needs digits) ---
    bad = {"design": {**DESIGN, "barcode_format": "EAN13"}, "event_end": END,
           "rows": [{"name": "A", "ticket": "4006381333931"}, {"name": "B", "ticket": "not-digits"}], **CALLER}
    q = httpx.post(f"{base}/api/batches", json=bad, timeout=30)
    r = httpx.post(f"{base}/api/batches", json=bad, headers=hdr(sign(q.json())), timeout=30) if q.status_code == 402 else q
    settles_before = sum(1 for p, _ in seen if p.endswith("/settle"))
    with sqlite3.connect(db_path) as c:
        n_batches = c.execute("SELECT COUNT(*) FROM pilots").fetchone()[0]
    check("a row that doesn't build -> 400 naming it, nothing kept",
          r.status_code == 400 and "not-digits" in r.json()["error"] and "nothing was charged" in r.json()["error"]
          and n_batches == 1, (r.text, n_batches))

    # --- validation ---
    r = httpx.post(f"{base}/api/batches", json={"design": DESIGN, "rows": ROWS, **CALLER})
    check("new batch without event_end -> 400", r.status_code == 400, r.text)
    r = httpx.post(f"{base}/api/batches", json={"design": {**DESIGN, "updatable": True}, "event_end": END, "rows": ROWS, **CALLER})
    check("design with updatable -> 400", r.status_code == 400 and "updatable" in r.text, r.text)
    r = httpx.post(f"{base}/api/batches", json={"design": DESIGN, "event_end": END, "rows": ROWS})
    check("missing contact_email -> 400", r.status_code == 400 and "contact_email" in r.text, r.text)
    r = httpx.post(f"{base}/api/batches", json={"design": DESIGN, "event_end": "2099-01-01", "rows": ROWS, **CALLER})
    check("event_end too far -> 400", r.status_code == 400 and "within" in r.text, r.text)
    r = httpx.post(f"{base}/api/batches", json={"design": DESIGN, "event_end": END, "rows": [{"name": "x"}], **CALLER})
    check("row without ticket -> 400", r.status_code == 400 and "ticket" in r.text, r.text)

    # --- batch update: re-signs all 4, charges 4 ---
    ch = {"secondary_fields": [{"key": "doors", "label": "Doors", "value": "8pm"}],
          "change_message": "Doors now %@", **CALLER}
    q = httpx.patch(f"{base}/api/batches/{bid}", json=ch, headers=auth, timeout=30)
    check("batch update -> 402 for 4 passes", q.status_code == 402 and q.json()["accepts"][0]["amount"] == "40000", q.text)
    r = httpx.patch(f"{base}/api/batches/{bid}", json=ch, headers={**auth, **hdr(sign(q.json()))}, timeout=60)
    check("paid update re-signs 4 passes", r.status_code == 200 and r.json()["updated"] == 4 and r.json()["charged"] == "$0.04", r.text)
    pj = pass_json(httpx.get(links["T-2"]).content)
    sf = pj["eventTicket"]["secondaryFields"][0]
    check("link now serves the new value with the changeMessage", sf["value"] == "8pm" and sf["changeMessage"] == "Doors now %@", sf)
    # --- #5: a failed settlement on update leaves every pass and the saved design as they were ---
    def batch_state(batch_id):
        with sqlite3.connect(db_path) as c:
            design = c.execute("SELECT design FROM pilots WHERE id = ?", (batch_id,)).fetchone()[0]
            passes = c.execute("SELECT p.serial, p.pkpass, p.updated, p.params FROM pilot_passes pp "
                               "JOIN passes p ON p.serial = pp.serial WHERE pp.pilot_id = ? ORDER BY p.serial",
                               (batch_id,)).fetchall()
            staged = c.execute("SELECT COUNT(*) FROM pass_staging").fetchone()[0]
        return design, passes, staged

    def paid_rows():
        return [x for x in requests_rows() if x[4]]

    before, paid_before = batch_state(bid), len(paid_rows())
    ch9 = {"secondary_fields": [{"key": "doors", "label": "Doors", "value": "9pm"}], **CALLER}
    q = httpx.patch(f"{base}/api/batches/{bid}", json=ch9, headers=auth, timeout=30)
    settles = sum(1 for p_, _ in seen if p_.endswith("/settle"))
    mode["settle"] = False
    r = httpx.patch(f"{base}/api/batches/{bid}", json=ch9, headers={**auth, **hdr(sign(q.json()))}, timeout=60)
    mode["settle"] = True
    after = batch_state(bid)
    sf = pass_json(httpx.get(links["T-2"]).content)["eventTicket"]["secondaryFields"][0]
    check("update whose settlement fails -> 402 'not charged', settlement was tried",
          r.status_code == 402 and "not charged" in r.json()["error"]
          and sum(1 for p_, _ in seen if p_.endswith("/settle")) == settles + 1, r.text)
    check("... pass content, updated stamps and saved design unchanged, staging emptied",
          after == (before[0], before[1], 0) and sf["value"] == "8pm" and "9pm" not in after[0], (sf, after[2]))
    check("... no paid rows logged", len(paid_rows()) == paid_before, paid_rows())

    # --- #5: only live passes are rebuilt and charged ---
    t3 = links["T-3"].split("/pass/")[1].split("/")[0]
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE passes SET expires = ? WHERE serial = ?", (int(time.time()) - 1, t3))
        t3_before = c.execute("SELECT pkpass, updated FROM passes WHERE serial = ?", (t3,)).fetchone()
    q = httpx.patch(f"{base}/api/batches/{bid}", json=ch9, headers=auth, timeout=30)
    check("with one pass expired, the update quote is for the 3 live ones",
          q.status_code == 402 and q.json()["accepts"][0]["amount"] == "30000", q.text)
    r = httpx.patch(f"{base}/api/batches/{bid}", json=ch9, headers={**auth, **hdr(sign(q.json()))}, timeout=60)
    with sqlite3.connect(db_path) as c:
        t3_after = c.execute("SELECT pkpass, updated FROM passes WHERE serial = ?", (t3,)).fetchone()
        n_staged = c.execute("SELECT COUNT(*) FROM pass_staging").fetchone()[0]
    sf = pass_json(httpx.get(links["T-2"]).content)["eventTicket"]["secondaryFields"][0]
    check("paid update re-signs and charges the 3 live passes only",
          r.status_code == 200 and r.json()["updated"] == 3 and r.json()["charged"] == "$0.03"
          and sf["value"] == "9pm" and t3_after == t3_before and n_staged == 0, r.text)
    check("... logged 3 paid update rows", sum(1 for x in paid_rows() if x[1] == "update") == 4 + 3,
          [x for x in paid_rows() if x[1] == "update"])

    badch = {"barcode_format": "x", **CALLER}
    r = httpx.patch(f"{base}/api/batches/{bid}", json=badch, headers=auth)
    check("unknown update key -> 400", r.status_code == 400, r.text)

    # --- MCP ---
    res = asyncio.run(mcp_call("create_wallet_pass_batch", {"design": DESIGN, "event_end": END,
                                                            "rows": [{"name": "M", "ticket": "M-1"}], **CALLER}))
    sc = res.structured_content or {}
    check("MCP batch without payment -> isError with PaymentRequired for $0.01",
          res.is_error and sc.get("accepts", [{}])[0].get("amount") == "10000", res)
    res = asyncio.run(mcp_call("create_wallet_pass_batch", {"design": DESIGN, "event_end": END,
                                                            "rows": [{"name": "M", "ticket": "M-1"}], **CALLER,
                                                            "x402_payment": sign(sc)}))
    sc = res.structured_content or {}
    check("MCP paid batch -> key + link + receipt in _meta",
          not res.is_error and sc.get("batch_key") and "/pass/" in sc["passes"][0]["link"]
          and (res.meta or {}).get("x402/payment-response"), res)
    mkey = sc.get("batch_key")
    res = asyncio.run(mcp_call("update_wallet_pass_batch", {"batch_key": mkey, "relevant_date": END, **CALLER}))
    check("MCP batch update without payment -> isError 402", res.is_error and (res.structured_content or {}).get("accepts"), res)
    res = asyncio.run(mcp_call("update_wallet_pass_batch", {"batch_key": mkey, "relevant_date": END, **CALLER,
                                                            "x402_payment": sign(res.structured_content)}))
    check("MCP paid batch update", not res.is_error and (res.structured_content or {}).get("updated") == 1, res)

    # --- #5: once the event window has ended, an update is refused before any payment work ---
    mbid, mauth = mkey.split(".")[0], {"authorization": f"Bearer {mkey}"}
    q = httpx.patch(f"{base}/api/batches/{mbid}", json=ch, headers=mauth, timeout=30)
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE pilots SET event_end_epoch = ? WHERE id = ?", (int(time.time()) - 8 * 86400, mbid))
    calls, paid_before, before = len(seen), len(paid_rows()), batch_state(mbid)
    r = httpx.patch(f"{base}/api/batches/{mbid}", json=ch, headers={**mauth, **hdr(sign(q.json()))}, timeout=30)
    check("update of a batch whose event window ended -> 400, nothing verified or settled or changed",
          q.status_code == 402 and r.status_code == 400 and "ended" in r.json()["error"] and len(seen) == calls
          and len(paid_rows()) == paid_before and batch_state(mbid) == before, (q.status_code, r.text))

    async def list_tools():
        async with streamable_http_client(f"{base}/mcp") as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                return (await s.list_tools()).tools
    tl = {t.name: t for t in asyncio.run(list_tools())}
    bt = tl.get("create_wallet_pass_batch")
    check("batch tool advertises contact_email/x402_ready as required and the price",
          bt and {"contact_email", "x402_ready"} <= set(bt.input_schema["required"]) and "$0.01" in bt.description
          and "{price}" not in bt.description, bt)

    # --- paid cap counts passes, not calls (limit 40/day/IP; 3+1+4+1+1 = 10 used) ---
    big = {"design": DESIGN, "event_end": END, "rows": [{"name": f"N{i}", "ticket": f"N-{i}"} for i in range(31)], **CALLER}
    r = httpx.post(f"{base}/api/batches", json=big, timeout=30)
    check("a batch over the paid daily cap -> 429 with what's left", r.status_code == 429 and "this call needs 31" in r.text, r.text)

    # --- single-pass paths unchanged ---
    r = httpx.post(f"{base}/api/passes", json={"style": "generic", "organization_name": "S", "description": "d", **CALLER})
    check("paid batch passes don't use up the same caller's free passes (#3)", r.status_code == 200, r.text)
    r = httpx.post(f"{base}/api/passes", json={"style": "generic", "organization_name": "S", "description": "d",
                                                "contact_email": "solo@realevent.dev", "x402_ready": False},
                   headers={"x-real-ip": "10.9.9.9"})
    check("single free pass still works", r.status_code == 200 and r.json().get("download_url"), r.text)
finally:
    proc.terminate()
    proc.wait()
    fac.shutdown()
    if fails:
        print((tmp / "server.log").read_text()[-3000:])

print("ALL PASSED" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
