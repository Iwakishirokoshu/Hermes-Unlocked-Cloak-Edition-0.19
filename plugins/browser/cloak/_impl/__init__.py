"""hermes-plugin-cloak — entry point.

CRITICAL: the very first thing this module does is install the
humanize sys.modules hook. This MUST happen before anything (including
this very file's other imports) touches cloakbrowser, otherwise the
original cloakbrowser.human.mouse_async / keyboard_async modules will
load first and our pydoll-derived math will be ignored.

After the hook is in place, ``plugin_entry(ctx)`` is the function
Hermes calls (via the ``hermes.plugins`` entry-point in pyproject.toml)
to register tools with the host's tool registry.
"""
from __future__ import annotations

# --- STEP 1: install humanize hook BEFORE any cloakbrowser import ---
from .humanize import install as _install_humanize_hook

_install_humanize_hook()

# --- STEP 1b: load /etc/cloak/manager.env when gateway has no systemd drop-in ---
from .env_bootstrap import bootstrap_cloak_env

bootstrap_cloak_env()

# --- STEP 2: only NOW import anything that may transitively touch cloakbrowser ---
import json
import logging
from typing import Any, Awaitable, Callable

from . import gmail_factory, hooks, tools_browser, tools_input, tools_manage

logger = logging.getLogger(__name__)

__version__ = "0.1.2"

__all__ = ["plugin_entry", "register", "__version__"]


def _as_json_result(result: Any) -> str:
    """Hermes tool handlers must return JSON strings — dicts break some providers."""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _wrap_async_tool(fn: Callable[..., Awaitable[Any]]) -> Callable[[dict, Any], Awaitable[str]]:
    async def handler(args: dict, **kw: Any) -> str:
        return _as_json_result(await fn(args, **kw))

    return handler


def _wrap_sync_tool(fn: Callable[..., Any]) -> Callable[[dict, Any], str]:
    def handler(args: dict, **kw: Any) -> str:
        return _as_json_result(fn(args, **kw))

    return handler


# ============================================================================
# Plugin registration
# ============================================================================


