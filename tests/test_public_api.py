"""Pins the public API to tests/fixtures/public_api.json, so refactors can't change it by accident:
the MCP tools exactly as list_tools() sends them (name, title, description, inputSchema,
outputSchema, annotations), the server instructions, and the JSON schemas of the REST request bodies (POST
/api/passes, PATCH /api/passes/{serial}, a batch design). Real server on a throwaway db, with fixed
env so env-dependent text (free limit, price) is stable.
Run: .venv/bin/python tests/test_public_api.py
After an intended API change: .venv/bin/python tests/test_public_api.py --update, and review the diff."""
import asyncio, atexit, difflib, json, os, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path

import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "public_api.json"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


port = free_port()
base = f"http://127.0.0.1:{port}"
tmp = Path(tempfile.mkdtemp())
dl = Path("/var/tmp") / f"wallet-mcp-test-public-api-{tmp.name}"  # /tmp is a small tmpfs that trips the disk guard
atexit.register(shutil.rmtree, dl, True)
atexit.register(shutil.rmtree, tmp, True)
# Production-like and fixed: payments on (as in production), every other limit at its default.
env = {k: v for k, v in os.environ.items() if not k.startswith("WALLET_MCP_")}
env.update(PORT=str(port), WALLET_MCP_DB=str(tmp / "test.db"), WALLET_MCP_DOWNLOADS=str(dl),
           WALLET_MCP_PREVIEWS=str(tmp / "previews"), WALLET_MCP_EXEMPT_FILE=str(tmp / "exempt.txt"),
           WALLET_MCP_PUBLIC_URL=base, WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}",
           WALLET_MCP_X402_PAY_TO="0x000000000000000000000000000000000000dEaD",
           WALLET_MCP_X402_FACILITATOR="http://127.0.0.1:9", WALLET_MCP_X402_PRICE="$0.01",
           WALLET_MCP_APNS_URL="http://127.0.0.1:9")

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


proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=open(tmp / "server.log", "w"))
try:
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/stats", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    surface = asyncio.run(mcp_surface())
    rest = subprocess.run([sys.executable, "-c", REST_MODELS], env=env, cwd=ROOT, capture_output=True, text=True,
                          check=True)
    surface["rest_request_schemas"] = json.loads(rest.stdout)
finally:
    proc.terminate()
    proc.wait(timeout=10)

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
