#!/usr/bin/env python3
"""
cloak-proxy-pool — persistent rotation for Cloak profiles.

Storage (shared contract with Hermes runtime / dashboard):
    Prefer $CLOAK_PROXY_POOL_FILE, else:
      Linux with /etc/cloak/proxies.json → that file
      otherwise ~/.hermes/cloak/proxies.json  (or $CLOAK_POOL_DIR / $CLOAK_DIR)

On-disk JSON is always an array of objects::

    [{"url": "http://...", "assigned_to": null, "used_at": null}, ...]

Legacy runtime dict ``{"proxies":[...]}`` is accepted on read.

CLI:
    python3 pool.py load           # parse stdin, merge new proxies
    python3 pool.py next <profile> # atomically claim next free proxy
    python3 pool.py release <profile>
    python3 pool.py status         # total/free/used
    python3 pool.py list           # masked dump (passwords hidden)
    python3 pool.py list --raw     # full URLs including passwords

Locks: fcntl on POSIX, msvcrt on Windows — safe across parallel processes.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

_HAS_FCNTL = False
_HAS_MSVCRT = False
try:
    import fcntl  # POSIX only

    _HAS_FCNTL = True
except ImportError:
    pass
try:
    import msvcrt  # Windows

    _HAS_MSVCRT = True
except ImportError:
    pass


def _resolve_pool_paths() -> tuple[Path, Path]:
    """Match plugins.browser.cloak.paths.proxy_pool_file() when possible."""
    explicit = os.environ.get("CLOAK_PROXY_POOL_FILE", "").strip()
    if explicit:
        pool = Path(explicit)
        return pool, Path(str(pool) + ".lock")

    cloak_dir_env = os.environ.get("CLOAK_DIR", "").strip()
    pool_dir_env = os.environ.get("CLOAK_POOL_DIR", "").strip()
    if pool_dir_env:
        base = Path(pool_dir_env)
    elif cloak_dir_env:
        base = Path(cloak_dir_env)
    else:
        legacy = Path("/etc/cloak/proxies.json")
        if os.name != "nt" and legacy.is_file():
            return legacy, Path(str(legacy) + ".lock")
        etc = Path("/etc/cloak")
        if os.name != "nt" and etc.is_dir():
            base = etc
        else:
            base = Path.home() / ".hermes" / "cloak"

    pool = base / "proxies.json"
    return pool, Path(str(pool) + ".lock")


POOL_FILE, LOCK_FILE = _resolve_pool_paths()


# ---------- IO ----------------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _locked():
    POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if _HAS_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        elif _HAS_MSVCRT:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                os.write(fd, b"0")
            except OSError:
                pass
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            yield
    finally:
        os.close(fd)


def _as_entries(data) -> list[dict]:
    """Normalize skill array *or* legacy runtime dict into skill objects."""
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("proxies") or data.get("pool") or []
    else:
        return []
    out: list[dict] = []
    for item in raw_items:
        if isinstance(item, str):
            url = parse_proxy_line(item)
            if url:
                out.append({"url": url, "assigned_to": None, "used_at": None})
        elif isinstance(item, dict):
            url = (item.get("url") or item.get("proxy") or "").strip()
            if not url:
                continue
            norm = parse_proxy_line(url)
            if not norm:
                continue
            out.append(
                {
                    "url": norm,
                    "assigned_to": item.get("assigned_to"),
                    "used_at": item.get("used_at"),
                }
            )
    return out


def _load() -> list[dict]:
    if not POOL_FILE.exists():
        return []
    try:
        with open(POOL_FILE, "r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
        return _as_entries(data)
    except (OSError, json.JSONDecodeError):
        return []


def _save(data: list[dict]) -> None:
    POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".proxies.", suffix=".tmp", dir=str(POOL_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, POOL_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------- Parsing -----------------------------------------------------------

_RE_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_RE_USERPASS_AT = re.compile(r"^(?P<u>[^:@\s]+):(?P<p>[^@\s]+)@(?P<h>[^:\s]+):(?P<port>\d+)$")
_RE_HOST_PORT_USERPASS = re.compile(r"^(?P<h>[^:\s]+):(?P<port>\d+):(?P<u>[^:\s]+):(?P<p>[^:\s]+)$")
_RE_HOST_PORT = re.compile(r"^(?P<h>[^:\s]+):(?P<port>\d+)$")


def parse_proxy_line(line: str) -> str | None:
    """Convert a single user-pasted line into a Manager-safe URL.

    Returns None for blanks/comments/garbage/socks4.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    if _RE_HAS_SCHEME.match(s):
        scheme = s.split("://", 1)[0].lower()
        if scheme == "socks5h":
            return "socks5://" + s.split("://", 1)[1]
        if scheme not in {"http", "https", "socks5"}:
            return None
        return s

    m = _RE_USERPASS_AT.match(s)
    if m:
        return f"http://{m['u']}:{m['p']}@{m['h']}:{m['port']}"

    m = _RE_HOST_PORT_USERPASS.match(s)
    if m:
        return f"http://{m['u']}:{m['p']}@{m['h']}:{m['port']}"

    m = _RE_HOST_PORT.match(s)
    if m:
        return f"http://{m['h']}:{m['port']}"

    return None


