#!/usr/bin/env python3
"""Non-interactive entry-point for the Gmail Factory vendor.

Reads job parameters from stdin (preferred) or argparse flags, drives
``core.batch_runner.run_batch`` and writes a structured JSON result to
stdout. All log output goes to stderr so callers (the Hermes plug-in)
can parse stdout cleanly.

Stdin payload schema (all fields optional):

    {
        "count": int >= 1,             // default 1
        "use_sms": bool,               // default false
        "sms_provider": str|null,      // overrides Config.* on the fly
        "warmup_minutes": int >= 0,    // default 0
        "flow_mode": "standard"|"youtube"|"workspace",  // default standard
        "password": str|null,          // overrides Config.YOUR_PASSWORD
        "recovery_email": str|null
    }

stdout payload (always JSON, even on failure):

    {
        "ok": bool,
        "total": int, "successes": int, "failures": int,
        "duration": float,
        "results": [
            {"index", "email", "password", "success", "error_type", "proxy"},
            ...
        ],
        "error": str|null
    }

Exit codes:
    0  — at least one account succeeded
    1  — every attempt failed
    2  — invalid input / engine bootstrap error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

VENDOR_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(VENDOR_ROOT))

from core.redaction import redact_proxy_for_log, redact_runtime_value


def _setup_logging() -> None:
    # Log to stderr; stdout is reserved for the JSON result.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load_payload() -> dict:
    """Read job parameters from argv (--stdin) or CLI flags."""
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--stdin", action="store_true",
                        help="Read JSON payload from stdin (preferred).")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--use-sms", action="store_true")
    parser.add_argument("--no-sms", dest="use_sms", action="store_false")
    parser.set_defaults(use_sms=False)
    parser.add_argument("--sms-provider", default=None)
    parser.add_argument("--warmup-minutes", type=int, default=0)
    parser.add_argument("--flow-mode", default="standard",
                        choices=["standard", "youtube", "workspace"])
    parser.add_argument("--password", default=None)
    parser.add_argument("--recovery-email", default=None)
    args = parser.parse_args()

    if args.stdin:
        raw = sys.stdin.read().strip()
        if not raw:
            raise ValueError("--stdin given but stdin was empty")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("stdin JSON must be an object")
        return payload

    return {
        "count": args.count,
        "use_sms": args.use_sms,
        "sms_provider": args.sms_provider,
        "warmup_minutes": args.warmup_minutes,
        "flow_mode": args.flow_mode,
        "password": args.password,
        "recovery_email": args.recovery_email,
    }


def _apply_payload_to_env(payload: dict) -> None:
    """Project payload fields onto the env vars that Config reads.

    Done before importing config.settings so the Config class sees them.
    Caller-supplied values override anything already in the process env.
    """
    if payload.get("password"):
        os.environ["YOUR_PASSWORD"] = str(payload["password"])
    if payload.get("recovery_email"):
        os.environ["RECOVERY_EMAIL"] = str(payload["recovery_email"])

    # ENGINE_MODE is always playwright in this slim vendor build.
    os.environ.setdefault("ENGINE_MODE", "playwright")


def _emit(result: dict, exit_code: int) -> None:
    """Write the JSON result to stdout and exit with the right code."""
    try:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.stdout.flush()
    finally:
        sys.exit(exit_code)


def main() -> None:
    _setup_logging()
    log = logging.getLogger("gmail_factory.hermes_runner")

    try:
        payload = _load_payload()
    except Exception as e:  # noqa: BLE001
        _emit({
            "ok": False, "total": 0, "successes": 0, "failures": 0,
            "duration": 0.0, "results": [],
            "error": f"invalid payload: {e}",
        }, exit_code=2)
        return  # unreachable

    count = max(1, int(payload.get("count", 1)))
    use_sms = bool(payload.get("use_sms", False))
    warmup_minutes = max(0, int(payload.get("warmup_minutes", 0)))
    flow_mode = str(payload.get("flow_mode", "standard"))
    log.info(
        "Job: count=%d use_sms=%s warmup=%dmin flow=%s cdp=%s",
        count, use_sms, warmup_minutes, flow_mode,
        bool(os.environ.get("BROWSER_CDP_URL", "").strip()),
    )

    _apply_payload_to_env(payload)

    try:
        from core.batch_runner import run_batch
    except Exception as e:  # noqa: BLE001
        log.error("Bootstrap failed: %s\n%s", e, traceback.format_exc())
        _emit({
            "ok": False, "total": 0, "successes": 0, "failures": 0,
            "duration": 0.0, "results": [],
            "error": f"bootstrap: {e}",
        }, exit_code=2)
        return  # unreachable

    try:
        # batch_runner exposes a synchronous run_batch which under the hood
        # uses ThreadPoolExecutor + asyncio.run per worker. We don't need
        # to wrap it in our own event loop.
        summary = run_batch(
            num_accounts=count,
            max_threads=1,  # serial for now; Cloak profile is single-tab
            warmup_minutes=warmup_minutes,
            flow_mode=flow_mode,
            use_sms_api=use_sms,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Batch failed: %s", redact_runtime_value(e))
        _emit({
            "ok": False, "total": count, "successes": 0, "failures": count,
            "duration": 0.0, "results": [],
            "error": redact_runtime_value(e),
        }, exit_code=1)
        return  # unreachable

    # Decorate each result with the password we used (run_batch already
    # stores the email in DatabaseManager; the password is the one passed
    # via YOUR_PASSWORD env / generated upstream).
    results = []
    for r in summary.get("results", []):
        results.append({
            "index": r.get("index"),
            "email": r.get("email"),
            "password": r.get("password"),
            "success": r.get("success", False),
            "error_type": redact_runtime_value(r.get("error_type")),
            "proxy": redact_proxy_for_log(r.get("proxy")),
        })

    successes = int(summary.get("successes", 0))
    payload_out = {
        "ok": successes > 0,
        "total": int(summary.get("total", count)),
        "successes": successes,
        "failures": int(summary.get("failures", count - successes)),
        "duration": float(summary.get("duration", 0.0)),
        "results": results,
        "error": None,
    }
    _emit(payload_out, exit_code=0 if successes > 0 else 1)


if __name__ == "__main__":
    main()
