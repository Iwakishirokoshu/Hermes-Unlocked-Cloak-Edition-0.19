"""Auto-close idle Cloak profiles.

A lightweight background thread that stops CloakBrowser-Manager profiles which
have been running without any browser activity for longer than
``CLOAK_IDLE_TIMEOUT_MIN`` minutes (0 = disabled).

Activity is tracked at a single choke point: every ``browser_*`` tool call goes
through :func:`plugins.browser.cloak._impl.browser_pool.BrowserPool.get`, which
calls :func:`touch` with the active profile's CDP URL. The reaper maps a running
profile to its activity by matching the profile id inside the recorded CDP URLs.

Design notes:
  * The timeout is re-read from the manager env file every loop, so toggling it
    from the dashboard (which writes ``/etc/cloak/manager.env``) takes effect
    within ~60s even across processes.
  * A freshly-launched profile we have never seen is *seeded* with "now" the
    first time the reaper observes it running, so it gets a full grace period
    rather than being closed immediately.
  * Only running profiles are ever stopped; profiles are never deleted.
  * Pure stdlib + ``requests`` (already a Cloak dependency). Never raises into
    the caller.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = "/etc/cloak/manager.env"
DEFAULT_MANAGER_URL = "http://127.0.0.1:8080"
_POLL_SECONDS = 60

_lock = threading.Lock()
_last_activity: Dict[str, float] = {}   # cdp_url -> ts of last tool call
_first_seen: Dict[str, float] = {}      # profile_id -> ts first observed running
_started = False


def touch(cdp_url: str) -> None:
    """Record activity for the profile behind ``cdp_url`` (called per tool use)."""
    if not cdp_url:
        return
    with _lock:
        _last_activity[cdp_url] = time.time()


def _read_timeout_min() -> int:
    """Fresh read of ``CLOAK_IDLE_TIMEOUT_MIN`` (env file wins so dashboard edits
    from another process are honoured); falls back to the live process env."""
    try:
        from plugins.browser.cloak.paths import manager_env_file

        path = os.environ.get("CLOAK_MANAGER_ENV") or str(manager_env_file())
    except Exception:
        path = os.environ.get("CLOAK_MANAGER_ENV", "")
    value = ""
    try:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if line.startswith("CLOAK_IDLE_TIMEOUT_MIN=") and not line.startswith("#"):
                        value = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    except OSError:
        pass
    if not value:
        value = os.environ.get("CLOAK_IDLE_TIMEOUT_MIN", "") or ""
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _manager() -> tuple:
    base = (os.environ.get("CLOAK_MANAGER_URL", DEFAULT_MANAGER_URL) or DEFAULT_MANAGER_URL).rstrip("/")
    token = os.environ.get("CLOAK_AUTH_TOKEN", "") or ""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return base, headers


def _profile_last_activity(profile_id: str, now: float) -> float:
    """Effective last-activity ts for a profile: max over matching CDP URLs, or
    a freshly-seeded first-seen timestamp."""
    with _lock:
        last: Optional[float] = None
        for url, ts in _last_activity.items():
            if profile_id and profile_id in url:
                last = ts if last is None else max(last, ts)
        if last is not None:
            return last
        seeded = _first_seen.get(profile_id)
        if seeded is None:
            _first_seen[profile_id] = now
            return now
        return seeded


def _reap_once(timeout_seconds: float) -> None:
    base, headers = _manager()
    try:
        resp = requests.get(f"{base}/api/profiles", headers=headers, timeout=5)
        if not resp.ok:
            return
        profiles = resp.json() or []
    except (requests.RequestException, ValueError):
        return

    now = time.time()
    running_ids = set()
    for prof in profiles:
        if str(prof.get("status", "")).lower() != "running":
            continue
        pid = prof.get("id")
        if not pid:
            continue
        running_ids.add(pid)
        idle = now - _profile_last_activity(pid, now)
        if idle <= timeout_seconds:
            continue
        try:
            requests.post(f"{base}/api/profiles/{pid}/stop", headers=headers, timeout=15)
            logger.info(
                "cloak idle-reaper: stopped profile %s (%s) after %.0f min idle",
                prof.get("name") or pid, pid, idle / 60.0,
            )
            with _lock:
                _first_seen.pop(pid, None)
                for url in [u for u in _last_activity if pid in u]:
                    _last_activity.pop(url, None)
        except requests.RequestException as exc:
            logger.debug("idle-reaper: stop %s failed: %s", pid, exc)

    # Forget bookkeeping for profiles that are no longer running.
    with _lock:
        for pid in [p for p in _first_seen if p not in running_ids]:
            _first_seen.pop(pid, None)


def _loop() -> None:
    logger.info("cloak idle-reaper thread running (poll %ds)", _POLL_SECONDS)
    while True:
        try:
            timeout_min = _read_timeout_min()
            if timeout_min > 0:
                _reap_once(timeout_min * 60.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("idle-reaper loop error: %s", exc)
        time.sleep(_POLL_SECONDS)


def start() -> None:
    """Start the reaper thread once. Idempotent and safe to call from anywhere
    (plugin register, dashboard save). The thread self-gates on the timeout, so
    it is cheap to run even when the feature is disabled."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_loop, name="cloak-idle-reaper", daemon=True)
    thread.start()
