"""First pass a day free for everyone, except contact_emails in the exempt file (old 30 -> 20 limit).

Runs db.py directly against a throwaway db and exempt file; no server."""
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(dir="/var/tmp"))
os.environ["WALLET_MCP_DB"] = str(tmp / "t.db")
os.environ["WALLET_MCP_EXEMPT_FILE"] = str(tmp / "exempt.txt")
for k in ("WALLET_MCP_IP_LIMIT", "WALLET_MCP_EXEMPT_IP_LIMIT"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

failures = 0


def check(name, ok, detail=""):
    global failures
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"))
    failures += not ok


db.init()
check("no exempt file -> nobody exempt", not db.is_exempt("payer@x.com"))
(tmp / "exempt.txt").write_text("# comment\nPayer@X.com\n\n")
check("listed email exempt, case-insensitive", db.is_exempt("payer@x.com") and db.is_exempt(" PAYER@x.com "))
check("comment lines ignored", not db.is_exempt("# comment"))
check("other emails not exempt", not db.is_exempt("someone@else.com") and not db.is_exempt(None))
check("default free limit is 1", db.per_ip_daily_limit() == 1)
check("exempt limit is 30 or 20", db.per_ip_daily_limit(True) == (20 if db._today_start() >= "2026-11-18" else 30))

ip = "203.0.113.9"
check("first call free", db.check_rate_limit(ip) is None)
db.log_request(ip=ip, style="generic", organization_name="o", success=True, error=None, duration_ms=1,
               serial_number="s", source="api")
err = db.check_rate_limit(ip)
check("second call refused", err and err.startswith("rate limit exceeded: 1 free pass a day"), err)
check("exempt caller on same IP still free", db.check_rate_limit(ip, exempt=True) is None)
check("other IP still gets its free pass", db.check_rate_limit("203.0.113.10") is None)

# End to end through the REST API: the exemption is keyed on the call's contact_email.
import socket, subprocess, time  # noqa: E402
import httpx  # noqa: E402

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base = f"http://127.0.0.1:{port}"
env = dict(os.environ, PORT=str(port), WALLET_MCP_DB=str(tmp / "e2e.db"), WALLET_MCP_PUBLIC_URL=base,
           WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}", WALLET_MCP_DOWNLOADS=str(tmp / "dl"), WALLET_MCP_X402_PAY_TO="")
root = Path(__file__).resolve().parent.parent
proc = subprocess.Popen([sys.executable, str(root / "server.py")], env=env, cwd=root,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/stats", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    p = {"style": "generic", "organization_name": "T", "description": "t", "x402_ready": False,
         "primary_fields": [{"label": "Name", "value": "Ada"}]}
    r1 = httpx.post(f"{base}/api/passes", json=p | {"contact_email": "a@realco.dev"})
    r2 = httpx.post(f"{base}/api/passes", json=p | {"contact_email": "b@realco.dev"})
    r3 = httpx.post(f"{base}/api/passes", json=p | {"contact_email": "payer@x.com"})
    check("api: first pass free", r1.status_code == 200, r1.text)
    check("api: second pass from the IP refused", r2.status_code in (402, 429), r2.text)
    check("api: exempt email still free", r3.status_code == 200, r3.text)
    d = httpx.get(f"{base}/api/dashboard").json()
    check("dashboard: counts every call", d["all"]["calls"] == 3 and d["all"]["at_limit"] == 1, d["all"])
    check("dashboard: lists every contact", {c["email"] for c in d["contacts"]} == {"a@realco.dev", "b@realco.dev", "payer@x.com"})
    log = httpx.get(f"{base}/api/requests", params={"status": "limit"}).json()
    check("log: status filter", log["matching"] == 1 and log["rows"][0]["contact_email"] == "b@realco.dev", log)
    page = httpx.get(f"{base}/api/requests", params={"limit": 1, "before_id": log["rows"][0]["id"]}).json()
    check("log: paging by before_id", len(page["rows"]) == 1 and page["rows"][0]["id"] < log["rows"][0]["id"], page)
    csv_text = httpx.get(f"{base}/api/requests.csv", params={"q": "payer"}).text
    check("csv: header + filtered rows", csv_text.startswith("id,ts,ip") and csv_text.count("payer@x.com") == 1, csv_text)
finally:
    proc.terminate()

print(f"\n{failures} failure(s)" if failures else "\nALL PASSED")
sys.exit(1 if failures else 0)
