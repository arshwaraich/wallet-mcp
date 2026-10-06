"""End-to-end tests for the REST API (POST /api/passes) alongside the MCP tool.
Starts a real server on a spare port with a throwaway db, never the production one.
Run: .venv/bin/python tests/test_api.py"""
import asyncio, os, socket, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

import httpx2 as httpx  # the mcp SDK v2 ships httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
db_path = Path(tmp) / "test.db"
# a pre-API database, to check the source column is migrated in
with sqlite3.connect(db_path) as conn:
    conn.execute("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ip TEXT,
                    style TEXT, organization_name TEXT, success INTEGER NOT NULL, error TEXT,
                    duration_ms INTEGER, serial_number TEXT)""")
    conn.execute("INSERT INTO requests (ts, ip, style, organization_name, success) VALUES ('2026-01-01T00:00:00', 'x', 'generic', 'Old', 1)")

port = free_port()
base = f"http://127.0.0.1:{port}"
env = dict(os.environ, PORT=str(port), WALLET_MCP_DB=str(db_path), WALLET_MCP_IP_LIMIT="3",
           WALLET_MCP_PUBLIC_URL=base, WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}")
proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/stats", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)

    PASS = {"style": "generic", "organization_name": "API Test", "description": "test",
            "primary_fields": [{"label": "Name", "value": "Ada"}]}

    # 1. a valid API call returns a working download link
    r = httpx.post(f"{base}/api/passes", json=PASS)
    check("api: 200 on a valid pass", r.status_code == 200, r.text)
    body = r.json()
    check("api: same response shape as the tool",
          set(body) == {"download_url", "expires_in_seconds", "serial_number", "pass_type_identifier"}, body)
    dl = httpx.get(body["download_url"])
    check("api: download is a pkpass", dl.status_code == 200 and dl.headers["content-type"] == "application/vnd.apple.pkpass"
          and dl.content[:2] == b"PK", dl.headers)

    # 2. input errors are 400s with a readable message
    r = httpx.post(f"{base}/api/passes", json={**PASS, "colour": "#fff"})
    check("api: unknown parameter rejected", r.status_code == 400 and any("colour" in d for d in r.json()["details"]), r.text)
    r = httpx.post(f"{base}/api/passes", json={"style": "generic"})
    check("api: missing required parameter rejected", r.status_code == 400 and any("organization_name" in d for d in r.json()["details"]), r.text)
    r = httpx.post(f"{base}/api/passes", content=b"not json", headers={"content-type": "application/json"})
    check("api: non-JSON body rejected", r.status_code == 400, r.text)
    r = httpx.post(f"{base}/api/passes", json=[PASS])
    check("api: JSON array rejected", r.status_code == 400, r.text)
    r = httpx.post(f"{base}/api/passes", json={**PASS, "style": "nope"})
    check("api: pass builder errors are 400s", r.status_code == 400 and "style" in r.json()["error"], r.text)
    r = httpx.get(f"{base}/api/passes")
    check("api: GET not allowed", r.status_code == 405, r.status_code)

    # 3. the MCP tool still works and is logged as mcp
    async def mcp_call():
        async with streamable_http_client(f"{base}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("create_wallet_pass", PASS)
    result = asyncio.run(mcp_call())
    check("mcp: tool call succeeds", not result.is_error, result)

    # 4. limits are shared: 1 api + 1 failed build + 1 mcp = 3 = the test limit, so both paths now refuse.
    #    (schema-invalid bodies are rejected before the limiter, like MCP args, so they don't count)
    r = httpx.post(f"{base}/api/passes", json=PASS)
    check("api: 429 once the shared per-IP limit is used up", r.status_code == 429 and "rate limit" in r.json()["error"], r.text)
    result = asyncio.run(mcp_call())
    check("mcp: refused by the same limit", result.is_error and "rate limit" in result.content[0].text, result)

    # 5. the log records which path each request came through
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT source, success FROM requests ORDER BY id").fetchall()
    check("db: old rows migrated as mcp", rows[0] == ("mcp", 1), rows)
    check("db: sources logged", [r[0] for r in rows[1:]] == ["api", "api", "mcp", "api", "mcp"], rows)
    stats = httpx.get(f"{base}/api/stats").json()
    check("stats: by_source and recent.source present",
          {s["source"] for s in stats["by_source"]} == {"api", "mcp"} and "source" in stats["recent"][0], stats["by_source"])
finally:
    proc.terminate()
    proc.wait()
    for p in (ROOT / "downloads").glob("*.pkpass"):
        if time.time() - p.stat().st_mtime < 120:
            pass  # left for the sweeper; production downloads share this dir, so don't touch them

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
