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
shared signing service, not a per-caller cert. Free up to the per-IP daily
limit; past it, a caller can pay per call with x402 (payments.py) instead of
being refused, without any change to the tool interface.
"""
import base64
import contextlib
import csv
import datetime
import hashlib
import inspect
import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, replace
from typing import Literal
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from pydantic import ConfigDict, ValidationError, create_model
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

import anyio

import apns
import csv_clean
import db
import image_fetch
import image_gen
import pass_builder
import payments
import pilot
import site_stats
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wallet-mcp")

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = Path(os.environ.get("WALLET_MCP_DOWNLOADS", BASE_DIR / "downloads"))  # tests use a temp dir
DOWNLOAD_DIR.mkdir(exist_ok=True)
DASHBOARD_HTML = (BASE_DIR / "dashboard.html").read_text()

DOWNLOAD_TTL_SECONDS = 60 * 60  # 1 hour, matches fileshare.py's convention
# Capacity guards, so no caller (paying or not) can take the box down. A pass is ~16 KB without
# images; each image URL can be 25 megapixels, so builds are memory-heavy and run at most 4 at once
# (the rest queue). systemd's MemoryMax/CPUQuota are the backstop.
MAX_PKPASS_BYTES = int(os.environ.get("WALLET_MCP_MAX_PASS_KB", 10 * 1024)) * 1024
MIN_FREE_DISK_BYTES = int(os.environ.get("WALLET_MCP_MIN_FREE_DISK_GB", 5)) * 1024 ** 3
BUILD_LIMITER = anyio.CapacityLimiter(4)
MAX_UPDATABLE_HOURS = 30 * 24  # updatable passes are stored for 1 hour by default, at most 30 days
DEFAULT_ICON_COLOR = "#c98500"  # site accent color

db.init()
pilot.init()

server = MCPServer(
    "wallet-mcp",
    instructions=(
        "Creates signed Apple Wallet (.pkpass) passes -- boarding passes, event tickets, "
        "coupons, store cards and generic passes -- and returns an https download link "
        "valid for 1 hour. No account or API key, but every call must send the user's contact_email and "
        "x402_ready. Pricing: a call is one create or one update. "
        f"The first {db.PER_IP_DAILY_LIMIT} successful calls each day per caller (IP address or contact_email) are free (while a shared pool of 500 free "
        "calls a day lasts); every call after them is paid. Failed calls don't count (up to 20 a day), and counts reset at 00:00 UTC"
        + (
            f". Each paid call costs {payments.PRICE} in USDC on Base, paid with x402 (the "
            "refusal carries the payment requirements; retry the same call with the signed payment in the "
            'x402_payment argument, or in _meta["x402/payment"]). A payment is settled only after the pass is built, so a failed '
            "build is never charged. Setup guide: https://walletmcppass.com/#wallet-setup. Without x402, wait for the reset"
            if payments.enabled else ""
        )
        + (
            ". For many passes from one design (event tickets for a guest list), use create_wallet_pass_batch: "
            f"permanent per-attendee links, paid from the first pass ({payments.PRICE} each, one x402 payment per call)"
            if payments.enabled else ""
        )
        + ". Passes are signed with this service's own "
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


def _unregister_download(token: str) -> None:
    (DOWNLOAD_DIR / f"{token}.pkpass").unlink(missing_ok=True)


def _sweep_downloads() -> None:
    while True:
        try:
            purged = db.purge_expired_passes()
            if purged:
                logger.info("deleted %d updatable pass(es) whose update window ended", purged)
            pilot.purge_orphans()
            db.purge_stale_stages()
        except Exception:
            logger.error("purging expired passes failed: %s", traceback.format_exc())
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


# x402 payment carriers: the MCP transport's _meta key, and the HTTP headers of x402 v1 and v2.
X402_META_KEY = "x402/payment"
X402_HEADERS = ("x-payment", "payment-signature")
X402_GUIDE_URL = "https://walletmcppass.com/#wallet-setup"
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PLACEHOLDER_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "yourcompany.com"}  # the last is our docs
PLACEHOLDER_EMAIL_TLDS = (".example", ".invalid", ".test", ".localhost")
CALLER_INFO_WHY = ("contact_email lets the operator of this service reach the user about it, and x402_ready shows "
                   "how many clients could pay after their free passes. Both are only logged, and neither changes "
                   "the pass, the price or the limits.")
# Added to a free successful result when the caller said x402_ready=false and has at most
# HINT_WHEN_LEFT free passes left today (sent every time, agents ignored it: 1 click in ~650).
# Information only: the pass, price and limits are the same either way.
HINT_WHEN_LEFT = 3
X402_SETUP_HINT = {
    "message": ("{left} Tell your user: AI agents can now pay for things on their own. Setting up a Coinbase Agentic "
                "Wallet for this agent once (sign in with an email, add a few dollars) lets it pay any app or "
                f"service that accepts agent payments, not just this one. Here, the first {db.PER_IP_DAILY_LIMIT} passes each day "
                f"are free and every pass after them is only a cent ({payments.PRICE}), so $1 covers 100 passes. Without a "
                "wallet, calls past the free ones are refused until 00:00 UTC. "
                "(Developers: this is x402; MCP clients can use @x402/mcp, other code the x402 SDKs.)"),
    "guide": X402_GUIDE_URL,
    "technical_steps": "https://walletmcppass.com/llms-full.txt",
}


@dataclass(frozen=True)
class Caller:
    """Who made a request, as recorded in the log. x402 notes whether the caller sent a payment;
    it's only used (and charged) once the caller is past the free per-IP limit."""
    ip: str
    source: str  # "mcp" or "api"
    client: str | None = None  # MCP clientInfo "name/version", else the User-Agent
    x402: bool = False
    payment: object = field(default=None, repr=False)  # the raw x402 payment, if one was sent
    contact_email: str | None = None  # from the caller
    x402_ready: bool | None = None  # from the caller

    @classmethod
    def from_mcp(cls, ctx: Context) -> "Caller":
        rctx = ctx.request_context
        request = rctx.request
        params = rctx.session.client_params
        if params is not None:
            client = f"{params.client_info.name}/{params.client_info.version}"
        else:
            client = request.headers.get("user-agent") if request is not None else None
        payment = (rctx.meta or {}).get(X402_META_KEY)
        return cls(
            ip=_client_ip(request) if request is not None else "stdio",
            source="mcp",
            client=client,
            x402=X402_META_KEY in (rctx.meta or {}),
            payment=payment,
        )

    @classmethod
    def from_api(cls, request: Request) -> "Caller":
        return cls(
            ip=_client_ip(request),
            source="api",
            client=request.headers.get("user-agent"),
            x402=any(h in request.headers for h in X402_HEADERS),
            payment=next((request.headers[h] for h in X402_HEADERS if h in request.headers), None),
        )


    def with_payment_argument(self, payment) -> "Caller":
        """The x402_payment tool argument / JSON body field. It exists because chat apps (Claude.ai,
        ChatGPT) let the model fill in arguments but not _meta or headers, so a payment signed by a
        wallet connector can only reach us this way. A _meta/header payment wins if both are sent."""
        if payment is None or self.payment is not None:
            return self
        return replace(self, payment=payment, x402=True)


def _log(caller: Caller, **fields) -> None:
    fields.setdefault("payment_tx", None)
    db.log_request(ip=caller.ip, source=caller.source, client=caller.client, x402=caller.x402,
                   contact_email=caller.contact_email, x402_ready=caller.x402_ready, **fields)


