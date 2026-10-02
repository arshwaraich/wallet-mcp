"""Semantic tags, semantic layouts and info links for pass.json.

Key names and types come from Apple's SemanticTags reference and apple/pass-builder
(PassSemantics.swift). The two disagree on a few names; both spellings are accepted
and emitted exactly as given -- see TIME_ZONE_ALIASES.

Validation is strict on purpose: Wallet refuses a pass with a badly typed value
(it won't open), while an unknown key is silently ignored -- so a typo would
quietly do nothing. Both are reported back to the caller instead.
"""
from datetime import datetime

STRING_TAGS = {
    # event tickets
    "additionalTicketAttributes", "admissionLevel", "admissionLevelAbbreviation", "attendeeName",
    "entranceDescription", "eventName", "genre", "venueEntrance", "venueEntranceDoor",
    "venueEntranceGate", "venueEntrancePortal", "venueName", "venuePhoneNumber", "venueRegionName",
    "venueRoom",
    "awayTeamAbbreviation", "awayTeamLocation", "awayTeamName", "homeTeamAbbreviation",
    "homeTeamLocation", "homeTeamName", "leagueAbbreviation", "leagueName", "sportName",
    # boarding passes
    "airlineCode", "airlineLoungePlaceID", "boardingGroup", "boardingSequenceNumber", "boardingZone",
    "carNumber", "confirmationNumber", "departureAirportCode", "departureAirportName",
    "departureCityName", "departureGate", "departureLocationDescription", "departurePlatform",
    "departureStationName", "departureTerminal", "destinationAirportCode", "destinationAirportName",
    "destinationCityName", "destinationGate", "destinationLocationDescription", "destinationPlatform",
    "destinationStationName", "destinationTerminal", "flightCode",
    "internationalDocumentsVerifiedDeclarationName", "membershipProgramName",
    "membershipProgramNumber", "membershipProgramStatus", "priorityStatus", "securityScreening",
    "ticketFareClass", "transitProvider", "transitStatus", "transitStatusReason", "vehicleName",
    "vehicleNumber", "vehicleType",
    # Apple's docs say departure/destinationLocationTimeZone, pass-builder's model says
    # departure/destinationAirportTimeZone. Unverified which one Wallet reads.
    "departureLocationTimeZone", "destinationLocationTimeZone",
    "departureAirportTimeZone", "destinationAirportTimeZone",
}
DATE_TAGS = {
    "currentArrivalDate", "currentBoardingDate", "currentDepartureDate", "eventEndDate",
    "eventStartDate", "originalArrivalDate", "originalBoardingDate", "originalDepartureDate",
    "venueBoxOfficeOpenDate", "venueCloseDate", "venueDoorsOpenDate", "venueFanZoneOpenDate",
    "venueGatesOpenDate", "venueOpenDate", "venueParkingLotsOpenDate",
}
NUMBER_TAGS = {"duration", "flightNumber"}
BOOL_TAGS = {"internationalDocumentsAreVerified", "silenceRequested", "tailgatingAllowed"}
STRING_LIST_TAGS = {
    "albumIDs", "artistIDs", "performerNames", "playlistIDs", "loungePlaceIDs",
    "departureLocationSecurityPrograms", "destinationLocationSecurityPrograms",
    "passengerAirlineSSRs", "passengerCapabilities", "passengerEligibleSecurityPrograms",
    "passengerInformationSSRs", "passengerServiceSSRs",
}
LOCATION_TAGS = {"departureLocation", "destinationLocation", "venueLocation"}
CURRENCY_TAGS = {"balance", "totalPrice"}

EVENT_TYPES = {
    "generic": "PKEventTypeGeneric",
    "livePerformance": "PKEventTypeLivePerformance",
    "movie": "PKEventTypeMovie",
    "sports": "PKEventTypeSports",
    "conference": "PKEventTypeConference",
    "convention": "PKEventTypeConvention",
    "workshop": "PKEventTypeWorkshop",
    "socialGathering": "PKEventTypeSocialGathering",
}
PERSON_NAME_KEYS = {
    "namePrefix", "givenName", "middleName", "familyName", "nameSuffix", "nickname",
    "phoneticRepresentation",
}
SEAT_KEYS = {
    "seatDescription", "seatIdentifier", "seatNumber", "seatRow", "seatSection", "seatAisle",
    "seatLevel", "seatType", "seatSectionColor",
}
EVENT_DATE_INFO_KEYS = {"date": str, "timeZone": str, "ignoreTimeComponents": bool,
                        "unannounced": bool, "undetermined": bool}

