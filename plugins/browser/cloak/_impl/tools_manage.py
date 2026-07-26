"""Profile-management tools registered in the `cloak` toolset.

  cloak_create_profile  — POST /api/profiles
  cloak_launch          — POST /api/profiles/{id}/launch + mutate BROWSER_CDP_URL env
  cloak_set_active      — find-or-create + launch + mutate env (one-shot)
  cloak_detect_captcha  — run in-page JS to classify the captcha + extract site_key
  cloak_solve_captcha   — route through 2captcha / capsolver, fall through to
                          MANUAL_INTERVENTION_REQUIRED on every failure
  cloak_stop            — POST /api/profiles/{id}/stop + clear BROWSER_CDP_URL
  cloak_list_profiles   — GET  /api/profiles (lookup helper for the agent)

All tools are async. They return plain dicts/strings — no Exceptions
escape into the agent's reasoning loop (failures get caught and surfaced
as error fields the LLM can read).

Mutating ``os.environ['BROWSER_CDP_URL']`` is the integration point with
Hermes's native browser tools — they read this env on every invocation
(see hermes-agent-main/tools/browser_tool.py:288-309 in the recon).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from agent.redact import redact_cdp_url
from .browser_pool import get_pool
from .captcha import (
    MANUAL_INTERVENTION_REQUIRED,
    CaptchaRouter,
    ManualInterventionRequired,
    detect_in_playwright_page,
)
from .manager_client import ManagerClient, ManagerError
from . import profile_state

logger = logging.getLogger(__name__)


def _pool_requested(use_pool: Optional[bool]) -> bool:
    if use_pool is not None:
        return bool(use_pool)
    from ..proxy_format import pool_enabled

    return pool_enabled()


def _prepare_profile_proxy(
    profile_name: str, explicit: str = "", use_pool: Optional[bool] = None
) -> tuple[str, Optional[str]]:
    """Resolve a create-time proxy without permitting a direct fail-open."""
    from ..proxy_format import profile_claim_owner, resolve_proxy

    raw_proxy = str(explicit or "").strip()
    claim_owner = (
        profile_claim_owner(profile_name)
        if not raw_proxy and _pool_requested(use_pool)
        else None
    )
    proxy = resolve_proxy(
        raw_proxy,
        use_pool,
        claim_as=claim_owner,
        fail_closed=True,
    )
    return proxy, claim_owner


def _missing_name_error(tool: str, field: str) -> Dict[str, Any]:
    """Fail-closed response for a create call that arrived without a name.

    Hermes does not validate tool arguments against the declared JSON schema,
    so a model that emits ``{}`` would otherwise create an unnamed profile —
    unfindable by name, so every retry creates another one, and never carrying
    a pool proxy because the pool is claimed per profile name.
    """
    return {
        "error": (
            f"{tool} requires a non-empty '{field}'. Pass a stable, unique name "
            "(e.g. 'reg-<task-id>') so the profile can be found again and so a "
            "pool proxy can be reserved for it."
        ),
        "code": "missing_name",
    }


def _release_pool_claim(claim_owner: Optional[str]) -> int:
    if not claim_owner:
        return 0
    from ..proxy_format import release_proxy

    return release_proxy(claim_owner)


# ----------------------------------------------------------------------------
# Tool schemas (for Hermes registry.register)
# ----------------------------------------------------------------------------

SCHEMA_CREATE = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Profile name (unique). E.g. 'twitter-scout' or 'acc-x-20260616-i47'."},
        "proxy": {"type": "string", "description": "Optional proxy URL: http://user:pass@host:port or socks5://...", "default": ""},
        "use_pool": {"type": "boolean", "description": "Take the next proxy from the configured proxy pool when no explicit proxy is given. Use this when the user says 'use a proxy from the pool'.", "default": False},
        "humanize": {"type": "boolean", "description": "Enable cloakbrowser humanize on this profile. Always true for stealth.", "default": True},
        "human_preset": {"type": "string", "enum": ["default", "careful"], "default": "default"},
        "headless": {"type": "boolean", "default": False},
        "geoip": {"type": "boolean", "description": "Auto-spoof timezone/locale from proxy GeoIP.", "default": True},
        "tags": {
            "type": "array",
            "description": "Optional Manager tags. Each item must be a Manager TagCreate object, e.g. {\"tag\": \"registration\", \"color\": \"blue\"}.",
            "items": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "color": {"type": "string"},
                },
                "required": ["tag"],
                "additionalProperties": False,
            },
            "default": [],
        },
        "notes": {"type": "string", "default": ""},
        "auto_launch": {"type": "boolean", "default": False},
    },
    "required": ["name"],
    "additionalProperties": True,
}


def _normalize_profile_tags(raw_tags: Any) -> List[Dict[str, str]]:
    """Adapt friendly tag names to Cloak Manager's TagCreate request objects."""
    if raw_tags is None:
        return []
    if not isinstance(raw_tags, (list, tuple)):
        raise ValueError("tags must be a list of tag objects")

    normalized: List[Dict[str, str]] = []
    for raw_tag in raw_tags:
        if isinstance(raw_tag, str):
            tag = raw_tag.strip()
            color = ""
        elif isinstance(raw_tag, dict):
            tag = str(raw_tag.get("tag", "")).strip()
            raw_color = raw_tag.get("color", "")
            color = raw_color.strip() if isinstance(raw_color, str) else ""
        else:
            raise ValueError("each tag must be a string or an object with a tag field")

        if not tag:
            raise ValueError("each tag needs a non-empty tag field")
        item = {"tag": tag}
        if color:
            item["color"] = color
        normalized.append(item)

    return normalized


