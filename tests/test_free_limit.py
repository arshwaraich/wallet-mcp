"""10 free passes a day for everyone, except contact_emails in the exempt file (old 30 -> 20 limit).

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
from _harness import Server, check, finish  # noqa: E402


db.init()
check("no exempt file -> nobody exempt", not db.is_exempt("payer@x.com"))
(tmp / "exempt.txt").write_text("# comment\nPayer@X.com\n\n")
check("listed email exempt, case-insensitive", db.is_exempt("payer@x.com") and db.is_exempt(" PAYER@x.com "))
check("comment lines ignored", not db.is_exempt("# comment"))
(tmp / "exempt.txt").write_text("# comment\nPayer@X.com\nold@x.com until=2000-01-01\nsoon@x.com until=2999-01-01\n")
check("until= in the past ends the exemption", not db.is_exempt("old@x.com"))
check("until= in the future keeps it", db.is_exempt("soon@x.com"))
check("until= is the first non-exempt day (UTC)", not db.is_exempt("t@x.com") and
      [e["active"] for e in db.exemptions()] == [True, False, True])
check("other emails not exempt", not db.is_exempt("someone@else.com") and not db.is_exempt(None))
check("default free limit is 10", db.per_ip_daily_limit() == 10)
check("exempt limit is 30 or 20", db.per_ip_daily_limit(True) == (20 if db._today_start() >= "2026-11-18" else 30))

ip = "203.0.113.9"
for _ in range(10):
    ok = db.check_rate_limit(ip) is None
    db.log_request(ip=ip, style="generic", organization_name="o", success=True, error=None, duration_ms=1,
                   serial_number="s", source="api")
check("first 10 calls free", ok)
err = db.check_rate_limit(ip)
check("11th call refused", err and err.startswith("rate limit exceeded: 10 free passes a day"), err)
check("exempt caller on same IP still free", db.check_rate_limit(ip, exempt=True) is None)
check("other IP still gets its free pass", db.check_rate_limit("203.0.113.10") is None)

def log(ip, email, success=True, refused=False):
    db.log_request(ip=ip, style="generic", organization_name="o", success=success, error=None if success else "x",
                   duration_ms=1, serial_number="s", source="api", contact_email=email, refused=refused)

for _ in range(10):
    log("198.51.100.1", "hopper@x.com")
check("same email, new IP refused", db.check_rate_limit("198.51.100.2", contact_email="Hopper@X.com") is not None)
check("new email, used IP refused", db.check_rate_limit("198.51.100.1", contact_email="other@x.com") is not None)
log("198.51.100.3", "failer@x.com", success=False)
log("198.51.100.3", "failer@x.com", success=False, refused=True)
check("failed calls and refusals don't use the free pass",
      db.check_rate_limit("198.51.100.3", contact_email="failer@x.com") is None)
for _ in range(db.FAIL_DAILY_LIMIT - 1):
    log("198.51.100.4", "flaky@x.com", success=False)
check("under the fail cap", db.check_fail_limit("198.51.100.4", "flaky@x.com") is None)
log("198.51.100.4", "flaky@x.com", success=False)
check("fail cap reached by IP", (db.check_fail_limit("198.51.100.4", "new@x.com") or "").startswith("too many failed"))
check("fail cap reached by email", db.check_fail_limit("198.51.100.5", "Flaky@x.com") is not None)
check("refusals don't count toward the fail cap", db.check_fail_limit("198.51.100.3", "failer@x.com") is None)
for _ in range(10):
    log("160.79.106.161", "first@x.com")
check("shared egress IP: another email still free", db.check_rate_limit("160.79.106.161", contact_email="second@x.com") is None)
check("shared egress IP: same email refused on any of the pool", db.check_rate_limit("160.79.106.23", contact_email="first@x.com") is not None)

# End to end through the REST API: the exemption is keyed on the call's contact_email.
import httpx2 as httpx  # noqa: E402

srv = Server({"WALLET_MCP_IP_LIMIT": "1", "WALLET_MCP_EXEMPT_FILE": str(tmp / "exempt.txt"),
              "WALLET_MCP_X402_PAY_TO": ""}, db_path=tmp / "e2e.db")
base = srv.base
try:
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
    srv.stop()

finish()
