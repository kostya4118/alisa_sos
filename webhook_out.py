"""Outbound webhook fired when an SOS is sent.

Each owner may set a personal ``sos_webhook_url``. On SOS the bot POSTs a
small JSON payload there — so the owner can wire up anything on their side
(Pushcut → iPhone Shortcut for a call/SMS, IFTTT, n8n, Home Assistant, a
custom script, …). Fire-and-forget: a failing or slow webhook never blocks
or breaks SOS delivery.
"""

import ipaddress
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0


def is_allowed_url(url: str) -> bool:
    """Basic validation + SSRF guard (reject loopback/private/link-local)."""
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost", "ip6-localhost", "metadata.google.internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname — allowed (we don't pre-resolve)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def fire(url: str, payload: dict) -> None:
    """POST payload to url. Never raises."""
    if not is_allowed_url(url):
        logger.warning("SOS webhook URL rejected: %r", url)
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.post(url, json=payload)
        logger.info("SOS webhook %s → %s", url, resp.status_code)
    except Exception:
        logger.exception("SOS webhook failed for %s", url)
