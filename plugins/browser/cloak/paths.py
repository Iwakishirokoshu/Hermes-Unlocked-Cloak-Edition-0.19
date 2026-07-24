"""Cross-platform Cloak config paths.

Linux installers historically used ``/etc/cloak/``. On Windows (and for
non-root Linux users) we fall back to ``~/.hermes/cloak/``. Explicit env
overrides always win.
"""
from __future__ import annotations

import os
from pathlib import Path


def cloak_dir() -> Path:
    """Resolved Cloak config directory (created lazily by callers)."""
    override = os.environ.get("CLOAK_DIR", "").strip()
    if override:
        return Path(override)
    if os.name == "nt" or not Path("/etc/cloak").is_dir():
        return Path.home() / ".hermes" / "cloak"
    return Path("/etc/cloak")


def manager_env_file() -> Path:
    override = os.environ.get("CLOAK_MANAGER_ENV", "").strip()
    if override:
        return Path(override)
    legacy = Path("/etc/cloak/manager.env")
    if legacy.is_file() and os.name != "nt":
        return legacy
    return cloak_dir() / "manager.env"


def proxy_pool_file() -> Path:
    override = os.environ.get("CLOAK_PROXY_POOL_FILE", "").strip()
    if override:
        return Path(override)
    pool_dir = os.environ.get("CLOAK_POOL_DIR", "").strip()
    if pool_dir:
        return Path(pool_dir) / "proxies.json"

    legacy = Path("/etc/cloak/proxies.json")
    if legacy.is_file() and os.name != "nt":
        return legacy
    return cloak_dir() / "proxies.json"


def ensure_cloak_dir() -> Path:
    path = cloak_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
