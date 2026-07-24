"""Cloak browser plugin — bundled, auto-loaded (kind: backend).

Two layers register here, independently and defensively:

1. :class:`plugins.browser.cloak.provider.CloakBrowserProvider` — the
   first-class browser backend that makes Cloak appear in the
   ``hermes tools`` / ``hermes setup`` picker and routes cloud-mode
   ``browser_*`` calls through CloakBrowser-Manager when
   ``browser.cloud_provider: cloak``. Dependency-light, always registers.

2. The rich ``_impl`` package (vendored from hermes-plugin-cloak) — the
   ``cloak_*`` profile-management tools, captcha detect/solve, humanized
   input overrides and the Gmail Factory toolset. These need the heavyweight
   ``cloakbrowser`` / ``playwright`` stack, so they register best-effort: a
   missing dependency degrades gracefully (provider still works) instead of
   crashing plugin discovery.

The legacy monkeypatch of ``tools.browser_tool`` (``_patch_native_browser_tools``)
is OFF by default in this edition: the provider model already routes browser
tools through Cloak when selected. Set ``CLOAK_LEGACY_BROWSER_PATCH=1`` to
restore the old patch behaviour.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register the Cloak provider (always) and rich tools (best-effort)."""
    # --- Layer 1: the browser provider. Must not depend on cloakbrowser. ---
    try:
        from plugins.browser.cloak.provider import CloakBrowserProvider

        ctx.register_browser_provider(CloakBrowserProvider())
        logger.info("Cloak: registered browser provider 'cloak'")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cloak: failed to register browser provider: %s", exc)

    # --- Layer 2: rich cloak_* / gmail_factory / captcha tools. ---
    try:
        from plugins.browser.cloak import _impl

        _impl.register(ctx)
        logger.info("Cloak: registered rich tools (cloak_*, gmail_factory, captcha)")
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Cloak: rich tools unavailable (install cloakbrowser/playwright to enable): %s",
            exc,
        )
