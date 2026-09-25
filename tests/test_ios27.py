"""Tests for the iOS 27 pass features. Run: .venv/bin/python -m pytest tests/ (or directly)."""
import hashlib, io, json, subprocess, sys, tempfile, zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import image_gen, pass_builder as pb

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


def unzip(pkpass):
    z = zipfile.ZipFile(io.BytesIO(pkpass))
    return {n: z.read(n) for n in z.namelist()}


# --- new barcode formats + QR fallback
for fmt, msg, key in [("EAN13", "4006381333931", "EAN13"), ("ITF", "12345678", "I2of5"),
                      ("Code39", "ABC-123", "Code39"), ("Codabar", "A12345B", "Codabar")]:
    d = pb.build_pass_json(style="storeCard", barcode_message=msg, barcode_format=fmt, **BASE)
    check(f"{fmt}: primary format is PKBarcodeFormat{key}", d["barcodes"][0]["format"] == f"PKBarcodeFormat{key}")
    check(f"{fmt}: QR fallback second", d["barcodes"][1]["format"] == "PKBarcodeFormatQR" and d["barcodes"][1]["message"] == msg)
    check(f"{fmt}: legacy barcode key is QR", d["barcode"]["format"] == "PKBarcodeFormatQR")
raises("EAN13 rejects letters", lambda: pb.build_pass_json(style="generic", barcode_message="ABC", barcode_format="EAN13", **BASE), "12 or 13 digits")
raises("ITF rejects odd length", lambda: pb.build_pass_json(style="generic", barcode_message="123", barcode_format="ITF", **BASE), "even number")
raises("Code39 rejects lowercase", lambda: pb.build_pass_json(style="generic", barcode_message="abc", barcode_format="Code39", **BASE), "uppercase")
d = pb.build_pass_json(style="generic", barcode_message="x", barcode_alt_text="No. 42", **BASE)
check("QR: single barcode, no fallback", len(d["barcodes"]) == 1 and d["barcodes"][0]["altText"] == "No. 42")

# --- featured actions
acts = [{"type": "membershipBenefits", "url": "https://example.com/b"}, {"type": "call", "url": "tel:+15555550100"}]
d = pb.build_pass_json(style="eventTicket", featured_actions=acts, **BASE)
check("featuredActions shape", d["featuredActions"] == [
    {"identifier": "action-1", "type": "membershipBenefits", "url": "https://example.com/b"},
    {"identifier": "action-2", "type": "call", "url": "tel:+15555550100"}])
raises("max 2 actions", lambda: pb.build_pass_json(style="generic", featured_actions=acts + acts[:1], **BASE), "at most 2")
raises("unknown action type", lambda: pb.build_pass_json(style="generic", featured_actions=[{"type": "place", "url": "https://x.y"}], **BASE), "must be one of")
raises("call needs tel:", lambda: pb.build_pass_json(style="generic", featured_actions=[{"type": "call", "url": "https://x.y"}], **BASE), "tel:")
raises("non-http url rejected", lambda: pb.build_pass_json(style="generic", featured_actions=[{"type": "shop", "url": "javascript:alert(1)"}], **BASE), "https://")

# --- poster generic
d = pb.build_pass_json(style="storeCard", poster=True,
                       header_fields=[{"label": "No.", "value": "42"}],
                       primary_fields=[{"label": "Name", "value": "Ada"}],
                       secondary_fields=[{"label": "Tier", "value": "Gold"}],
                       footer_fields=[{"label": "Since", "value": "2026"}],
                       back_fields=[{"label": "Terms", "value": "..."}], **BASE)
check("poster: posterGeneric present", "posterGeneric" in d)
check("poster: storeCard fallback kept with secondary", d["storeCard"]["secondaryFields"][0]["value"] == "Gold")
check("poster: poster has header/primary/footer/back", set(d["posterGeneric"]) == {"headerFields", "primaryFields", "footerFields", "backFields"})
check("poster: no footer on fallback", "footerFields" not in d["storeCard"])
raises("poster rejected for boardingPass", lambda: pb.build_pass_json(style="boardingPass", poster=True, **BASE), "only supported")
raises("footer without poster", lambda: pb.build_pass_json(style="generic", footer_fields=[{"value": "x"}], **BASE), "poster=True")

# --- colors
check("rgb() color accepted", pb._hex_to_rgb_string("rgb(0, 97, 59)") == "rgb(0, 97, 59)")
check("hex color", pb._hex_to_rgb_string("#fff") == "rgb(255, 255, 255)")
check("image_gen accepts rgb() icon color", len(image_gen.icon_set("rgb(0, 97, 59)", "AB")) == 3)
raises("bad color", lambda: pb._hex_to_rgb_string("green"), "invalid")

# --- regression: default calls produce exactly what the pre-change code produced
old_src = subprocess.run(["git", "show", "HEAD:pass_builder.py"], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parent.parent).stdout
old = {}
exec(compile(old_src, "old_pass_builder", "exec"), old)
for style in sorted(pb.STYLE_KEYS):
    kw = dict(style=style, barcode_message="HELLO", barcode_format="PDF417", background_color="#123456",
              primary_fields=[{"label": "a", "value": "b"}], back_fields=[{"value": "c"}], **BASE)
    check(f"regression: {style} unchanged", pb.build_pass_json(**kw) == old["build_pass_json"](**kw))

# --- full signed package
d = pb.build_pass_json(style="generic", poster=True, barcode_message="4006381333931", barcode_format="EAN13",
                       featured_actions=acts[:1], primary_fields=[{"label": "Name", "value": "Ada"}], **BASE)
files = {**image_gen.icon_set("#336699", "TO"), **image_gen.logo_set("#336699", "Test Org"),
         **image_gen.background_set("#336699"), **image_gen.primary_logo_set("#ffffff", "Test Org")}
z = unzip(pb.build_pkpass(d, files))
manifest = json.loads(z["manifest.json"])
check("pkpass: manifest covers every file", set(manifest) == set(z) - {"manifest.json", "signature"})
check("pkpass: manifest hashes match", all(hashlib.sha1(z[n]).hexdigest() == h for n, h in manifest.items()))
with tempfile.TemporaryDirectory() as t:
    Path(t, "sig").write_bytes(z["signature"]); Path(t, "man").write_bytes(z["manifest.json"])
    r = subprocess.run(["openssl", "smime", "-verify", "-binary", "-inform", "DER", "-in", f"{t}/sig",
                        "-content", f"{t}/man", "-noverify", "-out", "/dev/null"], capture_output=True, text=True)
check("pkpass: signature verifies over manifest", r.returncode == 0, r.stderr)
check("pkpass: background@3x is 1035x1515", __import__("PIL.Image").Image.open(io.BytesIO(z["background@3x.png"])).size == (1035, 1515))

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
