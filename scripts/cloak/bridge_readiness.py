"""Validate the full Manager -> auth bridge -> CDP WebSocket route.

This probe creates an isolated temporary profile, starts it, upgrades a real
CDP WebSocket through the configured bridge, then stops and removes the
profile. It exits non-zero on every failure and never prints credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

try:
    from . import ws_probe
except ImportError:  # Direct execution: python scripts/cloak/bridge_readiness.py
    import ws_probe  # type: ignore[no-redef]


def _headers() -> dict[str, str]:
    token = os.environ.get("CLOAK_AUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("CLOAK_AUTH_TOKEN is required for protected readiness")
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _request(
    manager_url: str,
    method: str,
    path: str,
    timeout: float,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    headers = _headers()
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{manager_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Manager returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError("Manager request failed") from exc
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Manager returned invalid JSON") from exc


def _bridge_ws_url(bridge_url: str, cdp_url: str) -> str:
    bridge = urllib.parse.urlparse(bridge_url)
    if bridge.scheme not in {"http", "https"} or not bridge.netloc:
        raise RuntimeError("bridge URL must use http:// or https://")
    cdp = urllib.parse.urlparse(cdp_url)
    if cdp.scheme:
        path = cdp.path or "/"
        query = cdp.query
    else:
        path, _, query = cdp_url.partition("?")
        path = path if path.startswith("/") else f"/{path}"
    scheme = "wss" if bridge.scheme == "https" else "ws"
    return urllib.parse.urlunparse((scheme, bridge.netloc, path, "", query, ""))


def _cleanup(manager_url: str, profile_id: str, timeout: float) -> None:
    try:
        _request(manager_url, "POST", f"/api/profiles/{profile_id}/stop", timeout)
    except RuntimeError:
        pass
    for attempt in range(3):
        try:
            _request(manager_url, "DELETE", f"/api/profiles/{profile_id}", timeout)
            return
        except RuntimeError:
            if attempt < 2:
                time.sleep(0.25)


def probe(
    manager_url: str,
    bridge_url: str,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> int:
    profile_id = ""
    try:
        profile = _request(
            manager_url,
            "POST",
            "/api/profiles",
            timeout,
            {"name": f"hermes-cdp-readiness-{uuid.uuid4().hex[:12]}"},
        )
        if not isinstance(profile, dict):
            raise RuntimeError("Manager did not return a profile object")
        profile_id = str(profile.get("id") or profile.get("profile_id") or "")
        if not profile_id:
            raise RuntimeError("Manager did not return a profile id")

        launch = _request(
            manager_url,
            "POST",
            f"/api/profiles/{profile_id}/launch",
            timeout,
        )
        launch_data = launch if isinstance(launch, dict) else {}
        cdp_url = str(
            launch_data.get("cdp_url") or f"/api/profiles/{profile_id}/cdp"
        )
        websocket_url = _bridge_ws_url(bridge_url, cdp_url)

        for attempt in range(max(1, retries)):
            if asyncio.run(ws_probe._probe(websocket_url, timeout)) == 0:
                return 0
            if attempt + 1 < max(1, retries):
                time.sleep(max(0.0, retry_delay))
        raise RuntimeError("CDP WebSocket upgrade did not become ready")
    except RuntimeError as exc:
        print(f"Cloak bridge readiness failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if profile_id:
            _cleanup(manager_url, profile_id, timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-url", required=True)
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=10)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    args = parser.parse_args(argv)
    return probe(
        args.manager_url,
        args.bridge_url,
        args.timeout,
        args.retries,
        args.retry_delay,
    )


if __name__ == "__main__":
    sys.exit(main())

