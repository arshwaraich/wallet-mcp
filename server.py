"""wallet-mcp: an MCP server that signs Apple Wallet (.pkpass) passes on request.

Single process serves four things on one port (proxied by nginx):
  - the MCP endpoint itself (streamable-http, at /mcp)
  - the same pass builder as a plain REST endpoint (POST /api/passes), for callers
    that aren't MCP clients; it shares the rate limits and request log
  - a usage dashboard (/ and /api/stats)
  - short-lived download links for built .pkpass files (/download/{token})

All passes are signed under the pass.com.arshwaraich.vps identity (see
pass_builder.py / the apple-wallet-pass-toolkit memory) -- this is a free,
shared signing service, not a per-caller cert. Free for now; a payment layer
can be layered on top of the rate limiter later without changing the tool
interface.
"""
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from pathlib import Path

from pydantic import ConfigDict, ValidationError, create_model
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

import db
import image_gen
import pass_builder
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

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
        "Creates signed Apple Wallet (.pkpass) passes -- boarding passes, event tickets, "
        "coupons, store cards and generic passes -- and returns an https download link "
        "valid for 1 hour. No account or API key; free; limited to 30 passes per day per "
        "caller IP and 500 per day in total. Passes are signed with this service's own "
        "certificate (pass.com.arshwaraich.vps), so Wallet shows them as added by this "
        "service, not by an airline or venue. A pass's barcode only works where the "
        "original one did if barcode_message is that original barcode's data."
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


