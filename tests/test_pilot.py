"""End-to-end tests for organizer pilots: palette, CSV cleaning (heuristic + mocked model), the pilot REST API,
attendee pass links, bulk updates with APNs push, limits. Real server on a spare port with a throwaway DB and a
fake APNs and a fake `claude` on PATH, never production.
Run: .venv/bin/python tests/test_pilot.py"""
import base64, csv, io, json, os, sqlite3, socket, stat, subprocess, sys, tempfile, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx2 as httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))
fails = 0
LIVE_TOKEN = "aa" * 32
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
        pushes.append((self.path.rsplit("/", 1)[-1], self.headers.get("apns-topic")))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def pass_json(pkpass: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(pkpass)) as zf:
        return json.loads(zf.read("pass.json"))


def png_b64(color=(30, 60, 160), size=(160, 50)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------- palette (pure) ----------------
import palette, pass_builder

lum = lambda s: pass_builder.parse_color(s)
for bg in ["#00ff00", "#ff00ff", "rgb(255, 255, 0)", "#ffffff", "#000000", "#777777", "#1a2b6d", "#ff3b30", "#0ff"]:
    r = palette.repair(bg)
    c_fg = palette.contrast(lum(r["foreground_color"]), lum(r["background_color"]))
    c_lb = palette.contrast(lum(r["label_color"]), lum(r["background_color"]))
    check(f"palette {bg}: fg contrast >= 4.5 ({c_fg:.1f}) and label >= 3 ({c_lb:.1f})", c_fg >= 4.5 and c_lb >= 3, r)
    check(f"palette {bg}: rgb() output", all(v.startswith("rgb(") for v in r.values()), r)
neon = palette.repair("#00ff00")
check("palette tames neon saturation", neon["background_color"] != "rgb(0, 255, 0)", neon)
kept = palette.repair("#1a2b6d", "#ffffff")
check("palette keeps a passing brand foreground", kept["foreground_color"] == "rgb(255, 255, 255)", kept)
bad = palette.repair("#1a2b6d", "#222266")
check("palette replaces a failing foreground", bad["foreground_color"] != "rgb(34, 34, 102)", bad)
check("palette accepts rgb() input", palette.repair("rgb(10,20,30)")["background_color"] == "rgb(10, 20, 30)")

# ---------------- csv_clean heuristics (no model: claude absent from PATH) ----------------
import csv_clean
csv_clean._ask_model = lambda rows: None  # the real model path is exercised through the server below
read = lambda n: (FIX / n).read_text(encoding="utf-8")
r = csv_clean.clean(read("semicolon_bom_title.csv"))
names = [x["name"] for x in r["rows"]]
check("BOM + ';' + title rows: header found", names == ["Alex Rivera", "Priya Nair", "Jo Lee"], r)
check("... dup, bad email reported", [p["reason"] for p in r["problems"]] ==
      ["duplicate of an earlier row", "invalid email 'not-an-email'"], r["problems"])
check("... blank ';;;' line skipped, types kept", r["rows"][0]["type"] == "VIP" and r["rows"][1]["type"] == "General admission")
check("... generated ticket format", all(len(x["ticket"]) == 9 and x["ticket"][4] == "-" for x in r["rows"]), r["rows"])
check("... no model used", r["used_model"] is False)
r2 = csv_clean.clean(read("semicolon_bom_title.csv"))
check("generated tickets are stable across runs", [x["ticket"] for x in r["rows"]] == [x["ticket"] for x in r2["rows"]])
check("generated tickets unique", len({x["ticket"] for x in r["rows"]}) == len(r["rows"]))
r = csv_clean.clean(read("tab_last_first.csv"))
check("tab-delimited 'Last, First' + ALL CAPS", [x["name"] for x in r["rows"]] ==
      ["Alex Rivera", "Priya Nair", "Sam Okoye", "Jo Lee"], r["rows"])
r = csv_clean.clean(read("eventbrite.csv"))
check("Eventbrite: barcode as ticket, type column",
      [(x["name"], x["ticket"], x["type"]) for x in r["rows"]][:2] ==
      [("Alex Rivera", "98765001", "Early Bird"), ("Priya Nair", "98765002", "General")], r["rows"])
check("Eventbrite: shared email allowed for different tickets", len(r["rows"]) == 4 and not r["problems"], r)
r = csv_clean.clean(read("google_forms.csv"))
check("Google Forms: Full name, case fixed", [x["name"] for x in r["rows"]] == ["Jordan Blake", "Mary O'Neil", "Ken McDonald"], r["rows"])
r = csv_clean.clean(read("typeform_zeffy.csv"))
check("Typeform/Zeffy: first+last, empty-name row is a problem",
      len(r["rows"]) == 3 and r["problems"][0]["reason"] == "missing name" and r["problems"][0]["line"] == 4, r)
r = csv_clean.clean(read("meetup_noheader_words.csv"))
check("Meetup: name only, email empty", [x["name"] for x in r["rows"]] == ["Alex Rivera", "Priya Nair"] and r["rows"][0]["email"] == "", r)
r = csv_clean.clean("name,email,ticket\nA B,a@b.co,T1\nC D,c@d.co,T1\n")
check("duplicate explicit ticket ids are problems", len(r["rows"]) == 1 and "duplicate ticket" in r["problems"][0]["reason"], r)
r = csv_clean.clean("\n\n  \n")
check("empty file: no rows, a problem", r["rows"] == [] and r["problems"], r)
r = csv_clean.clean("Alex Rivera,alex@corp.io\nPriya Nair,priya@corp.io\n")
check("headerless file guessed from content", [x["name"] for x in r["rows"]] == ["Alex Rivera", "Priya Nair"], r)

# ---------------- fake claude + server ----------------
apns_port = free_port()
apns = HTTPServer(("127.0.0.1", apns_port), FakeAPNs)
threading.Thread(target=apns.serve_forever, daemon=True).start()

tmp = Path(tempfile.mkdtemp())
bindir = tmp / "bin"
bindir.mkdir()
claude_log, claude_mode = tmp / "claude.log", tmp / "claude.mode"
claude_mode.write_text("ok")
fake = bindir / "claude"
fake.write_text(f"""#!{sys.executable}
import sys, json
data = sys.stdin.read()
open({str(claude_log)!r}, "a").write(json.dumps({{"args": sys.argv[1:], "stdin": data, "cwd": __import__("os").getcwd()}}) + "\\n")
mode = open({str(claude_mode)!r}).read().strip()
if mode == "fail":
    sys.exit(1)
if mode == "garbage":
    print("sorry, I cannot do that"); sys.exit(0)
print("Here is the plan:\\n" + json.dumps({{"header_index": 2, "columns": {{"name": 0, "first": None, "last": None,
      "email": 1, "ticket": 2, "type": None}}, "name_order": "first_last"}}))
""")
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

port = free_port()
base = f"http://127.0.0.1:{port}"
db_path = tmp / "test.db"
env = dict(os.environ, PORT=str(port), WALLET_MCP_DB=str(db_path), WALLET_MCP_IP_LIMIT="2", WALLET_MCP_MIN_FREE_DISK_GB="1",
           WALLET_MCP_EXEMPT_FILE=str(tmp / "exempt.txt"), WALLET_MCP_DOWNLOADS=str(tmp / "dl"),
           WALLET_MCP_PREVIEWS=str(tmp / "previews"), WALLET_MCP_PUBLIC_URL=base,
           WALLET_MCP_PUBLIC_HOST=f"127.0.0.1:{port}", WALLET_MCP_APNS_URL=f"http://127.0.0.1:{apns_port}",
           PATH=f"{bindir}:{os.environ['PATH']}")
(tmp / "previews").mkdir()
(tmp / "dl").mkdir()

DESIGN = {
    "style": "eventTicket", "organization_name": "Gala Co", "description": "Gala 2026",
    "background_color": "#00ff66", "foreground_color": "#101010", "label_color": "#00ff66",
    "logo_png_b64": png_b64(), "logo_text": "Gala 2026",
    "header_fields": [{"key": "type", "label": "Ticket", "value": "{type}"}],
    "primary_fields": [{"key": "name", "label": "Attendee", "value": "{name}"}],
    "secondary_fields": [{"key": "when", "label": "Doors", "value": "7pm"}],
    "back_fields": [{"key": "tid", "label": "Ticket ID", "value": "{ticket} ({email})"}],
}
design_file = tmp / "design.json"
design_file.write_text(json.dumps(DESIGN))


def cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "pilot.py"), *args], env=env, cwd=ROOT, capture_output=True, text=True)


