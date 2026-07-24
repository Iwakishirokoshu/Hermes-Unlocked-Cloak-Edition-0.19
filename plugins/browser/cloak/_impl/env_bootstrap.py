"""Bootstrap Cloak env vars from the manager env file.

Hermes gateway is often started manually without systemd EnvironmentFile.
Without this bootstrap the plugin falls back to wrong defaults and
``cloak_*`` tools fail with 401.
"""
from __future__ import annotations

import logging
import os

from agent.redact import redact_cdp_url

logger = logging.getLogger(__name__)

_LOADED = False


def bootstrap_cloak_env(env_file: str | None = None) -> bool:
    """Merge manager.env into ``os.environ`` without overriding existing keys."""
    global _LOADED
    if _LOADED:
        return True

    if env_file:
        path = env_file
    else:
        try:
            from plugins.browser.cloak.paths import manager_env_file

            path = str(manager_env_file())
        except Exception:
            path = os.environ.get("CLOAK_MANAGER_ENV", "")
    if not path or not os.path.isfile(path):
        return False

    loaded = 0
    try:
        from plugins.browser.cloak.env_file import parse_env_file

        parsed = parse_env_file(path)
        for key, value in parsed.items():
            if key in os.environ:
                continue
            os.environ[key] = value
            loaded += 1
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return False

    _LOADED = True
    if loaded:
        logger.info(
            "Loaded %d Cloak env var(s) from %s (CLOAK_MANAGER_URL=%s)",
            loaded,
            path,
            redact_cdp_url(os.environ.get("CLOAK_MANAGER_URL", "?")),
        )
    return True
