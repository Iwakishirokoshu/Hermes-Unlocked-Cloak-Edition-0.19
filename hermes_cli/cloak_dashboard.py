"""Cloak Manager dashboard panel (Cloak Edition).

A self-contained status panel surfaced by the Hermes dashboard so the
operator can, at a glance:

  * open the browser-visible ``CLOAK_MANAGER_BROWSER_URL`` without exposing
    the internal Compose-only manager hostname,
  * see a masked ``CLOAK_AUTH_TOKEN`` by default and reveal it only on explicit request,
  * see whether the manager is reachable + how many profiles are running,
  * manage captcha key presence, proxy pool, and idle timeout.

Implemented as a standalone HTML route (``GET /cloak``) plus JSON endpoints
under ``/api/cloak/*``. Mounted before the SPA catch-all in
:mod:`hermes_cli.web_server`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

DEFAULT_MANAGER_URL = "http://127.0.0.1:8080"
DEFAULT_DOCKER_NAME = "cloakbrowser-manager"

# Captcha solver keys. The actual solving happens client-side (in the Hermes
# process) by plugins.browser.cloak._impl.captcha, which reads these env vars.
# The 2captcha client checks TWO_CAPTCHA_API_KEY first, then TWOCAPTCHA_API_KEY,
# so we persist BOTH spellings to be safe.
_CAPTCHA_FIELDS: Dict[str, List[str]] = {
    # ui field        -> env var(s) written
    "capsolver": ["CAPSOLVER_API_KEY"],
    "twocaptcha": ["TWO_CAPTCHA_API_KEY", "TWOCAPTCHA_API_KEY"],
    "notletters": ["NOTLETTERS_API_KEY"],
}
# Read-back: ui field -> first env var to display presence/mask for.
_CAPTCHA_READ: Dict[str, str] = {
    "capsolver": "CAPSOLVER_API_KEY",
    "twocaptcha": "TWO_CAPTCHA_API_KEY",
    "notletters": "NOTLETTERS_API_KEY",
}

_env_loaded = False


def _bootstrap_env() -> None:
    """Merge manager.env into ``os.environ`` once (non-clobbering)."""
    global _env_loaded
    if _env_loaded:
        return
    path = _env_path()
    if path and os.path.isfile(path):
        try:
            from plugins.browser.cloak.env_file import parse_env_file

            for key, value in parse_env_file(path).items():
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            pass
    _env_loaded = True


def _mask_token(token: str) -> str:
    if not token:
        return ""
    dot = "\u2022"
    if len(token) <= 8:
        return dot * len(token)
    return token[:4] + dot * (len(token) - 8) + token[-4:]


def _token_fields(token: str, reveal: bool) -> Dict[str, Any]:
    """Return masked token metadata and reveal only on explicit request."""
    fields: Dict[str, Any] = {
        "has_token": bool(token),
        "token_masked": _mask_token(token),
    }
    if reveal and token:
        fields["token"] = token
    return fields


def _mask_url(url: str) -> str:
    """Redact userinfo from URLs before sending to the dashboard UI."""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = host
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return url
    except Exception:
        return url


def _manager_url() -> str:
    _bootstrap_env()
    return os.environ.get("CLOAK_MANAGER_URL", DEFAULT_MANAGER_URL).rstrip("/")


def _manager_browser_url() -> str:
    """Return the Manager address a dashboard visitor can actually open.

    ``CLOAK_MANAGER_URL`` normally names the internal Compose service
    (``http://manager:8080``).  That is correct for Hermes, but a Windows
    browser cannot resolve it.  Installations therefore provide a distinct
    browser-facing URL; non-Compose deployments retain the manager URL as a
    backwards-compatible fallback.
    """
    _bootstrap_env()
    return os.environ.get("CLOAK_MANAGER_BROWSER_URL", "").strip().rstrip("/") or _manager_url()


def _env_path() -> str:
    override = os.environ.get("CLOAK_MANAGER_ENV", "").strip()
    if override:
        return override
    try:
        from plugins.browser.cloak.paths import manager_env_file

        return str(manager_env_file())
    except Exception:
        return str((os.path.expanduser("~/.hermes/cloak/manager.env")))


def _write_env_keys(updates: Dict[str, str]) -> None:
    """Persist ``updates`` to the manager env file (update-in-place or append)
    and mirror them into the live ``os.environ`` so they take effect at once.

    Raises ``OSError`` / ``PermissionError`` if the file cannot be written.
    """
    path = _env_path()
    lines: List[str] = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    seen = set()
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    content = "\n".join(out).rstrip("\n") + "\n"

    # Atomic write into the same directory, preserving 0600 perms.
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".manager.env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    # Mirror into the running process so captcha clients see the keys now.
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def _restart_manager() -> Dict[str, Any]:
    """Best-effort restart of the CloakBrowser-Manager container so server-side
    captcha consumers (if any) reload the env. Never raises."""
    docker = shutil.which("docker")
    if not docker:
        return {"restarted": False, "reason": "docker not available"}
    name = os.environ.get("CLOAK_DOCKER_NAME", DEFAULT_DOCKER_NAME)
    try:
        proc = subprocess.run(
            [docker, "restart", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return {"restarted": True}
        return {"restarted": False, "reason": (proc.stderr or "").strip()[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"restarted": False, "reason": str(exc)[:200]}


def _captcha_status() -> Dict[str, Any]:
    """Presence + masked preview for each captcha solver key."""
    _bootstrap_env()
    out: Dict[str, Any] = {}
    for field, env_var in _CAPTCHA_READ.items():
        value = os.environ.get(env_var, "") or ""
        out[field] = {"set": bool(value), "masked": _mask_token(value) if value else ""}
    provider = (os.environ.get("CAPTCHA_PROVIDER", "auto") or "auto").lower()
    out["provider"] = provider
    out["env_file"] = _env_path()
    out["writable"] = _env_writable()
    return out


def _pf():
    """Lazy import of the dependency-light proxy_format helper."""
    from plugins.browser.cloak import proxy_format

    return proxy_format


def _scheme_of(url: Any) -> str:
    """Return the proxy scheme (http/https/socks5/...) for display badges."""
    try:
        from urllib.parse import urlparse

        if isinstance(url, dict):
            raw = url.get("url") or url.get("proxy") or ""
        else:
            raw = url
        return (urlparse(str(raw)).scheme or "http").lower()
    except Exception:  # noqa: BLE001
        return "http"


def _proxy_status() -> Dict[str, Any]:
    _bootstrap_env()
    try:
        pf = _pf()
        pool = pf.load_pool()
        proxies = pool.get("proxies") or []
        schemes: Dict[str, int] = {}
        items: List[Dict[str, str]] = []
        for p in proxies:
            scheme = _scheme_of(pf.proxy_url(p))
            schemes[scheme] = schemes.get(scheme, 0) + 1
            items.append({"masked": pf.mask_proxy(p), "scheme": scheme})
        return {
            "count": len(proxies),
            "strategy": pool.get("strategy", "round_robin"),
            "auto_assign": pf.pool_enabled(),
            "proxies": items,
            "schemes": schemes,
            "pool_file": pf.pool_file(),
            "writable": _path_writable(pf.pool_file()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "proxies": [], "error": str(exc), "auto_assign": False}


def _read_env_value(key: str) -> str:
    """Read a single key fresh from the manager env file (falls back to the
    live process env). Used for settings that another process may have changed."""
    path = _env_path()
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if line.startswith(f"{key}=") and not line.startswith("#"):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(key, "") or ""


def _idle_status() -> Dict[str, Any]:
    """Auto-close-idle-profiles configuration."""
    raw = _read_env_value("CLOAK_IDLE_TIMEOUT_MIN")
    try:
        minutes = int(raw or 0)
    except ValueError:
        minutes = 0
    return {
        "timeout_min": minutes,
        "enabled": minutes > 0,
        "writable": _env_writable(),
    }


def _path_writable(path: str) -> bool:
    """Whether the current process can create or atomically replace ``path``.

    A fresh Cloak install has no ``~/.hermes/cloak`` directory yet. The save
    endpoints create that directory themselves, so reporting it as read-only
    until the first save is misleading. Walk to the nearest existing parent
    instead and check the permissions required for creating missing children.
    """
    target = os.path.abspath(path)
    if os.path.isfile(target):
        parent = os.path.dirname(target) or "."
        return os.access(target, os.W_OK) and os.access(parent, os.W_OK | os.X_OK)

    directory = os.path.dirname(target) or "."
    while not os.path.exists(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent
    return os.path.isdir(directory) and os.access(directory, os.W_OK | os.X_OK)


def _env_writable() -> bool:
    return _path_writable(_env_path())
def _auth_headers(token: Optional[str]) -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _collect_status(reveal: bool = False) -> Dict[str, Any]:
    _bootstrap_env()
    base_url = _manager_url()
    browser_url = _manager_browser_url()
    token = os.environ.get("CLOAK_AUTH_TOKEN", "") or ""
    proxy_base = os.environ.get("CLOAK_CDP_PROXY_BASE", "") or ""

    status: Dict[str, Any] = {
        "manager_url": browser_url,
        "configured": bool(os.environ.get("CLOAK_MANAGER_URL")),
        **_token_fields(token, reveal),
        "cdp_proxy_base": _mask_url(proxy_base),
        "env_file": _env_path(),
        "reachable": False,
        "manager_reachable": False,
        "cdp_bridge_reachable": None,
        "protected_ready": False,
        "protected_error": None,
        "running_count": None,
        "profiles": [],
        "captcha": _captcha_status(),
        "proxy_pool": _proxy_status(),
        "idle": _idle_status(),
        "error": None,
    }
    # /api/status is exempt from auth on CloakBrowser-Manager — cheap smoke check.
    try:
        resp = requests.get(
            f"{base_url}/api/status", headers=_auth_headers(token), timeout=3
        )
        status["manager_reachable"] = True
        status["reachable"] = True  # backward-compatible transport signal
        if resp.ok:
            status["manager_reachable"] = True
            status["reachable"] = True  # backward-compatible transport signal
            try:
                data = resp.json()
                if isinstance(data, dict):
                    status["running_count"] = data.get("running_count")
            except ValueError:
                pass
    except requests.RequestException as exc:
        status["error"] = "manager endpoint unavailable"
        return status

    # The protected endpoint is the real readiness check: /api/status alone
    # can be public while an invalid/missing token prevents browser operation.
    try:
        resp = requests.get(
            f"{base_url}/api/profiles", headers=_auth_headers(token), timeout=4
        )
        if resp.ok:
            profiles: List[Dict[str, Any]] = []
            payload = resp.json()
            if not isinstance(payload, list):
                status["protected_error"] = "protected endpoint returned invalid JSON"
                return status
            for p in payload:
                if not isinstance(p, dict):
                    continue
                profiles.append(
                    {
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "status": p.get("status"),
                        "humanize": p.get("humanize"),
                        "tags": p.get("tags", []),
                    }
                )
            status["profiles"] = profiles
            if token:
                if not proxy_base:
                    status["protected_error"] = "authenticated CDP bridge is not configured"
                    status["cdp_bridge_reachable"] = False
                    return status
                try:
                    bridge = requests.get(
                        f"{proxy_base.rstrip('/')}/api/profiles",
                        timeout=4,
                    )
                    if not bridge.ok:
                        status["protected_error"] = "authenticated CDP bridge unavailable"
                        status["cdp_bridge_reachable"] = False
                        return status
                except requests.RequestException:
                    status["protected_error"] = "authenticated CDP bridge unavailable"
                    status["cdp_bridge_reachable"] = False
                    return status
                status["cdp_bridge_reachable"] = True
            status["protected_ready"] = True
        else:
            status["protected_error"] = f"protected endpoint returned HTTP {resp.status_code}"
    except (requests.RequestException, ValueError):
        status["protected_error"] = "protected endpoint unavailable"
    return status


@router.get("/api/cloak/status")
def cloak_status(reveal: int = 0) -> JSONResponse:
    """Return status; reveal the token only after an explicit dashboard request."""
    return JSONResponse(
        _collect_status(reveal=bool(reveal)),
        headers={"Cache-Control": "no-store"},
    )

@router.post("/api/cloak/captcha")
def cloak_captcha_save(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """Persist captcha solver API keys to the manager env file.

    Accepts JSON like::

        {"capsolver": "CAP-...", "twocaptcha": "abc...",
         "notletters": "", "provider": "auto", "restart": false}

    Only fields present in the payload are touched (an empty string clears the
    key; an omitted field is left unchanged). Keys are written to
    ``/etc/cloak/manager.env`` and mirrored into the live process so the
    in-process captcha clients pick them up immediately.
    """
    updates: Dict[str, str] = {}
    changed: List[str] = []

    for field, env_vars in _CAPTCHA_FIELDS.items():
        if field in payload:
            value = (payload.get(field) or "").strip()
            for env_var in env_vars:
                updates[env_var] = value
            changed.append(field)

    if "provider" in payload:
        provider = (payload.get("provider") or "auto").strip().lower()
        if provider not in {"auto", "capsolver", "2captcha", "twocaptcha"}:
            provider = "auto"
        updates["CAPTCHA_PROVIDER"] = provider
        changed.append("provider")

    if not updates:
        return JSONResponse({"ok": False, "error": "nothing to update"}, status_code=400)

    try:
        _write_env_keys(updates)
    except (OSError, PermissionError) as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": f"cannot write {_env_path()}: {exc}",
                "hint": "run the dashboard as root or chmod the env file so it is writable",
            },
            status_code=500,
        )

    result: Dict[str, Any] = {"ok": True, "changed": changed, "captcha": _captcha_status()}
    if payload.get("restart"):
        result["manager_restart"] = _restart_manager()
    return JSONResponse(result)


@router.post("/api/cloak/proxies")
def cloak_proxies_save(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """Save / append / clear the proxy pool and toggle auto-assign.

    JSON::

        {"text": "host:port:user:pass\\n1.2.3.4:8080",
         "default_scheme": "http",      # http|https|socks5
         "mode": "replace",             # replace|append
         "strategy": "round_robin",     # round_robin|random
         "auto_assign": true,           # write CLOAK_USE_PROXY_POOL
         "clear": false}

    Proxies in any common format are normalised to the URL form CloakBrowser
    accepts. Returns the parsed count + masked list + any invalid lines.
    """
    try:
        pf = _pf()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"proxy pool unavailable: {exc}"}, status_code=500)

    try:
        if payload.get("clear"):
            pf.clear_pool()
            invalid: List[str] = []
        else:
            text = payload.get("text", "") or ""
            scheme = (payload.get("default_scheme") or "http").lower()
            strategy = (payload.get("strategy") or "round_robin").lower()
            ok, invalid = pf.parse_lines(text, scheme)
            mode = (payload.get("mode") or "replace").lower()
            if mode == "append":
                pf.add_proxies(ok)
                # strategy may still change on append:
                pool = pf.load_pool()
                if strategy in {"round_robin", "random"} and pool.get("strategy") != strategy:
                    pool["strategy"] = strategy
                    pf.save_pool(pool)
            else:
                pf.set_proxies(ok, strategy=strategy)

        # Auto-assign toggle (persist + live env) when explicitly provided.
        if "auto_assign" in payload:
            flag = "1" if payload.get("auto_assign") else "0"
            try:
                _write_env_keys({"CLOAK_USE_PROXY_POOL": flag})
            except (OSError, PermissionError) as exc:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"pool saved but cannot persist auto-assign flag: {exc}",
                        "proxy_pool": _proxy_status(),
                    },
                    status_code=500,
                )
    except (OSError, PermissionError) as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": f"cannot write {pf.pool_file()}: {exc}",
                "hint": "run the dashboard as root or make the pool file writable",
            },
            status_code=500,
        )

    return JSONResponse({"ok": True, "invalid": invalid, "proxy_pool": _proxy_status()})


@router.post("/api/cloak/idle")
def cloak_idle_save(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """Configure auto-closing of idle Cloak profiles.

    JSON::

        {"enabled": true, "timeout_min": 60}

    Persists ``CLOAK_IDLE_TIMEOUT_MIN`` to the manager env file. The reaper
    thread (plugins.browser.cloak._impl.idle_reaper) re-reads this value every
    minute, so changes take effect without a restart. ``0`` / disabled turns
    the reaper off.
    """
    enabled = bool(payload.get("enabled"))
    try:
        minutes = int(payload.get("timeout_min") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if not enabled:
        minutes = 0
    minutes = max(0, min(minutes, 1440))  # clamp to [0, 24h]

    try:
        _write_env_keys({"CLOAK_IDLE_TIMEOUT_MIN": str(minutes)})
    except (OSError, PermissionError) as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": f"cannot write {_env_path()}: {exc}",
                "hint": "run the dashboard as root or make the env file writable",
            },
            status_code=500,
        )

    # Best-effort: kick the reaper to start now if it isn't running yet.
    try:
        from plugins.browser.cloak._impl import idle_reaper

        idle_reaper.start()
    except Exception:  # noqa: BLE001
        pass

    return JSONResponse({"ok": True, "idle": _idle_status()})


@router.get("/cloak", response_class=HTMLResponse)
def cloak_panel(request: Request) -> HTMLResponse:
    """Standalone Cloak Manager panel (no SPA rebuild required).

    In loopback mode the dashboard gates ``/api/*`` on the ephemeral
    ``_SESSION_TOKEN`` (injected into the SPA and echoed back as a Bearer
    header). The SPA owns that token, not this standalone page — so we read it
    from ``app.state`` (published by the web server) and inject it here so the
    panel's own ``/api/cloak/*`` calls authenticate. In OAuth-gated mode the
    token is empty and the panel authenticates with the session cookie instead
    (``credentials: same-origin``).
    """
    token = ""
    if not getattr(request.app.state, "auth_required", False):
        token = getattr(request.app.state, "legacy_session_token", "") or ""
    html = _PANEL_HTML.replace("__CLOAK_DASH_TOKEN__", token)
    return HTMLResponse(html)


_PANEL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cloak Manager - Hermes</title>
<style>
  :root {
    color-scheme: dark;
    --bg:#0a0c10; --bg2:#0e1116; --card:#141922; --card2:#0d1117;
    --line:#1e2531; --line2:#2a3340; --txt:#e8ecf3; --mut:#8d97a8;
    --accent:#6aa5ff; --accent2:#3b82f6; --accent-bg:#13233f;
    --ok:#4ade80; --ok-bg:#0e2a1a; --ok-line:#1d4d31;
    --bad:#f87171; --bad-bg:#2a1414; --bad-line:#4d1d1d;
    --warn:#fbbf24; --purple:#c084fc; --purple-bg:#241632;
  }
  * { box-sizing: border-box; }
  html,body { height:100%; }
  body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:
           radial-gradient(900px 500px at 85% -10%, rgba(59,130,246,.10), transparent 60%),
           radial-gradient(700px 400px at 0% 0%, rgba(192,132,252,.07), transparent 55%),
           var(--bg);
         color:var(--txt); padding:0; -webkit-font-smoothing:antialiased; }
  .wrap { max-width: 900px; margin: 0 auto; padding: 26px 22px 60px; }

  /* Header */
  .hero { display:flex; align-items:center; gap:14px; margin-bottom:6px; }
  .logo { width:38px; height:38px; border-radius:11px; flex:0 0 auto;
          background:linear-gradient(135deg,#3b82f6,#8b5cf6); display:grid; place-items:center;
          box-shadow:0 6px 22px rgba(59,130,246,.35); font-size:19px; }
  h1 { font-size: 21px; margin:0; font-weight:650; letter-spacing:.2px; display:flex; align-items:center; gap:10px; }
  .sub { color:var(--mut); font-size:13px; margin:6px 0 22px; line-height:1.5; }

  /* Cards */
  .card { background:linear-gradient(180deg, rgba(255,255,255,.018), transparent), var(--card);
          border:1px solid var(--line); border-radius:16px; padding:18px 20px; margin-bottom:16px;
          box-shadow:0 1px 0 rgba(255,255,255,.02), 0 10px 30px -18px rgba(0,0,0,.7); }
  .card-h { display:flex; justify-content:space-between; align-items:center; gap:10px; margin:0 0 12px; }
  .card-h .title { display:flex; align-items:center; gap:9px; font-size:15px; font-weight:600; }
  .card-h .ic { font-size:16px; opacity:.9; }
  .hint { color:var(--mut); font-size:12.5px; margin:0 0 12px; line-height:1.5; }

  .row { display:flex; justify-content:space-between; align-items:center; gap:14px; padding:10px 0; border-bottom:1px solid var(--line); }
  .row:last-child { border-bottom:none; }
  .k { color:var(--mut); font-size:13px; }
  .v { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12.5px; word-break:break-all; text-align:right; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }

  .badge { display:inline-flex; align-items:center; gap:6px; padding:3px 11px; border-radius:999px; font-size:12px; font-weight:600; }
  .dot { width:7px; height:7px; border-radius:50%; background:currentColor; box-shadow:0 0 0 3px rgba(255,255,255,.05); }
  .ok { background:var(--ok-bg); color:var(--ok); border:1px solid var(--ok-line); }
  .bad { background:var(--bad-bg); color:var(--bad); border:1px solid var(--bad-line); }
  .muted { background:#161b24; color:var(--mut); border:1px solid var(--line2); }

  button { background:linear-gradient(180deg,#4f8cff,#3b82f6); color:#fff; border:none; border-radius:9px;
           padding:8px 15px; font-size:13px; font-weight:550; cursor:pointer; transition:filter .12s, transform .05s; }
  button:hover { filter:brightness(1.08); }
  button:active { transform:translateY(1px); }
  button.ghost { background:#161b24; color:#cbd3e0; border:1px solid var(--line2); }
  button.ghost:hover { background:#1b2230; }
  button.danger { background:#1b1212; color:#fca5a5; border:1px solid var(--bad-line); }
  .btns { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; align-items:center; }
  .open-btn { font-size:13.5px; padding:9px 18px; }

  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--mut); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.4px; }
  tr:last-child td { border-bottom:none; }

  .pill { font-size:11px; padding:2px 9px; border-radius:999px; background:#161b24; border:1px solid var(--line2); color:var(--mut); }
  .pill.run { background:var(--ok-bg); color:var(--ok); border-color:var(--ok-line); }
  .pill.set { background:var(--ok-bg); color:var(--ok); border-color:var(--ok-line); }
  .sch { font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:6px; text-transform:uppercase; letter-spacing:.3px; }
  .sch-http  { background:var(--accent-bg); color:#93c5fd; border:1px solid #1e3a6b; }
  .sch-https { background:var(--ok-bg); color:var(--ok); border:1px solid var(--ok-line); }
  .sch-socks5,.sch-socks5h,.sch-socks4 { background:var(--purple-bg); color:var(--purple); border:1px solid #4c2a6b; }
  .empty { color:var(--mut); font-size:13px; padding:12px 0; text-align:center; }

  .frow { display:grid; grid-template-columns: 160px 1fr 90px; align-items:center; gap:12px; padding:7px 0; }
  .frow label { color:var(--mut); font-size:13px; }
  .frow input[type=password], .frow input[type=number], .frow input[type=text], .frow select {
    background:var(--card2); color:var(--txt); border:1px solid var(--line2); border-radius:9px;
    padding:8px 11px; font-size:13px; font-family: ui-monospace, Menlo, monospace; width:100%; transition:border-color .12s, box-shadow .12s; }
  .frow input:focus, .frow select:focus, textarea:focus { outline:none; border-color:var(--accent2); box-shadow:0 0 0 3px rgba(59,130,246,.18); }
  textarea { width:100%; background:var(--card2); color:var(--txt); border:1px solid var(--line2); border-radius:10px;
    padding:11px; font-size:13px; font-family: ui-monospace, Menlo, monospace; resize:vertical; line-height:1.5; }
  code { background:var(--card2); border:1px solid var(--line2); border-radius:6px; padding:1.5px 6px; font-size:11.5px; color:#aab4c5; }
  .msg { color:var(--mut); font-size:12.5px; margin:0; align-self:center; }

  /* Toggle switch */
  .switch { position:relative; display:inline-block; width:42px; height:24px; flex:0 0 auto; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; inset:0; cursor:pointer; background:#222a36; border:1px solid var(--line2);
            border-radius:999px; transition:.18s; }
  .slider:before { content:""; position:absolute; height:16px; width:16px; left:3px; top:3px;
            background:#cbd3e0; border-radius:50%; transition:.18s; }
  .switch input:checked + .slider { background:linear-gradient(180deg,#4f8cff,#3b82f6); border-color:transparent; }
  .switch input:checked + .slider:before { transform:translateX(18px); background:#fff; }
  .toggle-row { display:flex; align-items:center; gap:12px; padding:8px 0; }
  .toggle-row .lab { font-size:13px; }
  .toggle-row .lab small { display:block; color:var(--mut); font-size:11.5px; margin-top:2px; }

  .seg { display:flex; gap:6px; background:var(--card2); border:1px solid var(--line2); border-radius:10px; padding:3px; }
  .seg label { flex:1; text-align:center; font-size:12.5px; color:var(--mut); padding:6px 8px; border-radius:7px; cursor:pointer; }
  .seg input { display:none; }
  .seg input:checked + span { color:#fff; }
  .seg label:has(input:checked) { background:var(--accent2); }
  .scheme-counts { display:flex; gap:6px; }
  @media (max-width:620px){ .frow { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="logo">&#128376;</div>
    <div>
      <h1>Cloak Manager <span id="conn" class="badge muted"><span class="dot"></span>checking...</span></h1>
    </div>
  </div>
  <p class="sub">Контроль вашего self-hosted CloakBrowser-Manager прямо из Hermes: статус, токен, капча-солверы, пул прокси (любые http/socks) и авто-закрытие неактивных профилей.</p>

  <div class="card">
    <div class="card-h"><span class="title"><span class="ic">&#128225;</span> Connection</span>
      <button class="open-btn" id="open-mgr">Open Manager &#8599;</button></div>
    <div class="row">
      <span class="k">Manager URL</span>
      <span class="v"><a id="mgr-link" href="#" target="_blank" rel="noopener">-</a></span>
    </div>
    <div class="row">
      <span class="k">Auth token</span>
      <span class="v" id="token">-</span>
    </div>
    <div class="row">
      <span class="k">CDP proxy base</span>
      <span class="v" id="proxy">-</span>
    </div>
    <div class="row">
      <span class="k">Running profiles</span>
      <span class="v" id="running">-</span>
    </div>
    <div class="btns">
      <button class="ghost" id="toggle-token" title="Token is hidden by default">Show token</button>
      <button class="ghost" id="refresh">Refresh</button>
    </div>
  </div>

  <div class="card">
    <div class="card-h"><span class="title"><span class="ic">&#129513;</span> Captcha solvers</span>
      <span id="cap-writable" class="badge muted">-</span></div>
    <p class="hint">Ключи сохраняются в <span id="cap-envfile" class="v">manager.env</span> и используются встроенным солвером (gmail_factory, регистрации). Применяются сразу.</p>

    <div class="frow">
      <label for="cap-capsolver">CapSolver API key</label>
      <input id="cap-capsolver" type="password" autocomplete="off" placeholder="CAP-..." />
      <span id="cap-capsolver-st" class="pill">unset</span>
    </div>
    <div class="frow">
      <label for="cap-twocaptcha">2Captcha API key</label>
      <input id="cap-twocaptcha" type="password" autocomplete="off" placeholder="32-char key" />
      <span id="cap-twocaptcha-st" class="pill">unset</span>
    </div>
    <div class="frow">
      <label for="cap-notletters">NotLetters API key</label>
      <input id="cap-notletters" type="password" autocomplete="off" placeholder="optional (SMS)" />
      <span id="cap-notletters-st" class="pill">unset</span>
    </div>
    <div class="frow">
      <label for="cap-provider">Provider</label>
      <select id="cap-provider">
        <option value="auto">auto (best per captcha)</option>
        <option value="capsolver">capsolver</option>
        <option value="2captcha">2captcha</option>
      </select>
      <span></span>
    </div>
    <div class="toggle-row">
      <label class="switch"><input type="checkbox" id="cap-restart" /><span class="slider"></span></label>
      <span class="lab">Restart manager container <small>перезапустить докер-контейнер после сохранения</small></span>
    </div>
    <div class="btns">
      <button id="cap-save">Save captcha keys</button>
      <span id="cap-msg" class="msg"></span>
    </div>
  </div>

  <div class="card">
    <div class="card-h"><span class="title"><span class="ic">&#127760;</span> Proxy pool <span id="pp-count" class="pill">0</span>
      <span id="pp-schemes" class="scheme-counts"></span></span>
      <span id="pp-writable" class="badge muted">-</span></div>
    <p class="hint">Вставьте прокси в любом формате (по одному в строке) — <b>http и socks можно вперемешку</b>, Cloak принимает оба. Префикс схемы (<code>socks5://</code>, <code>http://</code>) у строки всегда главнее. Сохраняется в <span id="pp-file" class="v">proxies.json</span>.<br>Форматы: <code>host:port</code> · <code>host:port:user:pass</code> · <code>user:pass@host:port</code> · <code>scheme://...</code></p>

    <textarea id="pp-text" rows="6" placeholder="1.2.3.4:8080&#10;1.2.3.4:8080:user:pass&#10;user:pass@1.2.3.4:8080&#10;socks5://1.2.3.4:1080&#10;http://user:pass@5.6.7.8:3128"></textarea>

    <div class="frow" style="grid-template-columns: 160px 1fr 1fr;">
      <label for="pp-scheme">Схема по умолчанию</label>
      <select id="pp-scheme" title="применяется только к строкам без префикса схемы">
        <option value="http">http (для строк без схемы)</option>
        <option value="https">https</option>
        <option value="socks5">socks5</option>
        <option value="socks5h">socks5h</option>
        <option value="socks4">socks4</option>
      </select>
      <select id="pp-strategy" title="ротация">
        <option value="round_robin">round-robin</option>
        <option value="random">random</option>
      </select>
    </div>
    <div class="frow" style="grid-template-columns: 160px 1fr;">
      <label>Режим</label>
      <div class="seg">
        <label><input type="radio" name="pp-mode" id="pp-replace" checked /><span>replace pool</span></label>
        <label><input type="radio" name="pp-mode" id="pp-append" /><span>append</span></label>
      </div>
    </div>
    <div class="toggle-row">
      <label class="switch"><input type="checkbox" id="pp-auto" /><span class="slider"></span></label>
      <span class="lab">Auto-assign из пула <small>выдавать прокси каждому новому профилю — «юзай прокси из пула» работает без скила</small></span>
    </div>
    <div class="btns">
      <button id="pp-save">Save proxies</button>
      <button class="danger" id="pp-clear">Clear pool</button>
      <span id="pp-msg" class="msg"></span>
    </div>

    <table style="margin-top:14px;">
      <thead><tr><th style="width:40px">#</th><th style="width:78px">Type</th><th>Proxy (password masked)</th></tr></thead>
      <tbody id="pp-list"><tr><td colspan="3" class="empty">No proxies.</td></tr></tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-h"><span class="title"><span class="ic">&#9203;</span> Auto-close idle profiles</span>
      <span id="idle-st" class="badge muted">-</span></div>
    <p class="hint">Если в профиле нет активности (никаких browser-действий) дольше заданного времени — Hermes сам остановит его в менеджере. Профиль не удаляется, только закрывается. Проверка каждую минуту.</p>
    <div class="toggle-row">
      <label class="switch"><input type="checkbox" id="idle-enabled" /><span class="slider"></span></label>
      <span class="lab">Включить авто-закрытие</span>
    </div>
    <div class="frow" style="grid-template-columns:160px 140px 1fr;">
      <label for="idle-min">Таймаут (минут)</label>
      <input id="idle-min" type="number" min="1" max="1440" step="5" value="60" />
      <span class="k" style="align-self:center">1–1440 мин (по умолч. 60)</span>
    </div>
    <div class="btns">
      <button id="idle-save">Save</button>
      <span id="idle-msg" class="msg"></span>
    </div>
  </div>

  <div class="card">
    <div class="card-h"><span class="title"><span class="ic">&#128100;</span> Profiles</span></div>
    <table>
      <thead><tr><th>Name</th><th>Status</th><th>Humanize</th><th>Tags</th></tr></thead>
      <tbody id="profiles"><tr><td colspan="4" class="empty">Loading...</td></tr></tbody>
    </table>
  </div>
</div>

<script>
let TOKEN_REVEALED = false;
const DASH_TOKEN = "__CLOAK_DASH_TOKEN__";
function base() { return (window.__HERMES_BASE__ || window.__HERMES_BASE_PATH__ || ""); }
function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (DASH_TOKEN) h["Authorization"] = "Bearer " + DASH_TOKEN;
  return h;
}
async function load(reveal) {
  const showToken = reveal === true;
  const url = base() + "/api/cloak/status" + (showToken ? "?reveal=1" : "");
  let d;
  try { d = await (await fetch(url, {credentials:"same-origin", headers: authHeaders()})).json(); }
  catch (e) { setConn(false, "fetch failed"); return; }
  if (d && d.detail === "Unauthorized") { setConn(false, "auth required — open via dashboard"); return; }
  render(d);
}
function setConn(ok, label) {
  const el = document.getElementById("conn");
  el.className = "badge " + (ok ? "ok" : "bad");
  el.innerHTML = '<span class="dot"></span>' + (label || (ok ? "reachable" : "unreachable"));
}
function render(d) {
  const protectedReady = !!d.protected_ready;
  const connectionLabel = !d.configured ? "not configured"
    : protectedReady ? "ready"
    : d.manager_reachable ? (d.protected_error || "protected API unavailable")
    : "unreachable";
  setConn(protectedReady, connectionLabel);
  const link = document.getElementById("mgr-link");
  link.textContent = d.manager_url; link.href = d.manager_url;
  document.getElementById("open-mgr").onclick = () => window.open(d.manager_url, "_blank");
  document.getElementById("proxy").textContent = d.cdp_proxy_base || "(none)";
  document.getElementById("running").textContent = (d.running_count ?? "-");
  const revealedToken = typeof d.token === "string" && d.token.length > 0;
  document.getElementById("token").textContent = d.has_token ? (revealedToken ? d.token : d.token_masked) : "(none)";
  TOKEN_REVEALED = revealedToken;
  const tokenToggle = document.getElementById("toggle-token");
  if (tokenToggle) {
    tokenToggle.textContent = revealedToken ? "Hide token" : "Show token";
    tokenToggle.title = revealedToken ? "Hide the Cloak Manager token" : "Reveal the Cloak Manager token";
  }
  const tb = document.getElementById("profiles");
  if (!d.profiles || !d.profiles.length) {
    const emptyLabel = protectedReady ? "No profiles."
      : d.manager_reachable ? (d.protected_error || "Protected API unavailable.")
      : "Manager not reachable.";
    tb.innerHTML = '<tr><td colspan="4" class="empty">' + emptyLabel + '</td></tr>';
    return;
  }
  tb.innerHTML = d.profiles.map(p => {
    const st = (p.status||"").toLowerCase()==="running" ? '<span class="pill run">running</span>' : '<span class="pill">'+(p.status||"-")+'</span>';
    const tags = (p.tags||[]).join(", ");
    return "<tr><td>"+(p.name||"-")+"</td><td>"+st+"</td><td>"+(p.humanize?"yes":"no")+"</td><td>"+tags+"</td></tr>";
  }).join("");
  renderCaptcha(d.captcha || {});
  renderProxies(d.proxy_pool || {});
  renderIdle(d.idle || {});
}
function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function schemeBadge(s){ const k=(s||"http").toLowerCase(); return '<span class="sch sch-'+esc(k)+'">'+esc(k)+'</span>'; }
function renderProxies(p) {
  const cnt = document.getElementById("pp-count");
  if (cnt) cnt.textContent = (p.count ?? 0);
  const f = document.getElementById("pp-file");
  if (f && p.pool_file) f.textContent = p.pool_file;
  const w = document.getElementById("pp-writable");
  if (w) { w.className = "badge " + (p.writable ? "ok" : "bad"); w.textContent = p.writable ? "writable" : "read-only"; }
  const auto = document.getElementById("pp-auto");
  if (auto && document.activeElement !== auto) auto.checked = !!p.auto_assign;
  const strat = document.getElementById("pp-strategy");
  if (strat && p.strategy) strat.value = p.strategy;
  const sc = document.getElementById("pp-schemes");
  if (sc) {
    const sm = p.schemes || {};
    sc.innerHTML = Object.keys(sm).sort().map(k => schemeBadge(k)+'<span class="pill" style="margin-left:-2px">'+sm[k]+'</span>').join(" ");
  }
  const tb = document.getElementById("pp-list");
  const list = p.proxies || [];
  if (!list.length) { tb.innerHTML = '<tr><td colspan="3" class="empty">No proxies.</td></tr>'; return; }
  tb.innerHTML = list.map((px,i) => {
    const masked = (typeof px === "string") ? px : (px.masked || "");
    const scheme = (typeof px === "string") ? (masked.split("://")[0]||"http") : (px.scheme || "http");
    return "<tr><td>"+(i+1)+"</td><td>"+schemeBadge(scheme)+"</td><td class='v' style='text-align:left'>"+esc(masked)+"</td></tr>";
  }).join("");
}
function renderIdle(idle) {
  const st = document.getElementById("idle-st");
  const en = document.getElementById("idle-enabled");
  const mn = document.getElementById("idle-min");
  if (en && document.activeElement !== en) en.checked = !!idle.enabled;
  if (mn && document.activeElement !== mn && idle.timeout_min) mn.value = idle.timeout_min;
  if (st) {
    if (idle.enabled) { st.className = "badge ok"; st.textContent = "on · " + (idle.timeout_min||0) + " min"; }
    else { st.className = "badge muted"; st.textContent = "off"; }
  }
}
async function saveIdle() {
  const msg = document.getElementById("idle-msg");
  msg.textContent = "saving...";
  const body = { enabled: document.getElementById("idle-enabled").checked,
                 timeout_min: parseInt(document.getElementById("idle-min").value || "0", 10) };
  try {
    const r = await fetch(base()+"/api/cloak/idle", { method:"POST", credentials:"same-origin",
      headers: authHeaders({"Content-Type":"application/json"}), body: JSON.stringify(body) });
    const d = await r.json();
    if (d.ok) { msg.textContent = d.idle && d.idle.enabled ? ("on · auto-close after "+d.idle.timeout_min+" min") : "disabled"; renderIdle(d.idle||{}); }
    else { msg.textContent = "error: " + (d.error || "failed"); }
  } catch (e) { msg.textContent = "request failed"; }
}
async function saveProxies(clear) {
  const msg = document.getElementById("pp-msg");
  msg.textContent = clear ? "clearing..." : "saving...";
  const body = {
    auto_assign: document.getElementById("pp-auto").checked,
    strategy: document.getElementById("pp-strategy").value,
    default_scheme: document.getElementById("pp-scheme").value,
    mode: document.getElementById("pp-append").checked ? "append" : "replace",
  };
  if (clear) { body.clear = true; }
  else { body.text = document.getElementById("pp-text").value; }
  try {
    const r = await fetch(base()+"/api/cloak/proxies", {
      method:"POST", credentials:"same-origin",
      headers: authHeaders({"Content-Type":"application/json"}), body: JSON.stringify(body) });
    const d = await r.json();
    if (d.ok) {
      const inv = (d.invalid && d.invalid.length) ? (" · " + d.invalid.length + " invalid skipped") : "";
      msg.textContent = (clear ? "cleared" : "saved " + ((d.proxy_pool||{}).count ?? 0) + " proxies") + inv;
      if (!clear) document.getElementById("pp-text").value = "";
      renderProxies(d.proxy_pool || {});
    } else {
      msg.textContent = "error: " + (d.error || "failed");
    }
  } catch (e) { msg.textContent = "request failed"; }
}
function setStPill(id, info) {
  const el = document.getElementById(id);
  if (!el) return;
  if (info && info.set) { el.className = "pill set"; el.textContent = info.masked || "set"; }
  else { el.className = "pill"; el.textContent = "unset"; }
}
function renderCaptcha(c) {
  setStPill("cap-capsolver-st", c.capsolver);
  setStPill("cap-twocaptcha-st", c.twocaptcha);
  setStPill("cap-notletters-st", c.notletters);
  const envf = document.getElementById("cap-envfile");
  if (envf && c.env_file) envf.textContent = c.env_file;
  const w = document.getElementById("cap-writable");
  if (w) { w.className = "badge " + (c.writable ? "ok" : "bad"); w.textContent = c.writable ? "writable" : "read-only"; }
  const prov = document.getElementById("cap-provider");
  if (prov && c.provider) prov.value = (c.provider === "twocaptcha" ? "2captcha" : c.provider);
}
async function saveCaptcha() {
  const msg = document.getElementById("cap-msg");
  msg.textContent = "saving...";
  const body = { provider: document.getElementById("cap-provider").value,
                 restart: document.getElementById("cap-restart").checked };
  // Only send a key field if the operator typed something (blank box = leave as-is).
  const fields = ["capsolver","twocaptcha","notletters"];
  for (const f of fields) {
    const v = document.getElementById("cap-"+f).value;
    if (v !== "") body[f] = v;
  }
  try {
    const r = await fetch(base()+"/api/cloak/captcha", {
      method:"POST", credentials:"same-origin",
      headers: authHeaders({"Content-Type":"application/json"}), body: JSON.stringify(body) });
    const d = await r.json();
    if (d.ok) {
      msg.textContent = "saved" + (d.manager_restart ? (d.manager_restart.restarted ? " · manager restarted" : " · restart failed") : "");
      fields.forEach(f => { document.getElementById("cap-"+f).value = ""; });
      renderCaptcha(d.captcha || {});
    } else {
      msg.textContent = "error: " + (d.error || "failed");
    }
  } catch (e) { msg.textContent = "request failed"; }
}
function toggleToken() {
  load(!TOKEN_REVEALED);
}
document.getElementById("refresh").onclick = () => load(false);
document.getElementById("toggle-token").onclick = toggleToken;
document.getElementById("cap-save").onclick = saveCaptcha;
document.getElementById("pp-save").onclick = () => saveProxies(false);
document.getElementById("pp-clear").onclick = () => saveProxies(true);
document.getElementById("idle-save").onclick = saveIdle;
load(false);
setInterval(() => { if (!TOKEN_REVEALED) load(false); }, 15000);
</script>
</body>
</html>
"""
