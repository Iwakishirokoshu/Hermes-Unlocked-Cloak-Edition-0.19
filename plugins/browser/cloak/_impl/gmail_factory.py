"""Gmail Factory tools — bridge from Hermes to the vendored Gmail-infinity build.

The factory itself lives in ``/usr/local/lib/gmail-factory/`` (installed by
``scripts/install_gmail_factory.sh``). It has its own venv at
``/usr/local/lib/gmail-factory/venv``. We never import the factory into
the Hermes process — instead we shell out to ``hermes_runner.py`` over a
JSON stdin/stdout protocol, so factory deps (Playwright, faker, …) stay
isolated from Hermes' venv.

Public surface (registered in ``__init__.py``):

  - ``gmail_factory_status``  — installed? venv? keys? proxies? accounts?
  - ``gmail_factory_create``  — create N accounts with optional SMS / warmup
  - ``gmail_factory_list``    — read accounts.json
  - ``gmail_factory_warmup``  — warm existing accounts via Playwright

The handler functions are plain async coroutines returning ``dict``;
``__init__.py`` wraps them with ``_wrap_async_tool`` so Hermes gets a
JSON string back, the canonical tool-handler shape.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from agent.redact import redact_cdp_url
from ..proxy_format import mask_proxy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Installation layout
# ---------------------------------------------------------------------------

FACTORY_ROOT = Path(os.environ.get("GMAIL_FACTORY_ROOT", "/usr/local/lib/gmail-factory"))
FACTORY_VENV_PY = FACTORY_ROOT / "venv" / "bin" / "python"
FACTORY_RUNNER = FACTORY_ROOT / "hermes_runner.py"
FACTORY_ENV_FILE = Path(os.environ.get(
    "GMAIL_FACTORY_ENV", "/etc/gmail-factory/runner.env"
))
FACTORY_ACCOUNTS_FILE = FACTORY_ROOT / "data" / "accounts.json"
FACTORY_PROXIES_FILE = FACTORY_ROOT / "config" / "proxies.txt"

# Cap to keep the agent from accidentally asking for hundreds of accounts.
MAX_COUNT_PER_CALL = 10

# How long to wait for the subprocess. ~5 min per account is realistic
# (prewarm + signup + verification). We add a comfy buffer.
PER_ACCOUNT_TIMEOUT_S = 540
MIN_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# Schemas (Hermes tool-call schema; matches the format used by tools_manage.py)
# ---------------------------------------------------------------------------

SCHEMA_STATUS = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

SCHEMA_CREATE = {
    "type": "object",
    "properties": {
        "count": {
            "type": "integer", "minimum": 1, "maximum": MAX_COUNT_PER_CALL,
            "description": f"Number of accounts to create (1..{MAX_COUNT_PER_CALL}).",
        },
        "use_sms": {
            "type": "boolean", "default": False,
            "description": "Use a real SMS provider for phone verification (Premium mode).",
        },
        "sms_provider": {
            "type": ["string", "null"],
            "enum": [None, "5sim", "sms-activate", "onlinesim", "getsms"],
            "default": None,
            "description": "Force a specific SMS provider; null = whatever Config picks.",
        },
        "warmup_minutes": {
            "type": "integer", "minimum": 0, "maximum": 30, "default": 0,
            "description": "Run post-signup warm-up for N minutes (0 = skip).",
        },
        "flow_mode": {
            "type": "string", "enum": ["standard", "youtube", "workspace"],
            "default": "standard",
        },
        "password": {
            "type": ["string", "null"], "default": None,
            "description": "Optional fixed password for all accounts in this batch; null/omit = auto-generate a unique random password per account.",
        },
        "recovery_email": {
            "type": ["string", "null"], "default": None,
        },
        "unmask": {
            "type": "boolean", "default": False,
            "description": "Return full unmasked passwords. Default false: passwords are partially masked in the response.",
        },
    },
    "additionalProperties": False,
}

SCHEMA_LIST = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        "unmask": {"type": "boolean", "default": False},
    },
    "additionalProperties": False,
}

SCHEMA_WARMUP = {
    "type": "object",
    "properties": {
        "emails": {
            "type": "array", "items": {"type": "string"}, "minItems": 1,
            "description": "List of @gmail.com addresses to warm up.",
        },
        "minutes": {
            "type": "integer", "minimum": 1, "maximum": 30, "default": 5,
        },
    },
    "required": ["emails"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# gmail_factory_status
# ---------------------------------------------------------------------------


async def gmail_factory_status(args: dict, **kwargs: Any) -> dict:  # noqa: ARG001
    """Report whether the vendor is installed and what's configured."""
    installed = FACTORY_ROOT.is_dir() and FACTORY_RUNNER.is_file()
    venv_ok = FACTORY_VENV_PY.exists()

    env_file_present = FACTORY_ENV_FILE.exists()
    env_keys: dict[str, bool] = {}
    if env_file_present:
        env_keys = _detect_env_keys(FACTORY_ENV_FILE)

    proxies_count = _count_lines(FACTORY_PROXIES_FILE)
    accounts_count = _count_accounts(FACTORY_ACCOUNTS_FILE)

    cdp_url = os.environ.get("BROWSER_CDP_URL", "").strip()

    return {
        "installed": installed,
        "venv": "ok" if venv_ok else "missing",
        "factory_root": str(FACTORY_ROOT),
        "runner": str(FACTORY_RUNNER),
        "env_file": str(FACTORY_ENV_FILE),
        "env_file_present": env_file_present,
        "sms_keys_configured": [
            k for k in ("FIVESIM_API_KEY", "SMS_ACTIVATE_API_KEY",
                        "ONLINESIM_API_KEY", "GETSMS_API_KEY") if env_keys.get(k)
        ],
        "captcha_keys_configured": [
            k for k in ("TWOCAPTCHA_API_KEY", "ANTICAPTCHA_API_KEY",
                        "CAPMONSTER_API_KEY", "CAPSOLVER_API_KEY") if env_keys.get(k)
        ],
        "proxies_count": proxies_count,
        "accounts_saved": accounts_count,
        "cloak_active": bool(cdp_url),
        "browser_cdp_url": redact_cdp_url(cdp_url) if cdp_url else None,
        "hint": _status_hint(installed, venv_ok, env_keys),
    }


