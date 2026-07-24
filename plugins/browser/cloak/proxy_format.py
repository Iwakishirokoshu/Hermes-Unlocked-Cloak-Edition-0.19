"""Proxy pool: parse many proxy formats into the URL form CloakBrowser accepts,
persist the pool and hand out proxies round-robin / claim-style.

Pure standard library — **no** heavyweight deps and **no** import side effects,
so both the dependency-light dashboard (:mod:`hermes_cli.cloak_dashboard`) and
the rich ``_impl`` tools can import it safely.

**On-disk contract (shared with skills/cloak-proxy-pool):** a JSON array of
objects::

    [{"url": "http://user:pass@host:port", "assigned_to": null, "used_at": null}, ...]

Legacy runtime dict ``{"proxies": [...], "cursor": N, "strategy": "..."}`` is
still accepted on read; saves always rewrite to the array contract so the CLI
skill never sees an empty pool after a dashboard/runtime write.

CloakBrowser-Manager accepts::

    http://user:pass@host:port
    https://...
    socks5://host:port

``socks5h://`` is coerced to ``socks5://``. ``socks4://`` is rejected (Manager
cannot speak SOCKS4; renaming the scheme does not make it work).
"""
from __future__ import annotations

import json
import os
import random
import re
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple
from urllib.parse import quote, urlparse

# Schemes we accept on input. socks4 is listed so we can reject it explicitly
# rather than silently treating it as http.
SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")
_MANAGER_SCHEMES = ("http", "https", "socks5")
# Hostname-resolving socks5h → Manager/Chrome socks5 (remote DNS is fine).
_MANAGER_SCHEME_MAP = {
    "socks5h": "socks5",
}

_SCHEME_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://(?P<rest>.+)$")

class ProxyResolutionError(RuntimeError):
    """A requested proxy could not be safely resolved for profile creation."""




def pool_file() -> str:
    from plugins.browser.cloak.paths import proxy_pool_file

    return str(proxy_pool_file())