@server.tool(
    title="Create Apple Wallet pass",
    annotations=ToolAnnotations(
        title="Create Apple Wallet pass",
        read_only_hint=False,
        destructive_hint=False,  # only ever creates a new pass file; never changes or deletes anything
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
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
    semantics: dict | None = None,
    semantic_layout: bool = False,
    info_links: dict | None = None,
    primary_logo_png_b64: str | None = None,
    secondary_logo_png_b64: str | None = None,
    strip_png_b64: str | None = None,
    thumbnail_png_b64: str | None = None,
    artwork_png_b64: str | None = None,
) -> dict:
    """Create a signed Apple Wallet pass (.pkpass) and return a link to download it.

    Builds one pass in one of the five Wallet styles -- boardingPass, eventTicket, coupon,
    storeCard or generic -- from the text fields, colors, barcode and optional PNG images
    given below, signs it with this service's Apple Pass Type ID certificate, and returns an
    https download_url valid for 1 hour. Opening the link on an iPhone shows "Add to
    Apple Wallet". Passes install on any iOS version; the iOS 26/27 layouts fall back to the
    classic style on older iPhones.

    Barcodes: QR, PDF417, Aztec and Code128 on every iOS version; Code39, Codabar, EAN13 and
    ITF on iOS 27+. The barcode encodes exactly barcode_message. Gate and till scanners read
    that data, so a copy of an existing boarding pass, ticket or loyalty card only scans if
    barcode_message is the original barcode's content (airline boarding passes are usually
    PDF417 or Aztec, holding an IATA BCBP string such as "M1DOE/JANE ..."). This tool does not issue
    tickets, check anyone in, or contact airlines or venues.

    Not supported: NFC passes, updating a pass after it is issued, Google Wallet.

    Returns {download_url, expires_in_seconds, serial_number, pass_type_identifier}.

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
        generate_logo: set False to include no auto-generated logo or primary logo at all (both are
            optional in Wallet; a supplied logo_png_b64/primary_logo_png_b64 is still used).
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
        semantics: machine-readable tags (Apple's SemanticTags) that Wallet uses for Siri/Maps/
            Calendar suggestions on every style, and to lay out the semantic designs below. Values
            are emitted as given; unknown keys and wrong types are rejected. Dates are ISO 8601
            with an offset. Common shapes: passengerName {"givenName", "familyName"}; flightNumber
            is a number (123, not "AI123"); seats [{"seatNumber", "seatRow", "seatSection", ...}];
            venueLocation {"latitude", "longitude"}; totalPrice {"amount": "12.50",
            "currencyCode": "INR"}; eventType one of generic, livePerformance, movie, sports,
            conference, convention, workshop, socialGathering.
        semantic_layout: use Wallet's semantic design, with the classic style kept for older
            iPhones -- which only show primary/secondary/auxiliary fields, so still fill those.
            - boardingPass (iOS 26+, airline only, transit_type "Air"): live flight status, gate
              changes, badges. Requires semantics airlineCode, flightNumber, departureAirportCode,
              departureCityName, destinationAirportCode, destinationCityName, originalBoardingDate,
              originalDepartureDate, originalArrivalDate, passengerName, plus departure and
              destination time zones (IANA, e.g. "Asia/Kolkata"). Apple's docs call these
              departureLocationTimeZone/destinationLocationTimeZone but Apple's pass-builder code
              uses departureAirportTimeZone/destinationAirportTimeZone; either is accepted, and
              giving both is safest.
            - eventTicket (iOS 26+ "poster event ticket"): requires eventName, venueName,
              venueRegionName, venueRoom; sports also needs awayTeamAbbreviation and
              homeTeamAbbreviation, livePerformance needs performerNames. Apple says this design
              is meant for NFC entry, not barcodes, and NFC passes need an Apple entitlement this
              server doesn't have -- Wallet may show the classic event ticket instead.
        info_links: top-level links that fill the event guide (event tickets) or the airline and
            services page (boarding passes). URL keys (http/https): accessibilityURL, addOnURL,
            bagPolicyURL, merchandiseURL, orderFoodURL, parkingInformationURL, purchaseParkingURL,
            sellURL, transferURL, transitInformationURL, contactVenueWebsite,
            directionsInformationURL, changeSeatURL, entertainmentURL,
            purchaseAdditionalBaggageURL, purchaseLoungeAccessURL, purchaseWifiURL, upgradeURL,
            managementURL, registerServiceAnimalURL, reportLostBagURL, requestWheelchairURL,
            transitProviderWebsiteURL. Text keys: contactVenueEmail, contactVenuePhoneNumber,
            transitProviderEmail, transitProviderPhoneNumber.
        Extra images, each a base64 PNG. Which image shows where depends on style and iOS version
        (from Apple's Pass Designer docs); Wallet ignores an image a layout has no slot for:
            primary_logo_png_b64: max 126x30pt. Boarding pass and coupon on iOS 27+, event ticket
                on iOS 18+, poster. On those, the plain logo is only shown by older iOS. A wordmark
                is auto-generated for poster and semantic_layout passes unless generate_logo=False.
            secondary_logo_png_b64: max 135x12pt. Event tickets, iOS 18+.
            strip_png_b64: 375x144pt banner behind the primary fields. Coupon, store card and
                event ticket; not shown on iOS 26+ coupon/store card.
            thumbnail_png_b64: 90x90pt. Generic and event ticket.
            artwork_png_b64: 358x448pt poster event ticket artwork (eventTicket with
                semantic_layout, iOS 27+); if neither this nor background_png_b64 is given, a
                gradient in background_color is generated, since Wallet needs one of them.
            background_png_b64 is also used on eventTicket (blurred behind the classic ticket).

    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    request = ctx.request_context.request
    ip = _client_ip(request) if request is not None else "stdio"
    try:
        return _create_pass(ip, "mcp", **params)
    except PassRejected as e:
        raise ToolError(e.message) from None


class PassRejected(Exception):
    """A caller-facing failure (rate limit or invalid input), shared by the MCP tool and REST API."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _create_pass(
    ip: str,
    source: str,
    *,
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
    semantics: dict | None = None,
    semantic_layout: bool = False,
    info_links: dict | None = None,
    primary_logo_png_b64: str | None = None,
    secondary_logo_png_b64: str | None = None,
    strip_png_b64: str | None = None,
    thumbnail_png_b64: str | None = None,
    artwork_png_b64: str | None = None,
) -> dict:
    """Rate-limit, build, sign and log one pass. `source` is "mcp" or "api", recorded in the log."""
    start = time.monotonic()
    rejection = db.check_rate_limit(ip)
    if rejection:
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=False,
            error=rejection, duration_ms=0, serial_number=None, source=source,
        )
        raise PassRejected(rejection, 429)

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
            semantics=semantics,
            semantic_layout=semantic_layout,
            info_links=info_links,
        )

        files: dict[str, bytes] = {}
        if icon_png_b64:
            files["icon.png"] = image_gen.decode_b64_png(icon_png_b64, "icon_png_b64")
        else:
            files.update(image_gen.icon_set(icon_color, icon_text or organization_name))
        if logo_png_b64:
            files["logo.png"] = image_gen.decode_b64_png(logo_png_b64, "logo_png_b64")
        elif generate_logo:
            files.update(image_gen.logo_set(logo_color or icon_color, logo_text or organization_name))
        if poster:
            if background_png_b64:
                files["background.png"] = image_gen.decode_b64_png(background_png_b64, "background_png_b64")
            else:
                files.update(image_gen.background_set(background_color or icon_color))
        elif background_png_b64 and style == "eventTicket":
            files["background.png"] = image_gen.decode_b64_png(background_png_b64, "background_png_b64")
        if semantic_layout and style == "eventTicket":
            if artwork_png_b64:
                files["artwork.png"] = image_gen.decode_b64_png(artwork_png_b64, "artwork_png_b64")
            elif not background_png_b64:  # Wallet needs one of the two
                files.update(image_gen.artwork_set(background_color or icon_color))
        if primary_logo_png_b64:
            files["primaryLogo.png"] = image_gen.decode_b64_png(primary_logo_png_b64, "primary_logo_png_b64")
        elif (poster or semantic_layout) and generate_logo:
            files.update(image_gen.primary_logo_set(foreground_color or "#ffffff", logo_text or organization_name))
        for name, param, b64 in (("secondaryLogo", "secondary_logo_png_b64", secondary_logo_png_b64),
                                 ("strip", "strip_png_b64", strip_png_b64),
                                 ("thumbnail", "thumbnail_png_b64", thumbnail_png_b64)):
            if b64:
                files[f"{name}.png"] = image_gen.decode_b64_png(b64, param)

        pkpass_bytes = pass_builder.build_pkpass(pass_dict, files)
        token = _register_download(pkpass_bytes)

        duration_ms = int((time.monotonic() - start) * 1000)
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=True,
            error=None, duration_ms=duration_ms, serial_number=serial, source=source,
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
            error=str(e), duration_ms=duration_ms, serial_number=serial, source=source,
        )
        raise PassRejected(str(e), 400) from None
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error("unexpected error building pass: %s", traceback.format_exc())
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=False,
            error=f"internal error: {e}", duration_ms=duration_ms, serial_number=serial, source=source,
        )
        raise