def _status_hint(installed: bool, venv_ok: bool, env_keys: dict[str, bool]) -> str:
    if not installed:
        return (
            "Gmail Factory is not installed. Run "
            "'/opt/hermes-cloak-patch/install.sh --with-gmail-factory' "
            "on the VPS."
        )
    if not venv_ok:
        return (
            "Vendor present but the dedicated venv is missing. Re-run "
            "the installer; the venv normally lives at "
            f"{FACTORY_VENV_PY.parent}."
        )
    sms_ok = any(env_keys.get(k) for k in (
        "FIVESIM_API_KEY", "SMS_ACTIVATE_API_KEY",
        "ONLINESIM_API_KEY", "GETSMS_API_KEY",
    ))
    if not sms_ok:
        return (
            "Installed but no SMS provider key found in "
            f"{FACTORY_ENV_FILE}. Free (ghost) mode will work; "
            "Premium (use_sms=true) won't until a key is added."
        )
    return "ready"


# ---------------------------------------------------------------------------
# gmail_factory_create
# ---------------------------------------------------------------------------


async def gmail_factory_create(args: dict, **kwargs: Any) -> dict:  # noqa: ARG001
    """Create accounts by shelling out to ``hermes_runner.py``."""
    if not _vendor_ready():
        return {
            "ok": False,
            "error": "vendor_not_installed",
            "hint": (
                "Run install.sh --with-gmail-factory first; see "
                "gmail_factory_status for details."
            ),
        }

    count = int(args.get("count", 1))
    if count < 1 or count > MAX_COUNT_PER_CALL:
        return {
            "ok": False,
            "error": "invalid_count",
            "hint": f"count must be in 1..{MAX_COUNT_PER_CALL}",
        }

    use_sms = bool(args.get("use_sms", False))
    if use_sms:
        env_keys = _detect_env_keys(FACTORY_ENV_FILE)
        sms_ok = any(env_keys.get(k) for k in (
            "FIVESIM_API_KEY", "SMS_ACTIVATE_API_KEY",
            "ONLINESIM_API_KEY", "GETSMS_API_KEY",
        ))
        if not sms_ok:
            return {
                "ok": False,
                "error": "sms_key_missing",
                "hint": (
                    "use_sms=true but no SMS provider key is set in "
                    f"{FACTORY_ENV_FILE}. Add at least one of "
                    "FIVESIM_API_KEY / SMS_ACTIVATE_API_KEY / "
                    "ONLINESIM_API_KEY / GETSMS_API_KEY, then retry."
                ),
            }

    payload = {
        "count": count,
        "use_sms": use_sms,
        "sms_provider": args.get("sms_provider"),
        "warmup_minutes": int(args.get("warmup_minutes", 0)),
        "flow_mode": args.get("flow_mode", "standard"),
        "password": args.get("password"),
        "recovery_email": args.get("recovery_email"),
    }
    unmask = bool(args.get("unmask", False))

    timeout = max(MIN_TIMEOUT_S, count * PER_ACCOUNT_TIMEOUT_S)
    result = await _run_factory(payload, timeout_s=timeout)

    # On any structured failure (timeout, subprocess_failed, ...) hand the
    # redacted shape back so the agent can show stderr_tail / hint to the user.
    # Only the success path needs the masking layer.
    if not result.get("ok"):
        return _redact_runner_output(result)

    # Mask passwords by default. The agent must explicitly pass unmask=true
    # (and tell the user to do the same) to see the raw value.
    masked_results = []
    for r in result.get("results", []):
        safe_result = _redact_runner_output(r)
        masked_results.append({
            **safe_result,
            "password": (r.get("password") if unmask else _mask(r.get("password"))),
            "password_masked": not unmask,
            "proxy": mask_proxy(r.get("proxy")) if r.get("proxy") else None,
        })

    return {
        "ok": True,
        "total": result.get("total"),
        "successes": result.get("successes"),
        "failures": result.get("failures"),
        "duration": result.get("duration"),
        "results": masked_results,
        "error": result.get("error"),
    }