def pool_enabled() -> bool:
    """Auto-assign-from-pool toggle (``CLOAK_USE_PROXY_POOL`` truthy)."""
    return os.environ.get("CLOAK_USE_PROXY_POOL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ---------------------------------------------------------------------------
# Parsing / normalisation
# ---------------------------------------------------------------------------
def normalize_proxy(raw: str, default_scheme: str = "http") -> Optional[str]:
    """Return a canonical Manager-safe URL or ``None``.

    Rejects ``socks4://`` (Manager does not support SOCKS4). Coerces
    ``socks5h://`` → ``socks5://``.
    """
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None

    scheme = (default_scheme or "http").lower()
    rest = line
    m = _SCHEME_RE.match(line)
    has_explicit_scheme = bool(m)
    if m:
        scheme = m.group("scheme").lower()
        rest = m.group("rest")
    if scheme == "socks4":
        return None
    if scheme not in SUPPORTED_SCHEMES:
        if has_explicit_scheme:
            return None
        scheme = (default_scheme or "http").lower()
        if scheme not in SUPPORTED_SCHEMES or scheme == "socks4":
            scheme = "http"
    scheme = _MANAGER_SCHEME_MAP.get(scheme, scheme)
    if scheme not in _MANAGER_SCHEMES:
        return None

    user: Optional[str] = None
    pwd: Optional[str] = None
    host: Optional[str] = None
    port: Optional[str] = None

    if "@" in rest:
        cred, hostpart = rest.rsplit("@", 1)
        if ":" in cred:
            user, pwd = cred.split(":", 1)
        else:
            user = cred
        hp = hostpart.split(":")
        if len(hp) >= 2:
            host, port = hp[0], hp[1]
    else:
        parts = rest.split(":")
        if len(parts) == 2:
            host, port = parts[0], parts[1]
        elif len(parts) == 4:
            # Disambiguate host:port:user:pass vs user:pass:host:port by which
            # slot holds the numeric port.
            if parts[1].isdigit() and not parts[3].isdigit():
                host, port, user, pwd = parts
            elif parts[3].isdigit() and not parts[1].isdigit():
                user, pwd, host, port = parts
            else:
                # Both/neither numeric — default to the most common provider
                # layout host:port:user:pass.
                host, port, user, pwd = parts
        else:
            return None

    if not host or not port or not str(port).isdigit():
        return None

    auth = ""
    if user:
        if pwd is not None and pwd != "":
            auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@"
        else:
            auth = f"{quote(user, safe='')}@"
    return f"{scheme}://{auth}{host}:{port}"


def parse_lines(text: str, default_scheme: str = "http") -> Tuple[List[str], List[str]]:
    """Parse a multi-line blob. Returns ``(normalised_unique, invalid_lines)``."""
    ok: List[str] = []
    bad: List[str] = []
    seen = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        norm = normalize_proxy(stripped, default_scheme)
        if not norm:
            bad.append(stripped)
            continue
        if norm in seen:
            continue
        seen.add(norm)
        ok.append(norm)
    return ok, bad


def mask_proxy(entry: Any) -> str:
    """Hide the password for display: ``http://user:****@host:port``."""
    try:
        url = _entry_url(entry)
        if not url:
            return "<invalid proxy>"
        p = urlparse(url)
        if not p.scheme or not p.hostname:
            return "<invalid proxy>"
        netloc = p.hostname
        if p.port:
            netloc = f"{netloc}:{p.port}"
        return f"{p.scheme}://{netloc}"
    except Exception:  # noqa: BLE001
        return "<invalid proxy>"


# ---------------------------------------------------------------------------
# Pool persistence + selection
# ---------------------------------------------------------------------------
def _entry_url(entry: Any) -> Optional[str]:
    """Extract a normalized proxy URL from a pool entry (str or skill object)."""
    if isinstance(entry, str):
        return normalize_proxy(entry)
    if isinstance(entry, dict):
        raw = entry.get("url") or entry.get("proxy") or ""
        return normalize_proxy(str(raw))
    return None




def proxy_url(entry: Any) -> str:
    """Return a normalized pool URL, or an empty value for invalid input."""
    return _entry_url(entry) or ""


def profile_claim_owner(profile_name: str) -> str:
    """Return the durable pool owner used before Manager has a profile ID."""
    name = str(profile_name or "").strip()
    if not name:
        raise ProxyResolutionError("A profile name is required to reserve a proxy from the pool.")
    return f"profile:{name}"

def _claim_owner_aliases(owner: str) -> set[str]:
    """Return canonical and legacy spellings for a profile claim owner."""
    value = str(owner or "").strip()
    if not value:
        return set()
    if value.startswith("profile:"):
        name = value[len("profile:") :].strip()
        return {value, name} if name else {value}
    return {value}


def _as_skill_entry(entry: Any) -> Optional[dict]:
    url = _entry_url(entry)
    if not url:
        return None
    if isinstance(entry, dict):
        return {
            "url": url,
            "assigned_to": entry.get("assigned_to"),
            "used_at": entry.get("used_at"),
        }
    return {"url": url, "assigned_to": None, "used_at": None}


def _normalize_pool_data(data: Any) -> dict:
    """Accept runtime dict *or* skill array format; always return dict view."""
    if isinstance(data, list):
        entries = []
        for item in data:
            ent = _as_skill_entry(item)
            if ent:
                entries.append(ent)
        return {
            "proxies": entries,
            "cursor": 0,
            "strategy": "round_robin",
            "format": "skill",
        }
    if isinstance(data, dict):
        proxies = data.get("proxies")
        if proxies is None and isinstance(data.get("pool"), list):
            return _normalize_pool_data(data["pool"])
        coerced = []
        for item in proxies or []:
            ent = _as_skill_entry(item)
            if ent:
                coerced.append(ent)
        out = {
            "proxies": coerced,
            "cursor": int(data.get("cursor") or 0),
            "strategy": data.get("strategy") or "round_robin",
            "format": "skill",
        }
        return out
    return {"proxies": [], "cursor": 0, "strategy": "round_robin", "format": "skill"}


def _to_disk(pool: dict) -> list:
    """Canonical on-disk form: skill array of objects."""
    out: list = []
    for item in pool.get("proxies") or []:
        ent = _as_skill_entry(item)
        if ent:
            out.append(ent)
    return out


def load_pool() -> dict:
    path = pool_file()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            return _normalize_pool_data(data)
        except Exception:  # noqa: BLE001
            pass
    return {"proxies": [], "cursor": 0, "strategy": "round_robin", "format": "skill"}


def save_pool(pool: dict) -> None:
    """Persist shared contract: dict with skill-object ``proxies`` list.

    CLI skill accepts both this and a bare array. Writing the dict keeps
    ``strategy`` / ``cursor`` for the dashboard without breaking the skill.
    """
    path = pool_file()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "version": 2,
        "strategy": pool.get("strategy") or "round_robin",
        "cursor": int(pool.get("cursor") or 0),
        "proxies": _to_disk(pool),
    }
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".proxies.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _pool_lock_path() -> str:
    return pool_file() + ".lock"


def _with_pool_lock(fn):
    """Inter-process exclusive lock around pool mutations (Windows + POSIX)."""
    lock_path = _pool_lock_path()
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(lock_fd, 0, os.SEEK_SET)
            try:
                os.write(lock_fd, b"0")
            except OSError:
                pass
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
            try:
                return fn()
            finally:
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def set_proxies(
    proxies: List[str], strategy: str = "round_robin", reset_cursor: bool = True
) -> dict:
    def _do() -> dict:
        entries = []
        seen = set()
        for raw in proxies:
            url = normalize_proxy(raw) if isinstance(raw, str) else _entry_url(raw)
            if not url or url in seen:
                continue
            seen.add(url)
            entries.append({"url": url, "assigned_to": None, "used_at": None})
        pool = {
            "proxies": entries,
            "strategy": strategy if strategy in {"round_robin", "random"} else "round_robin",
            "cursor": 0 if reset_cursor else int(load_pool().get("cursor") or 0),
            "format": "skill",
        }
        save_pool(pool)
        return pool

    return _with_pool_lock(_do)


