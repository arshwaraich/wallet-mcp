"""wallet-mcp: an MCP server that signs Apple Wallet (.pkpass) passes on request.

Single process serves three things on one port (proxied by nginx at /wallet-mcp/):
  - the MCP endpoint itself (streamable-http, at /mcp)
  - a usage dashboard (/ and /api/stats)
  - short-lived download links for built .pkpass files (/download/{token})

All passes are signed under the pass.com.arshwaraich.vps identity (see
pass_builder.py / the apple-wallet-pass-toolkit memory) -- this is a free,
shared signing service, not a per-caller cert. Free for now; a payment layer
can be layered on top of the rate limiter later without changing the tool
interface.
"""
import logging
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

import db
import image_gen
import pass_builder
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wallet-mcp")

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
DASHBOARD_HTML = (BASE_DIR / "dashboard.html").read_text()

DOWNLOAD_TTL_SECONDS = 60 * 60  # 1 hour, matches fileshare.py's convention
DEFAULT_ICON_COLOR = "#c98500"  # site accent color

db.init()

server = MCPServer(
    "wallet-mcp",
    instructions=(
        "Signs Apple Wallet (.pkpass) passes and returns a temporary https download "
        "link. Free, shared signing identity (pass.com.arshwaraich.vps) -- passes are "
        "cosmetic/utility only, not airline- or venue-issued official passes. Rate "
        "limited per caller; expect rejection if you exceed it."
    ),
)

# Expiry is tracked by file mtime, not in-memory state, so a service restart
# (crash + Restart=on-failure, or a reboot) can't orphan undeletable files.
def _register_download(data: bytes) -> str:
    token = secrets.token_urlsafe(16)
    path = DOWNLOAD_DIR / f"{token}.pkpass"
    path.write_bytes(data)
    return token


def _sweep_downloads() -> None:
    while True:
        now = time.time()
        for path in DOWNLOAD_DIR.glob("*.pkpass"):
            try:
                if now - path.stat().st_mtime > DOWNLOAD_TTL_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        time.sleep(30)


threading.Thread(target=_sweep_downloads, daemon=True).start()


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


PUBLIC_BASE_URL = os.environ.get("WALLET_MCP_PUBLIC_URL", "https://vps.arshwaraich.com/wallet-mcp")


