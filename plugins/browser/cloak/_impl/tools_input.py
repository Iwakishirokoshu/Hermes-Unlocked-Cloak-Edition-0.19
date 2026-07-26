"""Input-tool overrides: ``browser_click``, ``browser_type``, ``browser_fill``,
``browser_press``, ``browser_hover``, ``browser_drag``, ``browser_scroll``.

These are registered with ``override=True`` so they replace Hermes's
native (agent-browser-driven) versions. They route the user-input action
through an in-process Playwright client that's been patched by
``cloakbrowser.human.patch_browser_async`` — which in turn calls our
pydoll-derived motor math via the sys.modules hook in ``humanize/__init__.py``.

Read-only tools (navigate, snapshot, screenshot, console, vision,
get_images, back) are NOT touched here — they stay on agent-browser,
which is plenty stealthy for state reads and doesn't benefit from human
timing.

Each handler acquires the active CDP URL from ``$BROWSER_CDP_URL`` (set
by ``cloak_set_active`` / ``cloak_launch``) and uses ``BrowserPool`` to
get-or-create the patched page.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from .browser_pool import get_pool
from . import profile_state
from agent.redact import redact_cdp_url

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------

_SEL_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": "CSS selector for the element."},
    },
    "required": ["selector"],
}

SCHEMA_CLICK = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Snapshot ref, e.g. e1 (the @e1 spelling is accepted too)."},
        "selector": {"type": "string", "description": "CSS selector (preferred for humanize)"},
        "timeout_ms": {"type": "integer", "default": 30000},
        "force": {"type": "boolean", "default": False},
    },
}

SCHEMA_TYPE = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Snapshot ref, e.g. e5. Resolved to a real selector by focusing the field first, so a ref from browser_snapshot is enough."},
        "selector": {"type": "string", "description": "CSS selector for the field — required for humanized text input, e.g. input[type='email'] or #email. Read the id/name/type off the element in the snapshot."},
        "text": {"type": "string"},
        "timeout_ms": {"type": "integer", "default": 30000},
        "verify": {
            "type": "boolean",
            "default": True,
            "description": (
                "Read the field's .value back after typing and, if the humanized "
                "typo simulator left an artifact (text != intended), clear and "
                "re-type up to max_retries times. Keep on for critical fields "
                "(email, password, username)."
            ),
        },
        "max_retries": {"type": "integer", "default": 2},
    },
    "required": ["text"],
}

SCHEMA_FILL = SCHEMA_TYPE

SCHEMA_PRESS = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "selector": {"type": "string"},
        "key": {"type": "string", "description": "e.g. 'Enter', 'Tab', 'ArrowDown'"},
        "timeout_ms": {"type": "integer", "default": 30000},
    },
    "required": ["key"],
}

SCHEMA_HOVER = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "selector": {"type": "string"},
        "timeout_ms": {"type": "integer", "default": 30000},
        "force": {"type": "boolean", "default": False},
    },
}

SCHEMA_DRAG = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "target": {"type": "string"},
        "timeout_ms": {"type": "integer", "default": 30000},
    },
    "required": ["source", "target"],
}

SCHEMA_SCROLL = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": "Element to scroll into view (optional)."},
        "delta_x": {"type": "integer", "default": 0},
        "delta_y": {"type": "integer", "default": 0},
    },
}


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------


_SNAPSHOT_REF_RE = re.compile(r"^@?(e\d+)$", re.IGNORECASE)


def _normalize_ref(ref: str) -> str:
    """Accept both spellings of a snapshot reference.

    ``browser_snapshot`` hands the model bare refs (``e5``) and the built-in
    browser tools take them that way, but the rest of this module only
    recognises the ``@e5`` spelling. A bare ref therefore fell through to
    ``page.locator("e5")`` — a CSS lookup for a nonexistent ``<e5>`` tag that
    burns the full timeout twice before failing, instead of being routed to the
    native click path, which is what actually owns the refs.
    """
    value = str(ref or "").strip()
    match = _SNAPSHOT_REF_RE.match(value)
    return f"@{match.group(1).lower()}" if match else value


def _normalize_target(ref: str = "", selector: str = "") -> str:
    return _normalize_ref(ref) or (selector or "").strip()


def _humanized_selector_required(ref: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "humanized": False,
        "error": "humanized_selector_required",
        "ref": ref,
        "message": (
            "This ref could not be resolved to a unique field — focusing it did not land "
            "on an editable element, or the element cannot be addressed unambiguously. "
            "Re-run browser_snapshot for a fresh ref, or pass a CSS selector directly "
            "(e.g. selector=\"input[type='email']\"). No native fallback was used for typing."
        ),
    }


# Identify the focused field and describe it with a selector stable enough for
# the verification pass. Prefers id, then a unique name/type, then a short
# structural path; returns null rather than a selector that matches many nodes.
_SELECTOR_FROM_ACTIVE_JS = r"""
() => {
  const el = document.activeElement;
  if (!el) return null;
  const editable = (el.matches && el.matches('input, textarea')) || el.isContentEditable;
  if (!editable) return null;
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, '\\$&');
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  if (el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
  const tag = el.tagName.toLowerCase();
  if (el.name) { const s = tag + '[name="' + String(el.name).replace(/"/g, '\\"') + '"]'; if (unique(s)) return s; }
  if (el.type) { const s = tag + '[type="' + String(el.type).replace(/"/g, '\\"') + '"]'; if (unique(s)) return s; }
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && parts.length < 6) {
    if (node.id) { parts.unshift('#' + esc(node.id)); break; }
    let part = node.tagName.toLowerCase();
    const parent = node.parentElement;
    if (parent) {
      const sibs = Array.prototype.filter.call(parent.children, c => c.tagName === node.tagName);
      if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
    }
    parts.unshift(part);
    node = node.parentElement;
  }
  const sel = parts.join(' > ');
  return unique(sel) ? sel : null;
}
"""


async def _selector_from_focus(page: Any) -> str:
    """CSS selector for the focused field, or "" when nothing editable has focus."""
    try:
        resolved = await page.evaluate(_SELECTOR_FROM_ACTIVE_JS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read the focused element: %s", exc)
        return ""
    return str(resolved or "").strip()


async def _selector_from_ref(page: Any, ref_target: str, task_id: Any) -> str:
    """Turn a snapshot ref into a CSS selector by borrowing agent-browser's click.

    Snapshot refs belong to agent-browser; this Playwright client cannot resolve
    them at all (``aria-ref`` lookups match zero elements here). Both clients
    drive the same tab, though — verified live — so a native click puts focus on
    the field and ``document.activeElement`` identifies it. From there the normal
    humanized path runs on a real selector, verification included.

    Returns "" when the ref could not be turned into a unique selector; the
    caller then refuses rather than typing into whatever happens to be focused.
    """
    try:
        raw = _native_click(ref_target, task_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("native focus click failed for %s: %s", ref_target, exc)
        return ""
    try:
        if isinstance(raw, str) and raw.lstrip().startswith("{"):
            import json as _json

            if not _json.loads(raw).get("success", True):
                logger.debug("native focus click reported failure for %s", ref_target)
                return ""
    except Exception:  # noqa: BLE001
        pass  # non-JSON reply — fall through and let the focus check decide
    return await _selector_from_focus(page)


def _no_target_error() -> Dict[str, Any]:
    """Neither a ref nor a selector arrived — say what to pass, not just what's missing."""
    return {
        "ok": False,
        "humanized": False,
        "error": "target_required",
        "message": (
            "Pass the field to type into: either ref from the latest "
            "browser_snapshot (e.g. ref=\"e5\") or a CSS selector "
            "(e.g. selector=\"input[type='email']\"). Re-run browser_snapshot "
            "if you do not have a current ref."
        ),
    }


def _locator_for(page: Any, target: str) -> Any:
    """Map @eN aria-refs or CSS selectors onto a Playwright locator."""
    if target.startswith("@"):
        # Playwright aria-ref (agent-browser / snapshot refs)
        return page.locator(f"aria-ref={target[1:]}")
    return page.locator(target)


def _native_click(ref: str, task_id: Any) -> Any:
    from tools.browser_tool import browser_click as native_click

    return native_click(ref=ref, task_id=task_id)




def _native_press(key: str, task_id: Any) -> Any:
    # Hermes 0.19 signature is (key, task_id) вЂ” no ref parameter.
    from tools.browser_tool import browser_press as native_press

    return native_press(key, task_id)


def _native_hover(ref: str, task_id: Any) -> Any:
    """Hermes 0.19 has no browser_hover export вЂ” drive agent-browser directly."""
    import json

    from tools import browser_tool as bt

    if not ref.startswith("@"):
        ref = f"@{ref}"
    effective = bt._last_session_key(task_id or "default")
    result = bt._run_browser_command(effective, "hover", [ref])
    if result.get("success"):
        return json.dumps(bt._copy_fallback_warning({"success": True, "hovered": ref}, result), ensure_ascii=False)
    return json.dumps(
        bt._copy_fallback_warning(
            {"success": False, "error": result.get("error", f"Failed to hover {ref}")},
            result,
        ),
        ensure_ascii=False,
    )


async def browser_click(args: dict, **kw: Any) -> Any:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    timeout_ms = args.get("timeout_ms", 30000)
    force = args.get("force", False)
    task_id = kw.get("task_id")
    target = _normalize_target(ref, selector)
    if not target:
        return {"error": "Provide ref (@e1) or CSS selector."}

    async with _hold_page(task_id) as page:
        if isinstance(page, dict):
            if target.startswith("@"):
                return _native_click(target, task_id)
            return page
        try:
            loc = _locator_for(page, target)
            await loc.click(timeout=timeout_ms, force=force)
            return {"ok": True, "target": target, "humanized": True}
        except Exception as exc:  # noqa: BLE001
            if target.startswith("@"):
                logger.debug("humanized click failed on %s (%s); native fallback", target, exc)
                return _native_click(target, task_id)
            return _error(exc, target=target)


async def browser_type(args: dict, **kw: Any) -> Any:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    text = args.get("text", "")
    timeout_ms = args.get("timeout_ms", 30000)
    task_id = kw.get("task_id")
    target = selector.strip() or _normalize_target(ref, selector)
    verify = args.get("verify", True)
    max_retries = int(args.get("max_retries", 2))
    target_source = "selector"

    async with _hold_page(task_id) as page:
        if isinstance(page, dict):
            return page
        if target.startswith("@"):
            resolved = await _selector_from_ref(page, target, task_id)
            if not resolved:
                return _humanized_selector_required(target)
            selector = resolved  # a real selector, so verification runs too
            target = resolved
            target_source = "ref"
        elif not target:
            # Nothing named. The model almost always clicks the field
            # first, so type where the caret already is rather than
            # refusing and being retried with the same empty arguments.
            resolved = await _selector_from_focus(page)
            if not resolved:
                return _no_target_error()
            selector = resolved
            target = resolved
            target_source = "focus"
        try:
            loc = _locator_for(page, target)
            value_before = ""
            if verify:
                try:
                    value_before = (await loc.input_value(timeout=timeout_ms)) or ""
                except Exception:  # noqa: BLE001
                    value_before = ""
            await loc.type(text, timeout=timeout_ms)
            result: Dict[str, Any] = {
                "ok": True, "target": target, "chars": len(text),
                "humanized": True, "target_source": target_source,
            }
            if verify and selector and not target.startswith("@"):
                expected = value_before + text
                await _apply_verification(page, selector, expected, timeout_ms, max_retries, result)
            return result
        except Exception as exc:  # noqa: BLE001
            return _error(exc, target=target)


async def browser_fill(args: dict, **kw: Any) -> Any:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    text = args.get("text", "")
    timeout_ms = args.get("timeout_ms", 30000)
    task_id = kw.get("task_id")
    target = selector.strip() or _normalize_target(ref, selector)
    verify = args.get("verify", True)
    max_retries = int(args.get("max_retries", 2))
    target_source = "selector"

    async with _hold_page(task_id) as page:
        if isinstance(page, dict):
            return page
        if target.startswith("@"):
            resolved = await _selector_from_ref(page, target, task_id)
            if not resolved:
                return _humanized_selector_required(target)
            selector = resolved  # a real selector, so verification runs too
            target = resolved
            target_source = "ref"
        elif not target:
            # Nothing named. The model almost always clicks the field
            # first, so type where the caret already is rather than
            # refusing and being retried with the same empty arguments.
            resolved = await _selector_from_focus(page)
            if not resolved:
                return _no_target_error()
            selector = resolved
            target = resolved
            target_source = "focus"
        try:
            loc = _locator_for(page, target)
            await _clear_field(page, target, timeout_ms)
            await loc.type(text, timeout=timeout_ms)
            result: Dict[str, Any] = {
                "ok": True, "target": target, "chars": len(text),
                "humanized": True, "target_source": target_source,
            }
            if verify and selector and not target.startswith("@"):
                await _apply_verification(page, selector, text, timeout_ms, max_retries, result)
            return result
        except Exception as exc:  # noqa: BLE001
            return _error(exc, target=target)


async def browser_press(args: dict, **kw: Any) -> Any:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    key = args.get("key", "")
    timeout_ms = args.get("timeout_ms", 30000)
    task_id = kw.get("task_id")
    if not key:
        return {"error": "Provide key (e.g. Enter)."}
    target = _normalize_target(ref, selector)

    async with _hold_page(task_id) as page:
        if isinstance(page, dict):
            return _native_press(key, task_id)
        try:
            if target:
                loc = _locator_for(page, target)
                await loc.press(key, timeout=timeout_ms)
            else:
                await page.keyboard.press(key)
            return {"ok": True, "key": key, "target": target or None, "humanized": True}
        except Exception as exc:  # noqa: BLE001
            logger.debug("humanized press failed (%s); native fallback", exc)
            return _native_press(key, task_id)


async def browser_hover(args: dict, **kw: Any) -> Any:
    ref = args.get("ref", "")
    selector = args.get("selector", "")
    timeout_ms = args.get("timeout_ms", 30000)
    force = args.get("force", False)
    task_id = kw.get("task_id")
    target = _normalize_target(ref, selector)
    if not target:
        return {"error": "Provide ref (@e1) or CSS selector."}

    async with _hold_page(task_id) as page:
        if isinstance(page, dict):
            if target.startswith("@"):
                return _native_hover(target, task_id)
            return page
        try:
            loc = _locator_for(page, target)
            await loc.hover(timeout=timeout_ms, force=force)
            return {"ok": True, "target": target, "humanized": True}
        except Exception as exc:  # noqa: BLE001
            if target.startswith("@"):
                logger.debug("humanized hover failed on %s (%s); native fallback", target, exc)
                return _native_hover(target, task_id)
            return _error(exc, target=target)


async def browser_drag(args: dict, **kw: Any) -> Dict[str, Any]:
    source = args.get("source", "")
    target = args.get("target", "")
    timeout_ms = args.get("timeout_ms", 30000)
    task_id = kw.get("task_id")
    async with _hold_page(task_id) as page:
        if isinstance(page, dict):
            return page
        try:
            if hasattr(page, "drag_and_drop"):
                await page.drag_and_drop(source, target, timeout=timeout_ms)
            else:
                await page.main_frame.drag_and_drop(source, target, timeout=timeout_ms)
            return {"ok": True, "source": source, "target": target}
        except Exception as exc:  # noqa: BLE001
            return _error(exc, selector=f"{source} -> {target}")


async def browser_scroll(args: dict, **kw: Any) -> Dict[str, Any]:
    selector = args.get("selector")
    delta_x = args.get("delta_x", 0)
    delta_y = args.get("delta_y", 0)
    task_id = kw.get("task_id")
    async with _hold_page(task_id) as page:
        if isinstance(page, dict):
            return page
        try:
            if selector:
                locator = page.locator(selector).first
                await locator.scroll_into_view_if_needed()
                return {"ok": True, "scrolled_into_view": selector}

            await page.mouse.wheel(delta_x, delta_y)
            return {"ok": True, "delta_x": delta_x, "delta_y": delta_y}
        except Exception as exc:  # noqa: BLE001
            return _error(exc, selector=selector or f"wheel({delta_x},{delta_y})")


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


async def _read_value(page: Any, selector: str) -> Optional[str]:
    """Read an element's current value/text. Returns None if it can't be read
    (e.g. element gone, exotic widget) so the caller can treat it as
    'unverifiable' rather than a mismatch."""
    try:
        return await page.input_value(selector, timeout=5000)
    except Exception:  # noqa: BLE001
        # Not a standard input (contenteditable, custom widget) — try DOM.
        try:
            return await page.eval_on_selector(
                selector,
                "el => (el.value !== undefined && el.value !== null) "
                "? el.value : (el.textContent || '')",
            )
        except Exception:  # noqa: BLE001
            return None


async def _clear_field(page: Any, selector: str, timeout_ms: int) -> None:
    """Empty a field before a humanized re-type. Select-all + Delete first
    (works for most inputs), falling back to Playwright fill('')."""
    try:
        await page.click(selector, timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    modifier = "Meta" if sys.platform == "darwin" else "Control"
    try:
        await page.keyboard.press(f"{modifier}+A")
        await page.keyboard.press("Delete")
    except Exception:  # noqa: BLE001
        pass
    # Belt-and-braces: ensure truly empty.
    try:
        if (await _read_value(page, selector)) not in (None, ""):
            await page.fill(selector, "", timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        pass


async def _apply_verification(
    page: Any,
    selector: str,
    expected: str,
    timeout_ms: int,
    max_retries: int,
    result: Dict[str, Any],
) -> None:
    """Read back the field and humanized-re-type on mismatch (typo artifacts).

    Mutates ``result`` in place with verification metadata. If the field can't
    be read, marks ``verified=False`` / ``method='unverifiable'`` but leaves
    ``ok`` untouched (we typed, we just couldn't confirm).

    Last resort (only when CLOAK_VERIFY_JS_FALLBACK=1): set the value via JS
    and dispatch input/change events. This is detectable (isTrusted=false), so
    it is OFF by default — stealth beats convenience for account work.
    """
    attempts = 0
    actual = await _read_value(page, selector)
    if actual is None:
        result.update({"verified": False, "value_matches": False, "method": "unverifiable"})
        return

    while actual != expected and attempts < max_retries:
        attempts += 1
        logger.info(
            "browser input verify: mismatch on %s (typo artifact), re-typing (attempt %d/%d)",
            selector, attempts, max_retries,
        )
        await _clear_field(page, selector, timeout_ms)
        await page.type(selector, expected, timeout=timeout_ms)
        actual = await _read_value(page, selector)

    if actual == expected:
        result.update({"verified": True, "value_matches": True, "retries": attempts, "method": "humanized"})
        return

    # Humanized retries exhausted and still wrong.
    if os.environ.get("CLOAK_VERIFY_JS_FALLBACK", "").strip().lower() in {"1", "true", "yes"}:
        try:
            await page.eval_on_selector(
                selector,
                """(el, v) => {
                    el.focus();
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                expected,
            )
            actual = await _read_value(page, selector)
            if actual == expected:
                result.update({
                    "verified": True, "value_matches": True, "retries": attempts,
                    "method": "js_fallback",
                    "warning": "value set via JS (isTrusted=false) — detectable; "
                               "disable CLOAK_VERIFY_JS_FALLBACK for max stealth.",
                })
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("JS fallback failed on %s: %s", selector, exc)

    result.update({
        "ok": False,
        "verified": False,
        "value_matches": False,
        "retries": attempts,
        "method": "mismatch",
        "error": "value_mismatch_after_retries",
        "expected": expected,
        "got": actual,
    })


async def _active_page(task_id: Any = None) -> Any:
    """Return the patched page for the active profile, or an error dict.

    Prefer ``_hold_page`` for mutating actions so concurrent tools serialize.
    """
    async with _hold_page(task_id) as page:
        return page


@asynccontextmanager
async def _hold_page(task_id: Any = None) -> AsyncIterator[Any]:
    """Yield the patched page while holding the per-CDP action lock."""
    cdp_url = profile_state.cdp_url_for_task(task_id)
    if not cdp_url:
        yield {
            "error": "No Cloak CDP binding for this task. Call cloak_set_active(profile=...) "
                     "or cloak_launch(profile=...) first.",
            "task_id": profile_state.task_key(task_id),
        }
        return
    preset = os.environ.get("CLOAK_HUMAN_PRESET", "default")
    try:
        async with get_pool().hold(cdp_url, preset=preset) as client:
            yield client.page
    except Exception as exc:  # noqa: BLE001
        yield {"error": f"BrowserPool.get failed: {redact_cdp_url(exc)}", "cdp_url": redact_cdp_url(cdp_url)}


def _error(exc: Exception, **context: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"error": redact_cdp_url(exc), "type": type(exc).__name__}
    out.update(context)
    return out
