"""Tests that every request logs its client (MCP clientInfo or User-Agent) and whether it carried an
x402 payment. Starts a real server on a spare port with a throwaway db, never the production one.
Run: .venv/bin/python tests/test_caller_log.py"""
import asyncio, os, socket, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

import httpx2 as httpx  # the mcp SDK v2 ships httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

ROOT = Path(__file__).resolve().parent.parent
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))
    fails += 0 if cond else 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


tmp = tempfile.mkdtemp()
port = free_port()
base = f"http://127.0.0.1:{port}"
db_path = Path(tmp) / "test.db"
env = dict(os.environ, PORT=str(port), WALLET_MCP_DB=str(db_path), WALLET_MCP_IP_LIMIT="100",
           WALLET_MCP_PUBLIC_URL=base, WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}")
proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
PASS = {"style": "generic", "organization_name": "Caller Test", "description": "test pass"}


def last_row():
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT source, action, client, x402, success FROM requests ORDER BY id DESC").fetchone()


async def mcp_call(tool, args, meta=None):
    async with streamable_http_client(f"{base}/mcp") as (read, write):
        async with ClientSession(read, write, client_info=Implementation(name="test-agent", version="9.9")) as s:
            await s.initialize()
            return await s.call_tool(tool, args, meta=meta)


try:
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/stats", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)

    # --- optional contact_email and x402_ready ---
    def info():
        with sqlite3.connect(db_path) as conn:
            return conn.execute("SELECT contact_email, x402_ready FROM requests ORDER BY id DESC").fetchone()

    def count():
        with sqlite3.connect(db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    r = httpx.post(f"{base}/api/passes", json=PASS)
    check("left out, both log as NULL", r.status_code == 200 and info() == (None, None), info())
    r = httpx.post(f"{base}/api/passes", json={**PASS, "contact_email": "a@example.com", "x402_ready": True})
    check("api create logs both", r.status_code == 200 and info() == ("a@example.com", 1), info())
    r = httpx.post(f"{base}/api/passes", json={**PASS, "x402_ready": False})
    check("x402_ready=False logs 0", r.status_code == 200 and info() == (None, 0), info())
    before = count()
    r = httpx.post(f"{base}/api/passes", json={**PASS, "contact_email": "not an email"})
    check("bad address -> 400, not logged as a call", r.status_code == 400 and count() == before, r.text)

    r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True, "contact_email": "b@example.com"})
    made = r.json()
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT params FROM passes WHERE serial = ?", (made["serial_number"],)).fetchone()[0]
    check("not stored with the pass", "b@example.com" not in stored and "x402_ready" not in stored, stored)
    r = httpx.patch(f"{base}/api/passes/{made['serial_number']}", json={"contact_email": "b@example.com"},
                    headers={"authorization": f"Bearer {made['edit_token']}"})
    check("an update with only these has nothing to change", r.status_code == 400, r.text)
    r = httpx.patch(f"{base}/api/passes/{made['serial_number']}", json={"description": "v2", "x402_ready": True},
                    headers={"authorization": f"Bearer {made['edit_token']}"})
    check("api update logs them", r.status_code == 200 and info() == (None, 1), r.text)
    res = asyncio.run(mcp_call("create_wallet_pass", {**PASS, "contact_email": "c@example.com", "x402_ready": False}))
    check("mcp create logs both", not res.is_error and info() == ("c@example.com", 0), info())
    res = asyncio.run(mcp_call("update_wallet_pass", {"serial_number": made["serial_number"],
                                                      "edit_token": made["edit_token"], "description": "v3",
                                                      "contact_email": "d@example.com"}))
    check("mcp update logs them", not res.is_error and info() == ("d@example.com", None), info())

    # --- REST ---
    r = httpx.post(f"{base}/api/passes", json={**PASS, "updatable": True}, headers={"user-agent": "rest-agent/1"})
    check("api create ok", r.status_code == 200, r.text)
    created = r.json()
    check("api create logs User-Agent, no payment", last_row() == ("api", "create", "rest-agent/1", 0, 1), last_row())

    r = httpx.patch(f"{base}/api/passes/{created['serial_number']}", json={"description": "v2"},
                    headers={"authorization": f"Bearer {created['edit_token']}", "user-agent": "rest-agent/1",
                             "payment-signature": "eyJ4NDAyVmVyc2lvbiI6Mn0="})
    check("api update ok", r.status_code == 200, r.text)
    check("api update with PAYMENT-SIGNATURE logs x402=1", last_row() == ("api", "update", "rest-agent/1", 1, 1),
          last_row())

    r = httpx.patch(f"{base}/api/passes/{created['serial_number']}", json={"description": "v3"},
                    headers={"authorization": f"Bearer {created['edit_token']}", "x-payment": "e30="})
    check("api update with X-PAYMENT logs x402=1", r.status_code == 200 and last_row()[3] == 1, last_row())

    # --- MCP ---
    res = asyncio.run(mcp_call("create_wallet_pass", PASS))
    check("mcp create ok", not res.is_error, res)
    check("mcp create logs clientInfo, no payment", last_row() == ("mcp", "create", "test-agent/9.9", 0, 1),
          last_row())

    res = asyncio.run(mcp_call("create_wallet_pass", PASS, meta={"x402/payment": {"x402Version": 2}}))
    check("mcp create with x402/payment meta ok", not res.is_error, res)
    check("mcp x402/payment meta logs x402=1", last_row() == ("mcp", "create", "test-agent/9.9", 1, 1), last_row())
finally:
    proc.terminate()
    proc.wait()

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