@server.tool()
async def create_wallet_pass(
    ctx: Context,
    style: str,
    organization_name: str,
    description: str,
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
    serial_number: str | None = None,
    icon_color: str = DEFAULT_ICON_COLOR,
    icon_text: str | None = None,
    logo_color: str | None = None,
    icon_png_b64: str | None = None,
    logo_png_b64: str | None = None,
    generate_logo: bool = True,
    poster: bool = False,
    footer_fields: list[dict] | None = None,
    background_png_b64: str | None = None,
    featured_actions: list[dict] | None = None,
    barcode_alt_text: str | None = None,
) -> dict:
    """Build and sign an Apple Wallet pass, returning a temporary download link.

    Args:
        style: one of "boardingPass", "eventTicket", "coupon", "generic", "storeCard".
        organization_name: shown as the pass issuer.
        description: accessibility description (required by Wallet, not usually shown).
        logo_text: text shown next to the logo image.
        transit_type: only for style="boardingPass" -- "Air", "Boat", "Bus", "Generic", or "Train" (default "Air").
        barcode_message: raw text/data encoded into the barcode. Omit for no barcode.
        barcode_format: a format name or an ordered list of them; each becomes a barcode with the
            same message, and Wallet shows the first one the device supports. Nothing is added that
            you don't list. Formats: "QR" (default), "PDF417", "Aztec", "Code128" (all iOS versions),
            and iOS 27+ only: "Code39", "Codabar", "EAN13", "ITF" -- older iPhones skip these, so
            e.g. ["EAN13", "Code128"] shows EAN13 on iOS 27 and Code128 before it. EAN13 needs
            12-13 digits, ITF an even number of digits, Code39 uppercase letters/digits.
        barcode_alt_text: human-readable text shown under the barcode (e.g. the card number).
        background_color / foreground_color / label_color: hex colors, e.g. "#1a1a19".
        relevant_date / expiration_date: ISO 8601 timestamps.
        voided: mark the pass voided/used (shows a "VOID" stamp).
        primary_fields / secondary_fields / auxiliary_fields / header_fields / back_fields:
            lists of {"key": optional str, "label": optional str, "value": str} shown on the pass.
        serial_number: unique id for this pass; auto-generated if omitted.
        icon_color / icon_text: control the auto-generated icon (a colored square with initials)
            when icon_png_b64 is not supplied.
        logo_color: color of the auto-generated logo image, a wordmark of logo_text (or
            organization_name) drawn on a transparent background over background_color. Defaults
            to icon_color. Wallet also renders logo_text as text beside the logo image, so the
            generated wordmark repeats it -- set generate_logo=False to show only the text.
        icon_png_b64 / logo_png_b64: base64-encoded PNG to use instead of auto-generated art.
        generate_logo: set False to include no logo image at all (logo is optional in Wallet;
            ignored if logo_png_b64 is given).
        poster: (iOS 27+) render as Apple's new full-bleed "Poster Generic" layout -- large background
            image, header field, up to 4 primary fields, up to 2 footer_fields, and a square QR code.
            Only for style "generic", "storeCard" or "coupon"; that style is kept as the fallback
            older iPhones display (secondary/auxiliary fields only show on the fallback). Good for
            memberships, loyalty cards and gift cards.
        footer_fields: up to 2 fields along the bottom of a poster pass (requires poster=True).
        background_png_b64: base64 PNG poster background, ideally 1035x1515 px (345x505pt @3x);
            a gradient in background_color is generated if omitted. Only used when poster=True.
        featured_actions: (iOS 27+) up to 2 tappable shortcut cards shown under the pass, each
            {"type": ..., "url": "https://..."}. type is one of: viewSchedule, watchTrailer,
            listenToMusic, call (url must be "tel:+..."), addToBalance, order, shop,
            membershipBenefits, bookAppointment, bookCar, bookFlight, bookStay, viewOffersRewards.
            Works on every style; ignored by older iPhones.

    Returns a dict with download_url (valid for 1 hour), serial_number, and pass_type_identifier.
    """
    request = ctx.request_context.request
    ip = _client_ip(request) if request is not None else "stdio"

    start = time.monotonic()
    rejection = db.check_rate_limit(ip)
    if rejection:
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=False,
            error=rejection, duration_ms=0, serial_number=None,
        )
        raise ToolError(rejection)

    serial = serial_number or str(uuid.uuid4())
    try:
        pass_dict = pass_builder.build_pass_json(
            style=style,
            organization_name=organization_name,
            description=description,
            serial_number=serial,
            logo_text=logo_text,
            transit_type=transit_type,
            barcode_message=barcode_message,
            barcode_format=barcode_format,
            background_color=background_color,
            foreground_color=foreground_color,
            label_color=label_color,
            relevant_date=relevant_date,
            expiration_date=expiration_date,
            voided=voided,
            primary_fields=primary_fields,
            secondary_fields=secondary_fields,
            auxiliary_fields=auxiliary_fields,
            header_fields=header_fields,
            back_fields=back_fields,
            footer_fields=footer_fields,
            poster=poster,
            featured_actions=featured_actions,
            barcode_alt_text=barcode_alt_text,
        )

        files: dict[str, bytes] = {}
        if icon_png_b64:
            files["icon.png"] = image_gen.decode_b64_png(icon_png_b64)
        else:
            files.update(image_gen.icon_set(icon_color, icon_text or organization_name))
        if logo_png_b64:
            files["logo.png"] = image_gen.decode_b64_png(logo_png_b64)
        elif generate_logo:
            files.update(image_gen.logo_set(logo_color or icon_color, logo_text or organization_name))
        if poster:
            if background_png_b64:
                files["background.png"] = image_gen.decode_b64_png(background_png_b64)
            else:
                files.update(image_gen.background_set(background_color or icon_color))
            files.update(image_gen.primary_logo_set(foreground_color or "#ffffff", logo_text or organization_name))

        pkpass_bytes = pass_builder.build_pkpass(pass_dict, files)
        token = _register_download(pkpass_bytes)

        duration_ms = int((time.monotonic() - start) * 1000)
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=True,
            error=None, duration_ms=duration_ms, serial_number=serial,
        )
        return {
            "download_url": f"{PUBLIC_BASE_URL}/download/{token}",
            "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
            "serial_number": serial,
            "pass_type_identifier": pass_builder.PASS_TYPE_IDENTIFIER,
        }
    except pass_builder.PassBuildError as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=False,
            error=str(e), duration_ms=duration_ms, serial_number=serial,
        )
        raise ToolError(str(e)) from None
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error("unexpected error building pass: %s", traceback.format_exc())
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=False,
            error=f"internal error: {e}", duration_ms=duration_ms, serial_number=serial,
        )
        raise


@server.custom_route("/", methods=["GET"])
async def dashboard(request: Request) -> Response:
    return Response(DASHBOARD_HTML, media_type="text/html")


@server.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> Response:
    return JSONResponse(db.stats())


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@server.custom_route("/download/{token}", methods=["GET"])
async def download(request: Request) -> Response:
    token = request.path_params["token"]
    if not _TOKEN_RE.match(token):
        return Response("expired or unknown download link", status_code=404)
    path = DOWNLOAD_DIR / f"{token}.pkpass"
    if not path.exists() or time.time() - path.stat().st_mtime > DOWNLOAD_TTL_SECONDS:
        return Response("expired or unknown download link", status_code=404)
    return FileResponse(
        path,
        media_type="application/vnd.apple.pkpass",
        filename="pass.pkpass",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8400"))
    public_host = os.environ.get("WALLET_MCP_PUBLIC_HOST", "vps.arshwaraich.com")
    transport_security = TransportSecuritySettings(
        allowed_hosts=[public_host, f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[f"https://{public_host}", f"http://127.0.0.1:{port}"],
    )
    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        transport_security=transport_security,
    )
