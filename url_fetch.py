"""
SSRF hardened URL fetcher.

Fetching a user supplied URL on the server is a Server Side Request Forgery
risk. Without controls, a caller can point the URL at cloud metadata endpoints
or internal services and read secrets. This module blocks private, loopback,
link local, and reserved addresses, revalidates every redirect hop, caps the
download size, and supports an optional domain allowlist.

Set URL_FETCH_ALLOWLIST to the host or hosts you actually pull documents from
(for example your storage bucket). The allowlist is the strongest control here,
because IP based blocking alone cannot fully close DNS rebinding.
"""

import os
import socket
import ipaddress
import logging
from urllib.parse import urlparse

import requests

log = logging.getLogger("markitdown-api.url")

MAX_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
FETCH_TIMEOUT = int(os.getenv("URL_FETCH_TIMEOUT_SECONDS", "120"))
MAX_REDIRECTS = int(os.getenv("URL_FETCH_MAX_REDIRECTS", "3"))
ALLOWLIST = [
    h.strip().lower()
    for h in os.getenv("URL_FETCH_ALLOWLIST", "").split(",")
    if h.strip()
]

# Extra ranges not always flagged by ipaddress helpers.
_EXTRA_BLOCKED = [
    ipaddress.ip_network("100.64.0.0/10"),   # carrier grade NAT
    ipaddress.ip_network("0.0.0.0/8"),       # this network
    ipaddress.ip_network("::ffff:0:0/96"),   # IPv4 mapped IPv6
]


class FetchError(Exception):
    """Raised when a URL is rejected or cannot be fetched safely."""


def _host_allowed(host: str) -> bool:
    if not ALLOWLIST:
        return True
    host = host.lower()
    return any(host == a or host.endswith("." + a) for a in ALLOWLIST)


def _ip_blocked(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local      # covers 169.254.169.254 metadata
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return any(ip in net for net in _EXTRA_BLOCKED)


def _validate(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError("Only http and https URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise FetchError("URL has no host.")
    if not _host_allowed(host):
        raise FetchError("Host is not on the allowlist.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError("Could not resolve host.") from e

    for info in infos:
        ip = info[4][0]
        if _ip_blocked(ip):
            raise FetchError("URL resolves to a blocked address.")
    return host


def fetch(url: str) -> tuple[bytes, str, str | None]:
    """
    Fetch a URL safely. Returns (data, filename, content_type).
    Follows redirects manually, revalidating each hop.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate(current)
        resp = requests.get(
            current,
            stream=True,
            timeout=FETCH_TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": "markitdown-api"},
        )
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                raise FetchError("Redirect without a location.")
            current = requests.compat.urljoin(current, loc)
            continue

        if resp.status_code != 200:
            resp.close()
            raise FetchError(f"Upstream returned status {resp.status_code}.")

        # Stream with a hard size cap. Do not trust Content-Length alone.
        buf = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            buf.extend(chunk)
            if len(buf) > MAX_BYTES:
                resp.close()
                raise FetchError(f"File exceeds {MAX_BYTES} bytes.")
        resp.close()

        path = urlparse(current).path
        filename = path.rsplit("/", 1)[-1] or "download"
        return bytes(buf), filename, resp.headers.get("Content-Type")

    raise FetchError("Too many redirects.")