SCHEMA_LAUNCH = {
    "type": "object",
    "properties": {
        "profile": {"type": "string", "description": "Profile id (UUID) or name to launch. Names come from cloak_list_profiles, so the operator can refer to a profile by name."},
        "allow_profile_switch": {
            "type": "boolean",
            "description": "Allow this task to switch away from its remembered profile. Set true when the operator names a different profile. Omit to follow the CLOAK_ALLOW_PROFILE_SWITCH setting.",
        },
    },
    "required": ["profile"],
}

SCHEMA_SET_ACTIVE = {
    "type": "object",
    "properties": {
        "profile": {"type": "string", "description": "Profile name (created if missing) to make active."},
        "create_if_missing": {"type": "boolean", "default": True},
        "humanize": {"type": "boolean", "default": True},
        "human_preset": {"type": "string", "enum": ["default", "careful"], "default": "default"},
        "proxy": {"type": "string", "default": ""},
        "use_pool": {"type": "boolean", "description": "Take the next proxy from the configured proxy pool when no explicit proxy is given.", "default": False},
        "allow_profile_switch": {
            "type": "boolean",
            "description": "Allow this task to switch away from its remembered profile. Omit to follow the CLOAK_ALLOW_PROFILE_SWITCH setting.",
        },
    },
    "required": ["profile"],
}

SCHEMA_PROXY_POOL = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "next", "release", "add", "clear"],
            "default": "list",
            "description": "list = show pool; next = reserve for claim_as; release = free claim_as; add = append proxies; clear = empty the pool.",
        },
        "claim_as": {"type": "string", "default": "", "description": "Profile name which owns a next or release reservation."},
        "text": {"type": "string", "default": "", "description": "For action=add: proxies (one per line) in any common format (host:port, host:port:user:pass, user:pass@host:port, scheme://...)."},
        "default_scheme": {"type": "string", "enum": ["http", "https", "socks5"], "default": "http", "description": "Scheme applied to lines without an explicit scheme://."},
    },
}

SCHEMA_STOP = {
    "type": "object",
    "properties": {
        "profile": {"type": "string", "description": "Profile id or name to stop."},
    },
    "required": ["profile"],
}

SCHEMA_LIST = {"type": "object", "properties": {}}

_KIND_ENUM = [
    "recaptcha_v2", "recaptcha_v3", "recaptcha_enterprise",
    "hcaptcha", "turnstile", "funcaptcha",
    "geetest", "geetest_v4", "amazon_waf",
    "friendly_captcha", "keycaptcha", "datadome",
    "kasada", "akamai", "imperva",
    "lemin", "mtcaptcha", "cybersiara", "cutcaptcha",
    "capy", "yandex", "tencent", "image",
]

SCHEMA_SOLVE_CAPTCHA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": _KIND_ENUM,
                 "description": "Captcha family. Use cloak_detect_captcha first if unsure."},
        "site_key": {"type": "string", "description": "Public site key / pkey / sitekey. Empty for image/geetest/datadome (use extra)."},
        "url": {"type": "string", "description": "Page URL where the captcha lives."},
        "extra": {"type": "object",
                  "description": "Kind-specific extras (action, min_score, gt, challenge, captcha_url, body, iv, context, user_id, etc.). See plugin README.",
                  "default": {}},
        "provider": {"type": "string", "enum": ["auto", "capsolver", "2captcha"],
                     "description": "Force a specific backend. Default = auto (router picks).",
                     "default": "auto"},
    },
    "required": ["kind", "url"],
}

SCHEMA_DETECT_CAPTCHA = {
    "type": "object",
    "properties": {},
    "description": "Detect any captcha currently rendered on the active CloakBrowser tab. "
                   "Returns {kind, site_key, page_url, extra, confidence}. kind=null = no captcha.",
}


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------


