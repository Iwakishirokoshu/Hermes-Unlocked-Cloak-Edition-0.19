"""Per-profile Playwright-client pool.

When our overridden `browser_click` / `browser_type` / etc. fire, they need
an async Playwright client attached to the manager-provisioned CDP URL of
the active profile. We DON'T want to reconnect for every tool call —
that:
  - costs 100-300ms per click (CDP handshake),
  - resets the in-process cursor position (cloakbrowser tracks cursor on
    the patched Browser object), so the next click would teleport from
    (0,0) instead of where the previous click left off.

This module keeps a dict ``{cdp_url: PooledClient}`` shared across the
worker process. ``PooledClient.get(cdp_url)`` is the only entry point —
it lazy-connects on first use, patches the browser via
``cloakbrowser.human.patch_browser_async`` (which now uses our pydoll math
because the sys.modules hook ran at plugin import time), and caches.

Concurrency: one asyncio Lock per ``cdp_url`` key, so simultaneous tool
calls against the same profile serialize through the same patched
Browser/Page (which is what cloakbrowser's wrappers expect anyway —
they assume single-threaded access per Browser).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)
from agent.redact import redact_cdp_url

_action_locks: Dict[str, threading.Lock] = {}
_action_locks_guard = threading.Lock()


def _action_lock_for(cdp_url: str) -> threading.Lock:
    with _action_locks_guard:
        lock = _action_locks.get(cdp_url)
        if lock is None:
            lock = threading.Lock()
            _action_locks[cdp_url] = lock
        return lock


@asynccontextmanager
async def hold_cdp_action(cdp_url: str) -> AsyncIterator[None]:
    """Serialize a CDP action across worker event loops for one profile."""
    lock = _action_lock_for(cdp_url)
    acquired = False
    try:
        while not acquired:
            acquired = lock.acquire(blocking=False)
            if not acquired:
                await asyncio.sleep(0.01)
        yield
    finally:
        if acquired:
            lock.release()


@dataclass(frozen=True)
class DropResult:
    local_reset: bool
    foreign_cleanup_scheduled: int = 0
    foreign_cleanup_pending: int = 0




@dataclass
class PooledClient:
    """A connected, patched Playwright client for one profile."""
    playwright: Any
    browser: Any
    context: Any
    page: Any
    cdp_url: str


class BrowserPool:
    """One pool per process. Get the singleton via ``get_pool()``."""

    def __init__(self) -> None:
        self._clients: Dict[str, PooledClient] = {}
        try:
            self._owner_loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self._owner_loop = None
        self._stale_urls: set[str] = set()
        self._state_guard = threading.Lock()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()
        # The HumanConfig instance we apply on each new connection.
        # Loaded lazily because cloakbrowser-resolve happens after
        # humanize.install().
        self._human_cfg: Any = None
        self._human_preset: Optional[str] = None

    # ------- locking ------- #

    async def _lock_for(self, cdp_url: str) -> asyncio.Lock:
        async with self._pool_lock:
            lock = self._locks.get(cdp_url)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[cdp_url] = lock
            return lock

    # ------- the only public method anyone needs ------- #

    async def _get_unlocked(self, cdp_url: str, preset: str) -> PooledClient:
        if self._consume_stale(cdp_url):
            stale = self._clients.pop(cdp_url, None)
            if stale is not None:
                await self._close_client(stale)
        existing = self._clients.get(cdp_url)
        if existing is not None and _is_alive(existing):
            return existing
        client = await self._connect_and_patch(cdp_url, preset)
        self._clients[cdp_url] = client
        return client

    async def get(self, cdp_url: str, preset: str = "default") -> PooledClient:
        """Return a connected, patched client for `cdp_url`. Cache-aware."""
        # Record activity so the idle-reaper knows this profile is in use.
        try:
            from . import idle_reaper

            idle_reaper.touch(cdp_url)
        except Exception:  # noqa: BLE001
            pass
        lock = await self._lock_for(cdp_url)
        async with lock:
            return await self._get_unlocked(cdp_url, preset)

    def _mark_stale(self, cdp_url: str) -> None:
        with self._state_guard:
            self._stale_urls.add(cdp_url)

    def _consume_stale(self, cdp_url: str) -> bool:
        with self._state_guard:
            if cdp_url not in self._stale_urls:
                return False
            self._stale_urls.discard(cdp_url)
            return True

    async def _mark_stale_on_owner_loop(self, cdp_url: str) -> None:
        self._mark_stale(cdp_url)

    async def _close_client(self, client: PooledClient) -> None:
        try:
            await client.browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("browser.close() failed on drop: %s", exc)
        try:
            await client.playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("playwright.stop() failed on drop: %s", exc)

    @asynccontextmanager
    async def hold(
        self, cdp_url: str, preset: str = "default"
    ) -> AsyncIterator[PooledClient]:
        """Serialize all page actions for one CDP URL (click/type/fill/...).

        ``get()`` only locks during connect; concurrent tool calls on the same
        page can still race. Callers that mutate the page should ``async with
        pool.hold(cdp_url)`` for the whole action.
        """
        try:
            from . import idle_reaper

            idle_reaper.touch(cdp_url)
        except Exception:  # noqa: BLE001
            pass
        lock = await self._lock_for(cdp_url)
        async with hold_cdp_action(cdp_url):
            async with lock:
                client = await self._get_unlocked(cdp_url, preset)
                yield client

    async def drop(self, cdp_url: str) -> DropResult:
        """Close this loop's client and safely invalidate other loop caches.

        Playwright objects are event-loop-affine. A different loop must never
        be allowed to close or mutate this pool's client cache directly; it is
        only marked stale and will be reset by that loop on its next use.
        """
        lock = await self._lock_for(cdp_url)
        async with hold_cdp_action(cdp_url):
            async with lock:
                client = self._clients.pop(cdp_url, None)
                if client is not None:
                    await self._close_client(client)
            scheduled, pending = invalidate_other_pools(cdp_url, keep=self)
        return DropResult(
            local_reset=client is not None,
            foreign_cleanup_scheduled=scheduled,
            foreign_cleanup_pending=pending,
        )
        # Other worker loops may still cache the same URL — forget only.

    async def drop_all(self) -> None:
        for url in list(self._clients.keys()):
            await self.drop(url)

    # ------- internals ------- #

    async def _connect_and_patch(self, cdp_url: str, preset: str) -> PooledClient:
        from playwright.async_api import async_playwright

        # Imported here so the humanize sys.modules hook (run at plugin
        # __init__ load) gets a chance to install before cloakbrowser is
        # imported anywhere.
        from cloakbrowser.human import patch_browser_async, resolve_config

        logger.info("BrowserPool: connecting to CDP %s (preset=%s)", redact_cdp_url(cdp_url), preset)

        pw = await async_playwright().start()
        connect_kwargs: Dict[str, Any] = {}
        token = os.environ.get("CLOAK_AUTH_TOKEN", "").strip()
        if token:
            connect_kwargs["headers"] = {"Authorization": f"Bearer {token}"}
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url, **connect_kwargs)
        except Exception:
            await pw.stop()
            raise

        # Manager-provisioned browser always has at least one context (it
        # launched chromium with launch_persistent_context). We grab that.
        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()

        pages = context.pages
        page = pages[0] if pages else await context.new_page()

        if self._human_cfg is None or self._human_preset != preset:
            self._human_cfg = resolve_config(preset)
            self._human_preset = preset

        # Patch the browser — sync in current cloakbrowser releases.
        patch_browser_async(browser, self._human_cfg)

        return PooledClient(
            playwright=pw,
            browser=browser,
            context=context,
            page=page,
            cdp_url=cdp_url,
        )


_pools: Dict[int, BrowserPool] = {}
_pools_guard = threading.Lock()


def get_pool() -> BrowserPool:
    """Return a BrowserPool bound to the *current* asyncio event loop.

    Playwright/asyncio primitives are loop-affine. A single process-wide
    pool shared across worker threads (each with its own loop) deadlocks
    or hangs; key pools by ``id(loop)`` instead.
    """
    try:
        loop = asyncio.get_running_loop()
        key = id(loop)
    except RuntimeError:
        key = 0
    with _pools_guard:
        pool = _pools.get(key)
        if pool is None:
            pool = BrowserPool()
            _pools[key] = pool
        return pool


def invalidate_other_pools(
    cdp_url: str, keep: Optional[BrowserPool] = None
) -> tuple[int, int]:
    """Mark a CDP URL stale in other event-loop pools.

    Returns ``(scheduled, pending)``. Cache mutation and Playwright cleanup
    stay on the owning loop, so all foreign pools are deliberately reported as
    pending until their next ``get``/``hold`` consumes the stale marker.
    """
    with _pools_guard:
        pools = list(_pools.values())
    pending = 0
    for pool in pools:
        if keep is not None and pool is keep:
            continue
        pool._mark_stale(cdp_url)
        pending += 1
    return 0, pending


def invalidate_everywhere(cdp_url: str) -> None:
    """Forget ``cdp_url`` in all loop pools (no cross-loop close)."""
    invalidate_other_pools(cdp_url, keep=None)


def _is_alive(client: PooledClient) -> bool:
    """Cheap liveness check — Playwright marks browsers as not-connected
    once the underlying transport drops."""
    try:
        if not client.browser.is_connected():
            return False
        if client.page.is_closed():
            return False
        if client.context not in client.browser.contexts:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False
