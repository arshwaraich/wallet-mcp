"""Tests for semantic tags, semantic layouts, info links and the extra image slots.
Run: .venv/bin/python tests/test_semantics.py"""
import asyncio, io, json, sys, types, zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pass_builder as pb

BASE = dict(organization_name="Test Org", description="test", serial_number="s1")
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {detail}" if detail and not cond else ""))
    fails += 0 if cond else 1


def raises(name, fn, needle):
    try:
        fn()
        check(name, False, "no error raised")
    except pb.PassBuildError as e:
        check(name, needle in str(e), str(e))


FLIGHT = {
    "airlineCode": "AI", "flightNumber": 864, "departureAirportCode": "BOM", "departureCityName": "Mumbai",
    "departureLocationTimeZone": "Asia/Kolkata", "departureAirportTimeZone": "Asia/Kolkata",
    "destinationAirportCode": "BLR", "destinationCityName": "Bengaluru",
    "destinationLocationTimeZone": "Asia/Kolkata", "destinationAirportTimeZone": "Asia/Kolkata",
    "originalBoardingDate": "2026-10-10T06:15+05:30", "originalDepartureDate": "2026-10-10T06:45+05:30",
    "originalArrivalDate": "2026-10-10T08:30+05:30", "passengerName": {"givenName": "Asha", "familyName": "Rao"},
    "seats": [{"seatNumber": "12A", "seatSectionColor": "#ff0000"}],
}
EVENT = {"eventName": "Coldplay", "venueName": "DY Patil Stadium", "venueRegionName": "Navi Mumbai",
         "venueRoom": "Main Arena", "eventType": "livePerformance", "performerNames": ["Coldplay"],
         "eventStartDate": "2026-11-01T19:30+05:30", "venueLocation": {"latitude": 19.04, "longitude": 73.03}}

# --- semantics pass-through and type checks
d = pb.build_pass_json(style="boardingPass", semantics=FLIGHT, **BASE)
check("semantics emitted", d["semantics"]["flightNumber"] == 864 and d["semantics"]["passengerName"]["givenName"] == "Asha")
check("semantics alone sets no layout scheme", "preferredStyleSchemes" not in d)
check("seat color normalized to rgb()", d["semantics"]["seats"][0]["seatSectionColor"] == "rgb(255, 0, 0)")
check("caller's dict not mutated", FLIGHT["seats"][0]["seatSectionColor"] == "#ff0000")
d = pb.build_pass_json(style="eventTicket", semantics={"eventType": "sports"}, **BASE)
check("eventType shorthand expanded", d["semantics"]["eventType"] == "PKEventTypeSports")
d = pb.build_pass_json(style="eventTicket", semantics={"eventType": "PKEventTypeMovie"}, **BASE)
check("eventType full name kept", d["semantics"]["eventType"] == "PKEventTypeMovie")
d = pb.build_pass_json(style="storeCard", semantics={"balance": {"amount": "250.00", "currencyCode": "INR"}}, **BASE)
check("semantics on any style", d["semantics"]["balance"]["currencyCode"] == "INR")
raises("unknown tag rejected", lambda: pb.build_pass_json(style="generic", semantics={"flightNo": "1"}, **BASE), "unknown semantic tag")
raises("flightNumber must be number", lambda: pb.build_pass_json(style="boardingPass", semantics={"flightNumber": "AI864"}, **BASE), "must be a number")
raises("bool is not a number", lambda: pb.build_pass_json(style="boardingPass", semantics={"duration": True}, **BASE), "must be a number")
raises("date needs offset", lambda: pb.build_pass_json(style="eventTicket", semantics={"eventStartDate": "2026-11-01T19:30"}, **BASE), "time zone offset")
raises("bad date", lambda: pb.build_pass_json(style="eventTicket", semantics={"eventStartDate": "next friday"}, **BASE), "ISO 8601")
check("Z date accepted", pb.build_pass_json(style="eventTicket", semantics={"eventStartDate": "2026-11-01T14:00:00Z"}, **BASE)["semantics"])
raises("passengerName as string rejected", lambda: pb.build_pass_json(style="boardingPass", semantics={"passengerName": "Asha Rao"}, **BASE), "must be an object")
raises("unknown name part", lambda: pb.build_pass_json(style="boardingPass", semantics={"passengerName": {"first": "A"}}, **BASE), "unknown keys")
raises("location needs lat/long", lambda: pb.build_pass_json(style="eventTicket", semantics={"venueLocation": {"latitude": 1}}, **BASE), "missing")
raises("performerNames must be list", lambda: pb.build_pass_json(style="eventTicket", semantics={"performerNames": "X"}, **BASE), "list of strings")
raises("bad eventType", lambda: pb.build_pass_json(style="eventTicket", semantics={"eventType": "party"}, **BASE), "must be one of")
raises("currency amount string", lambda: pb.build_pass_json(style="storeCard", semantics={"totalPrice": {"amount": 5, "currencyCode": "INR"}}, **BASE), "must be strings")