async def cloak_create_profile(args: dict, **kw: Any) -> Dict[str, Any]:
    """Create a stealth profile on the manager. Returns the new profile record."""
    task_id = kw.get("task_id")
    name = str(args.get("name") or "").strip()
    if not name:
        return _missing_name_error("cloak_create_profile", "name")
    try:
        tags = _normalize_profile_tags(args.get("tags"))
    except ValueError as exc:
        return {"error": f"invalid profile tags: {exc}", "code": "invalid_tags"}
    try:
        proxy, claim_owner = _prepare_profile_proxy(
            name, args.get("proxy", ""), args.get("use_pool")
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"proxy configuration error: {redact_cdp_url(exc)}", "code": "proxy_unavailable"}
    humanize = args.get("humanize", True)
    human_preset = args.get("human_preset", "default")
    headless = args.get("headless", False)
    geoip = args.get("geoip", True)
    notes = args.get("notes", "")
    auto_launch = args.get("auto_launch", False)
    body: Dict[str, Any] = {
        "name": name,
        "humanize": humanize,
        "human_preset": human_preset,
        "headless": headless,
        "geoip": geoip,
        "tags": tags,
        "notes": notes,
        "auto_launch": auto_launch,
    }
    if proxy:
        body["proxy"] = proxy
    for key, value in args.items():
        if key not in body and key not in ("name", "use_pool", "proxy"):
            body[key] = value

    try:
        async with ManagerClient() as mgr:
            result = await mgr.create_profile(**body)
    except ManagerError as exc:
        if int(getattr(exc, "status_code", 0) or 0) <= 0:
            logger.warning("cloak_create_profile outcome is unknown; retaining proxy claim")
            return {
                "error": f"profile creation outcome is unknown: {redact_cdp_url(exc)}",
                "status_code": getattr(exc, "status_code", 0),
                "proxy_claim_retained": bool(claim_owner),
            }
        result: Dict[str, Any] = {"error": redact_cdp_url(exc), "status_code": exc.status_code}
        try:
            released = _release_pool_claim(claim_owner)
            if claim_owner:
                result["proxy_released"] = released
        except Exception as release_exc:  # noqa: BLE001
            logger.error("Could not release proxy claim after confirmed create failure: %s", redact_cdp_url(release_exc))
            result["proxy_release_error"] = redact_cdp_url(release_exc)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("cloak_create_profile outcome is unknown; retaining proxy claim: %s", redact_cdp_url(exc))
        return {
            "error": f"profile creation outcome is unknown: {redact_cdp_url(exc)}",
            "proxy_claim_retained": bool(claim_owner),
        }
    profile_id = str(result.get("id") or result.get("profile_id") or "")
    if profile_id:
        profile_state.remember_profile(
            task_id,
            profile_id=profile_id,
            profile_name=str(result.get("name") or name),
            proxy=proxy,
            source="cloak_create_profile",
        )
        result["task_id"] = profile_state.task_key(task_id)
        result["remembered_for_task"] = True
        result["launch_next_with_profile_id"] = profile_id
    try:
        from ..proxy_format import mask_proxy

        if result.get("proxy"):
            result = dict(result)
            result["proxy"] = mask_proxy(result["proxy"])
    except Exception:  # noqa: BLE001
        pass
    return result



async def cloak_launch(args: dict, **kw: Any) -> Dict[str, Any]:
    """Launch a profile and bind its CDP URL to the current process env."""
    profile = args.get("profile", "")
    return await _launch_profile(
        profile,
        task_id=kw.get("task_id"),
        allow_profile_switch=_profile_switch_allowed(args.get("allow_profile_switch")),
    )


async def _profile_display_name(
    mgr: ManagerClient,
    requested: str,
    profile_id: str,
    binding: Optional[Dict[str, Any]],
) -> str:
    """Name of the profile actually being launched.

    Reporting ``binding["profile_name"]`` meant a launch that switched profiles
    answered with the name of the *previous* one, so "back to wf-alpha" replied
    "wf-beta" and the model could not tell whether the switch had happened.
    """
    value = str(requested or "").strip()
    if value and not profile_state.is_uuid(value):
        return value  # resolved by name, so that is the name
    try:
        record = await mgr.get_profile(profile_id)
        name = str((record or {}).get("name") or "").strip()
        if name:
            return name
    except Exception:  # noqa: BLE001
        pass
    return str((binding or {}).get("profile_name") or value)