def register(ctx: Any) -> None:
    """Hermes plugin entry-point. Called by ``hermes_cli/plugins.py`` as
    ``register(ctx)`` for both directory-style and pip-installed plugins.

    ``ctx`` is a ``PluginContext`` (see ``hermes_cli/plugins.py:289+``). It
    exposes ``register_tool(name, toolset, schema, handler, ...)`` and
    ``register_hook(name, callback)``.
    """
    import os as _os

    _register_manage_tools(ctx)
    _register_input_overrides(ctx)
    # Cloak Edition: the CloakBrowserProvider routes browser_* tools through
    # Cloak natively when ``browser.cloud_provider: cloak``. The legacy
    # monkeypatch of tools.browser_tool is therefore OFF by default; opt in
    # with CLOAK_LEGACY_BROWSER_PATCH=1 for the standalone-patch behaviour.
    if _os.environ.get("CLOAK_LEGACY_BROWSER_PATCH", "").strip().lower() in {"1", "true", "yes"}:
        _patch_native_browser_tools()
        logger.info("hermes-plugin-cloak: legacy browser_tool monkeypatch ENABLED")
    ctx.register_hook("pre_tool_call", hooks.on_pre_tool_call)
    # Auto-close idle profiles (gated on CLOAK_IDLE_TIMEOUT_MIN; thread self-idles
    # when disabled). Set from the dashboard Cloak panel.
    try:
        from . import idle_reaper

        idle_reaper.start()
    except Exception as exc:  # noqa: BLE001
        logger.debug("idle-reaper not started: %s", exc)
    hybrid_count = _register_hybrid_tools_if_available(ctx)
    gmail_count = 0
    if _os.environ.get("CLOAK_ENABLE_GMAIL_FACTORY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        gmail_count = _register_gmail_factory_tools(ctx)
    else:
        logger.info(
            "hermes-plugin-cloak: gmail_factory_* skipped "
            "(set CLOAK_ENABLE_GMAIL_FACTORY=1 to enable)"
        )
    logger.info(
        "hermes-plugin-cloak: registered 8 cloak_* tools (incl. captcha detect/solve, proxy pool) "
        "+ 7 browser_* overrides%s%s",
        f" + {hybrid_count} hybrid tools" if hybrid_count else "",
        f" + {gmail_count} gmail_factory_* tools" if gmail_count else "",
    )


# Backwards-compat alias for any code path that still calls plugin_entry().
plugin_entry = register


def _patch_native_browser_tools() -> None:
    """Make sure Hermes never silently falls back to a LOCAL Chromium.

    Hermes v0.17 ignores ``register_tool(..., override=True)`` for already
    bundled browser tools and dispatches by ``tools.browser_tool.<name>``
    attribute. So we patch the module attributes directly:

    * ``browser_navigate`` is **fully replaced** by our Cloak Playwright
      version (humanized via pydoll + Cloak profile).
    * All 9 OTHER native browser tools (``snapshot``, ``click``, ``type``,
      ``press``, ``scroll``, ``console``, ``back``, ``get_images``,
      ``vision``) get a thin **guard** wrapper:

      - If ``CLOAK_MANAGER_URL`` is configured but ``BROWSER_CDP_URL`` is
        empty -> refuse with ``no_active_cloak_profile`` and tell the
        agent to call ``cloak_set_active`` first.
      - Otherwise pass through to the native handler — which will then
        attach to Cloak Manager over CDP (because ``BROWSER_CDP_URL`` is
        set) and inherit the stealth profile + humanize math we already
        installed in ``cloakbrowser.human.*`` via the sys.modules hook.

    We don't rewrite click/type/etc. because native Hermes uses
    ref-based addressing (``e1, e2, ...`` returned by ``browser_snapshot``)
    while our selector-based humanized variants are registered separately
    under the same names — the registry override still gives the agent
    access to them through the toolset, but native is the canonical
    flow and we route it through Cloak Manager via CDP.
    """
    import os as _os
    import json as _json
    import tools.browser_tool as bt

    cloak_nav = _wrap_sync_tool(tools_browser.browser_navigate)

    def _patched_navigate(url: str, task_id=None) -> str:
        return cloak_nav({"url": url}, task_id=task_id)

    bt.browser_navigate = _patched_navigate

    # Every native browser tool that could create a local Chromium fallback.
    # We guard, we don't replace — once BROWSER_CDP_URL is set the native
    # implementation just speaks CDP to Cloak Manager and that's exactly
    # what we want (stealth profile, humanize hooks applied server-side).
    _GUARDED = (
        # read-only / state
        "browser_snapshot",
        "browser_screenshot",
        "browser_console",
        "browser_back",
        "browser_get_images",
        "browser_vision",
        # input — native uses ref-based API, route via Cloak CDP
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
    )

    def _make_guard(native_fn, tool_name):
        def guarded(*args, **kwargs):
            mgr_url = _os.environ.get("CLOAK_MANAGER_URL", "").strip()
            cdp_url = _os.environ.get("BROWSER_CDP_URL", "").strip()
            if mgr_url and not cdp_url:
                return _json.dumps(
                    {
                        "success": False,
                        "error": "no_active_cloak_profile",
                        "hint": (
                            "Call cloak_set_active(profile='...') first, or "
                            "cloak_create_profile + cloak_launch. Using "
                            f"{tool_name} now would spin up a LOCAL Chromium "
                            "and bypass the Cloak stealth profile."
                        ),
                        "tool": tool_name,
                        "guard": "hermes-plugin-cloak",
                    },
                    ensure_ascii=False,
                )
            return native_fn(*args, **kwargs)

        guarded.__wrapped_by_cloak__ = True
        guarded.__name__ = getattr(native_fn, "__name__", tool_name)
        return guarded

    patched_count = 0
    skipped = []
    for tname in _GUARDED:
        native = getattr(bt, tname, None)
        if native is None:
            skipped.append(tname)
            continue
        if getattr(native, "__wrapped_by_cloak__", False):
            continue
        setattr(bt, tname, _make_guard(native, tname))
        patched_count += 1

    msg = (
        "hermes-plugin-cloak: patched tools.browser_tool.browser_navigate "
        f"+ guarded {patched_count} native browser tools"
    )
    if skipped:
        msg += f" (skipped: {', '.join(skipped)} -- not present in this Hermes build)"
    logger.info(msg)


# Backwards-compat alias for any code path that still calls the old name.
_patch_native_browser_navigate = _patch_native_browser_tools


def _register_manage_tools(ctx: Any) -> None:
    """Toolset 'cloak' — 6 management tools."""
    ctx.register_tool(
        name="cloak_create_profile",
        toolset="cloak",
        schema=tools_manage.SCHEMA_CREATE,
        handler=_wrap_async_tool(tools_manage.cloak_create_profile),
        is_async=True,
        description="Create a stealth browser profile on CloakBrowser-Manager.",
        emoji="🪪",
    )
    ctx.register_tool(
        name="cloak_launch",
        toolset="cloak",
        schema=tools_manage.SCHEMA_LAUNCH,
        handler=_wrap_async_tool(tools_manage.cloak_launch),
        is_async=True,
        description=(
            "Launch a stealth profile by id or name. Sets $BROWSER_CDP_URL "
            "so subsequent browser_* calls route through it."
        ),
        emoji="🚀",
    )
    ctx.register_tool(
        name="cloak_set_active",
        toolset="cloak",
        schema=tools_manage.SCHEMA_SET_ACTIVE,
        handler=_wrap_async_tool(tools_manage.cloak_set_active),
        is_async=True,
        description=(
            "One-shot: find-or-create a profile by name, launch it if "
            "not running, set $BROWSER_CDP_URL. Use this for warmed "
            "scout profiles (e.g. 'twitter-scout')."
        ),
        emoji="🎯",
    )
    ctx.register_tool(
        name="cloak_stop",
        toolset="cloak",
        schema=tools_manage.SCHEMA_STOP,
        handler=_wrap_async_tool(tools_manage.cloak_stop),
        is_async=True,
        description="Stop a profile and drop its cached Playwright client.",
        emoji="🛑",
    )
    ctx.register_tool(
        name="cloak_list_profiles",
        toolset="cloak",
        schema=tools_manage.SCHEMA_LIST,
        handler=_wrap_async_tool(tools_manage.cloak_list_profiles),
        is_async=True,
        description="List all profiles known to CloakBrowser-Manager.",
        emoji="📋",
    )
    ctx.register_tool(
        name="cloak_proxy_pool",
        toolset="cloak",
        schema=tools_manage.SCHEMA_PROXY_POOL,
        handler=_wrap_async_tool(tools_manage.cloak_proxy_pool),
        is_async=True,
        description=(
            "Manage the proxy pool used for new stealth profiles "
            "(/etc/cloak/proxies.json). action=list|next|add|clear. Use "
            "action=next to grab a proxy when the user asks to 'use a proxy "
            "from the pool', then pass it to cloak_create_profile/cloak_set_active "
            "(or just enable auto-assign in the dashboard)."
        ),
        emoji="🌐",
    )
    ctx.register_tool(
        name="cloak_detect_captcha",
        toolset="cloak",
        schema=tools_manage.SCHEMA_DETECT_CAPTCHA,
        handler=_wrap_async_tool(tools_manage.cloak_detect_captcha),
        is_async=True,
        description=(
            "Inspect the active CloakBrowser tab and classify any captcha "
            "(turnstile, hcaptcha, recaptcha v2/v3/enterprise, funcaptcha, "
            "geetest, datadome, kasada, akamai, imperva, lemin, mtcaptcha, "
            "amazon_waf, friendly, keycaptcha, cybersiara, capy, yandex, "
            "tencent, image, cloudflare_interstitial). "
            "Returns {kind, site_key, page_url, extra, confidence}. "
            "kind=null means no captcha is present."
        ),
        emoji="🔍",
    )
    ctx.register_tool(
        name="cloak_solve_captcha",
        toolset="cloak",
        schema=tools_manage.SCHEMA_SOLVE_CAPTCHA,
        handler=_wrap_async_tool(tools_manage.cloak_solve_captcha),
        is_async=True,
        description=(
            "Solve a captcha through the configured providers (CapSolver "
            "and/or 2captcha; router picks the best per-kind). Supports 22+ "
            "kinds — see cloak_detect_captcha. On any failure returns "
            "'MANUAL_INTERVENTION_REQUIRED' — agent MUST then call "
            "kanban_block(reason=...) to bring the human to the VNC."
        ),
        emoji="🧩",
    )


def _register_hybrid_tools_if_available(ctx: Any) -> int:
    """Phase 1.5 — register pydoll-based hybrid tools when the [hybrid]
    optional dep is present. Returns the number of tools registered."""
    try:
        from . import tools_hybrid
    except ImportError as exc:
        logger.debug("hybrid tools unavailable: %s", exc)
        return 0
    if not tools_hybrid._HAS_PYDOLL:
        logger.info(
            "hermes-plugin-cloak: pydoll-python not installed — skipping hybrid tools. "
            "Install with: pip install 'hermes-plugin-cloak[hybrid]'"
        )
        return 0
    tools_hybrid.register(ctx)
    return 4


def _register_gmail_factory_tools(ctx: Any) -> int:
    """Toolset 'gmail_factory' — 4 tools backed by the vendored Gmail-infinity
    build at /usr/local/lib/gmail-factory/. See gmail_factory.py for the
    subprocess protocol and the licence notice in vendor/gmail_factory/NOTICE.

    Returns the number of tools registered (always 4 in this build) so the
    startup log can report the actual count.
    """
    ctx.register_tool(
        name="gmail_factory_status",
        toolset="gmail_factory",
        schema=gmail_factory.SCHEMA_STATUS,
        handler=_wrap_async_tool(gmail_factory.gmail_factory_status),
        is_async=True,
        description=(
            "Check whether the Gmail Factory vendor is installed and "
            "configured: venv, SMS/CAPTCHA keys, proxies, saved accounts, "
            "and whether a Cloak profile is currently active."
        ),
        emoji="🧪",
    )
    ctx.register_tool(
        name="gmail_factory_create",
        toolset="gmail_factory",
        schema=gmail_factory.SCHEMA_CREATE,
        handler=_wrap_async_tool(gmail_factory.gmail_factory_create),
        is_async=True,
        description=(
            "Create one or more Gmail accounts via the vendored "
            "Gmail-infinity engine. Routes through the active Cloak profile "
            "when BROWSER_CDP_URL is set (i.e. after cloak_set_active). "
            "Passwords are masked by default — pass unmask=true to see them."
        ),
        emoji="📧",
    )
    ctx.register_tool(
        name="gmail_factory_list",
        toolset="gmail_factory",
        schema=gmail_factory.SCHEMA_LIST,
        handler=_wrap_async_tool(gmail_factory.gmail_factory_list),
        is_async=True,
        description=(
            "List Gmail accounts already created by the factory. Reads "
            "vendor data/accounts.json. Passwords masked by default."
        ),
        emoji="📒",
    )
    ctx.register_tool(
        name="gmail_factory_warmup",
        toolset="gmail_factory",
        schema=gmail_factory.SCHEMA_WARMUP,
        handler=_wrap_async_tool(gmail_factory.gmail_factory_warmup),
        is_async=True,
        description=(
            "Warm up existing Gmail accounts by simulating real activity "
            "(reading mail, YouTube, Search). Currently delegates inline; "
            "standalone warmup is on the roadmap."
        ),
        emoji="🔥",
    )
    return 4


def _register_input_overrides(ctx: Any) -> None:
    """Toolset 'browser' — navigate override + humanized input overrides."""
    ctx.register_tool(
        name="browser_navigate",
        toolset="browser",
        schema=tools_browser.SCHEMA_NAVIGATE,
        handler=_wrap_sync_tool(tools_browser.browser_navigate),
        is_async=False,
        description=(
            "Navigate via CloakBrowser when CLOAK_MANAGER_URL is set "
            "(auto-launches profile), then Hermes CDP browser."
        ),
        emoji="🌐",
        override=True,
    )
    overrides = [
        ("browser_click",  tools_input.SCHEMA_CLICK,  tools_input.browser_click,  "Click an element with humanized mouse movement."),
        ("browser_type",   tools_input.SCHEMA_TYPE,   tools_input.browser_type,   "Type text into an element with humanized typing (QWERTY-typo simulation)."),
        ("browser_fill",   tools_input.SCHEMA_FILL,   tools_input.browser_fill,   "Clear and refill an input with humanized typing."),
        ("browser_press",  tools_input.SCHEMA_PRESS,  tools_input.browser_press,  "Press a key (Enter / Tab / ArrowDown / ...) on a focused element."),
        ("browser_hover",  tools_input.SCHEMA_HOVER,  tools_input.browser_hover,  "Move mouse to an element with humanized Bezier path."),
        ("browser_drag",   tools_input.SCHEMA_DRAG,   tools_input.browser_drag,   "Drag an element from source to target with humanized motion."),
        ("browser_scroll", tools_input.SCHEMA_SCROLL, tools_input.browser_scroll, "Scroll the page (delta) or bring an element into view."),
    ]
    for name, schema, handler, desc in overrides:
        ctx.register_tool(
            name=name,
            toolset="browser",
            schema=schema,
            handler=_wrap_async_tool(handler),
            is_async=True,
            description=desc,
            override=True,
        )