def add_proxies(proxies: List[str]) -> dict:
    def _do() -> dict:
        pool = load_pool()
        existing = {
            (_entry_url(p) or "")
            for p in (pool.get("proxies") or [])
            if _entry_url(p)
        }
        for raw in proxies:
            url = normalize_proxy(raw) if isinstance(raw, str) else _entry_url(raw)
            if not url or url in existing:
                continue
            pool["proxies"].append({"url": url, "assigned_to": None, "used_at": None})
            existing.add(url)
        pool["format"] = "skill"
        save_pool(pool)
        return pool

    return _with_pool_lock(_do)


def clear_pool() -> dict:
    return set_proxies([], strategy=load_pool().get("strategy", "round_robin"))


def count() -> int:
    return len(load_pool().get("proxies") or [])


def release_proxy(assigned_to: str) -> int:
    """Clear claim marks for ``assigned_to``. Returns number released."""

    def _do() -> int:
        pool = load_pool()
        owner_aliases = _claim_owner_aliases(assigned_to)
        released = 0
        for entry in pool.get("proxies") or []:
            if isinstance(entry, dict) and entry.get("assigned_to") in owner_aliases:
                entry["assigned_to"] = None
                entry["used_at"] = None
                released += 1
        if released:
            save_pool(pool)
        return released

    return _with_pool_lock(_do)


def claim_proxy(claim_as: str) -> Optional[str]:
    """Hand out the next free proxy atomically (file lock + RMW).

    Entries are skill objects ``{url, assigned_to, used_at}``. Claims the first
    unassigned entry. When the pool is exhausted (all assigned), returns
    ``None`` — does **not** recycle busy proxies.
    """

    owner = str(claim_as or "").strip()
    if not owner:
        raise ProxyResolutionError("A profile or task owner is required to claim a proxy.")
    owner_aliases = _claim_owner_aliases(owner)

    def _do() -> Optional[str]:
        pool = load_pool()
        proxies = pool.get("proxies") or []
        if not proxies:
            return None

        for entry in proxies:
            if isinstance(entry, dict) and entry.get("assigned_to") in owner_aliases:
                if entry.get("assigned_to") != owner:
                    entry["assigned_to"] = owner
                    save_pool(pool)
                return _entry_url(entry)

        free = [
            e
            for e in proxies
            if isinstance(e, dict) and e.get("url") and not e.get("assigned_to")
        ]
        if not free:
            return None

        if pool.get("strategy") == "random":
            entry = random.choice(free)
        else:
            # Stable order as stored; cursor only among free slots
            idx = int(pool.get("cursor", 0)) % len(free)
            entry = free[idx]
            pool["cursor"] = (idx + 1) % max(len(free), 1)

        entry["assigned_to"] = owner
        entry["used_at"] = datetime.now(timezone.utc).isoformat()
        save_pool(pool)
        return entry.get("url")

    return _with_pool_lock(_do)



def next_proxy(claim_as: Optional[str] = None) -> Optional[str]:
    """Backward-compatible low-level proxy reservation helper."""
    owner = str(claim_as or "").strip() or f"manual:{uuid.uuid4().hex}"
    return claim_proxy(owner)

def resolve_proxy(
    explicit: str = "",
    use_pool: Optional[bool] = None,
    *,
    claim_as: Optional[str] = None,
    fail_closed: bool = False,
) -> str:
    """Proxy resolution used by profile-creation paths.

    Order: explicit proxy arg > pool (when ``use_pool`` or the global
    ``CLOAK_USE_PROXY_POOL`` toggle is on) > "".

    Explicit / env values always go through ``normalize_proxy`` (socks4
    rejected, socks5h coerced).
    """
    explicit = (explicit or "").strip()
    if explicit:
        normalized = normalize_proxy(explicit)
        if normalized:
            return normalized
        if fail_closed:
            raise ProxyResolutionError("The requested proxy is invalid or unsupported by CloakBrowser.")
        return ""
    want_pool = pool_enabled() if use_pool is None else bool(use_pool)
    if want_pool:
        owner = str(claim_as or "").strip()
        if not owner:
            if fail_closed:
                raise ProxyResolutionError("A profile or task owner is required to reserve a pool proxy.")
            pooled = next_proxy()
        else:
            pooled = claim_proxy(owner)
        if pooled:
            return pooled
        if fail_closed:
            raise ProxyResolutionError("Proxy pool is empty or all proxies are already assigned.")
    return ""
