"""APNs pushes that tell Wallet an updatable pass changed.

A pass push is an empty JSON payload to the device's push token, with the Pass
Type ID as the topic, authenticated with the same certificate that signs passes.
The device then calls back to the PassKit web service for the new pass. APNs needs
HTTP/2, which the venv has no client for, so this shells out to curl (the same way
pass_builder shells out to openssl for signing).
"""
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pass_builder

logger = logging.getLogger("wallet-mcp.apns")

APNS_URL = os.environ.get("WALLET_MCP_APNS_URL", "https://api.push.apple.com")


def _push_one(push_token: str) -> int:
    try:
        out = subprocess.run(
            [
                "curl", "-sS", "--http2", "--max-time", "10",
                "--cert", str(pass_builder.CERT), "--key", str(pass_builder.PASSKEY),
                "-H", f"apns-topic: {pass_builder.PASS_TYPE_IDENTIFIER}",
                "-d", "{}", "-o", "/dev/null", "-w", "%{http_code}",
                f"{APNS_URL}/3/device/{push_token}",
            ],
            capture_output=True, text=True, timeout=15,
        )
        return int(out.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return 0


def push(push_tokens: list[str]) -> tuple[int, list[str]]:
    """Push to every token. Returns (accepted count, tokens APNs says are dead)."""
    if not push_tokens:
        return 0, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(_push_one, push_tokens))
    # 410 = device unregistered, 400 = BadDeviceToken; either way the token is useless.
    dead = [t for t, c in zip(push_tokens, codes) if c in (400, 410)]
    failed = [c for c in codes if c not in (200, 400, 410)]
    if failed:
        logger.warning("APNs push failures: %s", failed)
    return codes.count(200), dead