def parse_cli(out):
    return dict(l.split(":", 1)[0].strip() and (l.split(":", 1)[0].strip(), l.split(":", 1)[1].strip()) for l in out.splitlines() if ":" in l)


end = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 86400))
res = cli("new", "--org", "Gala Co", "--event", "Gala 2026", "--contact", "org@gala.co", "--event-end", end,
          "--design", str(design_file))
check("cli new succeeds", res.returncode == 0, res.stderr[-500:] + res.stdout)
info = parse_cli(res.stdout)
key = info.get("key", "")
pid = key.split(".")[0]
check("cli prints key, organizer url, sample link",
      len(pid) == 8 and info.get("organizer URL", "").endswith(f"/?event={key}") and "/pass/" in info.get("sample link", ""), res.stdout)
small = cli("new", "--org", "Tiny", "--event", "Tiny", "--contact", "t@tiny.co", "--event-end", end,
            "--design", str(design_file), "--max", "3", "--keep-colors")
skey = parse_cli(small.stdout).get("key", "")
spid = skey.split(".")[0]
check("cli new --max/--keep-colors", small.returncode == 0 and spid, small.stderr[-300:])
lst = cli("list")
check("cli list shows both pilots", pid in lst.stdout and spid in lst.stdout, lst.stdout)
shw = cli("show", pid)
check("cli show prints JSON without the key", shw.returncode == 0 and json.loads(shw.stdout)["org"] == "Gala Co" and key.split(".")[1] not in shw.stdout, shw.stdout[:300])
prev = cli("preview", pid, str(tmp / "prev.json"))
pj_prev = json.loads((tmp / "prev.json").read_text()) if prev.returncode == 0 else {}
check("cli preview writes design + sample row", pj_prev.get("sample", {}).get("ticket") == "SAMPLE-0001" and "design" in pj_prev, prev.stderr)
with sqlite3.connect(db_path) as c:
    design_stored = json.loads(c.execute("SELECT design FROM pilots WHERE id=?", (pid,)).fetchone()[0])
    kept_stored = json.loads(c.execute("SELECT design FROM pilots WHERE id=?", (spid,)).fetchone()[0])
    key_hash = c.execute("SELECT key_hash FROM pilots WHERE id=?", (pid,)).fetchone()[0]
