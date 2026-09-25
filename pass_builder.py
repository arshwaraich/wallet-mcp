"""Builds and signs .pkpass files under the pass.com.arshwaraich.vps Pass Type ID.

Reuses the certificate/key issued in the manual apple-wallet toolkit session
(see the apple-wallet-pass-toolkit memory) rather than re-deriving signing
from scratch. Only pass.json content changes per request; the signing
identity is fixed.
"""
import hashlib
import io
import re
import string
import subprocess
import tempfile
import zipfile
from pathlib import Path

SIGNING_DIR = Path.home() / "projects/apple-wallet/wallet_signing"
PASSKEY = SIGNING_DIR / "passkey.pem"
CERT = SIGNING_DIR / "pass.pem"
WWDR = SIGNING_DIR / "AppleWWDRCAG4.cer"

PASS_TYPE_IDENTIFIER = "pass.com.arshwaraich.vps"
TEAM_IDENTIFIER = "6U9WK83U3W"

BARCODE_FORMATS = {
    "QR": "PKBarcodeFormatQR",
    "PDF417": "PKBarcodeFormatPDF417",
    "Aztec": "PKBarcodeFormatAztec",
    "Code128": "PKBarcodeFormatCode128",
    # iOS 27+ only. Older Wallet skips these and shows the next listed format it
    # supports, if any -- the caller decides whether to list one.
    "Code39": "PKBarcodeFormatCode39",
    "Codabar": "PKBarcodeFormatCodabar",
    "EAN13": "PKBarcodeFormatEAN13",
    "ITF": "PKBarcodeFormatI2of5",  # Apple's key is I2of5, not ITF (see apple/pass-builder)
}

_BARCODE_MESSAGE_RULES = {
    "Code39": (re.compile(r"^[0-9A-Z \-.$/+%]+$"), "digits, uppercase A-Z, space and - . $ / + %"),
    "Codabar": (re.compile(r"^[A-Da-d]?[0-9\-$:/.+]+[A-Da-d]?$"), "digits and - $ : / . +, optionally wrapped in A-D start/stop characters"),
    "EAN13": (re.compile(r"^\d{12,13}$"), "12 or 13 digits"),
    "ITF": (re.compile(r"^(\d\d)+$"), "an even number of digits"),
}

# iOS 27 Featured Actions -- up to two tappable cards shown under the pass.
# "place" is deliberately omitted: it needs an Apple Maps place ID rather than
# a URL, and Apple's own pass-builder doesn't model that key yet.
FEATURED_ACTION_TYPES = {
    "viewSchedule", "watchTrailer", "listenToMusic", "call", "addToBalance",
    "order", "shop", "membershipBenefits", "bookAppointment", "bookCar",
    "bookFlight", "bookStay", "viewOffersRewards",
}
MAX_FEATURED_ACTIONS = 2

# Styles that can carry an iOS 27 posterGeneric layout alongside them as the
# pre-iOS 27 fallback (Apple's docs list exactly these).
POSTER_FALLBACK_STYLES = {"generic", "storeCard", "coupon"}

TRANSIT_TYPES = {
    "Air": "PKTransitTypeAir",
    "Boat": "PKTransitTypeBoat",
    "Bus": "PKTransitTypeBus",
    "Generic": "PKTransitTypeGeneric",
    "Train": "PKTransitTypeTrain",
}

STYLE_KEYS = {"boardingPass", "eventTicket", "coupon", "generic", "storeCard"}


class PassBuildError(ValueError):
    pass


_RGB_FUNC_RE = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+\s*)?\)$"
)


def parse_color(color: str) -> tuple[int, int, int]:
    """Parse a color into an (r, g, b) tuple.

    Accepts hex (documented input) and also CSS rgb()/rgba() strings, since
    MCP clients don't always follow the "hex color" instruction literally
    (see id=14 in wallet-mcp usage log: an rgb() string tripped this up).
    """
    color = color.strip()
    m = _RGB_FUNC_RE.match(color)
    if m:
        r, g, b = (int(v) for v in m.groups())
    else:
        h = color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6 or any(c not in string.hexdigits for c in h):
            raise PassBuildError(f"invalid hex color: {color!r}")
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    if not all(0 <= v <= 255 for v in (r, g, b)):
        raise PassBuildError(f"invalid color value out of range: {color!r}")
    return r, g, b


