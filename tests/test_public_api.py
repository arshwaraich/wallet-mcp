"""Pins the public API to tests/fixtures/public_api.json, so refactors can't change it by accident:
the MCP tools exactly as list_tools() sends them (name, title, description, inputSchema,
outputSchema, annotations), the server instructions, and the JSON schemas of the REST request bodies (POST
/api/passes, PATCH /api/passes/{serial}, a batch design). Real server on a throwaway db, with fixed
env so env-dependent text (free limit, price) is stable.
Run: .venv/bin/python tests/test_public_api.py
After an intended API change: .venv/bin/python tests/test_public_api.py --update, and review the diff."""
import asyncio, difflib, json, subprocess, sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from _harness import PAY_TO, ROOT, Server

FIXTURE = ROOT / "tests" / "fixtures" / "public_api.json"

# Production-like and fixed: payments on (as in production), every other limit at its default.
srv = Server({"WALLET_MCP_X402_PAY_TO": PAY_TO, "WALLET_MCP_X402_FACILITATOR": "http://127.0.0.1:9",
              "WALLET_MCP_X402_PRICE": "$0.01"}, clean_env=True, start=False)
base, env = srv.base, srv.env

REST_MODELS = """
import json, server
print(json.dumps({name: getattr(server, name).model_json_schema()
                  for name in ("_PassRequest", "_UpdateRequest", "_BatchDesign")}))
"""


async def mcp_surface():
    async with streamable_http_client(f"{base}/mcp") as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
    return {
        "instructions": init.instructions,
        # Exactly what clients receive (wire field names), one entry per tool.
        "tools": {t.name: t.model_dump(mode="json", by_alias=True, exclude_none=True)
                  for t in sorted(tools, key=lambda t: t.name)},
    }


srv.start()
try:
    surface = asyncio.run(mcp_surface())
    rest = subprocess.run([sys.executable, "-c", REST_MODELS], env=env, cwd=ROOT, capture_output=True, text=True,
                          check=True)
    surface["rest_request_schemas"] = json.loads(rest.stdout)
finally:
    srv.stop()

actual = json.dumps(surface, indent=2, sort_keys=True) + "\n"
if "--update" in sys.argv:
    FIXTURE.write_text(actual)
    print(f"wrote {FIXTURE}")
    sys.exit(0)
expected = FIXTURE.read_text()
if actual == expected:
    print(f"PASS public API matches {FIXTURE.name} ({len(surface['tools'])} tools, "
          f"{len(surface['rest_request_schemas'])} REST request schemas)")
    print("\nall passed")
    sys.exit(0)
print(f"FAIL public API differs from {FIXTURE.name}:")
sys.stdout.writelines(difflib.unified_diff(expected.splitlines(True), actual.splitlines(True),
                                           "expected", "actual", n=2))
sys.exit(1)