check("stored key is a hash, not the token", key.split(".")[1] not in key_hash and len(key_hash) == 64)
check("palette repaired at creation", design_stored["background_color"] != "#00ff66" and design_stored["label_color"].startswith("rgb("), design_stored["background_color"])
check("--keep-colors keeps the brand colours", kept_stored["background_color"] == "#00ff66", kept_stored["background_color"])
check("default barcode placeholder", design_stored["barcode_message"] == "{ticket}")

proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/stats", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    api = f"{base}/api/pilot/{pid}"
    post = lambda path, body, pid_=pid: httpx.post(f"{base}/api/pilot/{pid_}/{path}", json=body, timeout=120)

    # --- auth ---
    for label, resp in [
        ("GET wrong key", httpx.get(api, params={"key": f"{pid}.wrongwrongwrongwrongwrong"})),
        ("GET no key", httpx.get(api)),
        ("GET another pilot's key", httpx.get(api, params={"key": skey})),
        ("GET unknown id", httpx.get(f"{base}/api/pilot/zzzzzzzz", params={"key": key})),
        ("clean wrong key", post("clean", {"key": "nope", "csv": "a"})),
        ("issue wrong key", post("issue", {"key": skey, "rows": [{"name": "A", "ticket": "T"}]})),
        ("update wrong key", post("update", {"key": "x.y", "changes": {"relevant_date": "2026-01-01T00:00:00Z"}})),
        ("links.csv wrong key", httpx.get(f"{api}/links.csv", params={"key": "bad"})),
    ]:
        check(f"auth: {label} -> 403 JSON error", resp.status_code == 403 and "error" in resp.json(), (resp.status_code, resp.text[:100]))
    check("auth: error body shape", httpx.get(api).json() == {"error": "invalid pilot key"})

    r = httpx.get(api, params={"key": key})
    j = r.json()
    check("GET pilot: contract fields", r.status_code == 200 and {"id", "org", "event_name", "event_end", "design", "sample_url",
          "preview_png_url", "issued", "max_passes", "contact_email"} <= set(j), j.keys())
    check("GET pilot: values", j["org"] == "Gala Co" and j["issued"] == 0 and j["max_passes"] == 2000 and j["contact_email"] == "org@gala.co", j)
    sample = httpx.get(j["sample_url"])
    spj = pass_json(sample.content)
    check("sample link serves a pkpass", sample.status_code == 200 and sample.headers["content-type"] == "application/vnd.apple.pkpass", sample.status_code)
    check("sample pass filled with the sample row", spj["eventTicket"]["primaryFields"][0]["value"] == "Alex Rivera"
          and spj["barcodes"][0]["message"] == "SAMPLE-0001", spj)
    check("sample pass uses repaired colours", spj["backgroundColor"] == design_stored["background_color"].replace("#", "") or spj["backgroundColor"].startswith("rgb("), spj["backgroundColor"])
    check("preview 404 before it exists", httpx.get(f"{api}/preview.png").status_code == 404 and j["preview_png_url"] is None)
    (tmp / "previews" / f"{pid}.png").write_bytes(base64.b64decode(png_b64()))
    pr = httpx.get(f"{api}/preview.png")
    check("preview.png served without a key", pr.status_code == 200 and pr.headers["content-type"] == "image/png" and pr.content[:4] == b"\x89PNG")
    check("preview_png_url set once it exists", httpx.get(api, params={"key": key}).json()["preview_png_url"] == f"{api}/preview.png".replace(base, base))

    # --- clean (heuristic, then mocked model) ---
    r = post("clean", {"key": key, "csv": read("tab_last_first.csv")})
    cj = r.json()
    check("clean: contract shape", r.status_code == 200 and set(cj) == {"rows", "problems", "plan", "used_model"}, cj)
    check("clean: heuristic path, no model call", cj["used_model"] is False and not claude_log.exists() and len(cj["rows"]) == 4, cj)
    check("clean: tickets generated", all(len(x["ticket"]) == 9 for x in cj["rows"]))
    r = post("clean", {"key": key, "csv": read("odd_headers_needs_model.csv")})
    cj = r.json()
    check("clean: unsure header calls the model", cj["used_model"] is True and claude_log.exists(), cj)
    check("clean: model's plan applied by code (ticket from col 2)", [x["ticket"] for x in cj["rows"]] == ["AR-1", "PN-2"] and cj["plan"]["columns"]["ticket"] == 2, cj)
    call = json.loads(claude_log.read_text().splitlines()[-1])
    a = call["args"]
    check("model flags", a[:1] == ["-p"] and a[a.index("--model") + 1] == "haiku" and "--no-session-persistence" in a
          and "--strict-mcp-config" in a and a[a.index("--tools") + 1] == "" and a[a.index("--output-format") + 1] == "text", a)
    check("model runs in a temp dir, not the repo", call["cwd"] != str(ROOT) and "wallet" not in call["cwd"], call["cwd"])
    big = "Title row\nHeader x,Mail,Code\n" + "".join(f"Person{i} X,p{i}@x.co,C{i}\n" for i in range(60))
    post("clean", {"key": key, "csv": big})
    sent = json.loads(claude_log.read_text().splitlines()[-1])["stdin"]
    check("model sees only the first ~25 rows", "Person10 X" in sent and "Person40 X" not in sent and sent.count("\n") < 40, sent.count("\n"))
    claude_mode.write_text("fail")
    r = post("clean", {"key": key, "csv": read("odd_headers_needs_model.csv")}).json()
    check("model failure falls back to the heuristic's best guess", r["used_model"] is False and len(r["rows"]) == 2, r)
    claude_mode.write_text("garbage")
    r = post("clean", {"key": key, "csv": read("odd_headers_needs_model.csv")}).json()
    check("unparseable model output falls back too", r["used_model"] is False and len(r["rows"]) == 2, r)
    claude_mode.write_text("ok")
    check("clean: csv must be text", post("clean", {"key": key, "csv": 5}).status_code == 400)
    check("clean: > 2 MB refused", post("clean", {"key": key, "csv": "a" * (2 * 1024 * 1024 + 1)}).status_code == 413)

    # --- issue ---
    rows = [{"name": "Alex Rivera", "email": "alex@corp.io", "ticket": "T-1", "type": "VIP"},
            {"name": "Priya Nair", "email": "", "ticket": "T-2", "type": "General"},
            {"name": "=HYPERLINK(1)", "email": "x@y.co", "ticket": "T-3", "type": "General"}]
    r = post("issue", {"key": key, "rows": rows})
    ij = r.json()
    check("issue: 200 and contract shape", r.status_code == 200 and ij["created"] == 3 and ij["reused"] == 0 and ij["issued_total"] == 3
          and all(p["new"] and p["link"].startswith(f"{base}/pass/") for p in ij["passes"]), ij)
    check("issue: echoes row fields", ij["passes"][0]["name"] == "Alex Rivera" and ij["passes"][0]["ticket"] == "T-1")
    link1 = ij["passes"][0]["link"]
    pk = httpx.get(link1)
    pj = pass_json(pk.content)
    check("/pass link serves a valid pkpass", pk.status_code == 200 and pk.headers["content-type"] == "application/vnd.apple.pkpass"
          and zipfile.is_zipfile(io.BytesIO(pk.content)), pk.status_code)
    check("pass is filled for the attendee", pj["eventTicket"]["primaryFields"][0]["value"] == "Alex Rivera"
          and pj["eventTicket"]["headerFields"][0]["value"] == "VIP" and pj["barcodes"][0]["message"] == "T-1"
          and pj["eventTicket"]["backFields"][0]["value"] == "T-1 (alex@corp.io)", pj)
    check("pass is updatable (web service + auth token)", pj["webServiceURL"] == f"{base}/passkit" and len(pj["authenticationToken"]) >= 16)
    with zipfile.ZipFile(io.BytesIO(pk.content)) as zf:
        check("pkpass has signature + logo", {"signature", "manifest.json", "logo.png", "icon.png"} <= set(zf.namelist()), zf.namelist())
    serial = link1.rsplit("/", 2)[-2]
    with sqlite3.connect(db_path) as c:
        exp = c.execute("SELECT expires FROM passes WHERE serial=?", (serial,)).fetchone()[0]
    want = int(time.time()) + 3 * 86400 + 7 * 86400
    check("expiry = event end + 7 days", abs(exp - want) < 120, exp - want)
    check("/pass wrong auth token is 404", httpx.get(link1.rsplit("/", 1)[0] + "/wrong").status_code == 404)
    check("/pass unknown serial is 404", httpx.get(f"{base}/pass/nope/nope").status_code == 404)

    r = post("issue", {"key": key, "rows": rows + [{"name": "New Person", "email": "n@p.co", "ticket": "T-4", "type": "General"}]})
    rj = r.json()
    check("re-issue is idempotent: same links, new only for the new ticket",
          rj["created"] == 1 and rj["reused"] == 3 and rj["issued_total"] == 4
          and [p["link"] for p in rj["passes"][:3]] == [p["link"] for p in ij["passes"]]
          and [p["new"] for p in rj["passes"]] == [False, False, False, True], rj)
    dup = post("issue", {"key": key, "rows": [rows[0], rows[0]]}).json()
    check("same ticket twice in one request: one link", dup["passes"][0]["link"] == dup["passes"][1]["link"] and dup["issued_total"] == 4, dup)
    for label, bodyrows in [("no rows", []), ("not a list", "x"), ("bad email", [{"name": "A", "email": "nope", "ticket": "Z"}]),
                            ("missing name", [{"name": "", "ticket": "Z"}]), ("missing ticket", [{"name": "A"}]),
                            ("1001 rows", [{"name": "A", "ticket": f"R{i}"} for i in range(1001)])]:
        r = post("issue", {"key": key, "rows": bodyrows})
        check(f"issue: {label} -> 400 JSON error", r.status_code == 400 and "error" in r.json(), (r.status_code, r.text[:100]))
    big_rows = [{"name": f"Guest {i}", "email": f"g{i}@x.co", "ticket": f"B-{i}", "type": "General"} for i in range(1000)]
    t0 = time.time()
    r = post("issue", {"key": key, "rows": big_rows})
    check(f"issue 1000 rows in one request ({time.time() - t0:.0f}s)", r.status_code == 200 and r.json()["created"] == 1000 and r.json()["issued_total"] == 1004, r.text[:200])

    # --- links.csv ---
    r = httpx.get(f"{api}/links.csv", params={"key": key})
    table = list(csv.reader(io.StringIO(r.text)))
    check("links.csv: text/csv, header and all issued rows", r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
          and table[0] == ["name", "email", "ticket", "type", "link"] and len(table) == 1 + 1004, (r.status_code, len(table)))
    byticket = {r_[2]: r_ for r_ in table[1:]}
    check("links.csv: links match issue", byticket["T-1"][4] == link1 and byticket["T-1"][0] == "Alex Rivera")
    check("links.csv: formula cells neutralised", byticket["T-3"][0] == "'=HYPERLINK(1)", byticket["T-3"])

    # --- update + push ---
    pt = "pass.com.arshwaraich.vps"
    auth1 = pj["authenticationToken"]
    reg = httpx.post(f"{base}/passkit/v1/devices/dev1/registrations/{pt}/{serial}", json={"pushToken": LIVE_TOKEN},
                     headers={"Authorization": f"ApplePass {auth1}"})
    serial2 = ij["passes"][1]["link"].rsplit("/", 2)[-2]
    auth2 = pass_json(httpx.get(ij["passes"][1]["link"]).content)["authenticationToken"]
    httpx.post(f"{base}/passkit/v1/devices/dev1/registrations/{pt}/{serial2}", json={"pushToken": LIVE_TOKEN},
               headers={"Authorization": f"ApplePass {auth2}"})
    check("device registered through the existing PassKit service", reg.status_code == 201, reg.status_code)
    before = httpx.get(link1).headers["last-modified"]
    time.sleep(1.1)
    pushes.clear()
    r = post("update", {"key": key, "changes": {"secondary_fields": [{"key": "when", "label": "Doors", "value": "8pm"}],
                                                  "change_message": "Doors now %@"}})
    uj = r.json()
    check("update: contract result", r.status_code == 200 and uj["updated"] == 1004 and uj["notified_devices"] == 1, uj)
    check("update: one push per device, with the pass topic", pushes == [(LIVE_TOKEN, pt)], pushes)
    new = pass_json(httpx.get(link1).content)
    check("update: link now serves the rebuilt pass", new["eventTicket"]["secondaryFields"][0]["value"] == "8pm"
          and new["eventTicket"]["primaryFields"][0]["value"] == "Alex Rivera", new)
    check("update: changeMessage written on changed fields", new["eventTicket"]["secondaryFields"][0].get("changeMessage") == "Doors now %@")
    check("update: pass keeps its serial and token", new["serialNumber"] == serial and new["authenticationToken"] == auth1)
    check("update: Last-Modified moved on", httpx.get(link1).headers["last-modified"] != before)
    d = httpx.get(api, params={"key": key}).json()["design"]
    check("update: merged into stored design (others untouched)", d["secondary_fields"][0]["value"] == "8pm" and d["primary_fields"][0]["value"] == "{name}")
    latest = httpx.get(f"{base}/passkit/v1/passes/{pt}/{serial}", headers={"Authorization": f"ApplePass {auth1}"})
    check("Wallet fetches the new version through /passkit", pass_json(latest.content)["eventTicket"]["secondaryFields"][0]["value"] == "8pm")
    sj = pass_json(httpx.get(httpx.get(api, params={"key": key}).json()["sample_url"]).content)
    check("update: sample pass rebuilt too", sj["eventTicket"]["secondaryFields"][0]["value"] == "8pm")
    for label, ch in [("empty", {}), ("unknown key", {"voided": True}), ("bad field list", {"primary_fields": "x"})]:
        r = post("update", {"key": key, "changes": ch})
        check(f"update: {label} -> 400", r.status_code == 400 and "error" in r.json(), r.text[:100])
    r = post("update", {"key": key, "changes": {"locations": [{"latitude": "x"}]}})
    check("update: a design that fails to build is refused, nothing changed", r.status_code == 400
          and "error" in r.json() and pass_json(httpx.get(link1).content)["eventTicket"]["secondaryFields"][0]["value"] == "8pm", r.text[:200])
    r = post("update", {"key": key, "changes": {"auxiliary_fields": [{"label": "Seat Row", "value": "A"}, {"label": "Seat Row", "value": "B"}, {"key": "when", "label": "Dup", "value": "C"}]}})
    keys = [f["key"] for f in httpx.get(api, params={"key": key}).json()["design"]["auxiliary_fields"]]
    check("update: whole group replaced; missing keys generated from label, unique across the pass",
          r.status_code == 200 and keys[0] == "seat-row" and keys[1] == "seat-row-2" and keys[2] == "when", keys)

    # --- max_passes ---
    sp = lambda path, body: post(path, body, spid)
    three = [{"name": f"S{i}", "ticket": f"S-{i}", "email": ""} for i in range(3)]
    r = sp("issue", {"key": skey, "rows": three})
    check("max_passes: up to the limit is fine", r.status_code == 200 and r.json()["issued_total"] == 3, r.text[:200])
    r = sp("issue", {"key": skey, "rows": [{"name": "Over", "ticket": "S-9", "email": ""}]})
    check("max_passes: over the limit -> 400 naming the cap", r.status_code == 400 and "limited to 3" in r.json()["error"], r.text)
    r = sp("issue", {"key": skey, "rows": three})
    check("max_passes: re-issuing existing tickets still works at the cap", r.status_code == 200 and r.json()["reused"] == 3, r.text[:200])
    check("pilot over the cap did not partially issue", httpx.get(f"{base}/api/pilot/{spid}", params={"key": skey}).json()["issued"] == 3)

    # --- logging + free limits ---
    with sqlite3.connect(db_path) as c:
        n, n_other, contact, client = c.execute(
            "SELECT COUNT(*), SUM(source != 'pilot'), MIN(contact_email), MIN(client) FROM requests WHERE client = ?",
            (f"pilot:{pid}",)).fetchone()
        creates = c.execute("SELECT COUNT(*) FROM requests WHERE client=? AND action='create' AND success=1", (f"pilot:{pid}",)).fetchone()[0]
        updates = c.execute("SELECT COUNT(*) FROM requests WHERE client=? AND action='update' AND success=1", (f"pilot:{pid}",)).fetchone()[0]
    check("each issued pass logged with source pilot, contact and client", n >= 1004 and n_other == 0 and contact == "org@gala.co" and creates == 1004, (n, n_other, contact, creates))
    check("each updated pass logged (action update)", updates == 2 * 1005, updates)
    PASS = {"style": "storeCard", "organization_name": "Bean Club", "description": "card", "contact_email": "org@gala.co",
            "x402_ready": False, "primary_fields": [{"label": "P", "value": "1"}], "barcode_message": "M-1"}
    codes = [httpx.post(f"{base}/api/passes", json=PASS).status_code for _ in range(3)]
    check("2000+ pilot rows don't use the free limit (limit 2: 2 pass, 3rd refused)", codes == [200, 200, 429] or codes == [200, 200, 402], codes)

    # --- cleanup sweep hook ---
    import db as dbm
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM passes WHERE serial = ?", (serial2,))
        before_n = c.execute("SELECT COUNT(*) FROM pilot_passes WHERE pilot_id = ?", (pid,)).fetchone()[0]
    deadline = time.time() + 70
    after_n = before_n
    while time.time() < deadline and after_n == before_n:
        time.sleep(2)
        with sqlite3.connect(db_path) as c:
            after_n = c.execute("SELECT COUNT(*) FROM pilot_passes WHERE pilot_id = ?", (pid,)).fetchone()[0]
    check("sweep purges pilot_passes rows whose pass is gone", after_n == before_n - 1, (before_n, after_n))
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("FAILED" if fails else "ALL PASSED", fails)
sys.exit(1 if fails else 0)
