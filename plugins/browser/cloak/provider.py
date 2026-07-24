"""Cloak (CloakBrowser) cloud-style browser provider — first-class backend.

Subclasses :class:`agent.browser_provider.BrowserProvider`. Unlike Browserbase
(a paid SaaS) Cloak talks to a *self-hosted* CloakBrowser-Manager (FastAPI on
:8080 by default) that owns anti-detect Chromium profiles. Each profile exposes
a CDP endpoint; this provider find-or-creates a profile, launches it, resolves
the authenticated CDP websocket URL and hands it back to
:mod:`tools.browser_tool` like any other cloud provider.

This module is intentionally dependency-light (stdlib + ``requests``) so the
provider always imports and shows up in the ``hermes tools`` picker even before
the heavyweight ``cloakbrowser`` / ``playwright`` stack is installed. The rich
``cloak_*`` management tools, captcha solving, humanized input and Gmail Factory
live in :mod:`plugins.browser.cloak._impl` and register separately (best-effort)
from :func:`plugins.browser.cloak.register`.

Config keys this provider responds to::

    browser:
      cloud_provider: "cloak"

Env vars::

    CLOAK_MANAGER_URL=http://127.0.0.1:8080   # CloakBrowser-Manager base URL
    CLOAK_AUTH_TOKEN=...                       # optional shared Bearer token
    CLOAK_PROFILE=...                          # optional fixed profile name
    CLOAK_MANAGER_ENV=/etc/cloak/manager.env   # optional env file to merge
    CLOAK_CDP_PROXY_BASE=http://127.0.0.1:8081 # optional auth-injecting CDP proxy
"""

from __future__ import annotations

import logging
import os
import uuid
from agent.redact import redact_cdp_url
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from agent.browser_provider import BrowserProvider

from plugins.browser.cloak import session_leases
from plugins.browser.cloak.paths import manager_env_file

logger = logging.getLogger(__name__)

DEFAULT_MANAGER_URL = "http://127.0.0.1:8080"
_ALLOWED_MANAGER_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
}

_env_loaded = False


def _bootstrap_env(env_file: Optional[str] = None) -> None:
    """Merge manager.env into ``os.environ`` (once, non-clobbering).

    Mirrors :mod:`plugins.browser.cloak._impl.env_bootstrap` but kept inline so
    the provider has no dependency on the heavy ``_impl`` package import.
    """
    global _env_loaded
    if _env_loaded:
        return
    path = env_file or str(manager_env_file())
    if path and os.path.isfile(path):
        try:
            from plugins.browser.cloak.env_file import parse_env_file

            for key, value in parse_env_file(path).items():
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError as exc:
            logger.warning("Cloak: could not read %s: %s", path, exc)
    _env_loaded = True



def _release_proxy_claim(claim_owner: str) -> None:
    if not claim_owner:
        return
    from plugins.browser.cloak.proxy_format import release_proxy

    release_proxy(claim_owner)

