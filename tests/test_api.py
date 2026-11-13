"""End-to-end tests for the REST API (POST /api/passes) alongside the MCP tool.
Starts a real server on a spare port with a throwaway db, never the production one.
Run: .venv/bin/python tests/test_api.py"""
import asyncio, sqlite3

import httpx2 as httpx  # the mcp SDK v2 ships httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from _harness import Server, check, finish, tempdir

db_path = tempdir() / "test.db"
# a pre-API database, to check the source column is migrated in
with sqlite3.connect(db_path) as conn:
    conn.execute("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ip TEXT,
                    style TEXT, organization_name TEXT, success INTEGER NOT NULL, error TEXT,
                    duration_ms INTEGER, serial_number TEXT)""")
    conn.execute("INSERT INTO requests (ts, ip, style, organization_name, success) VALUES ('2026-01-01T00:00:00', 'x', 'generic', 'Old', 1)")

srv = Server({"WALLET_MCP_IP_LIMIT": "3"}, db_path=db_path)
base = srv.base
try:
    PASS = {"style": "generic", "organization_name": "API Test", "description": "test", "contact_email": "t@realco.dev", "x402_ready": False,
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
    r = httpx.post(f"{base}/api/passes", json={**PASS, "padding": "A" * (2 * 1024 * 1024)})
    check("api: a 2 MB body (above nginx's 1 MB) is parsed, not refused for size", r.status_code == 400
          and r.json()["error"] == "invalid request", r.text[:300])
    r = httpx.post(f"{base}/api/passes", json={**PASS, "padding": "A" * (3 * 1024 * 1024)})
    check("api: a body over 3 MB -> 413", r.status_code == 413 and r.json() == {"error": "request body too large"},
          r.text[:300])
    r = httpx.patch(f"{base}/api/passes/x", json={"organization_name": "X"})
    check("api: PATCH without a bearer edit_token -> 401", r.status_code == 401 and r.json() == {
        "error": "send the pass's edit_token as 'Authorization: Bearer <edit_token>'"}, r.text)
    r = httpx.patch(f"{base}/api/passes/x", content=b"[]", headers={"authorization": "Bearer t"})
    check("api: PATCH with a non-object body -> 400",
          r.status_code == 400 and r.json() == {"error": "request body must be a JSON object"}, r.text)

    # 3. the MCP tool still works and is logged as mcp
    async def mcp_call():
        async with streamable_http_client(f"{base}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("create_wallet_pass", PASS)
    result = asyncio.run(mcp_call())
    check("mcp: tool call succeeds", not result.is_error, result)

    # 4. limits are shared: 1 api + 1 mcp + 1 more api = 3 = the test limit, so both paths now refuse.
    #    (failed builds don't count, and schema-invalid bodies are rejected before the limiter)
    r = httpx.post(f"{base}/api/passes", json=PASS)
    check("api: failed build didn't use up the limit", r.status_code == 200, r.text)
    r = httpx.post(f"{base}/api/passes", json=PASS)
    check("api: 429 once the shared per-IP limit is used up", r.status_code == 429 and "rate limit" in r.json()["error"], r.text)
    result = asyncio.run(mcp_call())
    check("mcp: refused by the same limit", result.is_error and "rate limit" in result.content[0].text, result)

    # 5. the log records which path each request came through
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT source, success FROM requests ORDER BY id").fetchall()
    check("db: old rows migrated as mcp", rows[0] == ("mcp", 1), rows)
    check("db: sources logged", [r[0] for r in rows[1:]] == ["api", "api", "mcp", "api", "api", "mcp"], rows)
    stats = httpx.get(f"{base}/api/stats").json()
    check("stats: by_source and recent.source present",
          {s["source"] for s in stats["by_source"]} == {"api", "mcp"} and "source" in stats["recent"][0], stats["by_source"])
finally:
    srv.stop()

finish()