# Wallet falls back to the classic layout if any of these is missing; we refuse instead,
# since the caller explicitly asked for the semantic layout. A tuple means "any of".
TIME_ZONE_ALIASES = {
    "departure": ("departureLocationTimeZone", "departureAirportTimeZone"),
    "destination": ("destinationLocationTimeZone", "destinationAirportTimeZone"),
}
REQUIRED_TAGS = {
    "boardingPass": [
        "airlineCode", "flightNumber", "departureAirportCode", "departureCityName",
        TIME_ZONE_ALIASES["departure"], "destinationAirportCode", "destinationCityName",
        TIME_ZONE_ALIASES["destination"], "originalArrivalDate", "originalBoardingDate",
        "originalDepartureDate", "passengerName",
    ],
    "eventTicket": ["eventName", "venueName", "venueRegionName", "venueRoom"],
}
REQUIRED_EVENT_TYPE_TAGS = {
    "PKEventTypeSports": ["awayTeamAbbreviation", "homeTeamAbbreviation"],
    "PKEventTypeLivePerformance": ["performerNames"],
}
SEMANTIC_STYLE_SCHEMES = {
    "boardingPass": ["semanticBoardingPass", "boardingPass"],
    "eventTicket": ["posterEventTicket", "eventTicket"],
}

# Top-level pass.json keys that fill the event guide (event tickets) or the airline
# and services page (boarding passes). appLaunchURL is left out: it needs an App Store
# app in associatedStoreIdentifiers.
INFO_LINK_URL_KEYS = {
    "accessibilityURL", "addOnURL", "bagPolicyURL", "merchandiseURL", "orderFoodURL",
    "parkingInformationURL", "purchaseParkingURL", "sellURL", "transferURL",
    "transitInformationURL", "contactVenueWebsite", "directionsInformationURL",
    "changeSeatURL", "entertainmentURL", "purchaseAdditionalBaggageURL",
    "purchaseLoungeAccessURL", "purchaseWifiURL", "upgradeURL", "managementURL",
    "registerServiceAnimalURL", "reportLostBagURL", "requestWheelchairURL",
    "transitProviderWebsiteURL",
}
INFO_LINK_TEXT_KEYS = {
    "contactVenueEmail", "contactVenuePhoneNumber", "transitProviderEmail",
    "transitProviderPhoneNumber",
}


class SemanticsError(ValueError):
    pass


