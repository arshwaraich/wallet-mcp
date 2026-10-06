"""wallet-mcp: an MCP server that signs Apple Wallet (.pkpass) passes on request.

Single process serves four things on one port (proxied by nginx):
  - the MCP endpoint itself (streamable-http, at /mcp)
  - the same pass builder as a plain REST endpoint (POST /api/passes), for callers
    that aren't MCP clients; it shares the rate limits and request log
  - a usage dashboard (/ and /api/stats)
  - short-lived download links for built .pkpass files (/download/{token})
  - Apple's PassKit web service (/passkit/v1/...), through which installed copies of
    updatable passes register and fetch new versions after update_wallet_pass

All passes are signed under the pass.com.arshwaraich.vps identity (see
pass_builder.py / the apple-wallet-pass-toolkit memory) -- this is a free,
shared signing service, not a per-caller cert. Free for now; a payment layer
can be layered on top of the rate limiter later without changing the tool
interface.
"""
import base64
import hashlib
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
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from pydantic import ConfigDict, ValidationError, create_model
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

import anyio

import apns
import db
import image_fetch
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
    locations: list[dict] | None = None,
    max_distance: float | None = None,
    updatable: bool = False,
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

    The link must be opened in Safari on the iPhone: Wallet adds a pass from Safari, but not
    from a .pkpass file handed over by another app. Google Wallet on Android also imports these
    files. Not supported: NFC passes.

    With updatable=True the pass can be changed later with update_wallet_pass, and copies
    already in Wallet refresh themselves. Its contents are then stored on this server.

    Returns {download_url, expires_in_seconds, serial_number, pass_type_identifier}, plus
    edit_token when updatable=True.

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
        locations: up to 10 places where the pass appears on the lock screen when the iPhone is
            nearby, each {"latitude": float, "longitude": float, "altitude": optional float,
            "relevantText": optional str shown on the lock screen, e.g. "Your gym card"}. Look up
            the coordinates of the actual branch, not just the brand. For boardingPass and
            eventTicket the pass is relevant near a location around relevant_date if one is set;
            the other styles use location alone. Wallet only shows a pass within its own default
            radius of a location, which is small for store cards and coupons.
        max_distance: meters; shrinks the default radius around every location (Wallet uses the
            smaller of the two, so it cannot enlarge it). Requires locations.
        voided: mark the pass voided/used (shows a "VOID" stamp).
        primary_fields / secondary_fields / auxiliary_fields / header_fields / back_fields:
            lists of {"key": optional str, "label": optional str, "value": str, "changeMessage":
            optional str} shown on the pass. changeMessage only matters on updatable passes: when
            an update changes that field's value, the iPhone shows it as a lock-screen notification,
            with %@ replaced by the new value (e.g. "You now have %@ points"); without %@ Wallet
            shows a generic message. Updates match fields by key, so give fields that will change
            an explicit key (auto keys are by position).
        serial_number: unique id for this pass; auto-generated if omitted.
        icon_color / icon_text: control the auto-generated icon (a colored square with initials)
            when icon_png_b64 is not supplied.
        logo_color: color of the auto-generated logo image, a wordmark of logo_text (or
            organization_name) drawn on a transparent background over background_color. Defaults
            to icon_color. Wallet also renders logo_text as text beside the logo image, so the
            generated wordmark repeats it -- set generate_logo=False to show only the text.
        icon_png_b64 / logo_png_b64: PNG to use instead of auto-generated art.
        Every *_png_b64 parameter takes either base64 PNG data or an https:// URL of an image
        (PNG, JPEG, WebP or GIF, up to 5 MB; non-PNG is converted to PNG). The URL is fetched once,
        when the pass is built.
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
        Extra images (base64 PNG or https URL). Which image shows where depends on style and iOS version
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
        updatable: keep this pass on the server so update_wallet_pass can change it later. The
            response then includes an edit_token: the only way to update the pass, and it can't be
            recovered, so give it to the user. Wallet registers each iPhone that adds the pass, and an
            update reaches them within seconds. The pass contents are stored (other passes only log
            the style and organization_name). Use for passes that change: loyalty points, gate or seat
            changes, memberships that can be cancelled.

    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    request = ctx.request_context.request
    ip = _client_ip(request) if request is not None else "stdio"
    try:
        return await anyio.to_thread.run_sync(lambda: _create_pass(ip, "mcp", **params))
    except PassRejected as e:
        raise ToolError(e.message) from None


