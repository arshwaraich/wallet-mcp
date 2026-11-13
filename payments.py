"""x402 payments for calls past the free per-IP daily limit.

Off unless WALLET_MCP_X402_PAY_TO is set. A caller over the limit gets the price instead of a
plain refusal, and can retry the same call with a signed USDC transfer: in the MCP request's
_meta["x402/payment"], or in a PAYMENT-SIGNATURE (or legacy X-PAYMENT) header on the REST API.
The facilitator checks that signature (verify) and submits the transfer on-chain (settle), so
this process never holds a key or touches the chain; the USDC goes straight to PAY_TO.

Settling happens after the pass is built but before anything is handed over or stored, so a
failed build costs the caller nothing and a failed settlement gets them nothing.
"""
import base64
import decimal
import json
import logging
import os
import threading

logger = logging.getLogger("wallet-mcp.payments")

PAY_TO = os.environ.get("WALLET_MCP_X402_PAY_TO", "")
# PayAI settles Base mainnet without an account. Coinbase's CDP facilitator
# (https://api.cdp.coinbase.com/platform/v2/x402) also needs CDP API-key auth headers,
# which isn't wired in yet.
FACILITATOR_URL = os.environ.get("WALLET_MCP_X402_FACILITATOR", "https://facilitator.payai.network")
NETWORK = os.environ.get("WALLET_MCP_X402_NETWORK", "eip155:8453")  # Base mainnet
PRICE = os.environ.get("WALLET_MCP_X402_PRICE", "$0.01")

enabled = bool(PAY_TO)

MCP_RESPONSE_META_KEY = "x402/payment-response"  # where a paid tool result carries its receipt


UNAVAILABLE = "payments are unavailable right now; try again later"  # the facilitator can't be reached


class PaymentError(Exception):
    """The payment was missing, invalid, or didn't settle; the caller can retry with a new one."""


_lock = threading.Lock()
_server = None
_requirements: dict = {}  # number of passes -> the x402 requirements for that many


def price_for(count: int) -> str:
    """The price of `count` passes as a "$" money string, e.g. 37 -> "$0.37"."""
    return f"${decimal.Decimal(PRICE.lstrip('$')) * count}"


def _resource_server(count: int = 1):
    """The x402 resource server and the requirements for `count` passes. The server is set up on
    first use because it asks the facilitator which networks it supports."""
    global _server
    with _lock:
        if _server is None:
            from x402 import x402ResourceServerSync
            from x402.http import HTTPFacilitatorClientSync
            from x402.mechanisms.evm.exact import ExactEvmServerScheme

            server = x402ResourceServerSync(HTTPFacilitatorClientSync({"url": FACILITATOR_URL}))
            server.register(NETWORK, ExactEvmServerScheme())
            server.initialize()
            _server = server
        if count not in _requirements:
            from x402.schemas import ResourceConfig

            if len(_requirements) > 2000:
                _requirements.clear()
            _requirements[count] = _server.build_payment_requirements(
                ResourceConfig(scheme="exact", pay_to=PAY_TO, price=price_for(count), network=NETWORK)
            )
    return _server, _requirements[count]


def payment_required(resource_url: str, message: str, count: int = 1) -> dict:
    """An x402 v2 PaymentRequired body: the price of `count` passes, the network and where to pay."""
    from x402.schemas import ResourceInfo

    server, requirements = _resource_server(count)
    what = "one Wallet pass" if count == 1 else f"{count} Wallet passes at {PRICE} each"
    body = server.create_payment_required_response(
        requirements, ResourceInfo(url=resource_url, description=f"{what}, {price_for(count)}"),
        message,
    )
    return body.model_dump(by_alias=True, exclude_none=True)


def encode_header(body: dict) -> str:
    return base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


def _parse(raw):
    """A PaymentPayload from the MCP meta value (dict or JSON string) or a base64 header."""
    from x402.schemas import PaymentPayload

    try:
        if isinstance(raw, str):
            text = raw.strip()
            raw = json.loads(text if text.startswith("{") else base64.b64decode(text))
        return PaymentPayload.model_validate(raw)
    except Exception:
        raise PaymentError("could not read the x402 payment: expected an x402 v2 PaymentPayload") from None


def verify(raw, count: int = 1):
    """Check a payment for `count` passes with the facilitator; returns what settle() needs.
    Nothing is charged yet."""
    payload = _parse(raw)
    try:
        server, requirements = _resource_server(count)
    except Exception as e:
        logger.error("x402 facilitator setup failed: %r", e)
        raise PaymentError(UNAVAILABLE) from None
    matched = server.find_matching_requirements(requirements, payload)
    if matched is None:
        raise PaymentError(f"the payment doesn't match the price: {price_for(count)} in USDC on {NETWORK} to {PAY_TO}")
    try:
        result = server.verify_payment(payload, matched)
    except Exception as e:
        logger.error("x402 verify raised: %r", e)
        raise PaymentError("payment verification failed: the facilitator didn't accept it") from None
    if not result.is_valid:
        # Facilitators can return a whole contract-call dump; the first paragraph is the reason.
        reason = (result.invalid_message or result.invalid_reason or "rejected by the facilitator").split("\n\n")[0]
        raise PaymentError(f"payment verification failed: {reason}")
    return payload, matched


def settle(verified) -> dict:
    """Submit the verified payment on-chain; returns the x402 SettleResponse as a dict."""
    payload, requirements = verified
    try:
        result = _resource_server()[0].settle_payment(payload, requirements)
    except Exception as e:
        logger.error("x402 settle raised: %r", e)
        raise PaymentError("payment settlement failed; you were not charged") from None
    if not result.success:
        reason = result.error_message or result.error_reason or "unknown error"
        raise PaymentError(f"payment settlement failed: {reason}; you were not charged")
    return result.model_dump(by_alias=True, exclude_none=True)
