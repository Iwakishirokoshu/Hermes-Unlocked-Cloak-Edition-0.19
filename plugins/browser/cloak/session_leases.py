"""Task/session-scoped CDP leases for Cloak.

Avoids process-global ``BROWSER_CDP_URL`` races when multiple Hermes tasks
run concurrently. ``BROWSER_CDP_URL`` is still written for tools that read
the env (legacy cloak_* path), but only for the *active* lease of the
calling task when set via :func:`bind_env_for_task`.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class Lease:
    task_id: str
    profile_id: str
    cdp_url: str
    cdp_http_url: str = ""
    profile_name: str = ""
    features: dict = field(default_factory=dict)


_lock = threading.RLock()
_leases: Dict[str, Lease] = {}  # task_id -> Lease
_by_profile: Dict[str, str] = {}  # profile_id -> task_id


def put(lease: Lease) -> Lease:
    with _lock:
        old = _leases.get(lease.task_id)
        if old and old.profile_id in _by_profile:
            _by_profile.pop(old.profile_id, None)
        _leases[lease.task_id] = lease
        if lease.profile_id:
            _by_profile[lease.profile_id] = lease.task_id
        return lease


def get(task_id: str) -> Optional[Lease]:
    with _lock:
        return _leases.get(task_id)


def get_by_profile(profile_id: str) -> Optional[Lease]:
    with _lock:
        task_id = _by_profile.get(profile_id)
        return _leases.get(task_id) if task_id else None


def pop(task_id: str) -> Optional[Lease]:
    with _lock:
        lease = _leases.pop(task_id, None)
        if lease and lease.profile_id in _by_profile:
            if _by_profile.get(lease.profile_id) == task_id:
                _by_profile.pop(lease.profile_id, None)
        return lease


def pop_profile(profile_id: str) -> Optional[Lease]:
    with _lock:
        task_id = _by_profile.pop(profile_id, None)
        if not task_id:
            return None
        return _leases.pop(task_id, None)


def bind_env_for_task(task_id: str) -> Optional[Lease]:
    """Mirror the task lease into process env for legacy tools.

    Still process-global — safe only when the agent drives one task at a
    time *or* callers pass task_id into tools that resolve via
    :mod:`session_leases` / ``profile_state``. Always stamps
    ``CLOAK_ACTIVE_TASK_ID`` so ``create_session`` will not steal this URL
    for a different task.
    """
    lease = get(task_id)
    if not lease:
        return None
    os.environ["BROWSER_CDP_URL"] = lease.cdp_url
    if lease.cdp_http_url:
        os.environ["CLOAK_CDP_HTTP_URL"] = lease.cdp_http_url
    if lease.profile_id:
        os.environ["CLOAK_ACTIVE_PROFILE_ID"] = lease.profile_id
    if lease.profile_name:
        os.environ["CLOAK_ACTIVE_PROFILE_NAME"] = lease.profile_name
    os.environ["CLOAK_ACTIVE_TASK_ID"] = str(task_id)
    return lease


def clear_env_if_matches(profile_id: str) -> None:
    if os.environ.get("CLOAK_ACTIVE_PROFILE_ID") == profile_id:
        for key in (
            "BROWSER_CDP_URL",
            "CLOAK_CDP_HTTP_URL",
            "CLOAK_ACTIVE_PROFILE_ID",
            "CLOAK_ACTIVE_PROFILE_NAME",
            "CLOAK_ACTIVE_TASK_ID",
        ):
            os.environ.pop(key, None)


def snapshot() -> Dict[str, Lease]:
    with _lock:
        return dict(_leases)