async def _launch_profile(
    profile: str,
    *,
    task_id: Any = None,
    allow_profile_switch: bool = False,
) -> Dict[str, Any]:
    binding_before = profile_state.get_binding(task_id)
    async with ManagerClient() as mgr:
        try:
            profile_id = await _resolve_profile_id(mgr, profile, task_id=task_id)
            launched_name = await _profile_display_name(mgr, profile, profile_id, binding_before)
            guard = _profile_switch_guard(
                binding_before,
                requested_profile_id=profile_id,
                allow_profile_switch=allow_profile_switch,
            )
            if guard is not None:
                return guard
            resp = await mgr.launch(profile_id)
        except ManagerError as exc:
            if exc.status_code == 409 and "already running" in exc.body.lower():
                resp = {
                    "profile_id": profile_id,
                    "status": "running",
                    "cdp_url": f"/api/profiles/{profile_id}/cdp",
                    "already_running": True,
                }
            else:
                out: Dict[str, Any] = {
                    "error": redact_cdp_url(exc),
                    "status_code": exc.status_code,
                }
                # The remembered profile is gone from Manager (deleted out of
                # band, or the profile store was reset). Drop the binding so the
                # next call starts clean instead of re-launching a ghost every
                # time a browser tool fires.
                if exc.status_code == 404 and _binding_points_at(binding_before, profile_id):
                    profile_state.clear_binding(task_id, profile_id=profile_id)
                    out["stale_binding_cleared"] = True
                return out

        launched_profile_id = str(resp.get("profile_id") or profile_id)
        if launched_profile_id != profile_id:
            return {
                "error": "Cloak launch returned a different profile_id than requested.",
                "requested_profile_id": profile_id,
                "launched_profile_id": launched_profile_id,
                "task_id": profile_state.task_key(task_id),
            }

        cdp_rel = resp.get("cdp_url", "")
        cdp_abs = ""
        if cdp_rel:
            cdp_abs = await mgr.bind_browser_cdp_env(cdp_rel)
            cdp_profile_id = profile_state.profile_id_from_cdp(cdp_abs or cdp_rel)
            if cdp_profile_id and cdp_profile_id != profile_id:
                return {
                    "error": "Resolved CDP URL points at a different profile.",
                    "requested_profile_id": profile_id,
                    "cdp_profile_id": cdp_profile_id,
                    "cdp_url": redact_cdp_url(cdp_abs),
                    "task_id": profile_state.task_key(task_id),
                }

        old_cdp = str((binding_before or {}).get("cdp_url") or "")
        if old_cdp and cdp_abs and old_cdp != cdp_abs:
            await get_pool().drop(old_cdp)

        try:
            status_after = await mgr.profile_status(profile_id)
        except ManagerError:
            status_after = {}

        profile_state.remember_profile(
            task_id,
            profile_id=profile_id,
            profile_name=str((binding_before or {}).get("profile_name") or profile),
            cdp_url=cdp_abs,
            cdp_http_url=os.environ.get("CLOAK_CDP_HTTP_URL", ""),
            proxy=(binding_before or {}).get("proxy"),
            source="cloak_launch",
        )

        return {
            "profile_id": resp.get("profile_id", profile_id),
            "profile_name": launched_name,
            "status": resp.get("status"),
            "status_after": status_after.get("status"),
            "cdp_url": redact_cdp_url(cdp_abs),
            "vnc_ws_port": resp.get("vnc_ws_port"),
            "display": resp.get("display"),
            "already_running": bool(resp.get("already_running")),
            "active": True,
            "task_id": profile_state.task_key(task_id),
        }


async def cloak_set_active(args: dict, **kw: Any) -> Dict[str, Any]:
    """Find-or-create the profile, ensure it's running, set env. One-shot."""
    return await set_active_profile(
        args.get("profile", ""),
        create_if_missing=args.get("create_if_missing", True),
        humanize=args.get("humanize", True),
        human_preset=args.get("human_preset", "default"),
        proxy=args.get("proxy", ""),
        use_pool=args.get("use_pool"),
        task_id=kw.get("task_id"),
        allow_profile_switch=_profile_switch_allowed(args.get("allow_profile_switch")),
    )


