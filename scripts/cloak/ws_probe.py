"""Minimal WebSocket upgrade probe for Cloak CDP bridge readiness.

Exits 0 only on a successful HTTP 101 Switching Protocols response.
Usage:
  python scripts/cloak/ws_probe.py --url ws://127.0.0.1:8081 --timeout 5
"""
from __future__ import annotations

import argparse
import asyncio
import sys


async def _probe(url: str, timeout: float) -> int:
    try:
        import websockets  # type: ignore
    except ImportError:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode()
            writer.write(req)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            status = data.decode("latin-1", errors="ignore").split("\r\n", 1)[0]
            # Strict: only 101 counts as a working WS upgrade path.
            if " 101 " in f" {status} " or status.endswith(" 101") or "101 Switching" in status:
                return 0
            return 1
        except Exception:
            return 1

    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(url, open_timeout=timeout, close_timeout=1):
                return 0
    except Exception:
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--timeout", type=float, default=5.0)
    args = p.parse_args(argv)
    return asyncio.run(_probe(args.url, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
