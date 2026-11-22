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
import csv
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
import credits
import db
import pass_builder
import payments
import pkpass
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
# Capacity guards, so no caller (paying or not) can take the box down. Builds are memory-heavy (each
# image URL can be 25 megapixels; pkpass.MAX_PKPASS_BYTES caps the result) and run at most 4 at once
# (the rest queue). systemd's MemoryMax/CPUQuota are the backstop.
MIN_FREE_DISK_BYTES = int(os.environ.get("WALLET_MCP_MIN_FREE_DISK_GB", 5)) * 1024 ** 3
CAPACITY_ERROR = "the service is temporarily out of capacity, try again later"
BUILD_LIMITER = anyio.CapacityLimiter(4)


def _disk_ok() -> bool:
    return shutil.disk_usage(DOWNLOAD_DIR).free >= MIN_FREE_DISK_BYTES
MAX_UPDATABLE_HOURS = 30 * 24  # updatable passes are stored for 1 hour by default, at most 30 days
DEFAULT_ICON_COLOR = pkpass.DEFAULT_ICON_COLOR

db.init()
credits.init()

server = MCPServer(
    "wallet-mcp",
    instructions=(
        "Creates signed Apple Wallet (.pkpass) passes -- boarding passes, event tickets, "
        "coupons, store cards and generic passes -- and returns an https download link "
        "valid for 1 hour. No account or API key, but every call must send the user's contact_email and "
        "x402_ready. Pricing: a call is one create or one update. "
        f"The first {db.PER_IP_DAILY_LIMIT} successful calls each day per caller (IP address or contact_email) are free (while a "
        "shared pool of 500 free calls a day lasts); every call after them costs "
        f"{payments.PRICE}. Failed calls don't count (up to 20 a day), and counts reset at 00:00 UTC"
        + (
            ". Pay by card: call buy_pass_credits to get a checkout link the user pays (packs "
            f"{credits.PACKS_TEXT}) and a credit_key; send credit_key on later calls and each paid call uses 1 "
            "credit (check_pass_credits shows the balance)"
            if credits.enabled else ""
        )
        + (
            f". {'Alternatively, agents with a crypto wallet can' if credits.enabled else 'Paid calls'} pay with x402 "
            "(USDC on Base): the refusal carries the payment requirements; retry the same call with the signed "
            'payment in the x402_payment argument, or in _meta["x402/payment"]. Guide: '
            "https://walletmcppass.com/#wallet-setup"
            if payments.enabled else ""
        )
        + ". Payment is taken only after the pass is built, so a failed build is never charged"
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
def _setup_hint(lead: str) -> dict:
    """How to keep going after the free passes: card credits first, x402 as the alternative."""
    hint = {"message": f"{lead} Every pass after the free {db.PER_IP_DAILY_LIMIT} a day is only a cent ({payments.PRICE})."}
    if credits.enabled:
        hint["message"] += (f" Tell your user they can buy pass credits by card ({credits.PACKS_TEXT}): call "
                            "buy_pass_credits and give them the checkout link.")
        hint["buy_by_card"] = "buy_pass_credits (REST: POST /api/credits)"
    if payments.enabled:
        hint["message"] += (" Agents with a crypto wallet can pay with x402 (USDC on Base) instead: "
                            "MCP clients can use @x402/mcp, other code the x402 SDKs.")
        hint["x402_guide"] = X402_GUIDE_URL
        hint["technical_steps"] = "https://walletmcppass.com/llms-full.txt"
    return hint


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
    credit_key: str | None = field(default=None, repr=False)  # prepaid card credits (credits.py)

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


    def with_payment_argument(self, payment, credit_key=None) -> "Caller":
        """The x402_payment tool argument / JSON body field. It exists because chat apps (Claude.ai,
        ChatGPT) let the model fill in arguments but not _meta or headers, so a payment signed by a
        wallet connector can only reach us this way. A _meta/header payment wins if both are sent."""
        if credit_key is not None:
            self = replace(self, credit_key=credit_key)
        if payment is None or self.payment is not None:
            return self
        return replace(self, payment=payment, x402=True)


def _log(caller: Caller, **fields) -> None:
    fields.setdefault("payment_tx", None)
    db.log_request(ip=caller.ip, source=caller.source, client=caller.client, x402=caller.x402,
                   contact_email=caller.contact_email, x402_ready=caller.x402_ready, **fields)


PUBLIC_BASE_URL = pkpass.PUBLIC_BASE_URL


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
    credit_key: str | None = None,
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
    edit_token and updatable_until when updatable=True. With x402_ready=False and few free
    passes left today, also free_passes_left_today and payment_setup (how to pay for more):
    pass that on to the user.

    Pricing: a call is one create or one update. The first {free} successful calls each day per caller (IP address
    or contact_email) are free (while a shared pool of 500 free calls a day lasts); failed calls don't
    count (up to 20 failures a day per caller), and counts reset at 00:00 UTC.
    Every call after them costs {price}. Pay by card: buy_pass_credits gives a checkout link for the
    user and a credit_key; send credit_key and each paid call uses 1 credit. Or, with a crypto
    wallet, x402 (USDC on Base): the refusal carries the payment requirements; have the user's
    wallet tool sign them and retry the same call with the result as x402_payment. A paid result
    includes the receipt. Payment is taken only after the pass is built, so a failed build is
    never charged.

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
        credit_key: the user's prepaid card credits from buy_pass_credits (starts with wpc_). Only
            spent on paid calls (past the free passes), 1 credit per pass, and only if the pass builds.

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
    credit_key: str | None = None,
) -> dict:
    """Change a pass made with create_wallet_pass(updatable=True); every iPhone that added it refreshes.

    Takes the serial_number and edit_token returned when the pass was created, the required
    contact_email and x402_ready, plus any of create_wallet_pass's parameters (x402_payment and credit_key included), with the same meanings. Each parameter given replaces the
    stored value completely (a fields list replaces the whole list, so resend unchanged fields
    too); parameters left out keep their current values. Clear a text value with "" and a list
    with []. To cancel a pass, set voided=True: Wallet marks it void on every iPhone.

    Wallet asks the server for the new version as soon as Apple delivers the push, usually within
    seconds. A field's changeMessage is shown on the lock screen when that field's value changes.

    Only works until the pass's updatable_until time (set by updatable_hours at creation).

    Returns {download_url, expires_in_seconds, serial_number, pass_type_identifier,
    notified_devices, updatable_until}. notified_devices counts the iPhones the push was sent to, not ones that
    have already refreshed. The download_url (valid 1 hour) is only needed to add the pass to a
    new device. With x402_ready=False the result may also have payment_setup, as for create_wallet_pass.
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
    if "payment" not in result or result["payment"].get("method") == "pass_credits":
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
_CALL_PARAMS = ("serial_number", "updatable", "updatable_hours", "contact_email", "x402_ready", "x402_payment",
                "credit_key")


def _create_pass(caller: Caller, request: dict) -> dict:
    """Rate-limit, build, sign and log one pass. request holds every create_wallet_pass parameter
    (defaults filled in), as the tool gets them or as _PassRequest validates them."""
    style, organization_name = request["style"], request["organization_name"]
    updatable, updatable_hours = request["updatable"], request["updatable_hours"]
    params = {k: v for k, v in request.items() if k not in _CALL_PARAMS}
    start = time.monotonic()
    caller = caller.with_payment_argument(request["x402_payment"], request["credit_key"])
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
        params = pkpass.resolve_image_urls(params)
        auth_token = secrets.token_urlsafe(24) if updatable else None
        pkpass_bytes = pkpass.build(params, serial, auth_token)
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
            db.store_pass(serial, auth_token, db.hash_token(edit_token), json.dumps(params), pkpass_bytes,
                          int(time.time()), expires)
            result["edit_token"] = edit_token
            result["updatable_until"] = db.iso(expires)
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
    _add_payment_hint(result, caller, paid=bool(receipt))
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


def _add_payment_hint(result: dict, caller: Caller, paid: bool = False) -> None:
    """Called after the successful call is logged, so the count includes it."""
    if caller.x402_ready is not False or not (payments.enabled or credits.enabled) or paid or caller.credit_key:
        return
    left = db.free_remaining(caller.ip, caller.contact_email)
    if not 0 <= left <= HINT_WHEN_LEFT:
        return
    lead = ("That was the last free pass today." if left == 0 else
            f"{left} free pass{'' if left == 1 else 'es'} left today.")
    result["free_passes_left_today"] = left
    result["payment_setup"] = _setup_hint(lead)


def _is_placeholder_email(address: str) -> bool:
    domain = address.rpartition("@")[2].lower()
    return domain in PLACEHOLDER_EMAIL_DOMAINS or domain.endswith(PLACEHOLDER_EMAIL_TLDS)


def _update_pass(caller: Caller, serial_number: str, edit_token: str, changes: dict) -> dict:
    """Merge `changes` into a stored updatable pass, re-sign it, and push it to installed devices."""
    start = time.monotonic()
    row = db.get_pass(serial_number)
    if row is None or not secrets.compare_digest(row["edit_hash"], db.hash_token(edit_token)):
        # One message for both cases, so serials can't be probed without their token.
        raise PassRejected(
            "unknown serial_number, wrong edit_token, or the pass's update window (updatable_hours) has ended", 404)
    contact_email, x402_ready = changes.pop("contact_email"), changes.pop("x402_ready")
    caller = caller.with_payment_argument(changes.pop("x402_payment", None), changes.pop("credit_key", None))
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
        params.update(pkpass.resolve_image_urls(changes))
        pkpass_bytes = pkpass.build(params, serial_number, row["auth_token"])
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
        "updatable_until": db.iso(row["expires"]),
    }
    if receipt:
        result["payment"] = receipt
    _add_payment_hint(result, caller, paid=bool(receipt))
    return result


# --- The payment gate. Order: capacity, the failure limit, the free daily limit, the paid daily cap, then
# the caller's card credits (held) or x402 payment (verified), neither charged yet. The work happens next, and _settle charges only once every pass is built
# and stored. Every refusal is logged with refused=1; the refusal texts are matched by the usage watcher, so
# they must not change. If the facilitator can't be reached to quote a price, the call gets a 503 (the
# server's problem, not the caller's: retry later). ---

def _gate(caller: Caller, count: int, *, free_tier: bool, tool: str, path: str, action: str, style: str,
          org: str):
    """Limits + payment for `count` passes. Returns the verified (not yet charged) payment, or None for a
    free call; otherwise logs the refusal and raises PassRejected. tool/path name the MCP tool and REST
    route the x402 resource points at."""
    if not free_tier and not (payments.enabled or credits.enabled):
        _refuse(caller, action, style, org, "paid-only calls need payments, which are not set up here", 503)
    if not _disk_ok():
        logger.error("refusing passes: less than %d GB of disk free", MIN_FREE_DISK_BYTES // 1024 ** 3)
        _refuse(caller, action, style, org, "server is low on disk space",
                PassRejected(CAPACITY_ERROR, 503))
    fail = db.check_fail_limit(caller.ip, caller.contact_email)
    if fail:
        _refuse(caller, action, style, org, fail, 429)
    reason = None
    if free_tier:
        reason = db.check_rate_limit(caller.ip, db.is_exempt(caller.contact_email), caller.contact_email)
        if reason is None:
            return None
        if not payments.enabled and not credits.enabled:
            _refuse(caller, action, style, org, reason, 429)
    over = db.check_paid_limit(caller.ip, count)
    if over:
        _refuse(caller, action, style, org, over, 429)
    if caller.credit_key is not None and credits.enabled and caller.payment is None:
        try:
            return credits.hold(caller.credit_key, count, action)
        except credits.CreditError as e:
            reason = f"{reason}; {e}" if reason else str(e)
    if caller.payment is not None and payments.enabled:
        try:
            return payments.verify(caller.payment, count)
        except payments.PaymentError as e:
            reason = str(e)
    if not payments.enabled:  # card credits only
        _refuse(caller, action, style, org, reason, PassRejected(f"{reason}. {_card_offer()}", 402))
    _refuse(caller, action, style, org, reason, _payment_required(caller, tool, path, reason, count, free_tier))


def _refuse(caller: Caller, action: str, style: str, org: str, error: str, rejection: "int | PassRejected"):
    """Log a refused call (refused=1: it counts toward no limit) and raise the rejection; an int status
    means a plain PassRejected with the logged text."""
    _log(caller, style=style, organization_name=org, success=False, error=error, duration_ms=0,
         serial_number=None, action=action, refused=True)
    raise rejection if isinstance(rejection, PassRejected) else PassRejected(error, rejection)


def _card_offer() -> str:
    """How to pay by card instead of x402, appended to paid refusals when credits are on."""
    if not credits.enabled:
        return ""
    return (f"Pay by card: call buy_pass_credits (REST: POST /api/credits) with a pack "
            f"({credits.PACKS_TEXT}), give your user the checkout_url to pay, then retry this exact call with the "
            "credit_key it returned (1 credit per pass). Tell the user to keep the credit_key: it works in any chat.")


def _payment_required(caller: Caller, tool: str, path: str, reason: str, count: int = 1,
                      free_tier: bool = True) -> PassRejected:
    """A 402 offering to do the call for the price of `count` passes; a 503 if the facilitator can't be
    reached to say what to pay."""
    if caller.source == "mcp":
        resource, how = f"mcp://tool/{tool}", "the signed payment as the x402_payment argument"
    else:
        resource, how = f"{PUBLIC_BASE_URL}{path}", "a PAYMENT-SIGNATURE header or an x402_payment body field"
    message = (f"{reason}. The first {db.PER_IP_DAILY_LIMIT} passes each day are free; every pass after them is "
               f"only a cent ({payments.PRICE}).")
    if credits.enabled:
        message += f" {_card_offer()}"
    if payments.enabled:
        message += (f" {'Or, if' if credits.enabled else 'If'} this agent has a crypto wallet, pay with x402 (USDC on "
                    f"Base): retry this exact call with {how}. Wallet setup: {X402_GUIDE_URL}")
    if not payments.enabled:
        return PassRejected(message, 402)
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
    if isinstance(verified, credits.Hold):
        try:
            return credits.spend(verified)
        except credits.CreditError as e:
            undo()
            raise payments.PaymentError(str(e)) from None
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


# pkpass.py builds passes; its image list must stay the tool's image parameters.
assert pkpass.IMAGE_PARAMS == [n for n in inspect.signature(create_wallet_pass).parameters if n.endswith("_png_b64")]


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
_create_tool = server._tool_manager.get_tool("create_wallet_pass")
_create_tool.description = _create_tool.description.replace("{free}", str(db.PER_IP_DAILY_LIMIT)).replace(
    "{price}", payments.PRICE)


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
    rows = (await anyio.to_thread.run_sync(lambda: db.request_log(limit=0, **_log_filters(request))))["rows"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=db.LOG_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"content-disposition": 'attachment; filename="wallet-mcp-requests.csv"'})


# --- REST helpers shared by every JSON route ---

# Base64 images travel in the /api/passes bodies. nginx lets 1 MB through there today; the app's cap sits
# above that so it never refuses a request nginx would pass.
BODY_LIMIT = 3 * 1024 * 1024


def _error(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


async def _json_body(request: Request, limit: int = BODY_LIMIT):
    """(body dict, None) or (None, error response): a JSON object of at most limit bytes."""
    raw = await request.body()
    if len(raw) > limit:
        return None, _error("request body too large", 413)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _error("request body must be a JSON object")
    if not isinstance(body, dict):
        return None, _error("request body must be a JSON object")
    return body, None


def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _reject_unknown(body: dict, allowed) -> JSONResponse | None:
    unknown = set(body) - set(allowed)
    return _error(f"unknown field(s): {', '.join(sorted(unknown))}") if unknown else None


# The REST body accepts exactly the tool's parameters, validated with the same
# types, so the API and the MCP tool can't drift apart.
_PassRequest = create_model("PassRequest", __config__=ConfigDict(extra="forbid"), **_tool_fields(create_wallet_pass))


@server.custom_route("/api/passes", methods=["POST"])
async def api_create_pass(request: Request) -> Response:
    body, err = await _json_body(request)
    if err:
        return err
    try:
        params = _PassRequest.model_validate(body).model_dump()
    except ValidationError as e:
        return _error("invalid request", details=_validation_details(e))
    try:
        caller = Caller.from_api(request)
        return _api_result(await anyio.to_thread.run_sync(lambda: _create_pass(caller, params), limiter=BUILD_LIMITER))
    except PassRejected as e:
        return _api_rejection(e)
    except Exception:
        return _error("internal error building the pass", 500)


_UpdateRequest = create_model("UpdateRequest", __config__=ConfigDict(extra="forbid"),
                              **_tool_fields(update_wallet_pass, exclude=("serial_number", "edit_token")))


@server.custom_route("/api/passes/{serial}", methods=["PATCH"])
async def api_update_pass(request: Request) -> Response:
    """Same as the update_wallet_pass tool; the edit_token goes in "Authorization: Bearer <token>"."""
    edit_token = _bearer(request)
    if not edit_token:
        return _error("send the pass's edit_token as 'Authorization: Bearer <edit_token>'", 401)
    body, err = await _json_body(request)
    if err:
        return err
    try:
        changes = _UpdateRequest.model_validate(body).model_dump()
    except ValidationError as e:
        return _error("invalid request", details=_validation_details(e))
    serial, caller = request.path_params["serial"], Caller.from_api(request)
    try:
        return _api_result(await anyio.to_thread.run_sync(
            lambda: _update_pass(caller, serial, edit_token, changes), limiter=BUILD_LIMITER))
    except PassRejected as e:
        return _api_rejection(e)
    except Exception:
        return _error("internal error building the pass", 500)


def _api_result(result: dict) -> JSONResponse:
    headers = {"PAYMENT-RESPONSE": payments.encode_header(result["payment"])} if "payment" in result else None
    return JSONResponse(result, headers=headers)


def _api_rejection(e: PassRejected) -> JSONResponse:
    """x402's HTTP transport: a 402 with the PaymentRequired body, also base64'd in PAYMENT-REQUIRED."""
    if e.payment_required is None:
        return _error(e.message, e.status)
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


# --- Card payments: prepaid pass credits through Dodo Payments (credits.py) ---

def _buy_credits(caller: Caller, pack, contact_email, credit_key) -> dict:
    if not credits.enabled:
        raise PassRejected("card payments are not set up on this server", 503)
    if not isinstance(contact_email, str) or not EMAIL_RE.fullmatch(contact_email) or _is_placeholder_email(contact_email):
        raise PassRejected("contact_email must be the user's real email address (Dodo sends the receipt there)", 400)
    try:
        out = credits.start_checkout(str(pack), contact_email, caller.ip, credit_key)
    except credits.CreditError as e:
        raise PassRejected(str(e), 400) from None
    except Exception:
        logger.error("dodo checkout failed: %s", traceback.format_exc())
        raise PassRejected("the card checkout is unavailable right now; try again later", 503) from None
    out["next_steps"] = (
        "Give the user checkout_url to pay by card (Dodo Payments, our merchant of record). Credits appear on the "
        "credit_key within a minute of payment; check with check_pass_credits. Then send credit_key with "
        "create_wallet_pass / update_wallet_pass: each pass past the free ones uses 1 credit."
        + (" Tell the user to save the credit_key somewhere safe: it is shown only now, can't be recovered, "
           "and works from any chat or app." if "credit_key" in out else ""))
    logger.info("checkout started: pack %s from %s", pack, caller.ip)
    return out


@server.tool(
    title="Buy Apple Wallet pass credits by card",
    annotations=ToolAnnotations(title="Buy Apple Wallet pass credits by card", read_only_hint=False,
                                destructive_hint=False, idempotent_hint=False, open_world_hint=True),
)
async def buy_pass_credits(ctx: Context, pack: Literal["100", "500", "2000"], contact_email: str,
                           credit_key: str | None = None) -> dict:
    """Start a card payment for prepaid pass credits, for users without an x402 crypto wallet.

    Packs: "100" (100 passes for $1), "500" (500 for $5), "2000" (2000 for $20). Nothing is charged
    by this call: it returns a checkout_url that the user opens and pays themselves. Only call it
    when the user asked to pay by card or agreed to buy a pack.

    Args:
        pack: "100", "500" or "2000".
        contact_email: the user's email; the checkout and receipt use it. Ask the user for it.
        credit_key: to top up an existing credit_key; leave out to get a new one.

    Returns {credit_key (new keys only: shown once, give it to the user to keep), checkout_url,
    pack, credits_left, next_steps}. Credits are added within a minute of payment; then pass
    credit_key to create_wallet_pass or update_wallet_pass.
    """
    caller = Caller.from_mcp(ctx)
    try:
        return await anyio.to_thread.run_sync(lambda: _buy_credits(caller, pack, contact_email, credit_key))
    except PassRejected as e:
        raise ToolError(e.message) from None


@server.tool(
    title="Check Apple Wallet pass credits",
    annotations=ToolAnnotations(title="Check Apple Wallet pass credits", read_only_hint=True,
                                destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
async def check_pass_credits(ctx: Context, credit_key: str) -> dict:
    """Credits left on a credit_key from buy_pass_credits, and its recent purchases
    (status pending until the card payment succeeds, then paid).

    Returns {credits_left, purchases: [{pack, passes, status, created, paid_at}]}.
    """
    info = credits.account(credit_key)
    if info is None:
        raise ToolError("unknown credit_key")
    return info


@server.custom_route("/api/credits", methods=["POST"])
async def api_buy_credits(request: Request) -> Response:
    """{pack, contact_email[, credit_key]} -> a Dodo checkout_url (and a new credit_key)."""
    body, err = await _json_body(request)
    if err or (err := _reject_unknown(body, {"pack", "contact_email", "credit_key"})):
        return err
    caller = Caller.from_api(request)
    try:
        return JSONResponse(await anyio.to_thread.run_sync(
            lambda: _buy_credits(caller, body.get("pack"), body.get("contact_email"), body.get("credit_key"))),
            headers={"Cache-Control": "no-store"})
    except PassRejected as e:
        return _error(e.message, e.status)


@server.custom_route("/api/credits", methods=["GET"])
async def api_check_credits(request: Request) -> Response:
    """Balance of the credit_key sent as 'Authorization: Bearer <credit_key>'."""
    info = credits.account(_bearer(request) or "")
    if info is None:
        return _error("send a valid credit_key as 'Authorization: Bearer <credit_key>'", 401)
    return JSONResponse(info, headers={"Cache-Control": "no-store"})


@server.custom_route("/api/dodo/webhook", methods=["POST"])
async def dodo_webhook(request: Request) -> Response:
    """Dodo Payments events (Standard Webhooks signed). 2xx tells Dodo to stop retrying."""
    if not credits.WEBHOOK_KEY:
        return _error("not configured", 503)
    body = await request.body()
    if len(body) > 1_000_000:
        return _error("too large", 413)
    try:
        event = credits.verify_webhook(request.headers, body)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("dodo webhook rejected: %s", e)
        return _error("invalid signature", 401)
    try:
        outcome = await anyio.to_thread.run_sync(lambda: credits.handle_event(event))
    except Exception:
        logger.error("dodo webhook failed (Dodo will retry): %s", traceback.format_exc())
        return _error("temporary failure", 500)
    logger.info("dodo webhook %s: %s", event.get("type"), outcome)
    return JSONResponse({"ok": True, "outcome": outcome})

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