PUBLIC_BASE_URL = os.environ.get("WALLET_MCP_PUBLIC_URL", "https://vps.arshw.com/wallet-mcp")


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
    updatable_hours: int = 1,
    contact_email: str | None = None,
    x402_ready: bool | Literal["unknown"] | None = None,
    x402_payment: dict | str | None = None,
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
    already in Wallet refresh themselves. Its contents are then stored on this server for
    updatable_hours (1 hour by default, at most 30 days), then deleted.

    Returns {download_url, expires_in_seconds, serial_number, pass_type_identifier}, plus
    edit_token and updatable_until when updatable=True. With x402_ready=False and 3 or fewer
    free passes left today, also free_passes_left_today and x402_setup (how to set up x402
    payments): pass that on to the user.

    Pricing: a call is one create or one update. The first 10 successful calls each day per caller (IP address
    or contact_email) are free (while a shared pool of 500 free calls a day lasts); failed calls don't
    count (up to 20 failures a day per caller), and counts reset at 00:00 UTC.
    Every call after them costs $0.01 in USDC on Base, paid with x402: the refusal carries the
    payment requirements; have the user's wallet tool sign them and retry the same call with the
    result as x402_payment. A paid result includes the payment receipt. Payment is settled only
    after the pass is built, so a failed build is never charged.

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
        updatable_hours: how long an updatable pass can be updated, 1 (default) to 720 (30 days),
            counted from creation; updates don't extend it. Afterwards the stored pass is deleted and
            can't be changed again, while copies in Wallet stay as they last were. Pick it to cover
            the pass's useful life, e.g. until a flight lands or an event ends. Only with updatable=True.
        contact_email: required. The user's address, so the operator of this service can reach them
            about it, for example if it changes or breaks for them. Ask the user for it; never take it
            from the pass details or make one up. It isn't added to the pass, and it's kept in the
            request log.
        x402_ready: required. True if this client can pay with x402, False if it can't, "unknown" if
            you don't know. It shows the operator how many clients could pay after their free passes.
            Only logged: it changes neither price nor limits.
        x402_payment: only after a refusal that asked for payment. The signed x402 payment for the
            requirements in that refusal (the PaymentPayload JSON object, or its base64), e.g. from
            a wallet connector's sign/pay tool. Same as sending it in _meta["x402/payment"].

    """
    params = {k: v for k, v in locals().items() if k != "ctx"}
    caller = Caller.from_mcp(ctx)
    try:
        return _mcp_result(await anyio.to_thread.run_sync(lambda: _create_pass(caller, params), limiter=BUILD_LIMITER))
    except PassRejected as e:
        return _mcp_rejection(e)


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
    contact_email: str | None = None,
    x402_ready: bool | Literal["unknown"] | None = None,
    x402_payment: dict | str | None = None,
) -> dict:
    """Change a pass made with create_wallet_pass(updatable=True); every iPhone that added it refreshes.

    Takes the serial_number and edit_token returned when the pass was created, the required
    contact_email and x402_ready, plus any of create_wallet_pass's parameters (x402_payment included), with the same meanings. Each parameter given replaces the
    stored value completely (a fields list replaces the whole list, so resend unchanged fields
    too); parameters left out keep their current values. Clear a text value with "" and a list
    with []. To cancel a pass, set voided=True: Wallet marks it void on every iPhone.

    Wallet asks the server for the new version as soon as Apple delivers the push, usually within
    seconds. A field's changeMessage is shown on the lock screen when that field's value changes.

    Only works until the pass's updatable_until time (set by updatable_hours at creation).

    Returns {download_url, expires_in_seconds, serial_number, pass_type_identifier,
    notified_devices, updatable_until}. notified_devices counts the iPhones the push was sent to, not ones that
    have already refreshed. The download_url (valid 1 hour) is only needed to add the pass to a
    new device. With x402_ready=False the result also has x402_setup, as for create_wallet_pass.
    """
    changes = {k: v for k, v in locals().items() if k not in ("ctx", "serial_number", "edit_token")}
    caller = Caller.from_mcp(ctx)
    try:
        return _mcp_result(await anyio.to_thread.run_sync(
            lambda: _update_pass(caller, serial_number, edit_token, changes), limiter=BUILD_LIMITER))
    except PassRejected as e:
        return _mcp_rejection(e)


def _mcp_result(result: dict) -> dict | CallToolResult:
    """A paid call also returns the x402 receipt in _meta, where x402 MCP clients look for it."""
    if "payment" not in result:
        return result
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2))],
        structured_content=result,
        meta={payments.MCP_RESPONSE_META_KEY: result["payment"]},
    )


def _mcp_rejection(e: "PassRejected") -> CallToolResult:
    """x402's MCP transport: a tool error whose structured content is the PaymentRequired body."""
    if e.payment_required is None:
        raise ToolError(e.message) from None
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(e.payment_required))],
        structured_content=e.payment_required,
        is_error=True,
    )