async def set_active_profile(
    profile: str,
    *,
    create_if_missing: bool = True,
    humanize: bool = True,
    human_preset: str = "default",
    proxy: str = "",
    use_pool: Optional[bool] = None,
    task_id: Any = None,
    allow_profile_switch: bool = False,
) -> Dict[str, Any]:
    """Core find-or-create + launch logic (used by hooks and cloak_set_active)."""
    profile = str(profile or "").strip()
    if not profile:
        return _missing_name_error("cloak_set_active", "profile")
    binding_before = profile_state.get_binding(task_id)
    # Only the create branch needs a proxy; resolve from the pool lazily so we
    # never burn a pool slot when the profile already exists.
    async with ManagerClient() as mgr:
        try:
            existing = await mgr.get_profile(profile) if profile_state.is_uuid(profile) else await mgr.find_profile_by_name(profile)
            if existing is None:
                if not create_if_missing:
                    return {"error": f"profile '{profile}' not found"}
                try:
                    proxy, claim_owner = _prepare_profile_proxy(profile, proxy, use_pool)
                except Exception as exc:  # noqa: BLE001
                    return {"error": f"proxy configuration error: {redact_cdp_url(exc)}", "code": "proxy_unavailable"}
                try:
                    existing = await mgr.create_profile(
                        name=profile,
                        humanize=humanize,
                        human_preset=human_preset,
                        proxy=proxy,
                    )
                except ManagerError as exc:
                    result: Dict[str, Any] = {"error": redact_cdp_url(exc), "status_code": exc.status_code}
                    if int(getattr(exc, "status_code", 0) or 0) <= 0:
                        logger.warning("set_active create outcome is unknown; retaining proxy claim")
                        return {
                            "error": f"profile creation outcome is unknown: {redact_cdp_url(exc)}",
                            "status_code": getattr(exc, "status_code", 0),
                            "proxy_claim_retained": bool(claim_owner),
                        }
                    try:
                        released = _release_pool_claim(claim_owner)
                        if claim_owner:
                            result["proxy_released"] = released
                    except Exception as release_exc:  # noqa: BLE001
                        logger.error("Could not release proxy claim after confirmed create failure: %s", redact_cdp_url(release_exc))
                        result["proxy_release_error"] = redact_cdp_url(release_exc)
                    return result
                except Exception as exc:  # noqa: BLE001
                    logger.error("set_active create outcome is unknown; retaining proxy claim: %s", redact_cdp_url(exc))
                    return {
                        "error": f"profile creation outcome is unknown: {redact_cdp_url(exc)}",
                        "proxy_claim_retained": bool(claim_owner),
                    }
                profile_state.remember_profile(
                    task_id,
                    profile_id=str(existing.get("id") or ""),
                    profile_name=str(existing.get("name") or profile),
                    proxy=proxy,
                    source="cloak_set_active.create",
                )

            profile_id = existing["id"]
            guard = _profile_switch_guard(
                binding_before,
                requested_profile_id=profile_id,
                allow_profile_switch=allow_profile_switch,
            )
            if guard is not None:
                return guard
            status = await mgr.profile_status(profile_id)
            launched = False
            if status.get("status") != "running":
                try:
                    launch_resp = await mgr.launch(profile_id)
                    launched = True
                except ManagerError as exc:
                    if exc.status_code == 409 and "already running" in exc.body.lower():
                        launch_resp = {
                            "profile_id": profile_id,
                            "status": "running",
                            "cdp_url": existing.get("cdp_url", f"/api/profiles/{profile_id}/cdp"),
                            "already_running": True,
                        }
                    else:
                        raise
            else:
                launch_resp = {
                    "profile_id": profile_id,
                    "status": "running",
                    "cdp_url": existing.get("cdp_url", f"/api/profiles/{profile_id}/cdp"),
                }
        except ManagerError as exc:
            return {"error": redact_cdp_url(exc), "status_code": exc.status_code}

        cdp_abs = await mgr.bind_browser_cdp_env(
            launch_resp.get("cdp_url", f"/api/profiles/{profile_id}/cdp")
        )
        cdp_profile_id = profile_state.profile_id_from_cdp(cdp_abs)
        if cdp_profile_id and cdp_profile_id != profile_id:
            return {
                "error": "Resolved CDP URL points at a different profile.",
                "requested_profile_id": profile_id,
                "cdp_profile_id": cdp_profile_id,
                "cdp_url": redact_cdp_url(cdp_abs),
                "task_id": profile_state.task_key(task_id),
            }
        old_cdp = str((binding_before or {}).get("cdp_url") or "")
        if old_cdp and old_cdp != cdp_abs:
            await get_pool().drop(old_cdp)
        profile_state.remember_profile(
            task_id,
            profile_id=profile_id,
            profile_name=str(existing.get("name") or profile),
            cdp_url=cdp_abs,
            cdp_http_url=os.environ.get("CLOAK_CDP_HTTP_URL", ""),
            proxy=proxy if proxy else (binding_before or {}).get("proxy"),
            source="cloak_set_active",
        )
        # Stamp CLOAK_ACTIVE_TASK_ID so provider.create_session won't steal
        # this CDP URL for a different task.
        profile_state.activate_task_binding(task_id)
        try:
            from plugins.browser.cloak import session_leases

            session_leases.put(
                session_leases.Lease(
                    task_id=profile_state.task_key(task_id),
                    profile_id=str(profile_id),
                    cdp_url=cdp_abs,
                    cdp_http_url=os.environ.get("CLOAK_CDP_HTTP_URL", ""),
                    profile_name=str(existing.get("name") or profile),
                    features={"stealth": True, "humanize": True, "prebound": True},
                )
            )
            session_leases.bind_env_for_task(profile_state.task_key(task_id))
        except Exception:  # noqa: BLE001
            pass
        return {
            "profile_id": profile_id,
            "profile_name": str(existing.get("name") or profile),
            "cdp_url": redact_cdp_url(cdp_abs),
            "already_running": bool(launch_resp.get("already_running")),
            "active": True,
            "launched": launched,
            "task_id": profile_state.task_key(task_id),
        }