def _hex_to_rgb_string(color: str) -> str:
    """Normalize a color to Apple's pass.json 'rgb(r, g, b)' format."""
    r, g, b = parse_color(color)
    return f"rgb({r}, {g}, {b})"


def sign_manifest(manifest_bytes: bytes) -> bytes:
    for path, label in ((PASSKEY, "signing key"), (CERT, "signing cert"), (WWDR, "WWDR intermediate")):
        if not path.exists():
            raise PassBuildError(f"missing {label} at {path}")
    result = subprocess.run(
        [
            "openssl", "smime", "-binary", "-sign",
            "-signer", str(CERT),
            "-inkey", str(PASSKEY),
            "-certfile", str(WWDR),
            "-outform", "DER",
        ],
        input=manifest_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PassBuildError(f"openssl signing failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def build_pkpass(pass_dict: dict, files: dict[str, bytes]) -> bytes:
    """files: filename -> raw bytes for every asset (icons/logos/strip)."""
    import json

    manifest_bytes = json.dumps(pass_dict, separators=(",", ":")).encode("utf-8")
    manifest = {"pass.json": hashlib.sha1(manifest_bytes).hexdigest()}
    for name, data in files.items():
        manifest[name] = hashlib.sha1(data).hexdigest()
    manifest_json = json.dumps(manifest, separators=(",", ":")).encode("utf-8")

    signature = sign_manifest(manifest_json)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pass.json", manifest_bytes)
        zf.writestr("manifest.json", manifest_json)
        zf.writestr("signature", signature)
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def make_field(key: str | None, label: str | None, value, idx: int) -> dict:
    d = {"key": key or f"field{idx}", "value": value}
    if label:
        d["label"] = label
    return d


def build_pass_json(
    *,
    style: str,
    organization_name: str,
    description: str,
    serial_number: str,
    logo_text: str | None = None,
    transit_type: str | None = None,
    barcode_message: str | None = None,
    barcode_format: str | list[str] = "QR",
    background_color: str | None = None,
    foreground_color: str | None = None,
    label_color: str | None = None,
    relevant_date: str | None = None,
    expiration_date: str | None = None,
    voided: bool = False,
    primary_fields: list[dict] | None = None,
    secondary_fields: list[dict] | None = None,
    auxiliary_fields: list[dict] | None = None,
    header_fields: list[dict] | None = None,
    back_fields: list[dict] | None = None,
    footer_fields: list[dict] | None = None,
    poster: bool = False,
    featured_actions: list[dict] | None = None,
    barcode_alt_text: str | None = None,
) -> dict:
    if style not in STYLE_KEYS:
        raise PassBuildError(f"unknown style {style!r}, must be one of {sorted(STYLE_KEYS)}")

    style_body: dict = {}
    if style == "boardingPass":
        tt = transit_type or "Air"
        if tt not in TRANSIT_TYPES:
            raise PassBuildError(f"unknown transit_type {tt!r}, must be one of {sorted(TRANSIT_TYPES)}")
        style_body["transitType"] = TRANSIT_TYPES[tt]

    # Wallet requires field keys to be unique across a style's sections. Auto keys
    # are prefixed per section, and colliding caller keys get a numeric suffix.
    # posterGeneric and its fallback style are separate scopes (Apple's own example
    # repeats keys across them), so each gets its own seen-set.
    def keyed_fields(seen: set[str]):
        def fields(raw: list[dict] | None, section: str) -> list[dict]:
            out = []
            for i, f in enumerate(raw or []):
                field = make_field(f.get("key") or f"{section}{i}", f.get("label"), f.get("value"), i)
                base, n = field["key"], 2
                while field["key"] in seen:
                    field["key"] = f"{base}_{n}"
                    n += 1
                seen.add(field["key"])
                out.append(field)
            return out
        return fields

    fields = keyed_fields(set())

    if primary_fields:
        style_body["primaryFields"] = fields(primary_fields, "primary")
    if secondary_fields:
        style_body["secondaryFields"] = fields(secondary_fields, "secondary")
    if auxiliary_fields:
        style_body["auxiliaryFields"] = fields(auxiliary_fields, "auxiliary")
    if header_fields:
        style_body["headerFields"] = fields(header_fields, "header")
    if back_fields:
        style_body["backFields"] = fields(back_fields, "back")

    poster_body: dict | None = None
    if poster:
        if style not in POSTER_FALLBACK_STYLES:
            raise PassBuildError(
                f"poster=True is only supported for styles {sorted(POSTER_FALLBACK_STYLES)}, not {style!r}"
            )
        # Poster layout: 1 header, up to 4 primary, 2 footer, back fields. Secondary/
        # auxiliary fields have no slot there, so they only appear on the fallback style.
        poster_body = {}
        poster_fields = keyed_fields(set())
        if header_fields:
            poster_body["headerFields"] = poster_fields(header_fields, "header")
        if primary_fields:
            poster_body["primaryFields"] = poster_fields(primary_fields, "primary")
        if footer_fields:
            poster_body["footerFields"] = poster_fields(footer_fields, "footer")
        if back_fields:
            poster_body["backFields"] = poster_fields(back_fields, "back")
    elif footer_fields:
        raise PassBuildError("footer_fields are only shown on poster passes; set poster=True or use auxiliary_fields")

    pass_dict: dict = {
        "formatVersion": 1,
        "passTypeIdentifier": PASS_TYPE_IDENTIFIER,
        "teamIdentifier": TEAM_IDENTIFIER,
        "serialNumber": serial_number,
        "organizationName": organization_name,
        "description": description,
        style: style_body,
    }
    if poster_body is not None:
        # Wallet on iOS 27+ prefers posterGeneric when present; older versions
        # ignore the unknown key and render the fallback style above.
        pass_dict["posterGeneric"] = poster_body
    if logo_text:
        pass_dict["logoText"] = logo_text
    if background_color:
        pass_dict["backgroundColor"] = _hex_to_rgb_string(background_color)
    if foreground_color:
        pass_dict["foregroundColor"] = _hex_to_rgb_string(foreground_color)
    if label_color:
        pass_dict["labelColor"] = _hex_to_rgb_string(label_color)
    if relevant_date:
        pass_dict["relevantDate"] = relevant_date
    if expiration_date:
        pass_dict["expirationDate"] = expiration_date
    if voided:
        pass_dict["voided"] = True
    if barcode_message:
        # Emitted exactly as given, in order; Wallet shows the first format it supports.
        formats = [barcode_format] if isinstance(barcode_format, str) else list(barcode_format)
        if not formats:
            raise PassBuildError("barcode_format must name at least one format")
        barcodes = []
        for fmt in formats:
            if fmt not in BARCODE_FORMATS:
                raise PassBuildError(f"unknown barcode_format {fmt!r}, must be one of {sorted(BARCODE_FORMATS)}")
            rule = _BARCODE_MESSAGE_RULES.get(fmt)
            if rule and not rule[0].match(barcode_message):
                raise PassBuildError(f"barcode_message for {fmt} must be {rule[1]}, got {barcode_message!r}")
            b = {"message": barcode_message, "format": BARCODE_FORMATS[fmt], "messageEncoding": "iso-8859-1"}
            if barcode_alt_text:
                b["altText"] = barcode_alt_text
            barcodes.append(b)
        # Only the iOS 9+ "barcodes" array; the deprecated single "barcode" key is left out.
        pass_dict["barcodes"] = barcodes

    if featured_actions:
        if len(featured_actions) > MAX_FEATURED_ACTIONS:
            raise PassBuildError(f"at most {MAX_FEATURED_ACTIONS} featured_actions allowed, got {len(featured_actions)}")
        actions = []
        for i, a in enumerate(featured_actions):
            a_type, url = a.get("type"), a.get("url")
            if a_type not in FEATURED_ACTION_TYPES:
                raise PassBuildError(
                    f"featured_actions[{i}].type {a_type!r} must be one of {sorted(FEATURED_ACTION_TYPES)}"
                )
            if not url or not re.match(r"^(https?://\S+|tel:\S+)$", url):
                raise PassBuildError(f"featured_actions[{i}].url must be an https:// or tel: URL, got {url!r}")
            if a_type == "call" and not url.startswith("tel:"):
                raise PassBuildError(f"featured_actions[{i}] of type 'call' needs a tel: URL, got {url!r}")
            actions.append({"identifier": f"action-{i + 1}", "type": a_type, "url": url})
        pass_dict["featuredActions"] = actions

    return pass_dict