# --- semantic layouts
d = pb.build_pass_json(style="boardingPass", transit_type="Air", semantics=FLIGHT, semantic_layout=True,
                       primary_fields=[{"label": "From", "value": "BOM"}], **BASE)
check("boarding: scheme with fallback", d["preferredStyleSchemes"] == ["semanticBoardingPass", "boardingPass"])
check("boarding: classic fields kept", d["boardingPass"]["primaryFields"][0]["value"] == "BOM")
one_tz = {k: v for k, v in FLIGHT.items() if "AirportTimeZone" not in k}
check("boarding: either time zone spelling satisfies", "preferredStyleSchemes" in pb.build_pass_json(style="boardingPass", semantics=one_tz, semantic_layout=True, **BASE))
no_tz = {k: v for k, v in FLIGHT.items() if "TimeZone" not in k}
raises("boarding: missing time zone named", lambda: pb.build_pass_json(style="boardingPass", semantics=no_tz, semantic_layout=True, **BASE), "departureLocationTimeZone or departureAirportTimeZone")
raises("boarding: missing tags listed", lambda: pb.build_pass_json(style="boardingPass", semantics={"airlineCode": "AI"}, semantic_layout=True, **BASE), "flightNumber")
raises("boarding: train rejected", lambda: pb.build_pass_json(style="boardingPass", transit_type="Train", semantics=FLIGHT, semantic_layout=True, **BASE), "airline passes only")
d = pb.build_pass_json(style="eventTicket", semantics=EVENT, semantic_layout=True, **BASE)
check("event: scheme with fallback", d["preferredStyleSchemes"] == ["posterEventTicket", "eventTicket"])
raises("event: live performance needs performers", lambda: pb.build_pass_json(style="eventTicket", semantics={k: v for k, v in EVENT.items() if k != "performerNames"}, semantic_layout=True, **BASE), "performerNames")
raises("event: sports needs team abbreviations", lambda: pb.build_pass_json(style="eventTicket", semantics={**EVENT, "eventType": "sports"}, semantic_layout=True, **BASE), "awayTeamAbbreviation")
raises("layout on generic rejected", lambda: pb.build_pass_json(style="generic", semantic_layout=True, **BASE), "only for styles")

# --- info links
d = pb.build_pass_json(style="eventTicket", info_links={"bagPolicyURL": "https://x.example/bags", "contactVenuePhoneNumber": "+91 22 1234"}, **BASE)
check("info links top-level", d["bagPolicyURL"] == "https://x.example/bags" and d["contactVenuePhoneNumber"] == "+91 22 1234")
raises("info link must be URL", lambda: pb.build_pass_json(style="eventTicket", info_links={"bagPolicyURL": "x.example"}, **BASE), "http(s) URL")
raises("unknown info link", lambda: pb.build_pass_json(style="eventTicket", info_links={"appLaunchURL": "https://x"}, **BASE), "unknown info_links key")

