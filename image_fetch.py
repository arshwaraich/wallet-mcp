"""Fetches caller-supplied https image URLs for the *_png_b64 parameters.

The server makes the request, so it must not become a way to reach internal
services: only https, every address the host resolves to must be public, and the
connection goes to the checked address (not a fresh DNS lookup), so a rebinding
DNS answer can't swap in a private one. Redirects are re-checked hop by hop.
"""
import http.client
import io
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

from PIL import Image

from pass_builder import PassBuildError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10
USER_AGENT = "wallet-mcp image fetch (+https://walletmcppass.com)"


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pre-resolved IP, with SNI and certificate checks against the hostname."""

    def __init__(self, host: str, port: int, ip: str):
        super().__init__(host, port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _public_address(host: str, port: int, field: str) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise PassBuildError(f"{field}: could not resolve host {host!r}") from None
    addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addrs or any(not a.is_global for a in addrs):
        raise PassBuildError(f"{field}: {host!r} is not a public internet host")
    addrs.sort(key=lambda a: a.version)  # IPv4 first; this VPS's IPv6 egress is unreliable
    return str(addrs[0])


def _download(url: str, field: str) -> bytes:
    deadline = time.monotonic() + TIMEOUT_SECONDS * 2
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise PassBuildError(f"{field}: only https:// image URLs are accepted, got {url[:100]!r}")
        port = parts.port or 443
        conn = _PinnedHTTPSConnection(parts.hostname, port, _public_address(parts.hostname, port, field))
        try:
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                url = urljoin(url, resp.getheader("Location"))
                continue
            if resp.status != 200:
                raise PassBuildError(f"{field}: fetching the image URL returned HTTP {resp.status}")
            chunks, size = [], 0
            while chunk := resp.read(65536):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise PassBuildError(f"{field}: image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
                if time.monotonic() > deadline:
                    raise PassBuildError(f"{field}: image download took too long")
                chunks.append(chunk)
            return b"".join(chunks)
        except (OSError, http.client.HTTPException) as e:
            raise PassBuildError(f"{field}: could not fetch the image URL ({e.__class__.__name__})") from None
        finally:
            conn.close()
    raise PassBuildError(f"{field}: too many redirects")


def fetch_png(url: str, field: str) -> bytes:
    """Download an https image and return it as PNG bytes (JPEG, WebP, GIF etc. are converted)."""
    raw = _download(url, field)
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise PassBuildError(f"{field}: image is {img.width}x{img.height}, too large")
            if img.format == "PNG":
                img.verify()
                return raw
            img.load()
            if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                img = img.convert("RGBA")
            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
    except PassBuildError:
        raise
    except Exception:
        raise PassBuildError(f"{field}: the URL did not return a readable image") from None
