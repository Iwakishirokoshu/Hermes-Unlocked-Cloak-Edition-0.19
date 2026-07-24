"""HTTP readiness probe for Cloak CDP auth proxy / bridge.

The Manager CDP WebSocket lives only at ``/api/profiles/{id}/cdp``.
Probing ``ws://host:8081/`` can never get a healthy 101. Auth-proxy
readiness is instead: GET ``/api/profiles`` through the proxy returns 200
(Bearer injected by nginx/bridge).

Usage:
  python scripts/cloak/http_probe.py --url http://127.0.0.1:8081/api/profiles --timeout 5
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request

PROTECTED_MANAGER_PATH = "/api/profiles"


def probe(url: str, timeout: float, bearer_token: str = "") -> int:
    try:
        headers = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 0 if getattr(resp, "status", 200) == 200 else 1
    except urllib.error.HTTPError:
        # 401/403 mean the proxy is up but auth injection failed — not ready.
        return 1
    except Exception:
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument(
        "--bearer-env",
        default="",
        help="read an optional Bearer token from this environment variable",
    )
    args = p.parse_args(argv)
    token = os.environ.get(args.bearer_env, "") if args.bearer_env else ""
    return 1 if args.bearer_env and not token else probe(args.url, args.timeout, token)


if __name__ == "__main__":
    sys.exit(main())
