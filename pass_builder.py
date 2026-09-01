"""Builds and signs .pkpass files under the pass.com.arshwaraich.vps Pass Type ID.

Reuses the certificate/key issued in the manual apple-wallet toolkit session
(see the apple-wallet-pass-toolkit memory) rather than re-deriving signing
from scratch. Only pass.json content changes per request; the signing
identity is fixed.
"""
import hashlib
import io
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
}

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


def _hex_to_rgb_string(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise PassBuildError(f"invalid hex color: {hex_color!r}")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
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
    barcode_format: str = "QR",
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
) -> dict:
    if style not in STYLE_KEYS:
        raise PassBuildError(f"unknown style {style!r}, must be one of {sorted(STYLE_KEYS)}")

    style_body: dict = {}
    if style == "boardingPass":
        tt = transit_type or "Air"
        if tt not in TRANSIT_TYPES:
            raise PassBuildError(f"unknown transit_type {tt!r}, must be one of {sorted(TRANSIT_TYPES)}")
        style_body["transitType"] = TRANSIT_TYPES[tt]

    def fields(raw: list[dict] | None) -> list[dict]:
        return [make_field(f.get("key"), f.get("label"), f.get("value"), i) for i, f in enumerate(raw or [])]

    if primary_fields:
        style_body["primaryFields"] = fields(primary_fields)
    if secondary_fields:
        style_body["secondaryFields"] = fields(secondary_fields)
    if auxiliary_fields:
        style_body["auxiliaryFields"] = fields(auxiliary_fields)
    if header_fields:
        style_body["headerFields"] = fields(header_fields)
    if back_fields:
        style_body["backFields"] = fields(back_fields)

    pass_dict: dict = {
        "formatVersion": 1,
        "passTypeIdentifier": PASS_TYPE_IDENTIFIER,
        "teamIdentifier": TEAM_IDENTIFIER,
        "serialNumber": serial_number,
        "organizationName": organization_name,
        "description": description,
        style: style_body,
    }
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
        if barcode_format not in BARCODE_FORMATS:
            raise PassBuildError(f"unknown barcode_format {barcode_format!r}, must be one of {sorted(BARCODE_FORMATS)}")
        barcode = {
            "message": barcode_message,
            "format": BARCODE_FORMATS[barcode_format],
            "messageEncoding": "iso-8859-1",
        }
        pass_dict["barcodes"] = [barcode]
        pass_dict["barcode"] = barcode  # legacy single-barcode key, older Wallet versions read this

    return pass_dict