def _mask_url(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.hostname:
            return url
        netloc = p.hostname
        if p.port:
            netloc = f"{netloc}:{p.port}"
        if p.username:
            cred = p.username + (":****" if p.password else "")
            return f"{p.scheme}://{cred}@{netloc}"
        return f"{p.scheme}://{netloc}"
    except Exception:
        return url


# ---------- Operations --------------------------------------------------------

def cmd_load(argv: list[str]) -> int:
    if sys.stdin.isatty() and not argv:
        print("error: load expects proxies on stdin (one per line)", file=sys.stderr)
        return 2

    raw = sys.stdin.read()
    parsed: list[str] = []
    for line in raw.splitlines():
        url = parse_proxy_line(line)
        if url:
            parsed.append(url)

    with _locked():
        data = _load()
        existing = {item.get("url") for item in data}
        added = 0
        for url in parsed:
            if url in existing:
                continue
            data.append({"url": url, "assigned_to": None, "used_at": None})
            existing.add(url)
            added += 1
        _save(data)

    print(json.dumps({"added": added, "total": len(data), "skipped_duplicates": len(parsed) - added}))
    return 0


def _canonical_profile_owner(profile: str) -> str:
    value = str(profile or "").strip()
    if not value:
        return ""
    return value if value.startswith("profile:") else f"profile:{value}"


def _profile_owner_aliases(profile: str) -> set[str]:
    canonical = _canonical_profile_owner(profile)
    if not canonical:
        return set()
    legacy = canonical[len("profile:") :]
    return {canonical, legacy}


def cmd_next(argv: list[str]) -> int:
    if not argv:
        print("error: usage: next <profile_name>", file=sys.stderr)
        return 2
    profile = str(argv[0] or "").strip()
    owner = _canonical_profile_owner(profile)
    aliases = _profile_owner_aliases(profile)

    with _locked():
        data = _load()
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            if item.get("assigned_to") in aliases:
                if item.get("assigned_to") != owner:
                    item["assigned_to"] = owner
                    _save(data)
                print(json.dumps({"url": item["url"], "index": idx, "profile": profile}))
                return 0
        for idx, item in enumerate(data):
            if not isinstance(item, dict) or item.get("assigned_to"):
                continue
            item["assigned_to"] = owner
            item["used_at"] = _now_iso()
            _save(data)
            print(json.dumps({"url": item["url"], "index": idx, "profile": profile}))
            return 0

    print(json.dumps({"error": "pool_exhausted", "url": None, "profile": profile}), file=sys.stderr)
    return 1


def cmd_release(argv: list[str]) -> int:
    if not argv:
        print("error: usage: release <profile_name>", file=sys.stderr)
        return 2
    profile = str(argv[0] or "").strip()
    aliases = _profile_owner_aliases(profile)

    with _locked():
        data = _load()
        released = 0
        for item in data:
            if item.get("assigned_to") in aliases:
                item["assigned_to"] = None
                item["used_at"] = None
                released += 1
        _save(data)

    print(json.dumps({"released": released, "profile": profile}))
    return 0


def cmd_status(argv: list[str]) -> int:
    with _locked():
        data = _load()
    used = sum(1 for it in data if it.get("assigned_to"))
    free = len(data) - used
    print(json.dumps({"total": len(data), "used": used, "free": free, "file": str(POOL_FILE)}))
    return 0


def cmd_list(argv: list[str]) -> int:
    raw = "--raw" in argv
    with _locked():
        data = _load()
    if raw:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        masked = []
        for item in data:
            row = dict(item)
            if row.get("url"):
                row["url"] = _mask_url(str(row["url"]))
            masked.append(row)
        print(json.dumps(masked, indent=2, ensure_ascii=False))
    return 0


# ---------- Main --------------------------------------------------------------

COMMANDS = {
    "load": cmd_load,
    "next": cmd_next,
    "release": cmd_release,
    "status": cmd_status,
    "list": cmd_list,
}


def main(argv: list[str]) -> int:
    # Re-resolve in case env was set after import (tests).
    global POOL_FILE, LOCK_FILE
    POOL_FILE, LOCK_FILE = _resolve_pool_paths()

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    fn = COMMANDS.get(cmd)
    if fn is None:
        print(f"error: unknown command '{cmd}'. valid: {', '.join(COMMANDS)}", file=sys.stderr)
        return 2
    return fn(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