# ---------------------------------------------------------------------------
# gmail_factory_list
# ---------------------------------------------------------------------------


async def gmail_factory_list(args: dict, **kwargs: Any) -> dict:  # noqa: ARG001
    """Read ``data/accounts.json`` and return a paginated, optionally
    masked, view."""
    limit = int(args.get("limit", 50))
    unmask = bool(args.get("unmask", False))

    if not FACTORY_ACCOUNTS_FILE.is_file():
        return {"ok": False, "error": "no_accounts_file", "accounts": []}

    try:
        raw = json.loads(FACTORY_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("accounts.json must be a list")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"parse_error: {e}", "accounts": []}

    rows = raw[-limit:]
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "email": r.get("email"),
            "password": (r.get("password") if unmask else _mask(r.get("password"))),
            "password_masked": not unmask,
            "created_at": r.get("created_at"),
            "status": r.get("status"),
            "strategy": r.get("strategy"),
        })

    return {
        "ok": True,
        "total": len(raw),
        "returned": len(out),
        "accounts": out,
    }


# ---------------------------------------------------------------------------
# gmail_factory_warmup
# ---------------------------------------------------------------------------


async def gmail_factory_warmup(args: dict, **kwargs: Any) -> dict:  # noqa: ARG001
    """Warm up existing accounts.

    Shells out to the same ``hermes_runner.py`` with a different payload
    (count=0 means "no creation, just warm the listed emails"). The
    runner translates that to ``core.account_warmer.warm_account_playwright``
    invocations.

    NOTE: the vendor's runner currently only supports inline warmup as a
    post-creation step. Standalone warmup is exposed here as a TODO —
    we return a structured "not_implemented" so the agent can fall back
    to manual interaction.
    """
    emails = args.get("emails") or []
    minutes = int(args.get("minutes", 5))
    if not emails:
        return {"ok": False, "error": "no_emails"}

    if not _vendor_ready():
        return {"ok": False, "error": "vendor_not_installed"}

    return {
        "ok": False,
        "error": "not_implemented",
        "hint": (
            "Standalone warmup is not wired yet. For now, pass "
            "warmup_minutes>0 to gmail_factory_create at creation time. "
            "Tracked: TODO add warm-only mode to hermes_runner.py."
        ),
        "received": {"emails": emails, "minutes": minutes},
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _vendor_ready() -> bool:
    return (
        FACTORY_ROOT.is_dir()
        and FACTORY_RUNNER.is_file()
        and FACTORY_VENV_PY.exists()
    )


def _mask(pw: str | None) -> str | None:
    if pw is None:
        return None
    if not isinstance(pw, str) or len(pw) <= 6:
        return "***"
    return pw[:2] + "***" + pw[-2:]


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
    except Exception:
        return 0


def _count_accounts(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
    except Exception:
        pass
    return 0


def _detect_env_keys(path: Path) -> dict[str, bool]:
    """Parse KEY=value lines from runner.env and return per-key truthiness.

    A key counts as "configured" iff its value is non-empty, not a
    placeholder like ``YOUR_*`` and not surrounded by quotes alone.
    """
    if not path.is_file():
        return {}
    result: dict[str, bool] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            configured = bool(value) and not value.startswith("YOUR_")
            result[key.strip()] = configured
    except Exception as e:  # noqa: BLE001
        logger.debug("Failed to parse %s: %s", path, e)
    return result


def _redact_runner_output(value: Any) -> Any:
    """Remove endpoint/proxy credentials from runner diagnostics recursively."""
    if isinstance(value, str):
        return redact_cdp_url(value)
    if isinstance(value, dict):
        return {
            key: (mask_proxy(item) if key == "proxy" and item else _redact_runner_output(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_runner_output(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_runner_output(item) for item in value)
    return value


async def _run_factory(payload: dict, timeout_s: int) -> dict:
    """Run ``hermes_runner.py --stdin``, send the payload, parse stdout."""
    env = _build_child_env()

    cmd = [str(FACTORY_VENV_PY), str(FACTORY_RUNNER), "--stdin"]
    logger.info("gmail_factory: launching %s (timeout=%ds)", " ".join(cmd), timeout_s)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(FACTORY_ROOT),
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": f"subprocess_failed: {e}"}

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode("utf-8")),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {
            "ok": False,
            "error": "timeout",
            "hint": (
                f"hermes_runner.py exceeded {timeout_s}s; see "
                "/var/log/hermes/* for partial logs."
            ),
        }

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    exit_code = proc.returncode

    parsed = _extract_last_json(stdout)
    if parsed is None:
        return {
            "ok": False,
            "error": "subprocess_failed",
            "exit_code": exit_code,
            "stdout_tail": redact_cdp_url(stdout[-1000:]),
            "stderr_tail": redact_cdp_url(stderr[-2000:]),
        }
    return parsed


def _extract_last_json(text: str) -> dict | None:
    """Find the LAST JSON object in ``text`` and return it as a dict.

    ``hermes_runner.py`` prints exactly one JSON object on its own line,
    but tolerating extra noise (e.g. ``DeprecationWarning`` from a child
    lib that escaped stderr re-routing) keeps the integration robust.
    """
    # Cheap fast path: stdout was just one JSON object.
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass

    # Generic path: scan for top-level {...} blocks back-to-front.
    decoder = json.JSONDecoder()
    matches: list[dict] = []
    pos = 0
    while pos < len(text):
        i = text.find("{", pos)
        if i == -1:
            break
        try:
            obj, end = decoder.raw_decode(text[i:])
            if isinstance(obj, dict):
                matches.append(obj)
            pos = i + max(1, end)
        except json.JSONDecodeError:
            pos = i + 1
    return matches[-1] if matches else None


def _build_child_env() -> dict:
    """Compose the env we hand to ``hermes_runner.py``.

    Layering (later overrides earlier):
      1. parent env (PATH, LANG, ...)
      2. /etc/gmail-factory/runner.env (KEY=value lines, comments ignored)
      3. BROWSER_CDP_URL from the current Hermes process (set by
         cloak_set_active) — this is what makes the vendor attach to
         Cloak instead of launching a local Chromium.
      4. CLOAK_INTEGRATION=1 marker, so the vendor can flip behaviour
         even if BROWSER_CDP_URL is somehow unset.
    """
    env = dict(os.environ)

    if FACTORY_ENV_FILE.is_file():
        try:
            for raw_line in FACTORY_ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Strip optional surrounding quotes.
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                # Don't accidentally clobber BROWSER_CDP_URL from parent.
                if key == "BROWSER_CDP_URL" and env.get("BROWSER_CDP_URL"):
                    continue
                env[key] = value
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to read %s: %s", FACTORY_ENV_FILE, e)

    cdp_url = os.environ.get("BROWSER_CDP_URL", "").strip()
    if cdp_url:
        env["BROWSER_CDP_URL"] = cdp_url
        env["CLOAK_INTEGRATION"] = "1"

    # Hermes propagates CLOAK_MANAGER_URL too; pass it along — the vendor
    # doesn't talk to the manager directly but it's useful for diagnostics.
    if os.environ.get("CLOAK_MANAGER_URL"):
        env["CLOAK_MANAGER_URL"] = os.environ["CLOAK_MANAGER_URL"]

    return env


# ---------------------------------------------------------------------------
# Backwards-compat: re-export the tool function names that __init__.py wires
# ---------------------------------------------------------------------------

__all__ = [
    "gmail_factory_status",
    "gmail_factory_create",
    "gmail_factory_list",
    "gmail_factory_warmup",
    "SCHEMA_STATUS",
    "SCHEMA_CREATE",
    "SCHEMA_LIST",
    "SCHEMA_WARMUP",
    "FACTORY_ROOT",
    "FACTORY_RUNNER",
    "FACTORY_VENV_PY",
    "FACTORY_ENV_FILE",
]
