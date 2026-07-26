"""Capture the Arkose data-exchange blob off the wire.

Sites that gate Arkose per session hand the browser a one-shot blob and expect
it back with the solve request. Figma sends it in the response to
``POST /api/arkose/on_for_user``; without it 2captcha answers ERROR_DATA and
CapSolver rejects the task outright, so no public key alone is enough.

The blob only exists in a network response body, which the agent cannot read
from a DOM snapshot. Arming a listener on demand does not work either — by the
time anyone thinks to ask, the request has already gone out with the click that
submitted the form. So capture runs passively: a response listener is attached
to the browser context when the pooled client connects, and the newest blob per
CDP endpoint is kept for the tool to collect.

The blob is single-use. :func:`take` hands it over and forgets it, so the same
blob is never submitted twice.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Only these responses are worth opening. Reading every body would mean pulling
# each page asset through the CDP channel.
_URL_HINT = re.compile(r"arkose|on_for_user|funcaptcha", re.I)
_BLOB_KEYS = ("data_exchange_blob", "dataExchangeBlob", "blob", "dataExchange")
_MAX_BLOB_CHARS = 200_000

_lock = threading.Lock()
_blobs: Dict[str, Dict[str, Any]] = {}


def _now_ms() -> float:
    return time.time() * 1000.0


def remember(cdp_url: str, blob: str, source_url: str = "") -> None:
    """Store the newest blob seen for this profile."""
    text = str(blob or "").strip()
    if not text or len(text) > _MAX_BLOB_CHARS:
        return
    with _lock:
        _blobs[str(cdp_url or "")] = {
            "blob": text,
            "source_url": source_url,
            "seen_at_ms": _now_ms(),
        }
    logger.info("Arkose blob captured from %s (%d chars)", source_url or "?", len(text))


def peek(cdp_url: str) -> Optional[Dict[str, Any]]:
    with _lock:
        found = _blobs.get(str(cdp_url or ""))
        return dict(found) if found else None


def take(cdp_url: str) -> Optional[Dict[str, Any]]:
    """Return the blob and forget it — the pair blob+token is single-use."""
    with _lock:
        found = _blobs.pop(str(cdp_url or ""), None)
        return dict(found) if found else None


def clear(cdp_url: str) -> None:
    with _lock:
        _blobs.pop(str(cdp_url or ""), None)


def _extract(payload: Any) -> str:
    """Find a blob anywhere in a decoded JSON body."""
    if isinstance(payload, dict):
        for key in _BLOB_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _extract(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _extract(item)
            if found:
                return found
    return ""


async def _on_response(response: Any, cdp_url: str) -> None:
    """Response hook. Never raises — a capture miss must not break browsing."""
    try:
        url = str(getattr(response, "url", "") or "")
        if not _URL_HINT.search(url):
            return
        try:
            payload = await response.json()
        except Exception:  # noqa: BLE001
            return
        blob = _extract(payload)
        if blob:
            remember(cdp_url, blob, url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Arkose capture skipped a response: %s", exc)


def install_capture(context: Any, cdp_url: str) -> bool:
    """Attach the listener to a browser context once.

    Listening on the context rather than a single page means a challenge that
    opens in another tab is still captured.
    """
    if context is None:
        return False
    if getattr(context, "_cloak_arkose_capture", False):
        return False
    try:
        context.on(
            "response",
            lambda response: asyncio.ensure_future(_on_response(response, cdp_url)),
        )
        setattr(context, "_cloak_arkose_capture", True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not attach the Arkose capture listener: %s", exc)
        return False
    logger.info("Arkose blob capture armed for %s", cdp_url)
    return True