@server.tool(
    title="Update Apple Wallet pass",
    annotations=ToolAnnotations(
        title="Update Apple Wallet pass",
        read_only_hint=False,
        destructive_hint=True,  # replaces the pass on every iPhone that holds it
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def update_wallet_pass(
    ctx: Context,
    serial_number: str,
    edit_token: str,
    style: str | None = None,
    organization_name: str | None = None,
    description: str | None = None,
    logo_text: str | None = None,
    transit_type: str | None = None,
    barcode_message: str | None = None,
    barcode_format: str | list[str] | None = None,
    background_color: str | None = None,
    foreground_color: str | None = None,
    label_color: str | None = None,
    relevant_date: str | None = None,
    expiration_date: str | None = None,
    voided: bool | None = None,
    primary_fields: list[dict] | None = None,
    secondary_fields: list[dict] | None = None,
    auxiliary_fields: list[dict] | None = None,
    header_fields: list[dict] | None = None,
    back_fields: list[dict] | None = None,
    icon_color: str | None = None,
    icon_text: str | None = None,
    logo_color: str | None = None,
    icon_png_b64: str | None = None,
    logo_png_b64: str | None = None,
    generate_logo: bool | None = None,
    poster: bool | None = None,
    footer_fields: list[dict] | None = None,
    background_png_b64: str | None = None,
    featured_actions: list[dict] | None = None,
    barcode_alt_text: str | None = None,
    semantics: dict | None = None,
    semantic_layout: bool | None = None,
    info_links: dict | None = None,
    primary_logo_png_b64: str | None = None,
    secondary_logo_png_b64: str | None = None,
    strip_png_b64: str | None = None,
    thumbnail_png_b64: str | None = None,
    artwork_png_b64: str | None = None,
    locations: list[dict] | None = None,
    max_distance: float | None = None,
) -> dict:
    """Change a pass made with create_wallet_pass(updatable=True); every iPhone that added it refreshes.

    Takes the serial_number and edit_token returned when the pass was created, plus any of
    create_wallet_pass's parameters, with the same meanings. Each parameter given replaces the
    stored value completely (a fields list replaces the whole list, so resend unchanged fields
    too); parameters left out keep their current values. Clear a text value with "" and a list
    with []. To cancel a pass, set voided=True: Wallet marks it void on every iPhone.

    Wallet asks the server for the new version as soon as Apple delivers the push, usually within
    seconds. A field's changeMessage is shown on the lock screen when that field's value changes.

    Returns {download_url, expires_in_seconds, serial_number, pass_type_identifier,
    notified_devices}. notified_devices counts the iPhones the push was sent to, not ones that
    have already refreshed. The download_url (valid 1 hour) is only needed to add the pass to a
    new device.
    """
    changes = {k: v for k, v in locals().items() if k not in ("ctx", "serial_number", "edit_token")}
    request = ctx.request_context.request
    ip = _client_ip(request) if request is not None else "stdio"
    try:
        return await anyio.to_thread.run_sync(lambda: _update_pass(ip, "mcp", serial_number, edit_token, changes))
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
    locations: list[dict] | None = None,
    max_distance: float | None = None,
    updatable: bool = False,
) -> dict:
    """Rate-limit, build, sign and log one pass. `source` is "mcp" or "api", recorded in the log."""
    params = {k: v for k, v in locals().items() if k not in ("ip", "source", "serial_number", "updatable")}
    start = time.monotonic()
    _check_rate_limit(ip, source, "create", style, organization_name)

    serial = serial_number or str(uuid.uuid4())
    try:
        if updatable and db.pass_exists(serial):
            raise pass_builder.PassBuildError(
                f"serial_number {serial!r} is already used by another updatable pass; omit it to get a fresh one"
            )
        params = _resolve_image_urls(params)
        auth_token = secrets.token_urlsafe(24) if updatable else None
        pkpass_bytes = _build_pkpass(params, serial, auth_token)
        result = {
            "download_url": f"{PUBLIC_BASE_URL}/download/{_register_download(pkpass_bytes)}",
            "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
            "serial_number": serial,
            "pass_type_identifier": pass_builder.PASS_TYPE_IDENTIFIER,
        }
        if updatable:
            edit_token = secrets.token_urlsafe(24)
            db.store_pass(serial, auth_token, _hash_token(edit_token), json.dumps(params), pkpass_bytes, int(time.time()))
            result["edit_token"] = edit_token
    except Exception as e:
        _log_failure(e, ip, source, "create", style, organization_name, serial, start)
        raise _rejection(e) from None
    db.log_request(
        ip=ip, style=style, organization_name=organization_name, success=True, error=None,
        duration_ms=int((time.monotonic() - start) * 1000), serial_number=serial, source=source,
    )
    return result


def _update_pass(ip: str, source: str, serial_number: str, edit_token: str, changes: dict) -> dict:
    """Merge `changes` into a stored updatable pass, re-sign it, and push it to installed devices."""
    start = time.monotonic()
    row = db.get_pass(serial_number)
    if row is None or not secrets.compare_digest(row["edit_hash"], _hash_token(edit_token)):
        # One message for both cases, so serials can't be probed without their token.
        raise PassRejected("unknown serial_number, or wrong edit_token for it", 404)
    params = json.loads(row["params"])
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        raise PassRejected("nothing to update: pass at least one field to change", 400)
    style = changes.get("style", params["style"])
    organization_name = changes.get("organization_name", params["organization_name"])
    _check_rate_limit(ip, source, "update", style, organization_name)
    try:
        params.update(_resolve_image_urls(changes))
        pkpass_bytes = _build_pkpass(params, serial_number, row["auth_token"])
        # Wallet compares these as tags, so each version must sort after the last.
        updated = max(int(time.time()), row["updated"] + 1)
        db.replace_pass(serial_number, json.dumps(params), pkpass_bytes, updated)
        notified, dead = apns.push(db.push_tokens(serial_number))
        db.drop_push_tokens(serial_number, dead)
    except Exception as e:
        _log_failure(e, ip, source, "update", style, organization_name, serial_number, start)
        raise _rejection(e) from None
    db.log_request(
        ip=ip, style=style, organization_name=organization_name, success=True, error=None,
        duration_ms=int((time.monotonic() - start) * 1000), serial_number=serial_number,
        source=source, action="update",
    )
    return {
        "download_url": f"{PUBLIC_BASE_URL}/download/{_register_download(pkpass_bytes)}",
        "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
        "serial_number": serial_number,
        "pass_type_identifier": pass_builder.PASS_TYPE_IDENTIFIER,
        "notified_devices": notified,
    }


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_rate_limit(ip: str, source: str, action: str, style: str, organization_name: str) -> None:
    rejection = db.check_rate_limit(ip)
    if rejection:
        db.log_request(
            ip=ip, style=style, organization_name=organization_name, success=False,
            error=rejection, duration_ms=0, serial_number=None, source=source, action=action,
        )
        raise PassRejected(rejection, 429)


def _log_failure(e: Exception, ip, source, action, style, organization_name, serial, start) -> None:
    if isinstance(e, pass_builder.PassBuildError):
        error = str(e)
    else:
        logger.error("unexpected error building pass: %s", traceback.format_exc())
        error = f"internal error: {e}"
    db.log_request(
        ip=ip, style=style, organization_name=organization_name, success=False, error=error,
        duration_ms=int((time.monotonic() - start) * 1000), serial_number=serial, source=source, action=action,
    )


def _rejection(e: Exception) -> Exception:
    return PassRejected(str(e), 400) if isinstance(e, pass_builder.PassBuildError) else e


_IMAGE_PARAMS = [name for name in inspect.signature(create_wallet_pass).parameters if name.endswith("_png_b64")]


def _resolve_image_urls(params: dict) -> dict:
    """Replace any https:// image URL with the fetched image as base64 PNG, so a stored
    updatable pass never needs to re-fetch a URL that may since have changed or gone."""
    out = dict(params)
    for name in _IMAGE_PARAMS:
        value = out.get(name)
        if isinstance(value, str) and value.strip().lower().startswith(("https://", "http://")):
            out[name] = base64.b64encode(image_fetch.fetch_png(value.strip(), name)).decode()
    return out


def _build_pkpass(params: dict, serial: str, auth_token: str | None) -> bytes:
    """Build and sign a pass from create_wallet_pass parameters. auth_token makes it updatable."""
    p = params
    icon_color = p.get("icon_color") or DEFAULT_ICON_COLOR
    style, organization_name = p["style"], p["organization_name"]
    logo_text, background_color = p.get("logo_text"), p.get("background_color")
    poster, semantic_layout = p.get("poster", False), p.get("semantic_layout", False)
    generate_logo = p.get("generate_logo", True)
    pass_dict = pass_builder.build_pass_json(
        **{k: p.get(k) for k in _PASS_JSON_PARAMS if k in p},
        serial_number=serial,
        web_service_url=f"{PUBLIC_BASE_URL}/passkit" if auth_token else None,
        authentication_token=auth_token,
    )

    files: dict[str, bytes] = {}
    if p.get("icon_png_b64"):
        files["icon.png"] = image_gen.decode_b64_png(p["icon_png_b64"], "icon_png_b64")
    else:
        files.update(image_gen.icon_set(icon_color, p.get("icon_text") or organization_name))
    if p.get("logo_png_b64"):
        files["logo.png"] = image_gen.decode_b64_png(p["logo_png_b64"], "logo_png_b64")
    elif generate_logo:
        files.update(image_gen.logo_set(p.get("logo_color") or icon_color, logo_text or organization_name))
    background_png_b64 = p.get("background_png_b64")
    if poster:
        if background_png_b64:
            files["background.png"] = image_gen.decode_b64_png(background_png_b64, "background_png_b64")
        else:
            files.update(image_gen.background_set(background_color or icon_color))
    elif background_png_b64 and style == "eventTicket":
        files["background.png"] = image_gen.decode_b64_png(background_png_b64, "background_png_b64")
    if semantic_layout and style == "eventTicket":
        if p.get("artwork_png_b64"):
            files["artwork.png"] = image_gen.decode_b64_png(p["artwork_png_b64"], "artwork_png_b64")
        elif not background_png_b64:  # Wallet needs one of the two
            files.update(image_gen.artwork_set(background_color or icon_color))
    if p.get("primary_logo_png_b64"):
        files["primaryLogo.png"] = image_gen.decode_b64_png(p["primary_logo_png_b64"], "primary_logo_png_b64")
    elif (poster or semantic_layout) and generate_logo:
        files.update(image_gen.primary_logo_set(p.get("foreground_color") or "#ffffff", logo_text or organization_name))
    for name, param in (("secondaryLogo", "secondary_logo_png_b64"), ("strip", "strip_png_b64"),
                        ("thumbnail", "thumbnail_png_b64")):
        if p.get(param):
            files[f"{name}.png"] = image_gen.decode_b64_png(p[param], param)

    return pass_builder.build_pkpass(pass_dict, files)


_PASS_JSON_PARAMS = [
    name for name in inspect.signature(pass_builder.build_pass_json).parameters
    if name not in ("serial_number", "web_service_url", "authentication_token")
]


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
        return JSONResponse({"error": "invalid request", "details": _validation_details(e)}, status_code=400)
    try:
        ip = _client_ip(request)
        return JSONResponse(await anyio.to_thread.run_sync(lambda: _create_pass(ip, "api", **params)))
    except PassRejected as e:
        return JSONResponse({"error": e.message}, status_code=e.status)
    except Exception:
        return JSONResponse({"error": "internal error building the pass"}, status_code=500)


_UpdateRequest = create_model(
    "UpdateRequest",
    __config__=ConfigDict(extra="forbid"),
    **{
        name: (param.annotation, None)
        for name, param in inspect.signature(update_wallet_pass).parameters.items()
        if name not in ("ctx", "serial_number", "edit_token")
    },
)


@server.custom_route("/api/passes/{serial}", methods=["PATCH"])
async def api_update_pass(request: Request) -> Response:
    """Same as the update_wallet_pass tool; the edit_token goes in "Authorization: Bearer <token>"."""
    scheme, _, edit_token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not edit_token:
        return JSONResponse({"error": "send the pass's edit_token as 'Authorization: Bearer <edit_token>'"},
                            status_code=401)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    try:
        changes = _UpdateRequest.model_validate(body).model_dump()
    except ValidationError as e:
        return JSONResponse({"error": "invalid request", "details": _validation_details(e)}, status_code=400)
    serial, ip = request.path_params["serial"], _client_ip(request)
    try:
        return JSONResponse(await anyio.to_thread.run_sync(
            lambda: _update_pass(ip, "api", serial, edit_token.strip(), changes)))
    except PassRejected as e:
        return JSONResponse({"error": e.message}, status_code=e.status)
    except Exception:
        return JSONResponse({"error": "internal error building the pass"}, status_code=500)


def _validation_details(e: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in err['loc']) or 'body'}: {err['msg']}"
        for err in e.errors(include_url=False)
    ]