# --- the tool itself: which image files end up in the package
import server

server.db.check_rate_limit = lambda ip: None
server.db.log_request = lambda **kw: None
captured = {}
server._register_download = lambda data: captured.setdefault("pkpass", data) and "tok"
ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(
    request=None, meta=None, session=types.SimpleNamespace(client_params=None)))
PNG = __import__("base64").b64encode(__import__("image_gen")._icon_png("#000000", "X", 10)).decode()


def files_for(**kw):
    captured.clear()
    asyncio.run(server.create_wallet_pass(ctx, organization_name="Test Org", description="t", **kw))
    z = zipfile.ZipFile(io.BytesIO(captured["pkpass"]))
    return set(z.namelist()), json.loads(z.read("pass.json"))


names, _ = files_for(style="eventTicket", semantics=EVENT, semantic_layout=True)
check("event layout: artwork + primary logo auto-generated", {"artwork@3x.png", "primaryLogo@3x.png"} <= names, str(names))
names, _ = files_for(style="eventTicket", semantics=EVENT, semantic_layout=True, background_png_b64=PNG)
check("event layout: background given -> no auto artwork", "background.png" in names and not any(n.startswith("artwork") for n in names), str(names))
names, _ = files_for(style="eventTicket", semantics=EVENT, semantic_layout=True, artwork_png_b64=PNG,
                     secondary_logo_png_b64=PNG, primary_logo_png_b64=PNG)
check("event layout: supplied images used as-is", {"artwork.png", "secondaryLogo.png", "primaryLogo.png"} <= names
      and "artwork@3x.png" not in names and "primaryLogo@3x.png" not in names, str(names))
names, _ = files_for(style="eventTicket", semantics=EVENT, semantic_layout=True, generate_logo=False)
check("generate_logo=False: no auto logo or primary logo", not any(n.startswith(("logo", "primaryLogo")) for n in names), str(names))
names, _ = files_for(style="coupon", strip_png_b64=PNG, thumbnail_png_b64=PNG)
check("strip + thumbnail included", {"strip.png", "thumbnail.png"} <= names)
check("plain pass: no primary logo", not any(n.startswith("primaryLogo") for n in names))
names, _ = files_for(style="generic", poster=True)
check("poster still gets primary logo", "primaryLogo@3x.png" in names)
names, pj = files_for(style="boardingPass", semantics=FLIGHT, semantic_layout=True)
check("boarding layout through the tool", pj["preferredStyleSchemes"][0] == "semanticBoardingPass" and "primaryLogo.png" in names)

# Caller-supplied images: a malformed one must come back as a ToolError naming the field, not an opaque internal error
from mcp.server.mcpserver.exceptions import ToolError
import base64 as _b64, image_gen as _ig


def tool_raises(name, needle, **kw):
    try:
        files_for(**kw)
        check(name, False, "no error raised")
    except ToolError as e:
        check(name, needle in str(e), str(e))


tool_raises("truncated artwork b64 -> ToolError naming field", "artwork_png_b64 is not valid base64",
            style="eventTicket", semantics=EVENT, semantic_layout=True, artwork_png_b64=PNG[:-3])
tool_raises("non-image strip -> ToolError", "strip_png_b64 does not decode to a readable PNG",
            style="coupon", strip_png_b64=_b64.b64encode(b"not an image").decode())
_jpg = io.BytesIO(); __import__("PIL.Image").Image.new("RGB", (4, 4)).save(_jpg, "JPEG")
tool_raises("JPEG icon -> ToolError", "Wallet needs PNG", style="generic", icon_png_b64=_b64.b64encode(_jpg.getvalue()).decode())
_wrapped = "data:image/png;base64," + "\n".join(PNG[i:i + 20] for i in range(0, len(PNG), 20))
check("data: URI + line-wrapped b64 accepted", _ig.decode_b64_png(_wrapped, "x") == _b64.b64decode(PNG))

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
