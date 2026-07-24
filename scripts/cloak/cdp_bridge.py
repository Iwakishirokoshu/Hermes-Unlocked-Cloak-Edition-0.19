"""Localhost CDP auth-injecting reverse proxy (Windows-friendly).

Native agent-browser cannot set Authorization on the WebSocket upgrade.
This tiny asyncio proxy listens on 127.0.0.1:8081 and injects
``Authorization: Bearer <token>`` toward CloakBrowser-Manager.

Reads the full HTTP request headers before injecting — a single ``read()``
is not enough under TCP segmentation.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import ssl
import sys
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("cloak.cdp_bridge")

_MAX_HEADER_BYTES = 1024 * 1024


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _inject_auth(
    raw: bytes, token: str, *, upstream_host_header: Optional[str] = None
) -> bytes:
    """Inject/replace Authorization header in the first HTTP request block."""
    try:
        head, rest = raw.split(b"\r\n\r\n", 1)
    except ValueError:
        return raw
    lines = head.split(b"\r\n")
    if not lines:
        return raw
    out = [lines[0]]
    auth = f"Authorization: Bearer {token}".encode()
    has_host = False
    replaced = False
    for line in lines[1:]:
        if line.lower().startswith(b"authorization:"):
            out.append(auth)
            replaced = True
        elif upstream_host_header and line.lower().startswith(b"host:"):
            out.append(f"Host: {upstream_host_header}".encode())
            has_host = True
        else:
            if line.lower().startswith(b"host:"):
                has_host = True
            out.append(line)
    if not replaced:
        out.append(auth)
    if upstream_host_header and not has_host:
        out.append(f"Host: {upstream_host_header}".encode())
    return b"\r\n".join(out) + b"\r\n\r\n" + rest


async def _read_http_request_head(
    reader: asyncio.StreamReader, *, timeout: float = 30.0
) -> bytes:
    """Read until the end of HTTP headers (``\\r\\n\\r\\n``), handling TCP splits."""
    buf = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while b"\r\n\r\n" not in buf:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError("timed out waiting for HTTP headers")
        chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
        if not chunk:
            break
        buf += chunk
        if len(buf) > _MAX_HEADER_BYTES:
            raise ValueError("HTTP headers exceed size limit")
    return buf


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_host: str,
    upstream_port: int,
    token: str,
    upstream_ssl: Optional[ssl.SSLContext],
    upstream_host_header: str,
) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        first = await _read_http_request_head(client_reader, timeout=30)
        if not first:
            client_writer.close()
            return
        if b"\r\n\r\n" not in first:
            logger.warning("bridge session %s: incomplete HTTP headers", peer)
            client_writer.close()
            return
        first = _inject_auth(
            first,
            token,
            upstream_host_header=upstream_host_header,
        )
        up_reader, up_writer = await asyncio.open_connection(
            upstream_host, upstream_port, ssl=upstream_ssl,
            server_hostname=upstream_host if upstream_ssl else None,
        )
        up_writer.write(first)
        await up_writer.drain()
        await asyncio.gather(
            _pipe(client_reader, up_writer),
            _pipe(up_reader, client_writer),
        )
    except Exception as exc:
        logger.debug("bridge session %s ended: %s", peer, exc)
        try:
            client_writer.close()
        except Exception:
            pass


def _upstream_target(upstream: str) -> tuple[str, int, Optional[ssl.SSLContext], str]:
    """Resolve a Manager upstream while preserving legacy bare-host defaults."""
    raw_upstream = upstream.strip()
    if not raw_upstream:
        raise ValueError("upstream is required")
    has_scheme = "://" in raw_upstream
    parsed = urlparse(raw_upstream if has_scheme else f"http://{raw_upstream}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("upstream must use http:// or https://")
    up_host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        up_port = parsed.port
    elif parsed.scheme == "https":
        up_port = 443
    elif has_scheme:
        up_port = 80
    else:
        # Backward compatibility for historical bare host or host:port input.
        # It targets the Manager's default port 8080.
        up_port = 8080
    up_ssl = ssl.create_default_context() if parsed.scheme == "https" else None
    up_host_header = parsed.netloc
    return up_host, up_port, up_ssl, up_host_header


async def _serve(listen_host: str, listen_port: int, upstream: str, token: str) -> None:
    up_host, up_port, up_ssl, up_host_header = _upstream_target(upstream)
    server = await asyncio.start_server(
        lambda r, w: _handle(
            r, w, up_host, up_port, token, up_ssl, up_host_header
        ),
        listen_host,
        listen_port,
    )
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    logger.info("CDP bridge listening on %s → %s:%s", sockets, up_host, up_port)
    async with server:
        await server.serve_forever()


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[cloak-cdp-bridge] %(message)s")
    p = argparse.ArgumentParser(description="Cloak CDP auth-injecting bridge")
    p.add_argument("--listen", default=os.environ.get("CLOAK_CDP_PROXY_BASE", "http://127.0.0.1:8081"))
    p.add_argument("--upstream", default=os.environ.get("CLOAK_MANAGER_URL", "http://127.0.0.1:8080"))
    p.add_argument("--token", default=os.environ.get("CLOAK_AUTH_TOKEN", ""))
    args = p.parse_args(argv)
    token = (args.token or "").strip()
    if not token:
        logger.error("CLOAK_AUTH_TOKEN / --token required")
        return 2
    listen = args.listen
    if "://" not in listen:
        listen = f"http://{listen}"
    lp = urlparse(listen)
    host = lp.hostname or "127.0.0.1"
    port = lp.port or 8081
    try:
        asyncio.run(_serve(host, port, args.upstream, token))
    except KeyboardInterrupt:
        return 0
    except ValueError as exc:
        logger.error("invalid bridge configuration: %s", exc)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