def _check_date(where: str, value) -> None:
    if not isinstance(value, str):
        raise SemanticsError(f"{where} must be an ISO 8601 date string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SemanticsError(f"{where} must be an ISO 8601 date, got {value!r}") from None
    if parsed.tzinfo is None:
        raise SemanticsError(f"{where} needs a time zone offset, e.g. 2026-10-02T19:30+05:30, got {value!r}")


def _check_object(where: str, value, allowed: set[str], required: set[str] = frozenset()) -> None:
    if not isinstance(value, dict):
        raise SemanticsError(f"{where} must be an object, got {value!r}")
    unknown = set(value) - allowed
    if unknown:
        raise SemanticsError(f"{where} has unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}")
    missing = required - set(value)
    if missing:
        raise SemanticsError(f"{where} is missing {sorted(missing)}")


def normalize(semantics: dict, color_to_rgb) -> dict:
    """Type-check a caller's semantics dict and return it ready for pass.json.

    Values are passed through as given; the only rewrites are eventType shorthand
    ("sports" -> "PKEventTypeSports") and seat colors to Wallet's rgb() form.
    """
    if not isinstance(semantics, dict):
        raise SemanticsError(f"semantics must be an object, got {semantics!r}")
    out = {}
    for key, value in semantics.items():
        where = f"semantics.{key}"
        if key in STRING_TAGS:
            if not isinstance(value, str):
                raise SemanticsError(f"{where} must be a string, got {value!r}")
        elif key in DATE_TAGS:
            _check_date(where, value)
        elif key in NUMBER_TAGS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SemanticsError(f"{where} must be a number, got {value!r}")
        elif key in BOOL_TAGS:
            if not isinstance(value, bool):
                raise SemanticsError(f"{where} must be true or false, got {value!r}")
        elif key in STRING_LIST_TAGS:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise SemanticsError(f"{where} must be a list of strings, got {value!r}")
        elif key in LOCATION_TAGS:
            _check_object(where, value, {"latitude", "longitude", "altitude"}, {"latitude", "longitude"})
            if not all(isinstance(value[k], (int, float)) and not isinstance(value[k], bool) for k in value):
                raise SemanticsError(f"{where} coordinates must be numbers, got {value!r}")
        elif key in CURRENCY_TAGS:
            _check_object(where, value, {"amount", "currencyCode"}, {"amount", "currencyCode"})
            if not all(isinstance(v, str) for v in value.values()):
                raise SemanticsError(f'{where} amount and currencyCode must be strings, e.g. {{"amount": "12.50", "currencyCode": "INR"}}')
        elif key == "passengerName":
            _check_object(where, value, PERSON_NAME_KEYS)
            if not value or not all(isinstance(v, str) for v in value.values()):
                raise SemanticsError(f'{where} must have string name parts, e.g. {{"givenName": "Asha", "familyName": "Rao"}}')
        elif key == "eventStartDateInfo":
            _check_object(where, value, set(EVENT_DATE_INFO_KEYS))
            for k, typ in EVENT_DATE_INFO_KEYS.items():
                if k in value and not isinstance(value[k], typ):
                    raise SemanticsError(f"{where}.{k} must be a {typ.__name__}, got {value[k]!r}")
            if "date" in value:
                _check_date(f"{where}.date", value["date"])
        elif key == "eventType":
            if value in EVENT_TYPES:
                value = EVENT_TYPES[value]
            elif value not in EVENT_TYPES.values():
                raise SemanticsError(f"{where} must be one of {sorted(EVENT_TYPES)}, got {value!r}")
        elif key == "seats":
            if not isinstance(value, list):
                raise SemanticsError(f"{where} must be a list of seat objects, got {value!r}")
            seats = []
            for i, seat in enumerate(value):
                _check_object(f"{where}[{i}]", seat, SEAT_KEYS)
                if not all(isinstance(v, str) for v in seat.values()):
                    raise SemanticsError(f"{where}[{i}] values must be strings, got {seat!r}")
                seat = dict(seat)
                if "seatSectionColor" in seat:
                    seat["seatSectionColor"] = color_to_rgb(seat["seatSectionColor"])
                seats.append(seat)
            value = seats
        elif key == "wifiAccess":
            if not isinstance(value, list):
                raise SemanticsError(f"{where} must be a list of {{ssid, password}} objects, got {value!r}")
            for i, net in enumerate(value):
                _check_object(f"{where}[{i}]", net, {"ssid", "password"}, {"ssid", "password"})
                if not all(isinstance(v, str) for v in net.values()):
                    raise SemanticsError(f"{where}[{i}] ssid and password must be strings")
        else:
            raise SemanticsError(f"unknown semantic tag {key!r} -- Wallet would silently ignore it")
        out[key] = value
    return out


def check_required(style: str, semantics: dict) -> None:
    """Raise unless semantics has every tag Wallet needs for the semantic layout."""
    missing = []
    for tag in REQUIRED_TAGS[style]:
        if isinstance(tag, tuple):
            if not any(t in semantics for t in tag):
                missing.append(" or ".join(tag))
        elif tag not in semantics:
            missing.append(tag)
    for tag in REQUIRED_EVENT_TYPE_TAGS.get(semantics.get("eventType"), []) if style == "eventTicket" else []:
        if tag not in semantics:
            missing.append(tag)
    if missing:
        raise SemanticsError(
            f"semantic_layout on {style} needs these semantic tags (Wallet shows the classic layout "
            f"without them): {', '.join(missing)}"
        )


def normalize_info_links(info_links: dict) -> dict:
    if not isinstance(info_links, dict):
        raise SemanticsError(f"info_links must be an object, got {info_links!r}")
    out = {}
    for key, value in info_links.items():
        if key in INFO_LINK_URL_KEYS:
            if not isinstance(value, str) or not value.startswith(("https://", "http://")):
                raise SemanticsError(f"info_links.{key} must be an http(s) URL, got {value!r}")
        elif key in INFO_LINK_TEXT_KEYS:
            if not isinstance(value, str) or not value.strip():
                raise SemanticsError(f"info_links.{key} must be a non-empty string, got {value!r}")
        else:
            raise SemanticsError(
                f"unknown info_links key {key!r}; allowed: {sorted(INFO_LINK_URL_KEYS | INFO_LINK_TEXT_KEYS)}"
            )
        out[key] = value
    return out