async def cloak_proxy_pool(args: dict | None = None, **kw: Any) -> Dict[str, Any]:
    """Inspect / mutate the proxy pool (``/etc/cloak/proxies.json``).

    Lets the agent honour 'use a proxy from the pool' without any extra skill:
    pick the next proxy and pass it to cloak_create_profile / cloak_set_active,
    or just enable auto-assign from the dashboard.
    """
    args = args or {}
    action = (args.get("action") or "list").lower().strip()
    try:
        from .. import proxy_format as pf
    except Exception as exc:  # noqa: BLE001
        return {"error": f"proxy pool unavailable: {redact_cdp_url(exc)}"}

    if action == "next":
        profile_name = str(args.get("claim_as") or "").strip()
        if not profile_name:
            return {"error": "claim_as (the profile name) is required to reserve a proxy"}
        try:
            chosen = pf.claim_proxy(pf.profile_claim_owner(profile_name))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"proxy reservation failed: {redact_cdp_url(exc)}"}
        if not chosen:
            return {"reserved": False, "note": "pool is empty or exhausted"}
        return {"reserved": True, "profile": profile_name, "masked": pf.mask_proxy(chosen)}

    if action == "release":
        profile_name = str(args.get("claim_as") or "").strip()
        if not profile_name:
            return {"error": "claim_as (the profile name) is required to release a proxy"}
        try:
            released = pf.release_proxy(pf.profile_claim_owner(profile_name))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"proxy release failed: {redact_cdp_url(exc)}"}
        return {"released": released, "profile": profile_name}


    if action == "add":
        ok, bad = pf.parse_lines(args.get("text", ""), args.get("default_scheme", "http"))
        pool = pf.add_proxies(ok)
        return {
            "added": len(ok),
            "invalid": [pf.describe_invalid(item) for item in bad],
            "invalid_hint": (
                "Lines above could not be parsed. Accepted: host:port, "
                "host:port:user:pass, user:pass@host:port, scheme://... "
                "(socks4 is rejected — CloakBrowser cannot speak it)."
            ) if bad else "",
            "count": len(pool.get("proxies") or []),
        }

    if action == "clear":
        pf.clear_pool()
        return {"cleared": True, "count": 0}

    # list (default)
    pool = pf.load_pool()
    proxies = pool.get("proxies") or []
    return {
        "count": len(proxies),
        "strategy": pool.get("strategy", "round_robin"),
        "auto_assign": pf.pool_enabled(),
        "proxies": [pf.mask_proxy(p) for p in proxies],
    }


