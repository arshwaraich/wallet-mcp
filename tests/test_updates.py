"""End-to-end tests for updatable passes (PassKit web service + APNs push) and image URLs.
Starts a real server on a spare port with a throwaway db and a fake APNs, never the production ones.
Run: .venv/bin/python tests/test_updates.py"""
import asyncio, datetime, inspect, io, json, os, sqlite3, socket, subprocess, sys, tempfile, threading, time, zipfile
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx2 as httpx  # the mcp SDK v2 ships httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
fails = 0
LIVE_TOKEN, DEAD_TOKEN = "aa" * 32, "bb" * 32
pushes = []


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))
    fails += 0 if cond else 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeAPNs(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        token = self.path.rsplit("/", 1)[-1]
        pushes.append((token, self.headers.get("apns-topic")))
        self.send_response(410 if token == DEAD_TOKEN else 200)
        self.end_headers()

    def log_message(self, *a):
        pass


def pass_json(pkpass: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(pkpass)) as zf:
        return json.loads(zf.read("pass.json"))


def pass_files(pkpass: bytes) -> set:
    with zipfile.ZipFile(io.BytesIO(pkpass)) as zf:
        return set(zf.namelist())


apns_port = free_port()
apns = HTTPServer(("127.0.0.1", apns_port), FakeAPNs)
threading.Thread(target=apns.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp()
port = free_port()
base = f"http://127.0.0.1:{port}"
db_path = Path(tmp) / "test.db"
env = dict(os.environ, PORT=str(port), WALLET_MCP_DB=str(db_path), WALLET_MCP_IP_LIMIT="100",
           WALLET_MCP_PUBLIC_URL=base, WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}",
           WALLET_MCP_APNS_URL=f"http://127.0.0.1:{apns_port}")
proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/stats", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)

    PASS = {"style": "storeCard", "organization_name": "Bean Club", "description": "loyalty card",
            "primary_fields": [{"key": "points", "label": "Points", "value": "100",
                                "changeMessage": "You now have %@ points"}],
            "secondary_fields": [{"label": "Member", "value": "Ada"}],
            "barcode_message": "MEMBER-1"}

    # --- creating ---
    plain = httpx.post(f"{base}/api/passes", json=PASS).json()
    check("plain pass has no edit_token", "edit_token" not in plain, plain)
    pj = pass_json(httpx.get(plain["download_url"]).content)
    check("plain pass has no webServiceURL", "webServiceURL" not in pj and "authenticationToken" not in pj, pj)

    r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True, "updatable_hours": 2})
    created = r.json()
    until = datetime.datetime.fromisoformat(created.get("updatable_until", "1970-01-01T00:00:00+00:00")).timestamp()
    check("updatable_until is updatable_hours from now", abs(until - (time.time() + 7200)) < 60, created)
    check("updatable create: 200 with edit_token", r.status_code == 200 and "edit_token" in created, r.text)
    serial, edit_token = created["serial_number"], created["edit_token"]
    pj = pass_json(httpx.get(created["download_url"]).content)
    check("updatable pass points Wallet at /passkit", pj.get("webServiceURL") == f"{base}/passkit", pj)
    auth = pj.get("authenticationToken", "")
    check("authenticationToken is long enough for Wallet (16+)", len(auth) >= 16, auth)
    check("changeMessage is written into the field",
          pj["storeCard"]["primaryFields"][0].get("changeMessage") == "You now have %@ points", pj)
    check("edit_token is not the authenticationToken", edit_token != auth)

    r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True, "serial_number": serial})
    check("updatable create refuses a serial already in use", r.status_code == 400, r.text)

    for bad in (0, 721, -5):
        r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True, "updatable_hours": bad})
        check(f"updatable_hours={bad!r} refused", r.status_code == 400, r.text)
    r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable_hours": 5})
    check("updatable_hours without updatable refused", r.status_code == 400, r.text)
    r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True, "updatable_hours": 720})
    check("updatable_hours=720 (30 days) accepted", r.status_code == 200, r.text)
    default = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True}).json()
    until = datetime.datetime.fromisoformat(default["updatable_until"]).timestamp()
    check("default update window is 1 hour", abs(until - (time.time() + 3600)) < 60, default)

    # --- device registration (what Wallet does after the pass is added) ---
    PT = "pass.com.arshwaraich.vps"
    reg = lambda dev: f"{base}/passkit/v1/devices/{dev}/registrations/{PT}/{serial}"
    ok_auth = {"Authorization": f"ApplePass {auth}"}
    r = httpx.post(reg("dev1"), json={"pushToken": LIVE_TOKEN}, headers={"Authorization": "ApplePass wrong"})
    check("register: wrong token is 401", r.status_code == 401, r.status_code)
    r = httpx.post(reg("dev1"), json={"pushToken": LIVE_TOKEN}, headers={"Authorization": f"ApplePass {edit_token}"})
    check("register: edit_token is not accepted as the pass token", r.status_code == 401, r.status_code)
    r = httpx.post(reg("dev1"), json={"pushToken": LIVE_TOKEN}, headers=ok_auth)
    check("register: new device is 201", r.status_code == 201, r.status_code)
    r = httpx.post(reg("dev1"), json={"pushToken": LIVE_TOKEN}, headers=ok_auth)
    check("register: same device again is 200", r.status_code == 200, r.status_code)
    r = httpx.post(reg("dev2"), json={"pushToken": "not/hex"}, headers=ok_auth)
    check("register: malformed push token is 400", r.status_code == 400, r.status_code)
    httpx.post(reg("dev2"), json={"pushToken": DEAD_TOKEN}, headers=ok_auth)

    serials_url = f"{base}/passkit/v1/devices/dev1/registrations/{PT}"
    r = httpx.get(serials_url)
    check("device serial list includes the pass", r.status_code == 200 and serial in r.json()["serialNumbers"], r.text)
    tag = r.json()["lastUpdated"]
    r = httpx.get(serials_url, params={"passesUpdatedSince": tag})
    check("nothing newer than lastUpdated: 204", r.status_code == 204, r.status_code)

    latest = f"{base}/passkit/v1/passes/{PT}/{serial}"
    r = httpx.get(latest, headers=ok_auth)
    check("latest pass is served to Wallet", r.status_code == 200 and r.headers["content-type"] == "application/vnd.apple.pkpass"
          and "last-modified" in r.headers, r.status_code)
    r = httpx.get(latest, headers={**ok_auth, "If-Modified-Since": r.headers["last-modified"]})
    check("unchanged pass: 304", r.status_code == 304, r.status_code)
    check("latest pass needs the token", httpx.get(latest).status_code == 401)

    # --- updating over REST ---
    patch = lambda body, token=edit_token: httpx.patch(
        f"{base}/api/passes/{serial}", json=body, headers={"Authorization": f"Bearer {token}"} if token else {})
    check("update: no token is 401", patch({"voided": True}, token=None).status_code == 401)
    r = patch({"voided": True}, token="wrong")
    check("update: wrong token is 404", r.status_code == 404, r.text)
    r = patch({})
    check("update: empty body is 400", r.status_code == 400, r.text)
    r = patch({"bogus": 1})
    check("update: unknown key is 400", r.status_code == 400, r.text)

    time.sleep(1.1)  # so Last-Modified moves on
    pushes.clear()
    r = patch({"primary_fields": [{"key": "points", "label": "Points", "value": "150",
                                   "changeMessage": "You now have %@ points"}]})
    body = r.json()
    check("update: 200", r.status_code == 200, r.text)
    check("update: one live device notified", body.get("notified_devices") == 1, body)
    check("update: APNs pushed with the pass type as topic",
          sorted(pushes) == sorted([(LIVE_TOKEN, PT), (DEAD_TOKEN, PT)]), pushes)
    r = httpx.get(serials_url, params={"passesUpdatedSince": tag})
    check("updated pass shows up as newer than the old tag", r.status_code == 200 and serial in r.json()["serialNumbers"], r.status_code)
    pj = pass_json(httpx.get(latest, headers=ok_auth).content)
    check("update: new value served", pj["storeCard"]["primaryFields"][0]["value"] == "150", pj)
    check("update: untouched fields kept", pj["storeCard"]["secondaryFields"][0]["value"] == "Ada"
          and pj["barcodes"][0]["message"] == "MEMBER-1", pj)
    check("update: same serial and token", pj["serialNumber"] == serial and pj["authenticationToken"] == auth, pj)
    pushes.clear()
    patch({"logo_text": "Bean Club!"})
    check("dead push token was dropped after APNs said 410", [t for t, _ in pushes] == [LIVE_TOKEN], pushes)

    # --- updating over MCP (revoke) ---
    async def mcp():
        async with streamable_http_client(f"{base}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                check("mcp: update tool listed", "update_wallet_pass" in tools, list(tools))
                res = await session.call_tool("update_wallet_pass", {"serial_number": serial, "edit_token": edit_token, "voided": True})
                check("mcp: revoke via voided=True", not res.is_error, res.content)
                res = await session.call_tool("update_wallet_pass", {"serial_number": serial, "edit_token": "nope", "voided": True})
                check("mcp: wrong token is a visible error", res.is_error and "edit_token" in res.content[0].text, res.content)
    asyncio.run(mcp())
    check("revoked pass is voided", pass_json(httpx.get(latest, headers=ok_auth).content).get("voided") is True)

    r = httpx.delete(reg("dev1"), headers=ok_auth)
    check("unregister: 200", r.status_code == 200, r.status_code)

    # --- update window ending ---
    httpx.post(reg("dev3"), json={"pushToken": LIVE_TOKEN}, headers=ok_auth)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE passes SET expires = ? WHERE serial = ?", (int(time.time()) - 1, serial))
    r = patch({"voided": False})
    check("expired window: update refused with a reason", r.status_code == 404 and "update window" in r.text, r.text)
    check("expired window: Wallet can no longer fetch it", httpx.get(latest, headers=ok_auth).status_code == 401)
    check("expired window: not listed as updated", httpx.get(f"{base}/passkit/v1/devices/dev3/registrations/{PT}").status_code == 204)
    for _ in range(40):  # the sweeper runs every 30s
        with sqlite3.connect(db_path) as conn:
            left = conn.execute("SELECT (SELECT COUNT(*) FROM passes WHERE serial = ?) + "
                                "(SELECT COUNT(*) FROM registrations WHERE serial = ?)", (serial, serial)).fetchone()[0]
        if not left:
            break
        time.sleep(1)
    check("expired window: stored pass and registrations deleted", left == 0, left)

    # --- image URLs ---
    for url, why in (("http://walletmcppass.com/icon-192.png", "http"), ("https://127.0.0.1/x.png", "loopback"),
                     ("https://169.254.169.254/latest", "link-local metadata"), ("https://100.119.64.32/x.png", "tailnet")):
        r = httpx.post(f"{base}/api/passes", json={**PASS, "logo_png_b64": url})
        check(f"image URL refused: {why}", r.status_code == 400, r.text)
    r = httpx.post(f"{base}/api/passes", json={**PASS, "logo_png_b64": "https://walletmcppass.com/icon-192.png",
                                               "thumbnail_png_b64": "https://walletmcppass.com/favicon.ico"})
    check("image URL fetched (PNG, and ICO converted)", r.status_code == 200, r.text)
    if r.status_code == 200:
        files = pass_files(httpx.get(r.json()["download_url"]).content)
        check("fetched images are in the pass", {"logo.png", "thumbnail.png"} <= files, files)
finally:
    proc.terminate()
    proc.wait()

# the update tool must offer exactly the create tool's pass parameters
sys.path.insert(0, str(ROOT))
os.environ["WALLET_MCP_DB"] = str(Path(tmp) / "sig.db")
import server  # noqa: E402
create = set(inspect.signature(server.create_wallet_pass).parameters) - {"ctx", "serial_number", "updatable", "updatable_hours"}
update = set(inspect.signature(server.update_wallet_pass).parameters) - {"ctx", "serial_number", "edit_token"}
check("update tool params match create tool params", create == update, create ^ update)

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