class PassRejected(Exception):
    """A caller-facing failure (rate limit or invalid input), shared by the MCP tool and REST API."""

    def __init__(self, message: str, status: int, payment_required: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payment_required = payment_required  # x402 PaymentRequired body, for status 402


# create_wallet_pass parameters that are about the call (or the pass's identity and storage), not its design.
_CALL_PARAMS = ("serial_number", "updatable", "updatable_hours", "contact_email", "x402_ready", "x402_payment")


def _create_pass(caller: Caller, request: dict) -> dict:
    """Rate-limit, build, sign and log one pass. request holds every create_wallet_pass parameter
    (defaults filled in), as the tool gets them or as _PassRequest validates them."""
    style, organization_name = request["style"], request["organization_name"]
    updatable, updatable_hours = request["updatable"], request["updatable_hours"]
    params = {k: v for k, v in request.items() if k not in _CALL_PARAMS}
    start = time.monotonic()
    caller = caller.with_payment_argument(request["x402_payment"])
    caller = _with_caller_info(caller, request["contact_email"], request["x402_ready"], "create", style,
                               organization_name)
    paid = _gate(caller, 1, free_tier=True, tool="create_wallet_pass", path="/api/passes", action="create",
                 style=style, org=organization_name)

    serial = request["serial_number"] or str(uuid.uuid4())
    try:
        if not updatable and updatable_hours != 1:
            raise pass_builder.PassBuildError("updatable_hours only applies with updatable=True")
        if not 1 <= updatable_hours <= MAX_UPDATABLE_HOURS:
            raise pass_builder.PassBuildError(
                f"updatable_hours must be between 1 and {MAX_UPDATABLE_HOURS} (30 days), got {updatable_hours!r}"
            )
        if updatable and db.pass_exists(serial):
            raise pass_builder.PassBuildError(
                f"serial_number {serial!r} is already used by another updatable pass; omit it to get a fresh one"
            )
        params = _resolve_image_urls(params)
        auth_token = secrets.token_urlsafe(24) if updatable else None
        pkpass_bytes = _build_pkpass(params, serial, auth_token)
        # Everything that can fail is written first and settled last, so a caller is never charged
        # for a pass they don't get; a failed settlement undoes the writes.
        token = _register_download(pkpass_bytes)
        result = {
            "download_url": f"{PUBLIC_BASE_URL}/download/{token}",
            "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
            "serial_number": serial,
            "pass_type_identifier": pass_builder.PASS_TYPE_IDENTIFIER,
        }
        if updatable:
            edit_token = secrets.token_urlsafe(24)
            expires = int(time.time()) + updatable_hours * 3600
            db.store_pass(serial, auth_token, _hash_token(edit_token), json.dumps(params), pkpass_bytes,
                          int(time.time()), expires)
            result["edit_token"] = edit_token
            result["updatable_until"] = _iso(expires)
        def undo():
            _unregister_download(token)
            if updatable:
                db.delete_pass(serial)
        receipt = _settle(paid, undo)
        if receipt:
            result["payment"] = receipt
    except Exception as e:
        _log_failure(e, caller, "create", style, organization_name, serial, start)
        raise _rejection(e, caller, "create") from None
    _log(
        caller, style=style, organization_name=organization_name, success=True, error=None,
        duration_ms=int((time.monotonic() - start) * 1000), serial_number=serial,
        payment_tx=receipt and receipt.get("transaction"),
    )
    _add_x402_setup_hint(result, caller, paid=bool(receipt))
    return result


def _with_caller_info(caller: Caller, contact_email: str | None, x402_ready: bool | str | None,
                      action: str, style: str, organization_name: str) -> Caller:
    """Adds the contact address and x402 status every call must send. A call without them is refused
    before the limits are checked, so it neither counts as a call nor gets charged, but it is logged
    so the operator can see who the requirement turns away."""
    problems = []
    if contact_email is None:
        problems.append("contact_email is required")
    elif len(contact_email) > 254 or not EMAIL_RE.fullmatch(contact_email):
        problems.append("contact_email is not a valid email address")
    elif _is_placeholder_email(contact_email):
        problems.append("contact_email must be a real address, not a placeholder")
    if x402_ready is None:
        problems.append('x402_ready is required (true, false or "unknown")')
    if problems:
        _refuse(caller, action, style, organization_name, f"{db.CALLER_INFO_ERROR}: {'; '.join(problems)}",
                PassRejected(f"{'; '.join(problems)}. {CALLER_INFO_WHY}", 400))
    return replace(caller, contact_email=contact_email, x402_ready=None if x402_ready == "unknown" else x402_ready)


def _add_x402_setup_hint(result: dict, caller: Caller, paid: bool = False) -> None:
    """Called after the successful call is logged, so the count includes it."""
    if caller.x402_ready is not False or not payments.enabled or paid:
        return
    left = db.free_remaining(caller.ip, caller.contact_email)
    if not 0 <= left <= HINT_WHEN_LEFT:
        return
    lead = ("That was the last free pass today." if left == 0 else
            f"{left} free pass{'' if left == 1 else 'es'} left today.")
    result["free_passes_left_today"] = left
    result["x402_setup"] = {**X402_SETUP_HINT, "message": X402_SETUP_HINT["message"].format(left=lead)}


def _is_placeholder_email(address: str) -> bool:
    domain = address.rpartition("@")[2].lower()
    return domain in PLACEHOLDER_EMAIL_DOMAINS or domain.endswith(PLACEHOLDER_EMAIL_TLDS)


def _update_pass(caller: Caller, serial_number: str, edit_token: str, changes: dict) -> dict:
    """Merge `changes` into a stored updatable pass, re-sign it, and push it to installed devices."""
    start = time.monotonic()
    row = db.get_pass(serial_number)
    if row is None or not secrets.compare_digest(row["edit_hash"], _hash_token(edit_token)):
        # One message for both cases, so serials can't be probed without their token.
        raise PassRejected(
            "unknown serial_number, wrong edit_token, or the pass's update window (updatable_hours) has ended", 404)
    contact_email, x402_ready = changes.pop("contact_email"), changes.pop("x402_ready")
    caller = caller.with_payment_argument(changes.pop("x402_payment", None))
    params = json.loads(row["params"])
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        raise PassRejected("nothing to update: pass at least one field to change", 400)
    style = changes.get("style", params["style"])
    organization_name = changes.get("organization_name", params["organization_name"])
    caller = _with_caller_info(caller, contact_email, x402_ready, "update", style, organization_name)
    paid = _gate(caller, 1, free_tier=True, tool="update_wallet_pass", path="/api/passes", action="update",
                 style=style, org=organization_name)
    try:
        params.update(_resolve_image_urls(changes))
        pkpass_bytes = _build_pkpass(params, serial_number, row["auth_token"])
        token = _register_download(pkpass_bytes)
        # Wallet compares these as tags, so each version must sort after the last.
        updated = max(int(time.time()), row["updated"] + 1)
        db.replace_pass(serial_number, json.dumps(params), pkpass_bytes, updated)
        # Settled after the writes (so a caller is never charged for an update that didn't happen)
        # but before any device is told; a failed payment restores the stored pass.
        def undo():
            _unregister_download(token)
            db.replace_pass(serial_number, row["params"], row["pkpass"], row["updated"])
        receipt = _settle(paid, undo)
        notified, dead = apns.push(db.push_tokens(serial_number))
        db.drop_push_tokens(serial_number, dead)
    except Exception as e:
        _log_failure(e, caller, "update", style, organization_name, serial_number, start)
        raise _rejection(e, caller, "update") from None
    _log(
        caller, style=style, organization_name=organization_name, success=True, error=None,
        duration_ms=int((time.monotonic() - start) * 1000), serial_number=serial_number, action="update",
        payment_tx=receipt and receipt.get("transaction"),
    )
    result = {
        "download_url": f"{PUBLIC_BASE_URL}/download/{token}",
        "expires_in_seconds": DOWNLOAD_TTL_SECONDS,
        "serial_number": serial_number,
        "pass_type_identifier": pass_builder.PASS_TYPE_IDENTIFIER,
        "notified_devices": notified,
        "updatable_until": _iso(row["expires"]),
    }
    if receipt:
        result["payment"] = receipt
    _add_x402_setup_hint(result, caller, paid=bool(receipt))
    return result


def _iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- The payment gate, shared by single passes and batches. Order: capacity, the failure limit, the free
# daily limit (single passes only; batches have no free tier), the paid daily cap, then the caller's x402
# payment, verified but not charged. The work happens next, and _settle charges only once every pass is built
# and stored. Every refusal is logged with refused=1; the refusal texts are matched by the usage watcher, so
# they must not change. If the facilitator can't be reached to quote a price, the call gets a 503 (the
# server's problem, not the caller's: retry later), for single passes and batches alike. ---

def _gate(caller: Caller, count: int, *, free_tier: bool, tool: str, path: str, action: str, style: str,
          org: str):
    """Limits + payment for `count` passes. Returns the verified (not yet charged) payment, or None for a
    free call; otherwise logs the refusal and raises PassRejected. tool/path name the MCP tool and REST
    route the x402 resource points at."""
    if not free_tier and not payments.enabled:
        _refuse(caller, action, style, org, "batches are paid and payments are not set up on this server", 503)
    if not _pilot_disk_ok():
        logger.error("refusing passes: less than %d GB of disk free", MIN_FREE_DISK_BYTES // 1024 ** 3)
        _refuse(caller, action, style, org, "server is low on disk space",
                PassRejected("the service is temporarily out of capacity, try again later", 503))
    fail = db.check_fail_limit(caller.ip, caller.contact_email)
    if fail:
        _refuse(caller, action, style, org, fail, 429)
    reason = None
    if free_tier:
        reason = db.check_rate_limit(caller.ip, db.is_exempt(caller.contact_email), caller.contact_email)
        if reason is None:
            return None
        if not payments.enabled:
            _refuse(caller, action, style, org, reason, 429)
    over = db.check_paid_limit(caller.ip, count)
    if over:
        _refuse(caller, action, style, org, over, 429)
    if caller.payment is not None:
        try:
            return payments.verify(caller.payment, count)
        except payments.PaymentError as e:
            reason = str(e)
    if not free_tier:
        reason = _batch_lead(reason, count)
    _refuse(caller, action, style, org, reason, _payment_required(caller, tool, path, reason, count, free_tier))


def _refuse(caller: Caller, action: str, style: str, org: str, error: str, rejection: "int | PassRejected"):
    """Log a refused call (refused=1: it counts toward no limit) and raise the rejection; an int status
    means a plain PassRejected with the logged text."""
    _log(caller, style=style, organization_name=org, success=False, error=error, duration_ms=0,
         serial_number=None, action=action, refused=True)
    raise rejection if isinstance(rejection, PassRejected) else PassRejected(error, rejection)


def _batch_lead(rejection: str | None, count: int) -> str:
    each = f"{count} new pass{'' if count == 1 else 'es'} at {payments.PRICE} each = {payments.price_for(count)}"
    return f"{rejection}: {each}" if rejection else f"Batches are paid: {each}"


def _payment_required(caller: Caller, tool: str, path: str, reason: str, count: int = 1,
                      free_tier: bool = True) -> PassRejected:
    """A 402 offering to do the call for the price of `count` passes; a 503 if the facilitator can't be
    reached to say what to pay."""
    if caller.source == "mcp":
        resource, how = f"mcp://tool/{tool}", "the signed payment as the x402_payment argument"
    else:
        resource, how = f"{PUBLIC_BASE_URL}{path}", "a PAYMENT-SIGNATURE header or an x402_payment body field"
    if free_tier:
        message = (f"{reason}. The first {db.PER_IP_DAILY_LIMIT} passes each day are free; every pass after them is only a cent ({payments.PRICE} in USDC on Base, "
                   f"via x402): retry this exact call with {how}. If this agent has no wallet yet, tell your user: AI "
                   f"agents can now pay for things on their own. Setting up a Coinbase Agentic Wallet for this agent "
                   f"once lets it pay any app or service that accepts agent payments, not just this one; here $1 "
                   f"covers 100 passes. Wallet setup instructions: {X402_GUIDE_URL}")
    else:
        message = (f"{reason} (USDC on Base, via x402). Retry this exact call with {how}; nothing is charged unless "
                   f"every pass builds. If this agent has no wallet yet, tell your user: setting up a Coinbase Agentic "
                   f"Wallet once lets the agent pay for things like this on its own. Wallet setup instructions: "
                   f"{X402_GUIDE_URL}")
    try:
        return PassRejected(message, 402, payments.payment_required(resource, message, count))
    except Exception:
        logger.error("x402 payment requirements unavailable: %s", traceback.format_exc())
        return PassRejected(reason if reason.startswith(payments.UNAVAILABLE) else f"{payments.UNAVAILABLE} ({reason})",
                            503)


def _settle(verified, undo):
    """Charge a verified payment (None: a free call, nothing to charge) once the work is built and stored.
    If it fails, undo() rolls the stored work back and the PaymentError is re-raised (not charged)."""
    if verified is None:
        return None
    try:
        return payments.settle(verified)
    except Exception:
        undo()
        raise


def _log_failure(e: Exception, caller, action, style, organization_name, serial, start) -> None:
    if isinstance(e, (pass_builder.PassBuildError, payments.PaymentError)):
        error = str(e)
    else:
        logger.error("unexpected error building pass: %s", traceback.format_exc())
        error = f"internal error: {e}"
    _log(
        caller, style=style, organization_name=organization_name, success=False, error=error,
        duration_ms=int((time.monotonic() - start) * 1000), serial_number=serial, action=action,
    )


def _rejection(e: Exception, caller: Caller, action: str) -> Exception:
    if isinstance(e, payments.PaymentError):
        return _payment_required(caller, f"{action}_wallet_pass", "/api/passes", str(e))
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

    pkpass = pass_builder.build_pkpass(pass_dict, files)
    if len(pkpass) > MAX_PKPASS_BYTES:
        raise pass_builder.PassBuildError(
            f"the pass is {len(pkpass) / 1024 ** 2:.2f} MB; the limit is {MAX_PKPASS_BYTES / 1024 ** 2:g} MB. "
            "Use smaller images: Wallet shows them at most a few hundred pixels wide")
    return pkpass


_PASS_JSON_PARAMS = [
    name for name in inspect.signature(pass_builder.build_pass_json).parameters
    if name not in ("serial_number", "web_service_url", "authentication_token")
]


def _check_pass_json(params: dict, serial: str = "check", auth_token: str | None = None) -> None:
    """Raises PassBuildError if params' pass.json doesn't build. Cheap: no images are fetched or decoded
    and nothing is signed, so it can run before a call is paid for."""
    pass_builder.build_pass_json(**{k: params.get(k) for k in _PASS_JSON_PARAMS if k in params},
                                 serial_number=serial, web_service_url="https://x" if auth_token else None,
                                 authentication_token=auth_token)


def _tool_fields(tool, exclude=(), **defaults) -> dict:
    """create_model field definitions for a tool's parameters (ctx left out): the same types and
    defaults, except those overridden in defaults."""
    return {
        name: (param.annotation,
               defaults.get(name, ... if param.default is inspect.Parameter.empty else param.default))
        for name, param in inspect.signature(tool).parameters.items()
        if name != "ctx" and name not in exclude
    }


def _advertise_caller_info_as_required(*tools) -> None:
    """Clients see both as required, while the functions default them to None so a call without them
    reaches _with_caller_info and gets logged instead of failing inside the MCP framework."""
    for tool in tools:
        schema = server._tool_manager.get_tool(tool.__name__).parameters
        schema["required"] = [*schema["required"], "contact_email", "x402_ready"]
        schema["properties"]["contact_email"] = {"type": "string", "title": "Contact Email"}
        schema["properties"]["x402_ready"] = {"enum": [True, False, "unknown"], "title": "X402 Ready"}


_advertise_caller_info_as_required(create_wallet_pass, update_wallet_pass)


@server.custom_route("/", methods=["GET"])
async def dashboard(request: Request) -> Response:
    return Response(DASHBOARD_HTML, media_type="text/html")


@server.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> Response:
    return JSONResponse({**db.stats(), "site": site_stats.stats()})


# Dashboard data (tailnet only: nginx sends the public domain's /api/ to the landing page, never here).
@server.custom_route("/api/dashboard", methods=["GET"])
async def api_dashboard(request: Request) -> Response:
    days = min(max(int(request.query_params.get("days") or 30), 1), 366)
    tz_min = min(max(int(request.query_params.get("tz") or 0), -840), 840)  # browser's minutes east of UTC
    return JSONResponse({**await anyio.to_thread.run_sync(db.dashboard_stats, days, tz_min),
                         "site": await anyio.to_thread.run_sync(site_stats.stats, days, tz_min)})


def _log_filters(request: Request) -> dict:
    q = request.query_params
    return {k: (q.get(k) or None) for k in ("status", "source", "action", "q", "since")}


_public_cache: dict = {"at": 0.0, "body": None}


@server.custom_route("/api/public-stats", methods=["GET"])
async def api_public_stats(request: Request) -> Response:
    """Counts shown on walletmcppass.com (public): passes made, last 7 days, last pass time. Cached 60 s."""
    if time.monotonic() - _public_cache["at"] > 60:
        _public_cache["body"] = await anyio.to_thread.run_sync(db.public_stats)
        _public_cache["at"] = time.monotonic()
    return JSONResponse(_public_cache["body"], headers={"Cache-Control": "public, max-age=60"})


@server.custom_route("/api/requests", methods=["GET"])
async def api_requests(request: Request) -> Response:
    before = request.query_params.get("before_id")
    limit = min(max(int(request.query_params.get("limit") or 100), 1), 1000)
    return JSONResponse(await anyio.to_thread.run_sync(lambda: db.request_log(
        before_id=int(before) if before else None, limit=limit, **_log_filters(request))))


@server.custom_route("/api/requests.csv", methods=["GET"])
async def api_requests_csv(request: Request) -> Response:
    import csv
    import io
    rows = (await anyio.to_thread.run_sync(lambda: db.request_log(limit=0, **_log_filters(request))))["rows"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=db.LOG_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"content-disposition": 'attachment; filename="wallet-mcp-requests.csv"'})


# The REST body accepts exactly the tool's parameters, validated with the same
# types, so the API and the MCP tool can't drift apart.
_PassRequest = create_model("PassRequest", __config__=ConfigDict(extra="forbid"), **_tool_fields(create_wallet_pass))


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
        caller = Caller.from_api(request)
        return _api_result(await anyio.to_thread.run_sync(lambda: _create_pass(caller, params), limiter=BUILD_LIMITER))
    except PassRejected as e:
        return _api_rejection(e)
    except Exception:
        return JSONResponse({"error": "internal error building the pass"}, status_code=500)


_UpdateRequest = create_model("UpdateRequest", __config__=ConfigDict(extra="forbid"),
                              **_tool_fields(update_wallet_pass, exclude=("serial_number", "edit_token")))


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
    serial, caller = request.path_params["serial"], Caller.from_api(request)
    try:
        return _api_result(await anyio.to_thread.run_sync(
            lambda: _update_pass(caller, serial, edit_token.strip(), changes), limiter=BUILD_LIMITER))
    except PassRejected as e:
        return _api_rejection(e)
    except Exception:
        return JSONResponse({"error": "internal error building the pass"}, status_code=500)


def _api_result(result: dict) -> JSONResponse:
    headers = {"PAYMENT-RESPONSE": payments.encode_header(result["payment"])} if "payment" in result else None
    return JSONResponse(result, headers=headers)


def _api_rejection(e: PassRejected) -> JSONResponse:
    """x402's HTTP transport: a 402 with the PaymentRequired body, also base64'd in PAYMENT-REQUIRED."""
    if e.payment_required is None:
        return JSONResponse({"error": e.message}, status_code=e.status)
    return JSONResponse(e.payment_required, status_code=402,
                        headers={"PAYMENT-REQUIRED": payments.encode_header(e.payment_required)})


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


# --- Organizer pilots (pilot.py): REST API behind a secret "<id>.<token>" key; attendee pass links ---

PILOT_BODY_LIMIT = 3 * 1024 * 1024
PILOT_CSV_LIMIT = 2 * 1024 * 1024
CLEAN_LIMITER = anyio.CapacityLimiter(2)  # a clean may wait on a model call for up to a minute
_pilot_locks: dict = {}


def _pilot_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _pilot_lock(pilot_id: str):
    return _pilot_locks.setdefault(pilot_id, anyio.Lock())


async def _pilot_body(request: Request):
    """(body dict, None) or (None, error response)."""
    raw = await request.body()
    if len(raw) > PILOT_BODY_LIMIT:
        return None, _pilot_error("request body too large", 413)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _pilot_error("request body must be a JSON object")
    if not isinstance(body, dict):
        return None, _pilot_error("request body must be a JSON object")
    return body, None


def _pilot_for(request: Request, key, allow_batch: bool = False):
    """(pilot, None) or (None, error response). Wrong key and unknown id look the same. Paid batches
    share the read-only routes (details, links.csv) but not the free issue/clean/update ones."""
    p = pilot.authenticate(request.path_params["id"], key)
    if p is None:
        return None, _pilot_error("invalid pilot key", 403)
    if p["kind"] == "batch" and not allow_batch:
        return None, _pilot_error("this is a paid batch: use create_wallet_pass_batch / update_wallet_pass_batch "
                                  "(REST: /api/batches)", 403)
    return pilot.with_design(p), None


def _pilot_disk_ok() -> bool:
    return shutil.disk_usage(DOWNLOAD_DIR).free >= MIN_FREE_DISK_BYTES


def _pilot_preview_path(pilot_id: str) -> Path:
    return pilot.PREVIEW_DIR / f"{pilot_id}.png"


@server.custom_route("/api/pilot/{id}", methods=["GET"])
async def api_pilot_get(request: Request) -> Response:
    p, err = _pilot_for(request, request.query_params.get("key"), allow_batch=True)
    if err:
        return err
    return JSONResponse({
        "id": p["id"], "org": p["org"], "event_name": p["event_name"], "event_end": p["event_end"],
        "design": p["design_obj"], "sample_url": pilot.sample_link(p),
        "preview_png_url": f"{PUBLIC_BASE_URL}/api/pilot/{p['id']}/preview.png"
        if _pilot_preview_path(p["id"]).exists() else None,
        "issued": pilot.issued_count(p["id"]), "max_passes": p["max_passes"], "contact_email": p["contact_email"],
    })


@server.custom_route("/api/pilot/{id}/preview.png", methods=["GET"])
async def api_pilot_preview(request: Request) -> Response:
    pilot_id = request.path_params["id"]
    path = _pilot_preview_path(pilot_id)
    if not pilot.ID_RE.match(pilot_id) or not path.is_file():
        return _pilot_error("no preview", 404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@server.custom_route("/api/pilot/{id}/clean", methods=["POST"])
async def api_pilot_clean(request: Request) -> Response:
    body, err = await _pilot_body(request)
    if err:
        return err
    p, err = _pilot_for(request, body.get("key"))
    if err:
        return err
    text = body.get("csv")
    if not isinstance(text, str) or not text.strip():
        return _pilot_error("csv must be the file's text")
    if len(text.encode()) > PILOT_CSV_LIMIT:
        return _pilot_error("csv is larger than 2 MB", 413)
    result = await anyio.to_thread.run_sync(lambda: csv_clean.clean(text, salt=p["id"]), limiter=CLEAN_LIMITER)
    return JSONResponse(result)


def _pilot_rows(raw):
    """(rows, None) or (None, message): validated {name, email, ticket, type} rows, repeats of a ticket dropped
    after the first (they get the same link)."""
    if not isinstance(raw, list) or not raw:
        return None, "rows must be a non-empty list"
    if len(raw) > pilot.MAX_ROWS_PER_REQUEST:
        return None, f"at most {pilot.MAX_ROWS_PER_REQUEST} rows per request"
    rows = []
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            return None, f"row {i}: must be an object"
        vals = {k: r.get(k, "") for k in ("name", "email", "ticket", "type")}
        if any(not isinstance(v, str) for v in vals.values()):
            return None, f"row {i}: name, email, ticket and type must be strings"
        vals = {k: v.strip() for k, v in vals.items()}
        vals["type"] = vals["type"] or csv_clean.DEFAULT_TYPE
        if not vals["name"] or len(vals["name"]) > 120:
            return None, f"row {i}: name is required (max 120 characters)"
        if not vals["ticket"] or len(vals["ticket"]) > 64:
            return None, f"row {i}: ticket is required (max 64 characters)"
        if len(vals["type"]) > 100:
            return None, f"row {i}: type is too long"
        if vals["email"] and not csv_clean.EMAIL_RE.match(vals["email"]):
            return None, f"row {i}: invalid email"
        rows.append(vals)
    return rows, None


# --- The engine shared by organizer pilots (free, set up by the operator) and paid batches. The real
# differences are parameters: batches log only once paid (logged=False), cap their own concurrent builds
# (at_once) so a big batch leaves BUILD_LIMITER slots for single passes, roll back a partial issue, and
# stage an update's new versions until the payment settles (stage=). ---

def _unique_rows(rows: list[dict]) -> dict:
    """ticket -> the first row with that ticket (repeats of a ticket get the same pass)."""
    unique: dict = {}
    for row in rows:
        unique.setdefault(row["ticket"], row)
    return unique


def _at_most(at_once: int | None):
    return anyio.Semaphore(at_once) if at_once else contextlib.nullcontext()


async def issue_rows(p: dict, unique: dict, ip: str, *, logged: bool = True, at_once: int | None = None) -> dict:
    """Issue (or find) the pass for every unique row, concurrently: ticket -> pilot.issue_one's result, or the
    exception it raised. Nothing is rolled back here: a caller that wants all-or-nothing unissues."""
    results: dict = {}
    limit = _at_most(at_once)

    async def work(ticket, row):
        async with limit:
            try:
                results[ticket] = await anyio.to_thread.run_sync(
                    lambda: pilot.issue_one(p, row, ip, logged), limiter=BUILD_LIMITER)
            except Exception as e:
                if not isinstance(e, pass_builder.PassBuildError):
                    logger.error("issuing a pass failed: %s", traceback.format_exc())
                results[ticket] = e

    async with anyio.create_task_group() as tg:
        for ticket, row in unique.items():
            tg.start_soon(work, ticket, row)
    return results


def assemble_passes(rows: list[dict], results: dict) -> list[dict]:
    """One entry per requested row, in request order; a repeated ticket has the same link and new=False."""
    passes, seen = [], set()
    for row in rows:
        r = results[row["ticket"]]
        passes.append({**row, "link": r["link"], "new": r["new"] and row["ticket"] not in seen})
        seen.add(row["ticket"])
    return passes


async def rebuild_all(p: dict, targets: list[dict], ip: str, *, logged: bool = True, at_once: int | None = None,
                      stage: str | None = None) -> tuple[list[str], list[Exception]]:
    """Rebuild every target's pass from p['design_obj'], concurrently. Returns (serials rebuilt, errors); a
    pass that is gone is in neither. With stage, the new versions go to db.pass_staging instead of going
    live, and the first error stops the remaining builds (the whole update is refused anyway)."""
    done, errors = [], []
    limit = _at_most(at_once)

    async def work(row):
        async with limit:
            if errors and stage:
                return
            try:
                if await anyio.to_thread.run_sync(lambda: pilot.rebuild_one(p, row, ip, logged, stage),
                                                  limiter=BUILD_LIMITER):
                    done.append(row["serial"])
            except Exception as e:
                errors.append(e)
                logger.error("rebuilding a pass failed: %s", traceback.format_exc())

    async with anyio.create_task_group() as tg:
        for row in targets:
            tg.start_soon(work, row)
    return done, errors


async def push_updates(serials: list[str]) -> int:
    """APNs push to every device holding one of these passes; drops tokens APNs says are dead."""
    tokens = await anyio.to_thread.run_sync(pilot.all_push_tokens, serials)
    notified, dead = await anyio.to_thread.run_sync(apns.push, tokens)
    pilot.drop_dead_tokens(dead)
    return notified


@server.custom_route("/api/pilot/{id}/issue", methods=["POST"])
async def api_pilot_issue(request: Request) -> Response:
    body, err = await _pilot_body(request)
    if err:
        return err
    p, err = _pilot_for(request, body.get("key"))
    if err:
        return err
    rows, message = _pilot_rows(body.get("rows"))
    if message:
        return _pilot_error(message)
    if pilot.expires_epoch(p) <= time.time():
        return _pilot_error("this event has ended; no more passes can be issued")
    if not _pilot_disk_ok():
        return _pilot_error("the service is temporarily out of capacity, try again later", 503)
    ip = _client_ip(request)
    unique = _unique_rows(rows)
    async with _pilot_lock(p["id"]):
        live = pilot.live_tickets(p["id"])
        fresh = [t for t in unique if t not in live]
        total = pilot.issued_count(p["id"])
        if total + len(fresh) > p["max_passes"]:
            return _pilot_error(f"this pilot is limited to {p['max_passes']} passes; {total} are issued and this "
                                f"request would add {len(fresh)} more")
        results = await issue_rows(p, unique, ip)
        for ticket, r in results.items():
            if isinstance(r, Exception):
                who = unique[ticket]["name"]
                msg = str(r) if isinstance(r, pass_builder.PassBuildError) else "internal error building the pass"
                return _pilot_error(f"could not build the pass for {who} ({ticket}): {msg}",
                                    400 if isinstance(r, pass_builder.PassBuildError) else 500)
        passes = assemble_passes(rows, results)
        created = sum(1 for x in passes if x["new"])
        return JSONResponse({"passes": passes, "created": created, "reused": len(passes) - created,
                             "issued_total": pilot.issued_count(p["id"])})


def _csv_cell(value: str) -> str:
    """Neutralise spreadsheet formulas in organizer-opened exports."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@server.custom_route("/api/pilot/{id}/links.csv", methods=["GET"])
async def api_pilot_links(request: Request) -> Response:
    p, err = _pilot_for(request, request.query_params.get("key"), allow_batch=True)
    if err:
        return err
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["name", "email", "ticket", "type", "link"])
    for r in pilot.issued_rows(p["id"]):
        w.writerow([_csv_cell(r["name"]), _csv_cell(r["email"]), r["ticket"], _csv_cell(r["type"]),
                    pilot.pass_link(r["serial"], r["auth_token"])])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{p["id"]}-links.csv"',
                             "Cache-Control": "no-store"})


@server.custom_route("/api/pilot/{id}/update", methods=["POST"])
async def api_pilot_update(request: Request) -> Response:
    body, err = await _pilot_body(request)
    if err:
        return err
    p, err = _pilot_for(request, body.get("key"))
    if err:
        return err
    try:
        changes = pilot.validate_changes(body.get("changes"))
    except ValueError as e:
        return _pilot_error(str(e))
    if not _pilot_disk_ok():
        return _pilot_error("the service is temporarily out of capacity, try again later", 503)
    ip = _client_ip(request)
    async with _pilot_lock(p["id"]):
        merged = pilot.merge_changes(p["design_obj"], changes)
        try:  # prove the new design builds before touching any stored pass
            await anyio.to_thread.run_sync(
                lambda: _build_pkpass(pilot.fill(merged, pilot.SAMPLE_ROW), str(uuid.uuid4()), None),
                limiter=BUILD_LIMITER)
        except pass_builder.PassBuildError as e:
            return _pilot_error(f"that change would not build: {e}")
        pilot.save_design(p["id"], merged)
        p["design_obj"] = merged
        targets = [{**r, "serial": r["serial"]} for r in pilot.issued_rows(p["id"])]
        if p["sample_serial"]:
            targets.append({**pilot.SAMPLE_ROW, "serial": p["sample_serial"]})
        done, errors = await rebuild_all(p, targets, ip)
        failed = len(errors)
        notified = await push_updates(done)
    attendees = len(done) - (1 if p["sample_serial"] in done else 0)
    result = {"updated": attendees, "notified_devices": notified}
    if failed:
        result["failed"] = failed
    return JSONResponse(result)


@server.custom_route("/pass/{serial}/{auth_token}", methods=["GET"])
async def pilot_pass(request: Request) -> Response:
    """The permanent attendee link: always the current version of the pass."""
    row = db.get_pass(request.path_params["serial"])
    token = request.path_params["auth_token"]
    if row is None or not secrets.compare_digest(token, row["auth_token"]) or not pilot.is_pilot_serial(row["serial"]):
        return Response("unknown or expired pass link", status_code=404)
    return Response(row["pkpass"], media_type="application/vnd.apple.pkpass",
                    headers={"Last-Modified": formatdate(row["updated"], usegmt=True), "Cache-Control": "no-cache",
                             "Content-Disposition": 'inline; filename="pass.pkpass"'})


# --- Paid batches: the organizer bulk upload (one design + attendee rows -> one permanent, updatable pass
# link each) for any caller, over MCP (create_wallet_pass_batch / update_wallet_pass_batch) and REST
# (/api/batches). Stored as pilots of kind 'batch'. No free tier: every new pass, and every pass re-signed by
# a batch update, costs payments.PRICE, paid as one x402 payment for the whole call. ---

BATCH_MAX_PASSES = int(os.environ.get("WALLET_MCP_BATCH_MAX_PASSES", 5000))
BATCH_MAX_DAYS = 366  # event_end at most this far ahead (passes are stored until event_end + 7 days)
BATCH_BUILDS_AT_ONCE = 2  # per call, so a big batch leaves BUILD_LIMITER slots for single passes
_BatchDesign = create_model("BatchDesign", __config__=ConfigDict(extra="forbid"),
                            **_tool_fields(create_wallet_pass, exclude=_CALL_PARAMS, style="eventTicket"))


def _batch_links_csv(p: dict, key: str) -> str:
    return f"{PUBLIC_BASE_URL}/api/pilot/{p['id']}/links.csv?key={key}"


async def _create_batch(caller: Caller, *, design, rows, event_end, batch_key, contact_email, x402_ready,
                        x402_payment) -> dict:
    """New batch (design + event_end) or more passes for an existing one (batch_key)."""
    tool, start = "create_wallet_pass_batch", time.monotonic()
    caller = caller.with_payment_argument(x402_payment)
    d = design if isinstance(design, dict) else {}
    caller = _with_caller_info(caller, contact_email, x402_ready, "create", str(d.get("style") or "eventTicket"),
                               str(d.get("organization_name") or "batch"))
    rows, message = _pilot_rows(rows)
    if message:
        raise PassRejected(message, 400)
    key = batch_key
    if batch_key is not None:
        if design is not None or event_end is not None:
            raise PassRejected("with batch_key, leave out design and event_end: the batch keeps its own "
                               "(change the design with update_wallet_pass_batch)", 400)
        found = pilot.authenticate(batch_key.split(".")[0], batch_key) if isinstance(batch_key, str) else None
        if found is None or found["kind"] != "batch":
            raise PassRejected("unknown batch_key", 404)
        p, new_batch = pilot.with_design(found), False
        if pilot.expires_epoch(p) <= time.time():
            raise PassRejected("this batch's event has ended; no more passes can be issued", 400)
    else:
        if not isinstance(design, dict) or not isinstance(event_end, str):
            raise PassRejected("a new batch needs design (create_wallet_pass's parameters, with {name} {ticket} "
                               "{type} {email} placeholders) and event_end (ISO 8601)", 400)
        try:
            design = _BatchDesign.model_validate(design).model_dump(exclude_unset=True)
            end = pilot.parse_end(event_end)
        except ValidationError as e:
            raise PassRejected(f"invalid design: {'; '.join(_validation_details(e))}", 400) from None
        except ValueError:
            raise PassRejected("event_end must be an ISO 8601 date or date-time, e.g. 2026-12-31T23:00:00Z", 400) from None
        if end.timestamp() > time.time() + BATCH_MAX_DAYS * 86400:
            raise PassRejected(f"event_end must be within {BATCH_MAX_DAYS} days", 400)
        if end.timestamp() + pilot.GRACE_SECONDS <= time.time():
            raise PassRejected("the design doesn't build: event end is in the past", 400)
    unique = _unique_rows(rows)
    verified = None
    if batch_key is None:
        # Only cheap checks before the payment gate: an unpaid call must not fetch images, sign anything or
        # store a batch (#4). The sample row's pass.json is built here (images aren't needed for it); the
        # images are fetched and a sample is signed by pilot.create, once the payment is verified.
        style, org = design.get("style", "eventTicket"), design["organization_name"]
        try:
            sample = pilot.fill(pilot.ensure_field_keys(pilot.normalize_design(design, org, design["description"])),
                                rows[0])
            _check_pass_json(sample)
        except pass_builder.PassBuildError as e:
            _log_failure(e, caller, "create", style, org, None, start)
            raise PassRejected(f"the design doesn't build: {e}", 400) from None
        if len(unique) > BATCH_MAX_PASSES:
            raise PassRejected(f"a batch holds at most {BATCH_MAX_PASSES} passes; this call would add {len(unique)}",
                               400)
        verified = await anyio.to_thread.run_sync(lambda: _gate(
            caller, len(unique), free_tier=False, tool=tool, path="/api/batches", action="create", style=style,
            org=org))
        try:
            made = await anyio.to_thread.run_sync(lambda: pilot.create(
                org, design["description"], caller.contact_email, event_end, design,
                BATCH_MAX_PASSES, keep_colors=True, kind="batch", sample_row=rows[0]), limiter=BUILD_LIMITER)
        except ValueError as e:  # PassBuildError: a bad image URL, or a design that doesn't sign
            _log_failure(pass_builder.PassBuildError(str(e)), caller, "create", style, org, None, start)
            raise PassRejected(f"the design doesn't build: {e}; nothing was charged", 400) from None
        p, key, new_batch = pilot.with_design(made["pilot"]), made["key"], True
    style, org = p["design_obj"].get("style", "eventTicket"), p["org"]
    try:
        async with _pilot_lock(p["id"]):
            fresh = [t for t in unique if t not in pilot.live_tickets(p["id"])]
            total = pilot.issued_count(p["id"])
            if total + len(fresh) > p["max_passes"]:
                raise PassRejected(f"a batch holds at most {p['max_passes']} passes; it has {total} and this call "
                                   f"would add {len(fresh)}", 400)
            if not new_batch and fresh:
                verified = await anyio.to_thread.run_sync(lambda: _gate(
                    caller, len(fresh), free_tier=False, tool=tool, path="/api/batches", action="create",
                    style=style, org=org))
            results = await issue_rows(p, unique, caller.ip, logged=False, at_once=BATCH_BUILDS_AT_ONCE)
            new_serials = [r["serial"] for r in results.values() if isinstance(r, dict) and r.get("new")]
            failed = [(t, r) for t, r in results.items() if isinstance(r, Exception)]
            if failed:
                pilot.unissue(p["id"], new_serials)
                ticket, e = failed[0]
                built = isinstance(e, pass_builder.PassBuildError)
                _log_failure(e, caller, "create", style, org, None, start)
                raise PassRejected(f"could not build the pass for {unique[ticket]['name']} ({ticket}): "
                                   f"{e if built else 'internal error'}; nothing was charged", 400 if built else 500)
            try:
                receipt = await anyio.to_thread.run_sync(
                    lambda: _settle(verified, lambda: pilot.unissue(p["id"], new_serials)))
            except payments.PaymentError as e:
                _log(caller, style=style, organization_name=org, success=False, error="payment settlement failed",
                     duration_ms=int((time.monotonic() - start) * 1000), serial_number=None, action="create")
                raise _payment_required(caller, tool, "/api/batches", str(e), len(fresh), free_tier=False) from None
    except BaseException:
        if new_batch:
            pilot.delete(p["id"])
        raise
    ms = int((time.monotonic() - start) * 1000) // max(len(new_serials), 1)
    for serial in new_serials:
        _log(caller, style=style, organization_name=org, success=True, error=None, duration_ms=ms,
             serial_number=serial, payment_tx=receipt and receipt.get("transaction"))
    passes = assemble_passes(rows, results)
    result = {"batch_id": p["id"], "passes": passes, "created": len(new_serials),
              "reused": len(passes) - len(new_serials), "issued_total": pilot.issued_count(p["id"]),
              "charged": payments.price_for(len(new_serials)) if receipt else "$0",
              "links_csv_url": _batch_links_csv(p, key),
              "updatable_until": _iso(pilot.expires_epoch(p))}
    if new_batch:
        result = {"batch_key": key, **result}
    if receipt:
        result["payment"] = receipt
    return result


async def _update_batch(caller: Caller, *, batch_key, changes, contact_email, x402_ready, x402_payment) -> dict:
    """Merge changes into a batch's design, re-sign every live pass in it, and push them to iPhones.
    Charged per live pass, and only once every new version is built: they are built into db.pass_staging,
    the payment is settled, and then they and the new design go live in one transaction (#5). A failed
    build or settlement leaves the passes and the saved design as they were."""
    tool, start = "update_wallet_pass_batch", time.monotonic()
    caller = caller.with_payment_argument(x402_payment)
    found = pilot.authenticate(batch_key.split(".")[0], batch_key) if isinstance(batch_key, str) else None
    if found is None or found["kind"] != "batch":
        raise PassRejected("unknown batch_key", 404)
    p = pilot.with_design(found)
    style, org = p["design_obj"].get("style", "eventTicket"), p["org"]
    caller = _with_caller_info(caller, contact_email, x402_ready, "update", style, org)
    try:
        changes = pilot.validate_changes(changes)
    except ValueError as e:
        raise PassRejected(str(e), 400) from None
    async with _pilot_lock(p["id"]):
        if pilot.expires_epoch(p) <= time.time():
            raise PassRejected("this batch's event has ended; its passes can no longer be updated", 400)
        targets = pilot.issued_rows(p["id"], live=True)
        if not targets:
            raise PassRejected("this batch has no live passes to update", 400)
        merged = pilot.merge_changes(p["design_obj"], changes)

        path = f"/api/batches/{p['id']}"

        def check_all():
            """Every pass's pass.json must build before anything is charged: that is where per-attendee
            values can fail. Cheap (nothing is signed), so it runs before the payment gate."""
            for row in targets:
                try:
                    _check_pass_json(pilot.fill(merged, row), row["serial"], row["auth_token"])
                except pass_builder.PassBuildError as e:
                    raise pass_builder.PassBuildError(f"{row['name']} ({row['ticket']}): {e}") from None

        try:
            await anyio.to_thread.run_sync(check_all)
        except pass_builder.PassBuildError as e:
            _log_failure(e, caller, "update", style, org, None, start)
            raise PassRejected(f"that change would not build: {e}; nothing was charged", 400) from None
        verified = await anyio.to_thread.run_sync(lambda: _gate(
            caller, len(targets), free_tier=False, tool=tool, path=path, action="update", style=style, org=org))
        stage, swapped = f"{p['id']}.{secrets.token_hex(8)}", False
        try:
            done, errors = await rebuild_all({**p, "design_obj": merged}, targets, caller.ip, logged=False,
                                             at_once=BATCH_BUILDS_AT_ONCE, stage=stage)
            if errors or len(done) != len(targets):
                e = errors[0] if errors else RuntimeError("a pass in this batch is no longer stored")
                built = isinstance(e, pass_builder.PassBuildError)
                _log_failure(e, caller, "update", style, org, None, start)
                raise PassRejected(f"that change would not build: {e}; nothing was charged" if built
                                   else "internal error rebuilding the passes; nothing was charged",
                                   400 if built else 500)
            try:
                receipt = await anyio.to_thread.run_sync(lambda: _settle(verified, lambda: db.discard_stage(stage)))
            except payments.PaymentError as e:
                _log(caller, style=style, organization_name=org, success=False, error="payment settlement failed",
                     duration_ms=int((time.monotonic() - start) * 1000), serial_number=None, action="update")
                raise _payment_required(caller, tool, path, str(e), len(targets), free_tier=False) from None
            try:
                replaced = await anyio.to_thread.run_sync(lambda: db.swap_stage(
                    stage, "UPDATE pilots SET design = ? WHERE id = ?", (json.dumps(merged), p["id"])))
            except Exception:
                logger.error("batch %s update was charged (%s) but its new versions could not be swapped in: %s",
                             p["id"], receipt.get("transaction"), traceback.format_exc())
                raise
            swapped = True
            if replaced != len(done):
                logger.error("batch %s update: %d passes staged, %d swapped in", p["id"], len(done), replaced)
        finally:
            if not swapped:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lambda: db.discard_stage(stage))
        notified = await push_updates(done)
    ms = int((time.monotonic() - start) * 1000) // max(len(targets), 1)
    for row in targets:
        _log(caller, style=style, organization_name=org, success=True, error=None, duration_ms=ms,
             serial_number=row["serial"], action="update", payment_tx=receipt.get("transaction"))
    return {"batch_id": p["id"], "updated": len(done), "notified_devices": notified,
            "charged": payments.price_for(len(targets)), "payment": receipt}


_BATCH_TOOL_ANNOTATIONS = dict(read_only_hint=False, idempotent_hint=False, open_world_hint=False)


@server.tool(
    title="Create Apple Wallet passes in bulk",
    annotations=ToolAnnotations(title="Create Apple Wallet passes in bulk", destructive_hint=False,
                                **_BATCH_TOOL_ANNOTATIONS),
)
async def create_wallet_pass_batch(
    ctx: Context,
    rows: list[dict],
    design: dict | None = None,
    event_end: str | None = None,
    batch_key: str | None = None,
    contact_email: str | None = None,
    x402_ready: bool | Literal["unknown"] | None = None,
    x402_payment: dict | str | None = None,
) -> dict:
    """Create one Apple Wallet pass per attendee from a single design, e.g. tickets for a guest list.

    Paid, with no free tier: each new pass costs {price} in USDC on Base via x402, charged as one
    payment for the whole call ({price} x the number of new passes). The first call is refused with
    the exact amount; retry it unchanged with the signed payment in x402_payment. Nothing is charged
    unless every pass builds. A row whose ticket is already in the batch is not charged again and gets
    the same link.

    Each pass gets a permanent https link (not the 1-hour download link of create_wallet_pass) that
    always serves the latest version, and stays installable and updatable until event_end + 7 days.
    Send the links to attendees yourself (email, WhatsApp, a form); this service sends nothing.

    Args:
        rows: up to 1000 attendees per call (5000 per batch), each {"name": str, "ticket": str,
            "type": optional str (default "General admission"), "email": optional str}. ticket is
            the unique id within the batch (and the default barcode message). Parse a CSV into
            these rows yourself.
        design: for a new batch, create_wallet_pass's parameters (style, organization_name,
            description, colors, fields, images, ...; not serial_number/updatable/contact_email)
            whose strings may contain {name} {ticket} {type} {email}, filled in per attendee.
            Defaults: style "eventTicket", barcode_message "{ticket}". Leave out with batch_key.
        event_end: for a new batch, when the event ends (ISO 8601, within a year). Leave out with batch_key.
        batch_key: to add attendees to a batch made earlier, the batch_key it returned.
        contact_email, x402_ready, x402_payment: as for create_wallet_pass (all calls are paid here).

    Returns {batch_key (new batch only: it can't be recovered, give it to the user), batch_id,
    passes: [{name, ticket, type, email, link, new}], created, reused, issued_total, charged,
    links_csv_url (all links as a CSV, also secret), updatable_until, payment}.
    """
    caller = Caller.from_mcp(ctx)
    try:
        return _mcp_result(await _create_batch(
            caller, design=design, rows=rows, event_end=event_end, batch_key=batch_key,
            contact_email=contact_email, x402_ready=x402_ready, x402_payment=x402_payment))
    except PassRejected as e:
        return _mcp_rejection(e)


@server.tool(
    title="Update Apple Wallet passes in bulk",
    annotations=ToolAnnotations(title="Update Apple Wallet passes in bulk", destructive_hint=True,
                                **_BATCH_TOOL_ANNOTATIONS),
)
async def update_wallet_pass_batch(
    ctx: Context,
    batch_key: str,
    header_fields: list[dict] | None = None,
    primary_fields: list[dict] | None = None,
    secondary_fields: list[dict] | None = None,
    auxiliary_fields: list[dict] | None = None,
    back_fields: list[dict] | None = None,
    relevant_date: str | None = None,
    locations: list[dict] | None = None,
    change_message: str | None = None,
    contact_email: str | None = None,
    x402_ready: bool | Literal["unknown"] | None = None,
    x402_payment: dict | str | None = None,
) -> dict:
    """Change every pass in a batch made with create_wallet_pass_batch (e.g. a new time or venue);
    every iPhone that added one refreshes within seconds.

    Paid: {price} per pass in the batch, as one x402 payment (refused first with the exact amount;
    retry unchanged with x402_payment). Nothing is charged if the change doesn't build.

    Each field list given replaces that list in the design (resend unchanged fields too; strings may
    use {name} {ticket} {type} {email}). change_message (e.g. "Doors now open at %@") is shown on the
    lock screen for fields whose value changed. contact_email and x402_ready are required.

    Returns {batch_id, updated, notified_devices, charged, payment}.
    """
    changes = {k: v for k, v in locals().items()
               if k not in ("ctx", "batch_key", "contact_email", "x402_ready", "x402_payment")}
    caller = Caller.from_mcp(ctx)
    try:
        return _mcp_result(await _update_batch(
            caller, batch_key=batch_key, changes=changes, contact_email=contact_email, x402_ready=x402_ready,
            x402_payment=x402_payment))
    except PassRejected as e:
        return _mcp_rejection(e)


for _tool in (create_wallet_pass_batch, update_wallet_pass_batch):
    _tool_obj = server._tool_manager.get_tool(_tool.__name__)
    _tool_obj.description = _tool_obj.description.replace("{price}", payments.PRICE)
_advertise_caller_info_as_required(create_wallet_pass_batch, update_wallet_pass_batch)


async def _batch_api_body(request: Request):
    body, err = await _pilot_body(request)
    if err:
        return None, None, err
    caller = Caller.from_api(request)
    return body, caller, None


_BATCH_CREATE_KEYS = {"rows", "design", "event_end", "contact_email", "x402_ready", "x402_payment"}
_BATCH_UPDATE_KEYS = {*pilot.UPDATE_KEYS, "contact_email", "x402_ready", "x402_payment"}


def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def _batch_api(run) -> Response:
    try:
        return _api_result(await run())
    except PassRejected as e:
        return _api_rejection(e)
    except Exception:
        logger.error("batch API failed: %s", traceback.format_exc())
        return JSONResponse({"error": "internal error"}, status_code=500)


@server.custom_route("/api/batches", methods=["POST"])
async def api_create_batch(request: Request) -> Response:
    """New batch: {design, event_end, rows, contact_email, x402_ready[, x402_payment]}."""
    body, caller, err = await _batch_api_body(request)
    if err:
        return err
    unknown = set(body) - _BATCH_CREATE_KEYS
    if unknown:
        return _pilot_error(f"unknown field(s): {', '.join(sorted(unknown))}")
    return await _batch_api(lambda: _create_batch(caller, batch_key=None, **{k: body.get(k) for k in _BATCH_CREATE_KEYS}))


@server.custom_route("/api/batches/{id}/passes", methods=["POST"])
async def api_add_to_batch(request: Request) -> Response:
    """More attendees: {rows, contact_email, x402_ready[, x402_payment]}, batch_key as Bearer."""
    key = _bearer(request)
    if not key or not key.startswith(request.path_params["id"] + "."):
        return JSONResponse({"error": "send the batch_key as 'Authorization: Bearer <batch_key>'"}, status_code=401)
    body, caller, err = await _batch_api_body(request)
    if err:
        return err
    unknown = set(body) - (_BATCH_CREATE_KEYS - {"design", "event_end"})
    if unknown:
        return _pilot_error(f"unknown field(s): {', '.join(sorted(unknown))}")
    return await _batch_api(lambda: _create_batch(caller, batch_key=key, design=None, event_end=None,
                                                  **{k: body.get(k) for k in _BATCH_CREATE_KEYS - {"design", "event_end"}}))


@server.custom_route("/api/batches/{id}", methods=["PATCH"])
async def api_update_batch(request: Request) -> Response:
    """Change every pass: {header_fields, ..., change_message, contact_email, x402_ready[, x402_payment]}."""
    key = _bearer(request)
    if not key or not key.startswith(request.path_params["id"] + "."):
        return JSONResponse({"error": "send the batch_key as 'Authorization: Bearer <batch_key>'"}, status_code=401)
    body, caller, err = await _batch_api_body(request)
    if err:
        return err
    unknown = set(body) - _BATCH_UPDATE_KEYS
    if unknown:
        return _pilot_error(f"unknown field(s): {', '.join(sorted(unknown))}")
    changes = {k: body[k] for k in pilot.UPDATE_KEYS if k in body}
    return await _batch_api(lambda: _update_batch(caller, batch_key=key, changes=changes,
                                                  contact_email=body.get("contact_email"),
                                                  x402_ready=body.get("x402_ready"),
                                                  x402_payment=body.get("x402_payment")))


@server.custom_route("/api/batches/{id}", methods=["GET"])
async def api_get_batch(request: Request) -> Response:
    """The batch and every link, batch_key as Bearer (or ?key= for a browser)."""
    key = _bearer(request) or request.query_params.get("key")
    found = pilot.authenticate(request.path_params["id"], key)
    if found is None or found["kind"] != "batch":
        return JSONResponse({"error": "unknown batch or wrong batch_key"}, status_code=404)
    p = pilot.with_design(found)
    passes = [{"name": r["name"], "ticket": r["ticket"], "type": r["type"], "email": r["email"],
               "link": pilot.pass_link(r["serial"], r["auth_token"])} for r in pilot.issued_rows(p["id"])]
    return JSONResponse({"batch_id": p["id"], "organization_name": p["org"], "description": p["event_name"],
                         "event_end": p["event_end"], "updatable_until": _iso(pilot.expires_epoch(p)),
                         "issued_total": len(passes), "max_passes": p["max_passes"], "design": p["design_obj"],
                         "passes": passes, "links_csv_url": _batch_links_csv(p, key)},
                        headers={"Cache-Control": "no-store"})



pilot.configure(_build_pkpass, _resolve_image_urls, PUBLIC_BASE_URL)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8400"))
    public_host = os.environ.get("WALLET_MCP_PUBLIC_HOST", "vps.arshw.com")
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