async def cloak_stop(args: dict, **kw: Any) -> Dict[str, Any]:
    """Stop a profile + drop its cached Playwright client from the pool."""
    task_id = kw.get("task_id")
    profile = args.get("profile", "")
    binding = profile_state.get_binding(task_id)
    profile_name = str((binding or {}).get("profile_name") or "")
    if not profile_name and not profile_state.is_uuid(profile):
        profile_name = str(profile or "").strip()

    already_stopped = False
    async with ManagerClient() as mgr:
        try:
            profile_id = await _resolve_profile_id(mgr, profile, task_id=task_id)
        except ManagerError as exc:
            return {"error": redact_cdp_url(exc), "status_code": exc.status_code}
        if not profile_name:
            try:
                record = await mgr.get_profile(profile_id)
                profile_name = str(record.get("name") or "")
            except Exception:  # noqa: BLE001
                pass
        try:
            await mgr.stop(profile_id)
        except ManagerError as exc:
            # "Already stopped" is the state the caller asked for. Report it and
            # still release the lease, pooled client and proxy claim — bailing
            # out here used to strand all three and drive the agent into a
            # retry loop against a profile that was already down.
            if not _is_already_stopped(exc):
                return {"error": redact_cdp_url(exc), "status_code": exc.status_code}
            already_stopped = True
        cdp_abs = mgr.absolute_cdp_url(f"/api/profiles/{profile_id}/cdp")

    await get_pool().drop(cdp_abs)
    if binding and binding.get("profile_id") == profile_id and binding.get("cdp_url"):
        await get_pool().drop(str(binding["cdp_url"]))
        profile_state.clear_binding(task_id, profile_id=profile_id)
    else:
        profile_state.clear_binding(profile_id=profile_id)

    # If we were active on this profile, clear the env.
    profile_state.clear_env_if_profile(profile_id, cdp_abs)

    result: Dict[str, Any] = {
        "profile_id": profile_id,
        "stopped": True,
        "already_stopped": already_stopped,
    }
    result.update(_stop_cdp_supervisor(task_id, profile_id))
    try:
        from .. import session_leases

        task_key = profile_state.task_key(task_id)
        lease = session_leases.pop(task_key)
        if lease is None:
            session_leases.pop_profile(profile_id)
        session_leases.clear_env_if_matches(profile_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Profile %s stopped but session lease cleanup failed: %s", profile_id, redact_cdp_url(exc))
        result["lease_cleanup_error"] = redact_cdp_url(exc)

    try:
        owners = [profile_id]
        if profile_name:
            from ..proxy_format import profile_claim_owner

            owners.append(profile_claim_owner(profile_name))
        result["proxy_released"] = sum(
            _release_pool_claim(owner) for owner in dict.fromkeys(owners)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Profile %s stopped but proxy claim cleanup failed: %s", profile_id, redact_cdp_url(exc))
        result["proxy_release_error"] = redact_cdp_url(exc)
    return result


async def cloak_list_profiles(args: dict | None = None, **kw: Any) -> Dict[str, Any]:
    async with ManagerClient() as mgr:
        try:
            profiles = await mgr.list_profiles()
        except ManagerError as exc:
            return {"error": redact_cdp_url(exc), "status_code": exc.status_code}
    # Trim to a compact form the LLM can scan. The proxy is masked — the model
    # needs to know *which* profile carries *which* egress, never the password.
    from ..proxy_format import mask_proxy

    return {
        "profiles": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "status": p.get("status"),
                "humanize": p.get("humanize"),
                "tags": p.get("tags", []),
                "has_proxy": bool(p.get("proxy")),
                "proxy": mask_proxy(p.get("proxy")) if p.get("proxy") else "",
                "created_at": p.get("created_at"),
            }
            for p in profiles
        ]
    }


async def cloak_solve_captcha(args: dict, **kw: Any) -> str:
    """Try to solve a captcha through the configured providers (CapSolver
    and/or 2captcha) in the order best for ``kind``. On every failure path
    (no API key, balance, unsolvable, timeout, unsupported kind) return
    the MANUAL_INTERVENTION_REQUIRED sentinel so the agent triggers
    kanban_block.

    ``extra`` carries kind-specific fields (e.g. ``{"action": "login",
    "min_score": 0.9}`` for recaptcha v3, ``{"gt": "...", "challenge":
    "..."}`` for geetest, ``{"captcha_url": "..."}`` for datadome, etc.).
    See the plugin README for the per-kind extra schema.
    """
    kind = args.get("kind", "")
    url = args.get("url", "")
    site_key = args.get("site_key", "")
    extra = args.get("extra")
    provider = args.get("provider", "auto")
    router = CaptchaRouter(override_provider=provider)
    try:
        token = await router.solve(kind, site_key=site_key, url=url, extra=extra or {})
        return token
    except ManualInterventionRequired as exc:
        logger.warning("cloak_solve_captcha kind=%s: %s — manual gate", kind, exc.reason)
        return MANUAL_INTERVENTION_REQUIRED
    except Exception as exc:  # noqa: BLE001
        logger.error("cloak_solve_captcha unexpected error: %s", redact_cdp_url(exc))
        return MANUAL_INTERVENTION_REQUIRED


async def cloak_detect_captcha(args: dict | None = None, **kw: Any) -> Dict[str, Any]:
    """Inspect the active CloakBrowser tab and classify any captcha.

    Returns ``{"kind": <str|null>, "site_key": ..., "page_url": ...,
    "extra": {...}, "confidence": "high|medium|low"}`` or an error dict
    if no active profile is bound.

    ``kind == null`` means no captcha detected — proceed normally.
    """
    cdp_url = profile_state.cdp_url_for_task(kw.get("task_id"))
    if not cdp_url:
        return {
            "error": "No Cloak CDP binding for this task. Call cloak_set_active(profile=...) first.",
        }
    preset = os.environ.get("CLOAK_HUMAN_PRESET", "default")
    try:
        async with get_pool().hold(cdp_url, preset=preset) as client:
            return await detect_in_playwright_page(client.page)
    except Exception as exc:  # noqa: BLE001
        safe_error = redact_cdp_url(str(exc))
        logger.error("cloak_detect_captcha failed: %s", safe_error)
        return {
            "error": f"captcha detection failed: {safe_error}",
            "cdp_url": redact_cdp_url(cdp_url),
        }


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