def _host_allowed(url: str) -> bool:
    """Allow localhost by default; extra hosts via CLOAK_ALLOWED_HOSTS."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in _ALLOWED_MANAGER_HOSTS:
        return True
    extra = os.environ.get("CLOAK_ALLOWED_HOSTS", "")
    allowed = {h.strip().lower() for h in extra.split(",") if h.strip()}
    return host in allowed


class CloakBrowserProvider(BrowserProvider):
    """CloakBrowser-Manager anti-detect browser backend.

    Self-hosted, free. The CDP URL points at a stealth Chromium profile managed
    by CloakBrowser-Manager rather than a paid cloud session.
    """

    @property
    def name(self) -> str:
        return "cloak"

    @property
    def display_name(self) -> str:
        return "Cloak"

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        _bootstrap_env()
        return os.environ.get("CLOAK_MANAGER_URL", DEFAULT_MANAGER_URL).rstrip("/")

    def _auth_token(self) -> Optional[str]:
        _bootstrap_env()
        return os.environ.get("CLOAK_AUTH_TOKEN") or None

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self._auth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def is_available(self) -> bool:
        """Available when a CloakBrowser-Manager URL is configured.

        Cheap, no network: just checks the env (after merging manager.env).
        The manager URL has a localhost default, so we treat the provider as
        available whenever the operator has opted into Cloak by setting the
        URL explicitly or shipping ``/etc/cloak/manager.env``.
        """
        _bootstrap_env()
        return bool(os.environ.get("CLOAK_MANAGER_URL"))

    # ------------------------------------------------------------------
    # CDP resolution helpers
    # ------------------------------------------------------------------

    def _absolute_cdp_url(self, base_url: str, cdp_path: str) -> str:
        if cdp_path.startswith(("http://", "https://", "ws://", "wss://")):
            return cdp_path
        if not cdp_path.startswith("/"):
            cdp_path = "/" + cdp_path
        return f"{base_url}{cdp_path}"

    def _resolve_cdp_ws(self, cdp_http_url: str) -> str:
        """Fetch ``/json/version`` with auth and return webSocketDebuggerUrl.

        Native agent-browser cannot attach a Bearer header on the websocket
        upgrade, so we resolve the WS URL here (auth applied) and hand that to
        the dispatcher. When ``CLOAK_CDP_PROXY_BASE`` is set we rewrite both
        URLs to point at a localhost auth-injecting proxy instead.
        """
        auth_required = bool(self._auth_token())
        proxy_base = os.environ.get("CLOAK_CDP_PROXY_BASE", "").strip().rstrip("/")
        if auth_required and not proxy_base:
            raise RuntimeError(
                "Authenticated Cloak CDP requires CLOAK_CDP_PROXY_BASE. "
                "Run the Cloak installer or repair the local CDP bridge."
            )

        base = cdp_http_url.rstrip("/")
        cdp_ws = cdp_http_url
        try:
            resp = requests.get(
                f"{base}/json/version", headers=self._headers(), timeout=15
            )
            if resp.status_code == 200:
                ws = str(resp.json().get("webSocketDebuggerUrl") or "").strip()
                if ws:
                    cdp_ws = ws
            else:
                logger.warning(
                    "Cloak CDP /json/version returned %s for %s",
                    resp.status_code,
                    redact_cdp_url(base),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cloak CDP WS resolve failed for %s: %s", redact_cdp_url(base), redact_cdp_url(exc))

        proxy_base = os.environ.get("CLOAK_CDP_PROXY_BASE", "").strip().rstrip("/")
        if proxy_base:
            proxy = urlparse(proxy_base)
            proxy_netloc = proxy.netloc or proxy.path
            if proxy.scheme not in {"http", "https"} or not proxy_netloc:
                raise RuntimeError(
                    "CLOAK_CDP_PROXY_BASE must be a valid http(s) bridge URL."
                )
            if proxy_netloc:
                ws = urlparse(cdp_ws)
                scheme = "wss" if proxy.scheme == "https" else "ws"
                cdp_ws = f"{scheme}://{proxy_netloc}{ws.path or ''}" + (
                    f"?{ws.query}" if ws.query else ""
                )
                logger.info("Cloak CDP WS rewritten via CLOAK_CDP_PROXY_BASE=%s", redact_cdp_url(proxy_base))
        return cdp_ws

    # ------------------------------------------------------------------
    # Manager REST helpers (sync)
    # ------------------------------------------------------------------

    def _find_profile_by_name(self, base_url: str, name: str) -> Optional[Dict[str, Any]]:
        resp = requests.get(
            f"{base_url}/api/profiles", headers=self._headers(), timeout=30
        )
        if not resp.ok:
            raise RuntimeError(
                f"Cloak list profiles failed: {resp.status_code} {redact_cdp_url(resp.text[:200])}"
            )
        for profile in resp.json() or []:
            if profile.get("name") == name:
                return profile
        return None

    def _create_profile(self, base_url: str, name: str) -> Dict[str, Any]:
        body = {
            "name": name,
            "humanize": True,
            "human_preset": os.environ.get("CLOAK_HUMAN_PRESET", "default"),
            "headless": os.environ.get("CLOAK_HEADLESS", "false").lower() == "true",
            "geoip": True,
        }
        configured_proxy = os.environ.get("CLOAK_PROXY", "").strip()
        claim_owner = ""
        try:
            from plugins.browser.cloak.proxy_format import (
                pool_enabled,
                profile_claim_owner,
                resolve_proxy,
            )

            if not configured_proxy and pool_enabled():
                claim_owner = profile_claim_owner(name)
            proxy = resolve_proxy(
                configured_proxy,
                claim_as=claim_owner or None,
                fail_closed=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Cloak proxy configuration error: {redact_cdp_url(exc)}") from exc
        if proxy:
            body["proxy"] = proxy
            logger.info("Cloak: profile %s using pool proxy", name)
        try:
            resp = requests.post(
                f"{base_url}/api/profiles",
                headers=self._headers(),
                json=body,
                timeout=30,
            )
        except requests.RequestException:
            logger.warning("Cloak create request outcome unknown; retaining proxy claim for %s", name)
            raise
        if not resp.ok:
            try:
                _release_proxy_claim(claim_owner)
            except Exception as exc:  # noqa: BLE001
                logger.error("Cloak: could not release proxy claim after create failure: %s", redact_cdp_url(exc))
            raise RuntimeError(
                f"Cloak create profile failed: {resp.status_code} {redact_cdp_url(resp.text[:200])}"
            )
        return resp.json()

    def _launch_profile(self, base_url: str, profile_id: str) -> Dict[str, Any]:
        resp = requests.post(
            f"{base_url}/api/profiles/{profile_id}/launch",
            headers=self._headers(),
            timeout=60,
        )
        if resp.status_code == 409 and "already running" in resp.text.lower():
            return {
                "profile_id": profile_id,
                "status": "running",
                "cdp_url": f"/api/profiles/{profile_id}/cdp",
                "already_running": True,
            }
        if not resp.ok:
            raise RuntimeError(
                f"Cloak launch failed: {resp.status_code} {redact_cdp_url(resp.text[:200])}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, task_id: str) -> Dict[str, object]:
        # Prefer a task-scoped lease if we already created one for this task.
        existing = session_leases.get(task_id)
        if existing:
            session_leases.bind_env_for_task(task_id)
            try:
                from plugins.browser.cloak._impl import profile_state

                profile_state.remember_profile(
                    task_id,
                    profile_id=existing.profile_id,
                    profile_name=existing.profile_name,
                    cdp_url=existing.cdp_url,
                    cdp_http_url=existing.cdp_http_url,
                    source="lease_reuse",
                )
            except Exception:  # noqa: BLE001
                pass
            logger.info("Cloak: reusing task lease for %s profile=%s", task_id, existing.profile_id)
            return {
                "session_name": f"cloak_{task_id}_{uuid.uuid4().hex[:8]}",
                "bb_session_id": existing.profile_id,
                "cdp_url": existing.cdp_url,
                "features": {**existing.features, "reused_lease": True},
            }

        # Out-of-band activation (cloak_set_active) may set BROWSER_CDP_URL.
        # ONLY adopt it when it was bound to THIS task — otherwise task-b
        # steals task-a's CDP session (critical isolation bug).
        active_task = os.environ.get("CLOAK_ACTIVE_TASK_ID", "").strip()
        preset_cdp = os.environ.get("BROWSER_CDP_URL", "").strip()
        preset_profile = os.environ.get("CLOAK_ACTIVE_PROFILE_ID", "").strip()
        if preset_cdp and active_task and active_task == str(task_id):
            lease = session_leases.put(
                session_leases.Lease(
                    task_id=task_id,
                    profile_id=preset_profile,
                    cdp_url=preset_cdp,
                    cdp_http_url=os.environ.get("CLOAK_CDP_HTTP_URL", ""),
                    profile_name=os.environ.get("CLOAK_ACTIVE_PROFILE_NAME", ""),
                    features={"stealth": True, "humanize": True, "prebound": True},
                )
            )
            logger.info("Cloak: binding task-matched CDP URL to task %s", task_id)
            return {
                "session_name": f"cloak_{task_id}_{uuid.uuid4().hex[:8]}",
                "bb_session_id": lease.profile_id,
                "cdp_url": lease.cdp_url,
                "features": lease.features,
            }
        if preset_cdp and active_task and active_task != str(task_id):
            logger.info(
                "Cloak: ignoring BROWSER_CDP_URL owned by task %s while creating task %s",
                active_task, task_id,
            )
        elif preset_cdp and not active_task:
            logger.info(
                "Cloak: ignoring unbound BROWSER_CDP_URL for task %s (require CLOAK_ACTIVE_TASK_ID)",
                task_id,
            )

        base_url = self._base_url()
        if not os.environ.get("CLOAK_MANAGER_URL"):
            raise ValueError(
                "Cloak requires CLOAK_MANAGER_URL (CloakBrowser-Manager base URL). "
                "Run `hermes tools` and configure the Cloak provider, or set "
                "CLOAK_MANAGER_URL in your environment."
            )
        if not _host_allowed(base_url):
            raise ValueError(
                f"Cloak manager host not allowed: {redact_cdp_url(base_url)}. "
                f"Add it to CLOAK_ALLOWED_HOSTS or use localhost."
            )

        profile_name = os.environ.get("CLOAK_PROFILE", "").strip() or f"hermes-{task_id}"
        human_preset = os.environ.get("CLOAK_HUMAN_PRESET", "default")

        try:
            profile = self._find_profile_by_name(base_url, profile_name)
            if profile is None:
                profile = self._create_profile(base_url, profile_name)
            profile_id = str(profile.get("id") or profile.get("profile_id") or "")
            if not profile_id:
                raise RuntimeError(f"Cloak profile has no id: {redact_cdp_url(profile)}")
            launch = self._launch_profile(base_url, profile_id)
        except requests.RequestException as exc:
            raise RuntimeError(f"CloakBrowser-Manager unreachable at {redact_cdp_url(base_url)}: {redact_cdp_url(exc)}") from exc

        cdp_rel = str(launch.get("cdp_url") or f"/api/profiles/{profile_id}/cdp")
        cdp_http = self._absolute_cdp_url(base_url, cdp_rel)
        if not _host_allowed(cdp_http) and not os.environ.get("CLOAK_CDP_PROXY_BASE"):
            # Proxy rewrite may point at localhost; raw manager CDP must still be allowlisted.
            parsed = urlparse(cdp_http)
            if (parsed.hostname or "").lower() not in _ALLOWED_MANAGER_HOSTS:
                extra_ok = _host_allowed(f"http://{parsed.hostname or ''}")
                if not extra_ok:
                    raise RuntimeError(f"Cloak CDP URL host not allowed: {redact_cdp_url(cdp_http)}")
        cdp_ws = self._resolve_cdp_ws(cdp_http)

        features = {
            "stealth": True,
            "humanize": True,
            "human_preset": human_preset,
            "profile": profile_name,
            "already_running": bool(launch.get("already_running")),
        }
        lease = session_leases.put(
            session_leases.Lease(
                task_id=task_id,
                profile_id=profile_id,
                cdp_url=cdp_ws,
                cdp_http_url=cdp_http,
                profile_name=profile_name,
                features=features,
            )
        )
        session_leases.bind_env_for_task(task_id)
        try:
            from plugins.browser.cloak._impl import profile_state

            profile_state.remember_profile(
                task_id,
                profile_id=profile_id,
                profile_name=profile_name,
                cdp_url=cdp_ws,
                cdp_http_url=cdp_http,
                source="provider_create",
            )
        except Exception:  # noqa: BLE001
            pass

        logger.info(
            "Cloak session ready: task=%s profile=%s id=%s",
            task_id, profile_name, profile_id,
        )
        return {
            "session_name": f"cloak_{task_id}_{uuid.uuid4().hex[:8]}",
            "bb_session_id": lease.profile_id,
            "cdp_url": lease.cdp_url,
            "features": features,
        }

    def close_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        base_url = self._base_url()
        lease = session_leases.get_by_profile(session_id)
        try:
            resp = requests.post(
                f"{base_url}/api/profiles/{session_id}/stop",
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code in {200, 201, 204}:
                session_leases.pop_profile(session_id)
                if lease and lease.profile_name:
                    try:
                        from plugins.browser.cloak.proxy_format import profile_claim_owner

                        _release_proxy_claim(profile_claim_owner(lease.profile_name))
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Cloak: session stopped but proxy claim release failed: %s", redact_cdp_url(exc))

                session_leases.clear_env_if_matches(session_id)
                return True
            logger.warning(
                "Cloak close session %s failed: HTTP %s - %s",
                session_id, resp.status_code, redact_cdp_url(resp.text[:200]),
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Cloak close session %s raised: %s", session_id, redact_cdp_url(exc))
            return False

    def emergency_cleanup(self, session_id: str) -> None:
        if not session_id:
            return
        try:
            requests.post(
                f"{self._base_url()}/api/profiles/{session_id}/stop",
                headers=self._headers(),
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cloak emergency cleanup failed for %s: %s", session_id, redact_cdp_url(exc))

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Cloak (CloakBrowser stealth)",
            "badge": "★ stealth · self-hosted · free",
            "tag": "Anti-detect Chromium profiles via self-hosted CloakBrowser-Manager",
            "env_vars": [
                {
                    "key": "CLOAK_MANAGER_URL",
                    "prompt": "CloakBrowser-Manager URL",
                    "default": DEFAULT_MANAGER_URL,
                    "url": "https://github.com/xaroundx/Hermes-Unlock-Cloak-Edition",
                },
                {
                    "key": "CLOAK_AUTH_TOKEN",
                    "prompt": "Cloak Manager auth token (optional, blank if none)",
                },
            ],
            "post_setup": "cloak",
        }