# --- PassKit web service (Apple's protocol; Wallet calls these, not callers) ---
# Wallet registers each device that adds an updatable pass, then after a push asks
# which of its passes changed and downloads them. Auth is the pass's own
# authenticationToken, sent as "Authorization: ApplePass <token>".

_DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_PUSH_TOKEN_RE = re.compile(r"^[0-9A-Fa-f]{16,200}$")


def _authorized_pass(request: Request):
    """The stored pass if the path names our pass type and the ApplePass token matches, else None."""
    if request.path_params["pass_type"] != pass_builder.PASS_TYPE_IDENTIFIER:
        return None
    row = db.get_pass(request.path_params["serial"])
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if row is None or scheme != "ApplePass" or not secrets.compare_digest(token.strip(), row["auth_token"]):
        return None
    return row


_REGISTRATION = "/passkit/v1/devices/{device}/registrations/{pass_type}/{serial}"


@server.custom_route(_REGISTRATION, methods=["POST"])
async def passkit_register(request: Request) -> Response:
    if _authorized_pass(request) is None or not _DEVICE_RE.match(request.path_params["device"]):
        return Response(status_code=401)
    try:
        push_token = (await request.json()).get("pushToken", "")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return Response(status_code=400)
    if not isinstance(push_token, str) or not _PUSH_TOKEN_RE.match(push_token):
        return Response(status_code=400)
    created = db.register_device(request.path_params["device"], request.path_params["serial"], push_token)
    if created is None:
        return Response(status_code=503)  # device cap reached for this pass
    return Response(status_code=201 if created else 200)