async def _resolve_profile_id(
    mgr: ManagerClient, profile: str, *, task_id: Any = None
) -> str:
    """Accept either a UUID or a profile name; return the UUID.

    An empty argument resolves to the profile this task is bound to — never to
    a name lookup. Manager happily stores profiles with an empty name, so a
    blank lookup used to match whichever nameless leftover came first and then
    stop/launch operated on a stranger's profile.
    """
    value = str(profile or "").strip()
    if not value:
        binding = profile_state.get_binding(task_id)
        bound = str((binding or {}).get("profile_id") or "").strip()
        if bound:
            return bound
        raise ManagerError(
            400,
            "",
            "no profile was given and this task has no remembered Cloak profile; "
            "pass profile=<id or name>",
        )
    # Fast path: looks like a UUID, just use it.
    if profile_state.is_uuid(value):
        return value
    existing = await mgr.find_profile_by_name(value)
    if existing is None:
        raise ManagerError(404, value, "profile not found by name")
    return existing["id"]


def _profile_switch_allowed(explicit: Optional[bool]) -> bool:
    """Whether this task may move to a profile other than the one it remembers.

    An explicit argument always wins. Otherwise ``CLOAK_ALLOW_PROFILE_SWITCH``
    decides: leave it off and a task stays pinned to one profile (protects a
    long registration from being hijacked mid-flow); turn it on and the operator
    can say "switch to profile X" and have it just happen.
    """
    if explicit is not None:
        return bool(explicit)
    from ..proxy_format import setting

    return setting("CLOAK_ALLOW_PROFILE_SWITCH").strip().lower() in {"1", "true", "yes", "on"}


def _binding_points_at(binding: Optional[Dict[str, Any]], profile_id: str) -> bool:
    return bool(binding) and str((binding or {}).get("profile_id") or "") == str(profile_id)


def _stop_cdp_supervisor(task_id: Any, profile_id: str) -> Dict[str, Any]:
    """Tear down the CDP supervisor that was watching the stopped profile.

    Hermes keeps one supervisor per task, holding a live websocket to the
    profile's CDP endpoint. Stopping the profile leaves that socket pointed at
    a dead endpoint, and Manager answers a websocket upgrade on a stopped
    profile with ``403 Forbidden`` (plain HTTP gets ``404``). The supervisor
    then retries the dead URL for every later ``browser_*`` call, so the agent
    sees an unexplained 403 loop instead of "nothing is running, launch one".

    Only the supervisor actually bound to *this* profile is stopped — another
    profile in the same task must keep running.
    """
    key = profile_state.task_key(task_id)
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY

        supervisor = SUPERVISOR_REGISTRY.get(key)
        if supervisor is None:
            return {}
        if profile_id and profile_id not in str(getattr(supervisor, "cdp_url", "")):
            return {}
        SUPERVISOR_REGISTRY.stop(key)
        logger.info("Stopped CDP supervisor for task %s (profile %s)", key, profile_id)
        return {"cdp_supervisor_stopped": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Profile %s stopped but its CDP supervisor could not be torn down: %s",
            profile_id, redact_cdp_url(exc),
        )
        return {"cdp_supervisor_error": redact_cdp_url(exc)}


def _is_already_stopped(exc: ManagerError) -> bool:
    """True when Manager refused a stop because the profile is not running."""
    if int(getattr(exc, "status_code", 0) or 0) not in (404, 409):
        return False
    return "not running" in str(getattr(exc, "body", "") or "").lower()


def _profile_switch_guard(
    binding: Optional[Dict[str, Any]],
    *,
    requested_profile_id: str,
    allow_profile_switch: bool,
) -> Optional[Dict[str, Any]]:
    expected = str((binding or {}).get("profile_id") or "")
    if not expected or expected == requested_profile_id or allow_profile_switch:
        return None
    return {
        "error": (
            "Task is already bound to a different Cloak profile. "
            "Use the remembered profile_id, create a new task, or pass "
            "allow_profile_switch=true intentionally."
        ),
        "expected_profile_id": expected,
        "requested_profile_id": requested_profile_id,
        "task_id": str((binding or {}).get("task_id") or ""),
        "active_profile_name": str((binding or {}).get("profile_name") or ""),
    }