@server.custom_route("/", methods=["GET"])
async def dashboard(request: Request) -> Response:
    return Response(DASHBOARD_HTML, media_type="text/html")


@server.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> Response:
    return JSONResponse(db.stats())


# The REST body accepts exactly the tool's parameters, validated with the same
# types, so the API and the MCP tool can't drift apart.
_PassRequest = create_model(
    "PassRequest",
    __config__=ConfigDict(extra="forbid"),
    **{
        name: (param.annotation, ... if param.default is inspect.Parameter.empty else param.default)
        for name, param in inspect.signature(_create_pass).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    },
)


@server.custom_route("/api/passes", methods=["POST"])
async def api_create_pass(request: Request) -> Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    try:
        params = _PassRequest.model_validate(body).model_dump()
    except ValidationError as e:
        details = [
            f"{'.'.join(str(part) for part in err['loc']) or 'body'}: {err['msg']}"
            for err in e.errors(include_url=False)
        ]
        return JSONResponse({"error": "invalid request", "details": details}, status_code=400)
    try:
        return JSONResponse(_create_pass(_client_ip(request), "api", **params))
    except PassRejected as e:
        return JSONResponse({"error": e.message}, status_code=e.status)
    except Exception:
        return JSONResponse({"error": "internal error building the pass"}, status_code=500)


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