@server.custom_route(_REGISTRATION, methods=["DELETE"])
async def passkit_unregister(request: Request) -> Response:
    if _authorized_pass(request) is None:
        return Response(status_code=401)
    db.unregister_device(request.path_params["device"], request.path_params["serial"])
    return Response(status_code=200)


@server.custom_route("/passkit/v1/devices/{device}/registrations/{pass_type}", methods=["GET"])
async def passkit_updated_serials(request: Request) -> Response:
    if request.path_params["pass_type"] != pass_builder.PASS_TYPE_IDENTIFIER:
        return Response(status_code=404)
    since = request.query_params.get("passesUpdatedSince", "")
    rows = db.device_serials(request.path_params["device"], int(since) if since.isdigit() else None)
    if not rows:
        return Response(status_code=204)
    return JSONResponse({
        "serialNumbers": [r["serial"] for r in rows],
        "lastUpdated": str(max(r["updated"] for r in rows)),
    })


@server.custom_route("/passkit/v1/passes/{pass_type}/{serial}", methods=["GET"])
async def passkit_latest_pass(request: Request) -> Response:
    row = _authorized_pass(request)
    if row is None:
        return Response(status_code=401)
    last_modified = formatdate(row["updated"], usegmt=True)
    since = request.headers.get("if-modified-since")
    if since:
        try:
            if parsedate_to_datetime(since).timestamp() >= row["updated"]:
                return Response(status_code=304)
        except (TypeError, ValueError):
            pass
    return Response(row["pkpass"], media_type="application/vnd.apple.pkpass",
                    headers={"Last-Modified": last_modified})


@server.custom_route("/passkit/v1/log", methods=["POST"])
async def passkit_log(request: Request) -> Response:
    try:
        logs = (await request.json()).get("logs", [])
        for line in logs[:20]:
            logger.info("Wallet device log: %s", str(line)[:500])
    except Exception:
        pass
    return Response(status_code=200)


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
