"""Regression tests for Cloak Edition critical fixes.

Run from repo root:
  python -m pytest tests/cloak -q
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _clean_cloak_env(monkeypatch, tmp_path):
    for key in (
        "BROWSER_CDP_URL",
        "CLOAK_CDP_HTTP_URL",
        "CLOAK_ACTIVE_PROFILE_ID",
        "CLOAK_ACTIVE_PROFILE_NAME",
        "CLOAK_ACTIVE_TASK_ID",
        "CLOAK_MANAGER_URL",
        "CLOAK_AUTH_TOKEN",
        "CLOAK_CDP_PROXY_BASE",
        "CLOAK_ALLOWED_HOSTS",
        "CLOAK_PROXY_POOL_FILE",
        "CLOAK_DIR",
        "CLOAK_ENABLE_GMAIL_FACTORY",
        "CLOAK_MANAGER_ENV",
        "CLOAK_PROXY",
        "CLOAK_USE_PROXY_POOL",
        "CLOAK_REQUIRE_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLOAK_DIR", str(tmp_path / "cloak"))
    monkeypatch.setenv("CLOAK_PROXY_POOL_FILE", str(tmp_path / "cloak" / "proxies.json"))
    # Reset lease table between tests
    from plugins.browser.cloak import session_leases

    with session_leases._lock:
        session_leases._leases.clear()
        session_leases._by_profile.clear()
    yield


def test_create_session_does_not_steal_foreign_cdp(monkeypatch):
    from plugins.browser.cloak import session_leases
    from plugins.browser.cloak.provider import CloakBrowserProvider

    monkeypatch.setenv("CLOAK_MANAGER_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("BROWSER_CDP_URL", "ws://stolen-from-task-a")
    monkeypatch.setenv("CLOAK_ACTIVE_PROFILE_ID", "profile-a")
    monkeypatch.setenv("CLOAK_ACTIVE_TASK_ID", "task-a")

    provider = CloakBrowserProvider()

    # Mock manager path so we don't need a live manager — if isolation works,
    # create_session("task-b") must NOT return the stolen URL and must call launch.
    launched = {}

    def fake_find(base_url, name):
        return None

    def fake_create(base_url, name):
        return {"id": "profile-b", "name": name}

    def fake_launch(base_url, profile_id):
        launched["id"] = profile_id
        return {"cdp_url": f"/api/profiles/{profile_id}/cdp", "already_running": False}

    monkeypatch.setattr(provider, "_find_profile_by_name", fake_find)
    monkeypatch.setattr(provider, "_create_profile", fake_create)
    monkeypatch.setattr(provider, "_launch_profile", fake_launch)
    monkeypatch.setattr(provider, "_resolve_cdp_ws", lambda http: "ws://task-b-only")
    monkeypatch.setattr(provider, "_absolute_cdp_url", lambda base, rel: f"{base}{rel}")

    session = provider.create_session("task-b")
    assert session["cdp_url"] == "ws://task-b-only"
    assert session["bb_session_id"] == "profile-b"
    assert session["cdp_url"] != "ws://stolen-from-task-a"
    lease = session_leases.get("task-b")
    assert lease is not None
    assert lease.cdp_url == "ws://task-b-only"


def test_create_session_adopts_only_matching_task(monkeypatch):
    from plugins.browser.cloak.provider import CloakBrowserProvider

    monkeypatch.setenv("CLOAK_MANAGER_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("BROWSER_CDP_URL", "ws://mine")
    monkeypatch.setenv("CLOAK_ACTIVE_PROFILE_ID", "p1")
    monkeypatch.setenv("CLOAK_ACTIVE_TASK_ID", "task-a")

    provider = CloakBrowserProvider()
    session = provider.create_session("task-a")
    assert session["cdp_url"] == "ws://mine"
    assert session["features"].get("prebound") is True


def test_profile_state_keeps_cdp_and_proxy_only_in_memory(tmp_path, monkeypatch):
    from plugins.browser.cloak._impl import profile_state as state

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    signed_cdp = "ws://cloak.test/devtools/browser/profile?token=signed-secret"
    http_cdp = "http://cloak.test/api/profiles/profile/cdp?token=signed-secret"
    proxy = "http://proxy-user:proxy-secret@proxy.test:8080"

    with state._lock:
        previous_loaded = state._loaded
        previous_bindings = {key: dict(value) for key, value in state._bindings.items()}
        state._loaded = False
        state._bindings = {}
    try:
        remembered = state.remember_profile(
            "state-task",
            profile_id="profile-state",
            profile_name="State profile",
            cdp_url=signed_cdp,
            cdp_http_url=http_cdp,
            proxy=proxy,
            source="test",
        )
        assert remembered["cdp_url"] == signed_cdp
        assert remembered["proxy"] == proxy
        assert state.cdp_url_for_task("state-task") == signed_cdp

        path = tmp_path / "hermes-home" / "cloak" / "session-bindings.json"
        on_disk = path.read_text(encoding="utf-8")
        assert "signed-secret" not in on_disk
        assert "proxy-secret" not in on_disk
        assert '"cdp_url"' not in on_disk
        assert '"cdp_http_url"' not in on_disk
        assert '"proxy"' not in on_disk
        assert "profile-state" in on_disk
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600

        # Simulate a fresh process: metadata survives, but no CDP credential
        # can be recovered from state at rest.
        with state._lock:
            state._loaded = False
            state._bindings = {}
        assert state.cdp_url_for_task("state-task") == ""
        reloaded = state.get_binding("state-task")
        assert reloaded is not None
        assert reloaded["profile_id"] == "profile-state"
        assert reloaded["profile_name"] == "State profile"
        assert "cdp_url" not in reloaded
        assert "cdp_http_url" not in reloaded
        assert "proxy" not in reloaded
    finally:
        for name in (
            "BROWSER_CDP_URL",
            "CLOAK_CDP_HTTP_URL",
            "CLOAK_ACTIVE_PROFILE_ID",
            "CLOAK_ACTIVE_PROFILE_NAME",
            "CLOAK_ACTIVE_TASK_ID",
        ):
            monkeypatch.delenv(name, raising=False)
        with state._lock:
            state._loaded = previous_loaded
            state._bindings = previous_bindings


def test_profile_state_sanitizes_legacy_secret_bindings(tmp_path, monkeypatch):
    import json

    from plugins.browser.cloak._impl import profile_state as state

    home = tmp_path / "hermes-home"
    path = home / "cloak" / "session-bindings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "legacy-task": {
                    "task_id": "legacy-task",
                    "profile_id": "legacy-profile",
                    "profile_name": "Legacy profile",
                    "cdp_url": "ws://cloak.test/a?token=legacy-secret",
                    "cdp_http_url": "http://cloak.test/a?token=legacy-secret",
                    "proxy": "http://legacy:proxy-secret@proxy.test:8080",
                    "unexpected": "remove-me",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    with state._lock:
        previous_loaded = state._loaded
        previous_bindings = {key: dict(value) for key, value in state._bindings.items()}
        state._loaded = False
        state._bindings = {}
    try:
        assert state.cdp_url_for_task("legacy-task") == ""
        binding = state.get_binding("legacy-task")
        assert binding == {
            "task_id": "legacy-task",
            "profile_id": "legacy-profile",
            "profile_name": "Legacy profile",
        }
        sanitized = path.read_text(encoding="utf-8")
        assert "legacy-secret" not in sanitized
        assert "proxy-secret" not in sanitized
        assert "unexpected" not in sanitized
    finally:
        with state._lock:
            state._loaded = previous_loaded
            state._bindings = previous_bindings


def test_proxy_next_is_atomic(tmp_path, monkeypatch):
    from plugins.browser.cloak import proxy_format as pf

    pool_file = tmp_path / "proxies.json"
    monkeypatch.setenv("CLOAK_PROXY_POOL_FILE", str(pool_file))
    pf.set_proxies([f"http://p{i}.example:8080" for i in range(20)])

    got = []
    lock = threading.Lock()

    def worker():
        for _ in range(5):
            url = pf.next_proxy()
            with lock:
                got.append(url)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 20 claims from a 20-proxy round-robin pool across 4 threads → all distinct
    # until wrap; we claimed exactly 20.
    assert len(got) == 20
    assert len(set(got)) == 20


def test_gmail_factory_opt_in(monkeypatch):
    import plugins.browser.cloak._impl as impl

    class Ctx:
        def register_tool(self, **kwargs):
            pass

        def register_hook(self, *a, **k):
            pass

    monkeypatch.setattr(impl, "_register_manage_tools", lambda ctx: None)
    monkeypatch.setattr(impl, "_register_input_overrides", lambda ctx: None)
    monkeypatch.setattr(impl, "_register_hybrid_tools_if_available", lambda ctx: 0)

    called = {"gmail": False}

    def gmail_reg(ctx):
        called["gmail"] = True
        return 4

    monkeypatch.setattr(impl, "_register_gmail_factory_tools", gmail_reg)

    monkeypatch.delenv("CLOAK_ENABLE_GMAIL_FACTORY", raising=False)
    impl.register(Ctx())
    assert called["gmail"] is False

    monkeypatch.setenv("CLOAK_ENABLE_GMAIL_FACTORY", "1")
    called["gmail"] = False
    impl.register(Ctx())
    assert called["gmail"] is True


def test_dashboard_masks_proxy_credentials():
    # Avoid importing FastAPI-heavy dashboard module in minimal envs.
    import ast
    from pathlib import Path

    src = Path("hermes_cli/cloak_dashboard.py").read_text(encoding="utf-8")
    assert "def _mask_url(" in src
    assert '"cdp_proxy_base": _mask_url(proxy_base)' in src

    # Execute just the mask helper
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_mask_url")
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {}
    exec(compile(mod, "<mask>", "exec"), ns)
    assert ns["_mask_url"]("http://user:secret@127.0.0.1:8081/path") == "http://127.0.0.1:8081/path"
    assert ns["_mask_url"]("http://127.0.0.1:8081") == "http://127.0.0.1:8081"



def test_dashboard_requires_protected_api_for_ready_badge():
    from pathlib import Path

    src = Path("hermes_cli/cloak_dashboard.py").read_text(encoding="utf-8")
    assert '"protected_ready": False' in src
    assert 'status["protected_ready"] = True' in src
    assert 'setConn(protectedReady, connectionLabel)' in src
    assert 'protected endpoint returned HTTP {resp.status_code}' in src

def test_tools_input_press_native_signature():
    """browser_press must call native (key, task_id) — not ref=."""
    from plugins.browser.cloak._impl import tools_input as ti
    import tools.browser_tool as bt

    calls = []

    def fake_press(key, task_id=None):
        calls.append((key, task_id))
        return '{"ok":true}'

    with mock.patch.object(bt, "browser_press", fake_press):
        out = ti._native_press("Enter", "task-1")
    assert calls == [("Enter", "task-1")]
    assert "ok" in out


def test_tools_input_no_browser_fill_import():
    import inspect
    from plugins.browser.cloak._impl import tools_input as ti

    src = inspect.getsource(ti.browser_fill)
    assert "browser_fill as native_fill" not in src
    assert "_native_type" not in src
    assert "await loc.fill(text" not in src
    assert "await loc.type(text" in src


def test_install_cloak_sh_does_not_echo_token():
    text = Path("scripts/install_cloak.sh").read_text(encoding="utf-8")
    assert "${FINAL_TOK" not in text
    assert "auth-header.conf" in text
    assert "Bearer token (paste" not in text

def test_install_sh_keeps_manager_token_out_of_argv():
    installer = Path("scripts/install_cloak.sh").read_text(encoding="utf-8")
    probe = Path("scripts/cloak/http_probe.py").read_text(encoding="utf-8")

    assert "Authorization: Bearer ${token}" not in installer
    assert "--bearer-env CLOAK_AUTH_TOKEN" in installer
    assert '"--bearer-env"' in probe


def test_install_cloak_declares_http_readiness_probe():
    installer = Path("scripts/install_cloak.sh").read_text(encoding="utf-8")

    assert 'HTTP_PROBE="$SCRIPT_DIR/cloak/http_probe.py"' in installer
    assert '"$PY" "$HTTP_PROBE"' in installer


def test_nginx_template_uses_placeholder_or_include():
    text = Path("scripts/cloak/nginx/cloak-cdp-proxy.conf.template").read_text(encoding="utf-8")
    assert "cloak_connection_upgrade" in text
    assert "Connection $http_connection" not in text
    assert "__CLOAK_MANAGER_UPSTREAM__" in text


def test_env_file_strips_bom(tmp_path):
    from plugins.browser.cloak.env_file import parse_env_file

    path = tmp_path / "manager.env"
    path.write_bytes(b"\xef\xbb\xbfCLOAK_MANAGER_URL=http://127.0.0.1:8080\nCLOAK_AUTH_TOKEN=abc\n")
    parsed = parse_env_file(str(path))
    assert "CLOAK_MANAGER_URL" in parsed
    assert parsed["CLOAK_MANAGER_URL"] == "http://127.0.0.1:8080"
    assert "\ufeff" not in parsed["CLOAK_MANAGER_URL"]


def test_cdp_bridge_reads_fragmented_headers():
    import asyncio
    from scripts.cloak import cdp_bridge as bridge

    async def _run():
        # Simulate TCP segmentation: headers arrive in small chunks
        full = (
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: x\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        reader = asyncio.StreamReader()
        # feed in 8-byte chunks
        for i in range(0, len(full), 8):
            reader.feed_data(full[i : i + 8])
        reader.feed_eof()
        raw = await bridge._read_http_request_head(reader, timeout=2)
        assert b"\r\n\r\n" in raw
        injected = bridge._inject_auth(raw, "sekrit")
        assert b"Authorization: Bearer sekrit" in injected

    asyncio.run(_run())


def test_get_cdp_override_respects_active_task(monkeypatch):
    import tools.browser_tool as bt

    monkeypatch.setenv("BROWSER_CDP_URL", "ws://owned-by-a")
    monkeypatch.setenv("CLOAK_ACTIVE_TASK_ID", "task-a")
    assert bt._get_cdp_override("task-a") == "ws://owned-by-a"
    assert bt._get_cdp_override("task-b") == ""


def test_get_cdp_override_prefers_lease(monkeypatch):
    import tools.browser_tool as bt
    from plugins.browser.cloak import session_leases

    session_leases.put(
        session_leases.Lease(task_id="task-b", profile_id="pb", cdp_url="ws://lease-b")
    )
    monkeypatch.setenv("BROWSER_CDP_URL", "ws://owned-by-a")
    monkeypatch.setenv("CLOAK_ACTIVE_TASK_ID", "task-a")
    assert bt._get_cdp_override("task-b") == "ws://lease-b"


def test_skill_proxy_pool_array_format(tmp_path, monkeypatch):
    import json
    from plugins.browser.cloak import proxy_format as pf

    path = tmp_path / "proxies.json"
    monkeypatch.setenv("CLOAK_PROXY_POOL_FILE", str(path))
    path.write_text(
        json.dumps(
            [
                {"url": "http://user:pass@1.2.3.4:8080", "assigned_to": None, "used_at": None},
                {"url": "socks5h://u:p@5.6.7.8:1080", "assigned_to": None, "used_at": None},
            ]
        ),
        encoding="utf-8",
    )
    pool = pf.load_pool()
    assert len(pool["proxies"]) == 2
    first = pf.next_proxy()
    assert first == "http://user:pass@1.2.3.4:8080"
    second = pf.next_proxy()
    # socks5h coerced to socks5 for Manager
    assert second == "socks5://u:p@5.6.7.8:1080"
    # Exhausted — do not recycle claimed proxies
    assert pf.next_proxy() is None


def test_socks4_rejected_socks5h_coerced():
    from plugins.browser.cloak.proxy_format import normalize_proxy

    assert normalize_proxy("socks4://1.2.3.4:1080") is None
    assert normalize_proxy("socks5h://1.2.3.4:1080") == "socks5://1.2.3.4:1080"


def test_ws_probe_rejects_http_404():
    import asyncio
    from scripts.cloak import ws_probe

    class FakeReader:
        async def read(self, n):
            return b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

    class FakeWriter:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_open(host, port):
        return FakeReader(), FakeWriter()

    async def _run2():
        real_import = __import__

        def fake_import(name, *a, **k):
            if name == "websockets":
                raise ImportError("nope")
            return real_import(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with mock.patch("asyncio.open_connection", fake_open):
                return await ws_probe._probe("ws://127.0.0.1:9", 1)

    assert asyncio.run(_run2()) == 1


def test_install_sh_recreates_on_regenerate_token():
    text = Path("scripts/install_cloak.sh").read_text(encoding="utf-8")
    assert "Token regenerated" in text
    assert "docker rm -f" in text
    # Readiness is HTTP /api/status through proxy — not WS on /
    assert "http://127.0.0.1:8081/api/profiles" in text
    assert 'ws_probe.py" --url "ws://127.0.0.1:8081"' not in text


def test_browser_pool_is_per_loop():
    import asyncio
    from plugins.browser.cloak._impl import browser_pool as bp

    async def one():
        return bp.get_pool()

    p1 = asyncio.run(one())
    p2 = asyncio.run(one())
    # Different event loops → different pool instances
    assert p1 is not p2


def test_runtime_save_readable_by_skill_cli(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys
    from plugins.browser.cloak import proxy_format as pf

    path = tmp_path / "proxies.json"
    monkeypatch.setenv("CLOAK_PROXY_POOL_FILE", str(path))
    pf.set_proxies(["http://user:secret@1.2.3.4:8080", "socks5://5.6.7.8:1080"])
    # Disk must be skill-readable (dict with proxies[] of objects, or array)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(on_disk, dict)
    assert isinstance(on_disk["proxies"], list)
    assert on_disk["proxies"][0]["url"].startswith("http://")

    skill = Path("skills/cloak-proxy-pool/pool.py")
    env = os.environ.copy()
    env["CLOAK_PROXY_POOL_FILE"] = str(path)
    out = subprocess.check_output(
        [sys.executable, str(skill), "status"], env=env, text=True
    )
    status = json.loads(out)
    assert status["total"] == 2
    assert status["free"] == 2

    listed = subprocess.check_output(
        [sys.executable, str(skill), "list"], env=env, text=True
    )
    assert "secret" not in listed
    assert "****" in listed or "user" in listed


def test_add_proxies_handles_skill_objects(tmp_path, monkeypatch):
    import json
    from plugins.browser.cloak import proxy_format as pf

    path = tmp_path / "proxies.json"
    monkeypatch.setenv("CLOAK_PROXY_POOL_FILE", str(path))
    path.write_text(
        json.dumps([{"url": "http://a:1@1.1.1.1:8080", "assigned_to": None, "used_at": None}]),
        encoding="utf-8",
    )
    # Must not raise unhashable type: dict
    pool = pf.add_proxies(["http://b:2@2.2.2.2:8080", "http://a:1@1.1.1.1:8080"])
    assert len(pool["proxies"]) == 2


def test_resolve_proxy_normalizes_explicit(monkeypatch):
    from plugins.browser.cloak.proxy_format import resolve_proxy

    assert resolve_proxy("socks5h://h:1") == "socks5://h:1"
    assert resolve_proxy("socks4://h:1") == ""


def test_install_ps1_overwrites_process_env_and_stops_bridge():
    text = Path("scripts/install_cloak.ps1").read_text(encoding="utf-8")
    assert "Stop-CdpBridge" in text
    assert "Set-Item -Path" in text
    assert "http_probe.py" in text
    assert "--token" not in text or "# Token via env" in text
    # Must not skip file values when Process env already set
    assert "-not [Environment]::GetEnvironmentVariable" not in text
    assert "/api/profiles" in text
    assert "cdp_bridge.pid" in text


def test_ensure_cdp_supervisor_uses_task_id():
    import inspect
    import tools.browser_tool as bt

    src = inspect.getsource(bt._ensure_cdp_supervisor)
    assert "_get_cdp_override(task_id)" in src
    assert "_get_cdp_override()" not in src


def test_http_probe_requires_200(monkeypatch):
    from scripts.cloak import http_probe
    from io import BytesIO

    class FakeResp(BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        http_probe.urllib.request,
        "urlopen",
        lambda *a, **k: FakeResp(b"ok"),
    )
    assert http_probe.probe("http://x/api/profiles", 1) == 0

    def boom(*a, **k):
        err = http_probe.urllib.error.HTTPError("http://x", 401, "no", hdrs=None, fp=None)
        raise err

    monkeypatch.setattr(http_probe.urllib.request, "urlopen", boom)
    assert http_probe.probe("http://x/api/profiles", 1) == 1


def test_cdp_bridge_rewrites_host_for_remote_upstream():
    from scripts.cloak import cdp_bridge as bridge

    raw = (
        b"GET /devtools/browser/abc HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8080\r\n"
        b"Authorization: Bearer old-token\r\n\r\n"
    )
    injected = bridge._inject_auth(
        raw,
        "new-token",
        upstream_host_header="manager.example:8443",
    )
    assert b"Host: manager.example:8443" in injected
    assert b"Authorization: Bearer new-token" in injected
    assert b"old-token" not in injected


def test_browser_pool_drop_marks_foreign_cache_stale():
    import asyncio
    from plugins.browser.cloak._impl import browser_pool as bp

    cdp_url = "ws://cloak.test/devtools/browser/a"
    owner = bp.BrowserPool()
    foreign = bp.BrowserPool()
    sentinel = object()
    foreign._clients[cdp_url] = sentinel

    with bp._pools_guard:
        original_pools = dict(bp._pools)
        bp._pools.clear()
        bp._pools.update({1: owner, 2: foreign})
    try:
        result = asyncio.run(owner.drop(cdp_url))
        assert result.local_reset is False
        assert result.foreign_cleanup_scheduled == 0
        assert result.foreign_cleanup_pending == 1
        # A foreign loop owns this client. It must not be popped or closed here.
        assert foreign._clients[cdp_url] is sentinel
        assert foreign._consume_stale(cdp_url) is True
    finally:
        with bp._pools_guard:
            bp._pools.clear()
            bp._pools.update(original_pools)


def test_cloak_navigation_holds_pool_action(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from plugins.browser.cloak._impl import browser_pool as bp
    from plugins.browser.cloak._impl import tools_browser as tb

    events = []

    class Page:
        async def goto(self, url, **kwargs):
            events.append(("goto", url))

    class Pool:
        @asynccontextmanager
        async def hold(self, cdp_url, preset="default"):
            events.append(("enter", cdp_url, preset))
            try:
                yield SimpleNamespace(page=Page())
            finally:
                events.append(("exit", cdp_url))

        async def get(self, *args, **kwargs):
            raise AssertionError("navigation must hold the action lock, not call get()")

    async def fake_meta(page):
        events.append(("meta",))
        return {"current_url": "https://example.test"}

    monkeypatch.setattr(bp, "get_pool", lambda: Pool())
    monkeypatch.setattr(tb, "_page_meta", fake_meta)
    monkeypatch.setattr(tb, "_nav_result", lambda *args, **kwargs: "ok")
    monkeypatch.setenv("CLOAK_POST_NAV_SETTLE_MS", "0")
    monkeypatch.setenv("CLOAK_NAV_ATTEMPTS", "1")

    assert asyncio.run(
        tb._navigate_via_cloak_inner("https://example.test", "ws://cloak.test/a")
    ) == "ok"
    assert [event[0] for event in events] == ["enter", "goto", "meta", "exit"]


def test_raw_cdp_serializes_only_matching_cloak_lease(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from plugins.browser.cloak import session_leases
    from plugins.browser.cloak._impl import browser_pool as bp
    from tools import browser_cdp_tool as cdp

    endpoint = "ws://cloak.test/devtools/browser/a?token=secret"
    session_leases.put(
        session_leases.Lease(task_id="task-a", profile_id="profile-a", cdp_url=endpoint)
    )
    events = []

    @asynccontextmanager
    async def fake_gate(cdp_url):
        events.append(("enter", cdp_url))
        try:
            yield
        finally:
            events.append(("exit", cdp_url))

    async def fake_call(ws_url, method, params, target_id, timeout):
        events.append(("call", ws_url, method))
        return {"ok": True}

    monkeypatch.setattr(bp, "hold_cdp_action", fake_gate)
    monkeypatch.setattr(cdp, "_cdp_call", fake_call)

    assert asyncio.run(
        cdp._cdp_call_serialized_for_cloak(
            endpoint, "Target.getTargets", {}, None, 1, "task-a"
        )
    ) == {"ok": True}
    assert [event[0] for event in events] == ["enter", "call", "exit"]

    events.clear()
    assert asyncio.run(
        cdp._cdp_call_serialized_for_cloak(
            endpoint, "Target.getTargets", {}, None, 1, "task-b"
        )
    ) == {"ok": True}
    assert [event[0] for event in events] == ["call"]


def test_captcha_detection_holds_pool_and_redacts_cdp_errors(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from plugins.browser.cloak._impl import tools_manage as tm

    events = []

    class Pool:
        @asynccontextmanager
        async def hold(self, cdp_url, preset="default"):
            events.append(("enter", cdp_url, preset))
            try:
                yield SimpleNamespace(page=object())
            finally:
                events.append(("exit", cdp_url))

    async def fake_detect(page):
        raise RuntimeError("CDP connect failed: ws://cloak.test/a?token=secret")

    monkeypatch.setattr(tm, "get_pool", lambda: Pool())
    monkeypatch.setattr(tm.profile_state, "cdp_url_for_task", lambda _task_id: "ws://cloak.test/a?token=secret")
    monkeypatch.setattr(tm, "detect_in_playwright_page", fake_detect)

    result = asyncio.run(tm.cloak_detect_captcha(task_id="task-a"))
    assert "secret" not in str(result)
    assert "token=***" in str(result)
    assert [event[0] for event in events] == ["enter", "exit"]


def test_proxy_claim_migrates_legacy_profile_owner(tmp_path, monkeypatch):
    import json
    from plugins.browser.cloak import proxy_format as pf

    path = tmp_path / "proxies.json"
    monkeypatch.setenv("CLOAK_PROXY_POOL_FILE", str(path))
    raw_proxy = "http://user:secret@1.2.3.4:8080"
    path.write_text(
        json.dumps(
            {
                "proxies": [
                    {"url": raw_proxy, "assigned_to": "demo", "used_at": None}
                ]
            }
        ),
        encoding="utf-8",
    )

    assert pf.claim_proxy("profile:demo") == raw_proxy
    pool = pf.load_pool()
    assert pool["proxies"][0]["assigned_to"] == "profile:demo"
    assert pf.release_proxy("profile:demo") == 1
    assert pf.mask_proxy(raw_proxy) == "http://1.2.3.4:8080"
    with pytest.raises(pf.ProxyResolutionError):
        pf.resolve_proxy("ftp://1.2.3.4:21", fail_closed=True)


def test_frame_cdp_serializes_matching_cloak_lease(monkeypatch):
    import asyncio
    import inspect
    from contextlib import asynccontextmanager

    from plugins.browser.cloak import session_leases
    from plugins.browser.cloak._impl import browser_pool as bp
    from tools import browser_cdp_tool as cdp

    endpoint = "ws://cloak.test/devtools/browser/frame?token=secret"
    session_leases.put(
        session_leases.Lease(
            task_id="frame-task", profile_id="frame-profile", cdp_url=endpoint
        )
    )
    events = []

    @asynccontextmanager
    async def fake_gate(cdp_url):
        events.append(("enter", cdp_url))
        try:
            yield
        finally:
            events.append(("exit", cdp_url))

    def fake_supervisor(task_id, frame_id, method, params, timeout):
        events.append(("call", task_id, frame_id, method))
        return "ok"

    monkeypatch.setattr(bp, "hold_cdp_action", fake_gate)
    monkeypatch.setattr(cdp, "_browser_cdp_via_supervisor", fake_supervisor)

    assert asyncio.run(
        cdp._browser_cdp_via_supervisor_serialized(
            "frame-task", "frame-1", "Runtime.evaluate", {}, 1
        )
    ) == "ok"
    assert [event[0] for event in events] == ["enter", "call", "exit"]

    events.clear()
    assert asyncio.run(
        cdp._browser_cdp_via_supervisor_serialized(
            "other-task", "frame-1", "Runtime.evaluate", {}, 1
        )
    ) == "ok"
    assert [event[0] for event in events] == ["call"]
    assert "_browser_cdp_via_supervisor_serialized" in inspect.getsource(cdp.browser_cdp)


def test_remote_manager_installers_publish_only_live_bridge_and_allowlist():
    sh = Path("scripts/install_cloak.sh").read_text(encoding="utf-8")
    ps1 = Path("scripts/install_cloak.ps1").read_text(encoding="utf-8")

    assert "ensure_manager_host_allowed" in sh
    assert 'start_external_cdp_bridge "$manager_url" "$token"' in sh
    assert "CLOAK_ALLOWED_HOSTS" in sh
    assert "clear_cdp_proxy_base" in sh
    assert "disable_local_nginx_cdp_proxy" in sh

    assert "Ensure-ManagerAllowedHost $managerUri" in ps1
    assert "CLOAK_ALLOWED_HOSTS" in ps1
    assert "Clear-CdpProxyBase" in ps1
    assert "Publish-CdpProxyBase" in ps1
    assert "Skipping CDP bridge (-NoBridge); CLOAK_CDP_PROXY_BASE cleared" in ps1


def test_installers_fail_closed_on_protected_readiness_failure():
    sh = Path("scripts/install_cloak.sh").read_text(encoding="utf-8")
    ps1 = Path("scripts/install_cloak.ps1").read_text(encoding="utf-8")

    # A failed protected Manager/bridge probe must not end in a false ready state.
    assert 'start_external_cdp_bridge "$manager_url" "$token" || true' not in sh

    assert "must not contain credentials, a query, or a fragment" in sh
    assert "must not contain credentials, a query, or a fragment" in ps1

    assert "refusing a successful provision result" in sh
    assert "CLOAK_MANAGER_PROVISION_OK=1" in sh
    assert "no ready release state was published" in sh
    assert "CloakBrowser-Manager did not pass protected readiness" in sh
    non_root_block = sh.split("if ! is_root; then", 1)[1].split("  fi", 1)[0]
    assert "return 1" in non_root_block

    assert "$cloakProvisionFailed = $true" in ps1
    assert "Cloak did not pass protected Manager/CDP bridge readiness" in ps1

def test_browser_tool_redacts_cdp_diagnostics_at_public_boundary():
    from tools import browser_tool

    secret = "ws://alice:supersecret@127.0.0.1:9222/devtools/browser/a?token=signed-secret"
    redacted = browser_tool._redact_browser_diagnostics(
        {"error": secret, "nested": [secret]}
    )
    rendered = repr(redacted)
    assert "supersecret" not in rendered
    assert "signed-secret" not in rendered
    assert "alice:" not in rendered


def test_gmail_factory_redacts_proxy_and_cdp_credentials():
    from plugins.browser.cloak.vendor.gmail_factory.core.redaction import (
        redact_proxy_for_log,
        redact_runtime_value,
    )

    cdp = "ws://alice:supersecret@127.0.0.1:9222/devtools/browser/a?token=signed-secret"
    proxy = "1.2.3.4:1080:alice:supersecret"

    rendered = f"{redact_runtime_value(cdp)} {redact_proxy_for_log(proxy)}"
    assert "alice" not in rendered
    assert "supersecret" not in rendered
    assert "signed-secret" not in rendered
    assert "1.2.3.4:1080:***:***" in rendered


def test_cdp_bridge_defaults_https_upstream_to_443():
    from scripts.cloak import cdp_bridge

    host, port, tls_context, host_header = cdp_bridge._upstream_target(
        "https://manager.example"
    )
    assert host == "manager.example"
    assert port == 443
    assert tls_context is not None
    assert host_header == "manager.example"


def test_provider_requires_bridge_for_authenticated_cdp(monkeypatch):
    from plugins.browser.cloak.provider import CloakBrowserProvider

    monkeypatch.setenv("CLOAK_AUTH_TOKEN", "manager-secret")
    provider = CloakBrowserProvider()
    with pytest.raises(RuntimeError, match="CLOAK_CDP_PROXY_BASE"):
        provider._resolve_cdp_ws("https://manager.example/api/profiles/p/cdp")


def test_manager_client_requires_bridge_for_authenticated_cdp():
    import asyncio

    from plugins.browser.cloak._impl.manager_client import ManagerClient

    async def run() -> None:
        client = ManagerClient(auth_token="manager-secret")
        try:
            with pytest.raises(RuntimeError, match="CLOAK_CDP_PROXY_BASE"):
                await client.bind_browser_cdp_env("/api/profiles/p/cdp")
        finally:
            await client.aclose()

    asyncio.run(run())


def test_bridge_readiness_targets_profile_cdp_path():
    from scripts.cloak import bridge_readiness

    assert bridge_readiness._bridge_ws_url(
        "http://127.0.0.1:8081",
        "https://manager.example/api/profiles/p/cdp?token=manager-secret",
    ) == "ws://127.0.0.1:8081/api/profiles/p/cdp?token=manager-secret"


def test_dashboard_requires_bridge_for_authenticated_cdp(monkeypatch):
    import sys
    import types

    class Router:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: lambda function: function

    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = Router
    fastapi.Body = lambda *_args, **_kwargs: None
    fastapi.Request = object
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = object
    responses.JSONResponse = object
    fastapi.responses = responses
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)

    from hermes_cli import cloak_dashboard

    monkeypatch.setenv("CLOAK_MANAGER_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("CLOAK_AUTH_TOKEN", "manager-secret")

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return []

    def fake_get(*_args, **_kwargs):
        return Response()

    monkeypatch.setattr(cloak_dashboard.requests, "get", fake_get)
    status = cloak_dashboard._collect_status()
    assert status["protected_ready"] is False
    assert status["cdp_bridge_reachable"] is False
    assert status["protected_error"] == "authenticated CDP bridge is not configured"


def test_browser_public_output_and_cdp_guards_redact_endpoint_credentials():
    from tools import browser_cdp_tool, browser_tool

    secret = "ws://alice:supersecret@127.0.0.1:9222/devtools/browser/a?token=signed-secret"

def test_bridge_readiness_cleans_temporary_profile(monkeypatch):
    from scripts.cloak import bridge_readiness

    calls = []

    def fake_request(manager_url, method, path, timeout, payload=None):
        calls.append((method, path, payload))
        if method == "POST" and path == "/api/profiles":
            return {"id": "probe-profile"}
        if method == "POST" and path.endswith("/launch"):
            return {"cdp_url": "/api/profiles/probe-profile/cdp"}
        return {"ok": True}

    async def fake_ws_probe(_url, _timeout):
        return 0

    monkeypatch.setenv("CLOAK_AUTH_TOKEN", "manager-secret")
    monkeypatch.setattr(bridge_readiness, "_request", fake_request)
    monkeypatch.setattr(bridge_readiness.ws_probe, "_probe", fake_ws_probe)

    assert bridge_readiness.probe(
        "http://manager.example",
        "http://127.0.0.1:8081",
        timeout=1,
        retries=1,
        retry_delay=0,
    ) == 0
    assert ("POST", "/api/profiles/probe-profile/stop", None) in calls
    assert ("DELETE", "/api/profiles/probe-profile", None) in calls



def test_pool_toggle_is_read_live_from_manager_env(tmp_path, monkeypatch):
    """The dashboard writes manager.env from a *different* process than the
    gateway that runs the tools. A value merged into os.environ at plugin-import
    time is therefore stale the moment the operator flips the toggle."""
    from plugins.browser.cloak import proxy_format as pf

    env_file = tmp_path / "live" / "manager.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLOAK_MANAGER_ENV", str(env_file))

    # Process env says off (what the gateway booted with).
    monkeypatch.setenv("CLOAK_USE_PROXY_POOL", "0")
    env_file.write_text("CLOAK_USE_PROXY_POOL=1\n", encoding="utf-8")
    assert pf.pool_enabled() is True

    # ...and back off again, still without a restart.
    env_file.write_text("CLOAK_USE_PROXY_POOL=0\n# turned off in the UI\n", encoding="utf-8")
    assert pf.pool_enabled() is False


def test_process_env_used_when_manager_env_is_silent(tmp_path, monkeypatch):
    from plugins.browser.cloak import proxy_format as pf

    env_file = tmp_path / "quiet" / "manager.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("CLOAK_IDLE_TIMEOUT_MIN=20\n", encoding="utf-8")
    monkeypatch.setenv("CLOAK_MANAGER_ENV", str(env_file))
    monkeypatch.setenv("CLOAK_USE_PROXY_POOL", "1")

    assert pf.pool_enabled() is True


def test_require_proxy_refuses_direct_egress(monkeypatch):
    from plugins.browser.cloak.proxy_format import ProxyResolutionError, resolve_proxy

    monkeypatch.setenv("CLOAK_REQUIRE_PROXY", "1")
    with pytest.raises(ProxyResolutionError):
        resolve_proxy("", use_pool=False, fail_closed=True)

    # Explicit proxy still satisfies the requirement.
    assert resolve_proxy("socks5://h:1", use_pool=False) == "socks5://h:1"


def test_create_profile_rejects_empty_name(monkeypatch):
    """Hermes does not validate tool args against the schema, so a model that
    emits `{}` must be stopped here — an unnamed profile is unfindable, so every
    retry creates another one and none of them can claim a pool proxy."""
    import asyncio
    from plugins.browser.cloak._impl import tools_manage as tm

    def explode(*args, **kwargs):
        raise AssertionError("manager must not be called without a profile name")

    monkeypatch.setattr(tm, "ManagerClient", explode)

    result = asyncio.run(tm.cloak_create_profile({}))
    assert result["code"] == "missing_name"

    result = asyncio.run(tm.cloak_set_active({"profile": "   "}))
    assert result["code"] == "missing_name"


def test_provider_backfills_proxy_on_existing_profile(monkeypatch):
    """find-or-create only set the proxy on the create branch, so a profile made
    while the pool was off stayed proxy-less for every later session."""
    from plugins.browser.cloak.provider import CloakBrowserProvider

    provider = CloakBrowserProvider()
    monkeypatch.setattr(
        provider, "_resolve_profile_proxy", lambda name: ("socks5://pool:1080", "profile:p")
    )
    updated = {}

    class Resp:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"id": "p1", "name": "p", "proxy": "socks5://pool:1080"}

    def fake_put(url, **kwargs):
        updated["url"] = url
        updated["json"] = kwargs.get("json")
        return Resp()

    monkeypatch.setattr("plugins.browser.cloak.provider.requests.put", fake_put)

    out = provider._adopt_existing_profile(
        "http://127.0.0.1:8080", {"id": "p1", "name": "p", "proxy": None}
    )
    assert out["proxy"] == "socks5://pool:1080"
    assert updated["url"].endswith("/api/profiles/p1")
    assert updated["json"] == {"proxy": "socks5://pool:1080"}


def test_provider_keeps_existing_profile_proxy(monkeypatch):
    from plugins.browser.cloak.provider import CloakBrowserProvider

    provider = CloakBrowserProvider()

    def explode(*args, **kwargs):
        raise AssertionError("a profile that already has a proxy must not be touched")

    monkeypatch.setattr(provider, "_resolve_profile_proxy", explode)
    monkeypatch.setattr("plugins.browser.cloak.provider.requests.put", explode)

    profile = {"id": "p1", "name": "p", "proxy": "socks5://kept:1080"}
    assert provider._adopt_existing_profile("http://127.0.0.1:8080", profile) is profile


def test_proxy_pool_skill_has_no_hardcoded_home(tmp_path):
    """The doc used to hardcode /root/.hermes/...; in the Docker image the agent
    runs as a non-root user whose Hermes home is the data dir, so it got EACCES
    and burned turns instead of loading the pool."""
    text = Path("skills/cloak-proxy-pool/SKILL.md").read_text(encoding="utf-8")
    assert "/root/.hermes/skills" not in text
    assert "cloak_proxy_pool(action=" in text


def test_registered_cloak_tools_expose_their_arguments():
    """The registry publishes {"type":"function","function":{**schema,"name":...}},
    so `schema` must be the function object. Handing it a bare parameters object
    left every cloak_* tool with `parameters: {}` — the model could only ever
    call them with `{}`."""
    from tools.schema_sanitizer import sanitize_tool_schemas
    from plugins.browser.cloak._impl import as_function_schema, tools_manage

    recorded: dict = {}

    class Ctx:
        def register_tool(self, *, name, schema, **kwargs):
            recorded[name] = schema

    from plugins.browser.cloak._impl import register_tool

    register_tool(
        Ctx(),
        name="cloak_create_profile",
        toolset="cloak",
        schema=tools_manage.SCHEMA_CREATE,
        handler=lambda args, **kw: "",
        description="Create a stealth browser profile.",
    )

    published = sanitize_tool_schemas(
        [{"type": "function", "function": {**recorded["cloak_create_profile"], "name": "cloak_create_profile"}}]
    )
    params = published[0]["function"]["parameters"]
    assert "name" in params["properties"], params
    assert "proxy" in params["properties"]
    assert "use_pool" in params["properties"]
    assert params["required"] == ["name"]

    # Already function-shaped schemas pass through untouched.
    fn_shaped = {"description": "d", "parameters": {"type": "object", "properties": {"a": {}}}}
    assert as_function_schema(fn_shaped) is fn_shaped


def test_every_cloak_registration_is_function_shaped():
    """Guard the whole plugin, not just one tool: no registration may reach the
    registry with a bare parameters object again."""
    from plugins.browser.cloak import _impl

    seen: dict = {}

    class Ctx:
        def register_tool(self, *, name, schema, **kwargs):
            seen[name] = schema

        def register_hook(self, *args, **kwargs):
            pass

    _impl._register_manage_tools(Ctx())
    _impl._register_input_overrides(Ctx())

    assert seen, "no tools registered"
    for name, schema in seen.items():
        assert isinstance(schema.get("parameters"), dict), f"{name} is not function-shaped"
        assert schema["parameters"].get("type") == "object", name


def test_blank_profile_never_matches_a_nameless_profile(monkeypatch):
    """Manager stores profiles with an empty name; a blank lookup used to match
    one of those leftovers, so cloak_stop/cloak_launch hit a stranger's profile."""
    import asyncio
    from plugins.browser.cloak._impl.manager_client import ManagerClient

    mgr = ManagerClient(base_url="http://127.0.0.1:8080", auth_token=None)

    async def fake_list():
        return [{"id": "nameless", "name": ""}, {"id": "real", "name": "reg-1"}]

    monkeypatch.setattr(mgr, "list_profiles", fake_list)
    try:
        assert asyncio.run(mgr.find_profile_by_name("")) is None
        assert asyncio.run(mgr.find_profile_by_name("   ")) is None
        assert asyncio.run(mgr.find_profile_by_name("reg-1"))["id"] == "real"
    finally:
        asyncio.run(mgr.aclose())


def test_resolve_profile_id_falls_back_to_task_binding(monkeypatch):
    import asyncio
    from plugins.browser.cloak._impl import profile_state
    from plugins.browser.cloak._impl import tools_manage as tm

    monkeypatch.setattr(
        profile_state, "get_binding", lambda task_id=None: {"profile_id": "bound-id"}
    )

    class Mgr:
        async def find_profile_by_name(self, name):
            raise AssertionError("blank profile must resolve from the binding, not by name")

    assert asyncio.run(tm._resolve_profile_id(Mgr(), "", task_id="t1")) == "bound-id"

    monkeypatch.setattr(profile_state, "get_binding", lambda task_id=None: None)
    with pytest.raises(tm.ManagerError):
        asyncio.run(tm._resolve_profile_id(Mgr(), "", task_id="t1"))


def test_stop_is_idempotent_when_profile_is_not_running():
    from plugins.browser.cloak._impl.manager_client import ManagerError
    from plugins.browser.cloak._impl.tools_manage import _is_already_stopped

    assert _is_already_stopped(ManagerError(404, '{"detail":"Profile is not running"}', "stop failed"))
    assert _is_already_stopped(ManagerError(409, "profile is NOT RUNNING", "stop failed"))
    assert not _is_already_stopped(ManagerError(404, '{"detail":"Profile not found"}', "stop failed"))
    assert not _is_already_stopped(ManagerError(500, "boom", "stop failed"))


def test_bare_snapshot_ref_is_not_treated_as_a_css_selector():
    """browser_snapshot emits bare refs (`e5`); the locator mapping only knew
    `@e5`. A bare ref used to become `page.locator("e5")` — a CSS lookup for a
    nonexistent <e5> tag that burned the full timeout twice before failing."""
    from plugins.browser.cloak._impl.tools_input import _normalize_target

    assert _normalize_target("e5") == "@e5"
    assert _normalize_target("@e5") == "@e5"
    assert _normalize_target("E12") == "@e12"
    # Real selectors must survive untouched, whichever slot they arrive in.
    assert _normalize_target("", "input[type='email']") == "input[type='email']"
    assert _normalize_target("div.card") == "div.card"
    assert _normalize_target("", "") == ""


def test_snapshot_ref_maps_onto_playwright_aria_ref():
    from plugins.browser.cloak._impl.tools_input import _locator_for, _normalize_target

    seen = []

    class Page:
        def locator(self, sel):
            seen.append(sel)
            return sel

    _locator_for(Page(), _normalize_target("e9"))
    assert seen == ["aria-ref=e9"]


def _stub_text_input_page(monkeypatch, *, active_selector, record, prefocused=False):
    """Wire tools_input onto a fake page whose focus follows the native click."""
    from contextlib import asynccontextmanager
    from plugins.browser.cloak._impl import tools_input as ti

    focused = {"selector": active_selector if prefocused else None}

    class Locator:
        def __init__(self, sel):
            self.sel = sel

        async def type(self, text, timeout=None):
            record["typed"] = (self.sel, text)

        async def input_value(self, timeout=None):
            return ""

    class Page:
        def locator(self, sel):
            return Locator(sel)

        async def evaluate(self, js):
            return focused["selector"]

    @asynccontextmanager
    async def fake_hold(task_id=None):
        yield Page()

    def fake_native_click(ref, task_id):
        record["clicked"] = ref
        focused["selector"] = active_selector
        return '{"success": true, "clicked": "%s"}' % ref

    monkeypatch.setattr(ti, "_hold_page", fake_hold)
    monkeypatch.setattr(ti, "_native_click", fake_native_click)
    async def fake_clear(page, target, timeout):
        record["cleared"] = target

    monkeypatch.setattr(ti, "_clear_field", fake_clear)
    async def fake_verify(page, selector, expected, timeout, retries, result):
        result["verified"] = True

    monkeypatch.setattr(ti, "_apply_verification", fake_verify)
    return ti


def test_snapshot_ref_reaches_humanized_typing(monkeypatch):
    """Refs belong to agent-browser and this Playwright client cannot resolve
    them (aria-ref matches nothing — measured on a live profile). Both clients
    drive the same tab, so a native click focuses the field and the focused
    element yields a real selector for the humanized path."""
    import asyncio

    record: dict = {}
    ti = _stub_text_input_page(monkeypatch, active_selector="#email", record=record)

    out = asyncio.run(ti.browser_type({"ref": "e5", "text": "hello", "verify": False}))
    assert out["ok"] is True
    assert out["target"] == "#email"
    assert record["clicked"] == "@e5"          # focus borrowed from agent-browser
    assert record["typed"] == ("#email", "hello")   # typed humanized, by selector


def test_unresolvable_ref_refuses_rather_than_typing_blind(monkeypatch):
    """If focus did not land on an editable field, typing anyway would put the
    text into whatever else has focus."""
    import asyncio

    record: dict = {}
    ti = _stub_text_input_page(monkeypatch, active_selector=None, record=record)

    for handler in (ti.browser_type, ti.browser_fill):
        out = asyncio.run(handler({"ref": "e5", "text": "hello"}))
        assert out["error"] == "humanized_selector_required"
        assert out["ref"] == "@e5"
        assert "selector=" in out["message"]
    assert "typed" not in record

def test_text_input_falls_back_to_the_focused_field(monkeypatch):
    """The model kept calling browser_fill with neither ref nor selector and
    being refused with the same message eight times running. It almost always
    clicks the field first, so type where the caret already is."""
    import asyncio

    record: dict = {}
    ti = _stub_text_input_page(
        monkeypatch, active_selector="#email", record=record, prefocused=True
    )

    out = asyncio.run(ti.browser_fill({"text": "hello"}))
    assert out["ok"] is True
    assert out["target"] == "#email"
    assert out["target_source"] == "focus"
    assert record["typed"] == ("#email", "hello")
    assert "clicked" not in record, "a focus fallback must not click anything"


def test_text_input_still_refuses_when_nothing_is_focused(monkeypatch):
    """No target and no caret in a field — there is nowhere sane to type."""
    import asyncio

    record: dict = {}
    ti = _stub_text_input_page(monkeypatch, active_selector=None, record=record)

    for handler in (ti.browser_type, ti.browser_fill):
        out = asyncio.run(handler({"text": "hello"}))
        assert out["error"] == "target_required", out
        assert "browser_snapshot" in out["message"]
        assert "selector=" in out["message"]
    assert "typed" not in record

class _KeyBuffer:
    """Minimal stand-in for the raw keyboard: keeps what the field would hold."""

    def __init__(self) -> None:
        self.text = ""

    async def down(self, key: str) -> None:
        if key == "Backspace":
            self.text = self.text[:-1]
        elif len(key) == 1:
            self.text += key

    async def up(self, key: str) -> None:
        return None

    async def type(self, text: str) -> None:
        self.text += text

    async def insert_text(self, text: str) -> None:
        self.text += text


class _FastCfg:
    """Preset with every delay collapsed and typos always firing."""

    mistype_chance = 1.0
    key_hold = (0, 0)
    mistype_delay_notice = (0, 0)
    mistype_delay_correct = (0, 0)
    typing_pause_range = (0, 0)
    typing_pause_chance = 0.0
    typing_delay = 0
    typing_delay_spread = 0
    shift_down_delay = (0, 0)


def test_every_typo_variety_still_lands_the_intended_text(monkeypatch):
    """Each typo variety must self-correct. `adjacent`, `double` and `skip` all
    end by typing the character correctly but reported it as untyped, so the
    caller typed it a second time and the duplicate survived — 75% of the typo
    weight, i.e. ~1.5% of every character."""
    import asyncio
    from plugins.browser.cloak._impl.humanize import keyboard_async as ka
    from plugins.browser.cloak._impl.humanize.constants import TypoType

    # Shift-symbol tables live in cloakbrowser, which the unit env does not
    # install; the text below has none, so stub the probe out.
    monkeypatch.setattr(ka, "_is_shift_symbol", lambda ch: False)
    monkeypatch.delenv("CLOAK_MISTYPE_CHANCE", raising=False)
    intended = "ab cd ef"
    for typo_type in TypoType:
        monkeypatch.setattr(ka, "_pick_typo_type", lambda t=typo_type: t)
        buf = _KeyBuffer()
        asyncio.run(ka.async_human_type(None, buf, intended, _FastCfg(), None))
        assert buf.text == intended, f"{typo_type} corrupted the text: {buf.text!r}"


def test_mistype_chance_can_be_turned_off(monkeypatch):
    import asyncio
    from plugins.browser.cloak._impl.humanize import keyboard_async as ka

    def explode() -> None:
        raise AssertionError("no typo may fire when CLOAK_MISTYPE_CHANCE=0")

    monkeypatch.setattr(ka, "_pick_typo_type", explode)
    monkeypatch.setattr(ka, "_is_shift_symbol", lambda ch: False)
    monkeypatch.setenv("CLOAK_MISTYPE_CHANCE", "0")

    intended = "jaidon.avalynn+edu@outlook.com".replace("+", "").replace("@", "")
    buf = _KeyBuffer()
    asyncio.run(ka.async_human_type(None, buf, intended, _FastCfg(), None))
    assert buf.text == intended

    # A malformed override must fall back to the preset, not crash typing.
    monkeypatch.setenv("CLOAK_MISTYPE_CHANCE", "not-a-number")
    assert ka._typo_probability(_FastCfg()) == 1.0


def test_idle_reaper_returns_the_pooled_proxy(tmp_path, monkeypatch):
    """cloak_stop releases the claim, but the reaper stops profiles straight
    through Manager, so every auto-closed profile drained one proxy for good."""
    from plugins.browser.cloak import proxy_format as pf
    from plugins.browser.cloak._impl import idle_reaper

    pf.set_proxies(["http://u:p@host-a:8080", "http://u:p@host-b:8080"])
    claimed = pf.claim_proxy(pf.profile_claim_owner("acc-idle"))
    assert claimed
    assert sum(1 for e in pf.load_pool()["proxies"] if e.get("assigned_to")) == 1

    released = idle_reaper._release_pool_claim("profile-uuid", "acc-idle")
    assert released == 1
    assert all(not e.get("assigned_to") for e in pf.load_pool()["proxies"])

    # Idempotent, and never raises for a profile that holds nothing.
    assert idle_reaper._release_pool_claim("profile-uuid", "acc-idle") == 0


def test_captcha_detector_survives_a_blocked_cookie_jar():
    """document.cookie throws SecurityError on opaque-origin documents
    (about:blank, data:, sandboxed frames, a page left in an error state). An
    unguarded read aborted the whole scan, so cloak_detect_captcha returned a
    tool error instead of "no captcha here"."""
    import re
    from plugins.browser.cloak._impl.captcha.detector import _DETECT_JS

    guarded = re.search(r"const cookies = \(\(\) => \{ try \{ return document\.cookie", _DETECT_JS)
    assert guarded, "the cookie jar must be read once behind a try/catch"

    # No other place may touch document.cookie directly (comments aside).
    code = "\n".join(
        line for line in _DETECT_JS.splitlines() if not line.strip().startswith("//")
    )
    reads = re.findall(r"document\.cookie", code)
    assert len(reads) == 1, f"unguarded document.cookie reads remain: {len(reads)}"
    assert "cookies.includes(" in _DETECT_JS


def test_cloak_stop_tears_down_the_matching_cdp_supervisor(monkeypatch):
    """Hermes keeps one CDP supervisor per task holding a live websocket. Manager
    answers a websocket upgrade on a stopped profile with 403, so leaving the
    supervisor up turns every later browser_* call into an unexplained 403 loop."""
    import sys
    import types

    from plugins.browser.cloak._impl import tools_manage as tm

    stopped: list = []

    class FakeSupervisor:
        def __init__(self, cdp_url):
            self.cdp_url = cdp_url

    class FakeRegistry:
        def __init__(self, supervisor):
            self._s = supervisor

        def get(self, task_id):
            return self._s

        def stop(self, task_id):
            stopped.append(task_id)

    def install(supervisor):
        # tools.browser_supervisor pulls in `websockets`, which the unit env
        # does not install; the production import is lazy, so stub the module.
        module = types.ModuleType("tools.browser_supervisor")
        module.SUPERVISOR_REGISTRY = FakeRegistry(supervisor)
        monkeypatch.setitem(sys.modules, "tools.browser_supervisor", module)

    install(FakeSupervisor("ws://bridge:8081/api/profiles/prof-1/cdp"))
    assert tm._stop_cdp_supervisor("task-a", "prof-1") == {"cdp_supervisor_stopped": True}
    assert stopped == ["task-a"]

    # A supervisor bound to a different profile in the same task is left alone.
    stopped.clear()
    install(FakeSupervisor("ws://bridge:8081/api/profiles/prof-2/cdp"))
    assert tm._stop_cdp_supervisor("task-a", "prof-1") == {}
    assert stopped == []


def test_launch_drops_a_binding_whose_profile_is_gone():
    from plugins.browser.cloak._impl.tools_manage import _binding_points_at

    assert _binding_points_at({"profile_id": "p1"}, "p1")
    assert not _binding_points_at({"profile_id": "p2"}, "p1")
    assert not _binding_points_at(None, "p1")
    assert not _binding_points_at({}, "p1")


def test_profile_switch_default_follows_the_setting(tmp_path, monkeypatch):
    """The operator should be able to say "use profile X" and have it happen,
    without the model having to remember an escape-hatch flag every time."""
    from plugins.browser.cloak._impl.tools_manage import _profile_switch_allowed

    env_file = tmp_path / "switch" / "manager.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLOAK_MANAGER_ENV", str(env_file))

    # Nothing configured: stay pinned to the remembered profile.
    env_file.write_text("", encoding="utf-8")
    assert _profile_switch_allowed(None) is False

    # Turned on in manager.env — no gateway restart needed.
    env_file.write_text("CLOAK_ALLOW_PROFILE_SWITCH=1\n", encoding="utf-8")
    assert _profile_switch_allowed(None) is True

    # An explicit argument always wins over the setting, both ways.
    assert _profile_switch_allowed(False) is False
    env_file.write_text("CLOAK_ALLOW_PROFILE_SWITCH=0\n# off\n", encoding="utf-8")
    assert _profile_switch_allowed(True) is True


def test_list_profiles_exposes_masked_proxy(monkeypatch):
    """Picking a profile by name only works if the listing says enough about it —
    including which egress it carries, with the password never leaving Cloak."""
    import asyncio
    from plugins.browser.cloak._impl import tools_manage as tm

    class Mgr:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def list_profiles(self):
            return [
                {"id": "p1", "name": "shopper-de", "status": "running",
                 "proxy": "socks5://user:hunter2@de.example:1080", "created_at": "2026-07-27T00:00:00Z"},
                {"id": "p2", "name": "scratch", "status": "stopped", "proxy": None},
            ]

    monkeypatch.setattr(tm, "ManagerClient", Mgr)
    out = asyncio.run(tm.cloak_list_profiles({}))
    first, second = out["profiles"]

    assert first["name"] == "shopper-de" and first["has_proxy"] is True
    assert first["proxy"] == "socks5://de.example:1080"
    assert "hunter2" not in str(out) and "user" not in first["proxy"]
    assert second["has_proxy"] is False and second["proxy"] == ""


def test_invalid_pasted_proxy_lines_are_identifiable_but_redacted():
    """Pasting twenty proxies with one typo used to report '<invalid proxy>' —
    true, redacted, and useless for finding the bad line."""
    from plugins.browser.cloak.proxy_format import describe_invalid

    assert describe_invalid("garbage-line") == "garbage-line"
    assert describe_invalid("socks4://1.2.3.4:1080") == "socks4://1.2.3.4:1080"
    # Credentials never survive the preview.
    out = describe_invalid("alice:hunter2@nope")
    assert out == "<creds>@nope"
    assert "hunter2" not in out
    assert describe_invalid("u:p@" + "x" * 80).endswith("…")
    assert describe_invalid("") == ""


def test_launch_reports_the_profile_it_actually_launched(monkeypatch):
    """A launch that switched profiles answered with the *previous* profile's
    name, so the model could not tell whether the switch had taken effect."""
    import asyncio
    from plugins.browser.cloak._impl import tools_manage as tm

    class Mgr:
        async def get_profile(self, profile_id):
            return {"id": profile_id, "name": "resolved-by-id"}

    binding = {"profile_name": "previously-bound", "profile_id": "old"}

    # Asked by name → that name is authoritative, no extra lookup needed.
    assert asyncio.run(
        tm._profile_display_name(Mgr(), "wf-alpha", "uuid-1", binding)
    ) == "wf-alpha"

    # Asked by UUID → look the real name up rather than echoing the binding.
    uuid = "3f5d019d-1111-2222-3333-444455556666"
    assert asyncio.run(
        tm._profile_display_name(Mgr(), uuid, uuid, binding)
    ) == "resolved-by-id"

    # Nothing requested (resolved from the binding) → the binding name is right.
    assert asyncio.run(
        tm._profile_display_name(Mgr(), "", "old", binding)
    ) == "resolved-by-id"


def test_launch_refreshes_the_session_lease(monkeypatch):
    """The provider reads the lease before the env, so a lease left by an
    earlier cloak_set_active shadowed every later cloak_launch: the browser kept
    connecting to a profile that had long stopped and got 403 forever."""
    import asyncio
    from plugins.browser.cloak import session_leases
    from plugins.browser.cloak._impl import profile_state
    from plugins.browser.cloak._impl import tools_manage as tm

    session_leases.put(
        session_leases.Lease(
            task_id="t1", profile_id="old-profile",
            cdp_url="ws://bridge:8081/api/profiles/old-profile/cdp",
        )
    )

    class Mgr:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get_profile(self, profile_id):
            return {"id": profile_id, "name": "new-name"}

        async def launch(self, profile_id):
            return {"profile_id": profile_id, "cdp_url": f"/api/profiles/{profile_id}/cdp"}

        async def profile_status(self, profile_id):
            return {"status": "running"}

        async def bind_browser_cdp_env(self, rel):
            url = "ws://bridge:8081" + rel
            os.environ["BROWSER_CDP_URL"] = url
            return url

    monkeypatch.setattr(tm, "ManagerClient", Mgr)
    monkeypatch.setattr(tm, "get_pool", lambda: mock.MagicMock())
    monkeypatch.setattr(profile_state, "get_binding", lambda task_id=None: None)
    monkeypatch.setattr(profile_state, "remember_profile", lambda *a, **k: {})
    monkeypatch.setattr(profile_state, "activate_task_binding", lambda task_id=None: "")

    new_id = "11111111-2222-3333-4444-555555555555"
    out = asyncio.run(tm._launch_profile(new_id, task_id="t1"))
    assert out.get("active") is True

    lease = session_leases.get("t1")
    assert lease is not None
    assert lease.profile_id == new_id, "the lease still names the old profile"
    assert new_id in lease.cdp_url


def test_provider_retires_a_lease_whose_profile_stopped(monkeypatch):
    from plugins.browser.cloak import session_leases
    from plugins.browser.cloak.provider import CloakBrowserProvider

    monkeypatch.setenv("CLOAK_MANAGER_URL", "http://127.0.0.1:8080")
    provider = CloakBrowserProvider()

    session_leases.put(
        session_leases.Lease(task_id="t2", profile_id="dead", cdp_url="ws://dead/cdp")
    )
    monkeypatch.setattr(provider, "_profile_is_running", lambda base, pid: False)
    monkeypatch.setattr(provider, "_find_profile_by_name", lambda base, name: None)
    monkeypatch.setattr(provider, "_create_profile", lambda base, name: {"id": "fresh"})
    monkeypatch.setattr(
        provider, "_launch_profile",
        lambda base, pid: {"cdp_url": f"/api/profiles/{pid}/cdp", "already_running": False},
    )
    monkeypatch.setattr(provider, "_resolve_cdp_ws", lambda http: "ws://fresh/cdp")
    monkeypatch.setattr(provider, "_absolute_cdp_url", lambda base, rel: f"{base}{rel}")

    session = provider.create_session("t2")
    assert session["bb_session_id"] == "fresh"
    assert session["cdp_url"] == "ws://fresh/cdp"
    assert session_leases.get("t2").profile_id == "fresh"


def test_provider_keeps_a_lease_whose_profile_still_runs(monkeypatch):
    from plugins.browser.cloak import session_leases
    from plugins.browser.cloak.provider import CloakBrowserProvider

    monkeypatch.setenv("CLOAK_MANAGER_URL", "http://127.0.0.1:8080")
    provider = CloakBrowserProvider()
    session_leases.put(
        session_leases.Lease(task_id="t3", profile_id="alive", cdp_url="ws://alive/cdp")
    )
    monkeypatch.setattr(provider, "_profile_is_running", lambda base, pid: True)

    def explode(*a, **k):
        raise AssertionError("a live lease must be reused, not rebuilt")

    monkeypatch.setattr(provider, "_create_profile", explode)
    session = provider.create_session("t3")
    assert session["cdp_url"] == "ws://alive/cdp"
    assert session["features"].get("reused_lease") is True


class _FakeFrame:
    def __init__(self, url, result=None, boom=False):
        self.url = url
        self._result = result
        self._boom = boom

    async def evaluate(self, js):
        if self._boom:
            raise RuntimeError("frame detached")
        return self._result or {"kind": None, "site_key": None,
                                "page_url": self.url, "extra": {}, "confidence": "high"}


class _FakePage:
    def __init__(self, frames):
        self.frames = frames

    async def evaluate(self, js):
        raise AssertionError("frame-aware scan must not fall back to page.evaluate")


def test_captcha_scan_reaches_into_iframes():
    """Arkose, hCaptcha and reCAPTCHA all render inside an iframe, and the top
    document cannot even read a cross-origin one — so a main-frame-only scan
    reported "no captcha" on exactly the pages that had one."""
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    page = _FakePage([
        _FakeFrame("https://www.figma.com/signup"),
        _FakeFrame("https://client-api.arkoselabs.com/challenge", {
            "kind": "funcaptcha", "site_key": "PKEY-123",
            "page_url": "https://client-api.arkoselabs.com/challenge",
            "extra": {"surl": "https://client-api.arkoselabs.com"}, "confidence": "high",
        }),
    ])
    out = asyncio.run(detect_in_playwright_page(page))
    assert out["kind"] == "funcaptcha"
    assert out["site_key"] == "PKEY-123"
    # Solvers key on the page the operator is on, not the challenge iframe.
    assert out["page_url"] == "https://www.figma.com/signup"
    assert out["extra"]["in_iframe"] is True
    assert out["extra"]["frame_url"] == "https://client-api.arkoselabs.com/challenge"
    assert out["extra"]["surl"] == "https://client-api.arkoselabs.com"


def test_captcha_in_the_main_frame_is_not_relabelled():
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    page = _FakePage([
        _FakeFrame("https://example.test/login", {
            "kind": "turnstile", "site_key": "0x4", "page_url": "https://example.test/login",
            "extra": {}, "confidence": "high",
        }),
    ])
    out = asyncio.run(detect_in_playwright_page(page))
    assert out["kind"] == "turnstile"
    assert out["page_url"] == "https://example.test/login"
    assert "in_iframe" not in out["extra"]


def test_a_detached_frame_does_not_blind_the_scan():
    """Frames come and go while a challenge loads; one bad frame must not hide
    a captcha sitting in the next one."""
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    page = _FakePage([
        _FakeFrame("https://example.test/"),
        _FakeFrame("https://gone.test/", boom=True),
        _FakeFrame("https://hcaptcha.test/", {
            "kind": "hcaptcha", "site_key": "abc", "page_url": "https://hcaptcha.test/",
            "extra": {}, "confidence": "high",
        }),
    ])
    out = asyncio.run(detect_in_playwright_page(page))
    assert out["kind"] == "hcaptcha"
    assert out["page_url"] == "https://example.test/"


def test_clean_page_reports_no_captcha():
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    page = _FakePage([_FakeFrame("https://example.test/"), _FakeFrame("https://ads.test/")])
    out = asyncio.run(detect_in_playwright_page(page))
    assert out["kind"] is None


def test_announced_but_unrendered_arkose_reads_as_pending():
    """The enforcement script loads long before the challenge exists, and with a
    good fingerprint the challenge may never render. Reporting that as a
    solvable funcaptcha hands the solver a null site_key; reporting it as "no
    captcha" tells the agent to carry on into a wall."""
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    announced = _FakePage([
        _FakeFrame("https://www.figma.com/signup", {
            "kind": None, "site_key": None, "pending": True,
            "page_url": "https://www.figma.com/signup",
            "extra": {"vendor": "arkose",
                      "pending_reason": "Arkose script present, challenge not rendered yet"},
            "confidence": "medium",
        }),
    ])
    out = asyncio.run(detect_in_playwright_page(announced))
    assert out["kind"] is None
    assert out["pending"] is True
    assert "arkose" in out["extra"]["vendor"]


def test_detect_waits_for_a_challenge_to_render(monkeypatch):
    """Poll while the widget is loading, and report the moment it appears."""
    import asyncio
    from plugins.browser.cloak._impl.captcha import detector as det

    scans = {"n": 0}

    async def fake_scan(page):
        scans["n"] += 1
        if scans["n"] < 3:
            return {"kind": None, "site_key": None, "pending": True,
                    "page_url": "u", "extra": {}, "confidence": "medium"}
        return {"kind": "funcaptcha", "site_key": "PKEY", "pending": False,
                "page_url": "u", "extra": {}, "confidence": "high"}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(det, "_scan_once", fake_scan)
    monkeypatch.setattr(det.asyncio, "sleep", no_sleep)

    out = asyncio.run(det.detect_in_playwright_page(object(), wait_ms=10000))
    assert out["kind"] == "funcaptcha"
    assert out["site_key"] == "PKEY"
    assert scans["n"] == 3


def test_detect_stops_once_the_page_reads_clean_twice(monkeypatch):
    """A challenge that resolves on its own must end the wait, not burn the
    whole budget — but one clean read is not enough, the widget may be mid-swap."""
    import asyncio
    from plugins.browser.cloak._impl.captcha import detector as det

    reads = [
        {"kind": None, "pending": True, "site_key": None, "page_url": "u", "extra": {}, "confidence": "medium"},
        {"kind": None, "pending": False, "site_key": None, "page_url": "u", "extra": {}, "confidence": "high"},
        {"kind": None, "pending": False, "site_key": None, "page_url": "u", "extra": {}, "confidence": "high"},
    ]
    seen = {"n": 0}

    async def fake_scan(page):
        out = reads[min(seen["n"], len(reads) - 1)]
        seen["n"] += 1
        return dict(out)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(det, "_scan_once", fake_scan)
    monkeypatch.setattr(det.asyncio, "sleep", no_sleep)

    out = asyncio.run(det.detect_in_playwright_page(object(), wait_ms=30000))
    assert out["kind"] is None
    assert out["pending"] is False
    assert seen["n"] == 3, "should stop on the second consecutive clean read"
    assert out["waited_ms"] < 30000


def test_anticaptcha_builds_the_documented_task_shapes():
    """Third solver alongside CapSolver and 2captcha."""
    from plugins.browser.cloak._impl.captcha.anticaptcha import (
        AntiCaptchaError, SUPPORTED_KINDS, _build_task, _extract_token, _hostname,
    )

    assert {"funcaptcha", "hcaptcha", "turnstile", "recaptcha_v2"} <= set(SUPPORTED_KINDS)

    arkose = _build_task("funcaptcha", "PKEY", "https://figma.com/signup",
                         {"surl": "https://client-api.arkoselabs.com/x"})
    assert arkose["type"] == "FunCaptchaTaskProxyless"
    assert arkose["websitePublicKey"] == "PKEY"
    # Anti-Captcha wants the bare subdomain, not the URL it was found at.
    assert arkose["funcaptchaApiJSSubdomain"] == "client-api.arkoselabs.com"
    assert _hostname("https://a.b.c/d/e") == "a.b.c"

    v3 = _build_task("recaptcha_v3", "KEY", "https://x.test", {"action": "signup", "min_score": 0.9})
    assert v3["minScore"] == 0.9 and v3["pageAction"] == "signup"

    turnstile = _build_task("turnstile", "0x4", "https://x.test", {"action": "login", "data": "cd"})
    assert turnstile["action"] == "login" and turnstile["cData"] == "cd"

    assert _extract_token("funcaptcha", {"gRecaptchaResponse": "tok"}) == "tok"
    assert _extract_token("image", {"text": "abc"}) == "abc"

    # A missing site key is caught here, not sent to the API as an empty string.
    with pytest.raises(AntiCaptchaError):
        _build_task("hcaptcha", "", "https://x.test", {})
    with pytest.raises(AntiCaptchaError):
        _build_task("kasada", "k", "https://x.test", {})


def test_anticaptcha_surfaces_in_band_errors():
    from plugins.browser.cloak._impl.captcha.anticaptcha import AntiCaptchaError, _raise_for_error

    _raise_for_error({"errorId": 0, "taskId": 7}, "createTask")  # no raise
    with pytest.raises(AntiCaptchaError) as excinfo:
        _raise_for_error(
            {"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "no funds"},
            "createTask",
        )
    assert "ERROR_ZERO_BALANCE" in str(excinfo.value)


def test_router_knows_all_three_providers(monkeypatch):
    from plugins.browser.cloak._impl.captcha import router as r

    assert set(r._CLIENTS) == {"2captcha", "capsolver", "anticaptcha"}
    assert "anticaptcha" in r._PREFERRED["funcaptcha"]
    assert "anticaptcha" in r._PREFERRED["hcaptcha"]
    # Kinds Anti-Captcha cannot do must not list it.
    assert "anticaptcha" not in r._PREFERRED["kasada"]

    monkeypatch.setenv("CAPTCHA_PROVIDER", "anticaptcha")
    assert r.CaptchaRouter().override == "anticaptcha"
    monkeypatch.setenv("CAPTCHA_PROVIDER", "anti-captcha")
    assert r.CaptchaRouter().override == "anticaptcha"
    monkeypatch.setenv("CAPTCHA_PROVIDER", "nonsense")
    assert r.CaptchaRouter().override == "auto"


def test_navigation_reports_a_captcha_without_being_asked(monkeypatch):
    """The agent kept walking into challenge pages because nothing told it one
    was there — it had to remember to call the detector itself."""
    import asyncio
    from plugins.browser.cloak._impl import tools_browser as tb

    monkeypatch.delenv("CLOAK_AUTODETECT_CAPTCHA", raising=False)

    async def fake_detect(page):
        return {"kind": "funcaptcha", "site_key": "PKEY", "pending": False,
                "page_url": "https://figma.com/signup", "extra": {}, "confidence": "high"}

    import plugins.browser.cloak._impl.captcha as captcha_pkg
    monkeypatch.setattr(captcha_pkg, "detect_in_playwright_page", fake_detect)

    found = asyncio.run(tb._scan_for_captcha(object()))
    assert found and found["kind"] == "funcaptcha"

    out = json.loads(tb._nav_result("https://figma.com/signup", "ws://x", {}, 1, captcha=found))
    assert out["captcha"]["site_key"] == "PKEY"
    assert "cloak_solve_captcha" in out["next_step"]

    # Pending reads differently: wait, do not try to solve nothing.
    pending = {"kind": None, "pending": True, "extra": {}, "site_key": None}
    out2 = json.loads(tb._nav_result("https://figma.com/signup", "ws://x", {}, 1, captcha=pending))
    assert "wait_ms" in out2["next_step"]

    # A clean page adds nothing.
    out3 = json.loads(tb._nav_result("https://example.test", "ws://x", {}, 1))
    assert "captcha" not in out3 and "next_step" not in out3


def test_captcha_autodetect_can_be_turned_off(monkeypatch):
    import asyncio
    from plugins.browser.cloak._impl import tools_browser as tb

    monkeypatch.setenv("CLOAK_AUTODETECT_CAPTCHA", "0")

    def explode(page):
        raise AssertionError("scan must not run when the switch is off")

    import plugins.browser.cloak._impl.captcha as captcha_pkg
    monkeypatch.setattr(captcha_pkg, "detect_in_playwright_page", explode)
    assert asyncio.run(tb._scan_for_captcha(object())) is None


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages


class _FakeTab:
    """A tab: has frames, and knows which context it belongs to."""

    def __init__(self, frames, context=None):
        self.frames = frames
        self.context = context

    async def evaluate(self, js):
        raise AssertionError("tab scan must go through frames")


def test_captcha_scan_covers_every_tab():
    """Registration opens in a second tab. The pooled client is pinned to the
    context's first tab, so the detector was reading the page the operator had
    already left and answering "no captcha" while the challenge sat one tab
    over — which is what made the agent give up and kill the profile."""
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    first = _FakeTab([_FakeFrame("https://www.figma.com/education/")])
    second = _FakeTab([
        _FakeFrame("https://www.figma.com/signup"),
        _FakeFrame("https://client-api.arkoselabs.com/challenge", {
            "kind": "funcaptcha", "site_key": "PKEY-XYZ",
            "page_url": "https://client-api.arkoselabs.com/challenge",
            "extra": {}, "confidence": "high",
        }),
    ])
    context = _FakeContext([first, second])
    first.context = context
    second.context = context

    out = asyncio.run(detect_in_playwright_page(first))
    assert out["kind"] == "funcaptcha"
    assert out["site_key"] == "PKEY-XYZ"
    assert out["extra"]["other_tab"] is True
    assert out["extra"]["tab_index"] == 1
    # Found in an iframe of that tab, so the solver still gets the tab's own URL.
    assert out["page_url"] == "https://www.figma.com/signup"


def test_a_captcha_on_the_current_tab_is_not_marked_as_elsewhere():
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    here = _FakeTab([_FakeFrame("https://x.test/", {
        "kind": "hcaptcha", "site_key": "k", "page_url": "https://x.test/",
        "extra": {}, "confidence": "high",
    })])
    other = _FakeTab([_FakeFrame("https://ads.test/")])
    context = _FakeContext([here, other])
    here.context = context
    other.context = context

    out = asyncio.run(detect_in_playwright_page(here))
    assert out["kind"] == "hcaptcha"
    assert "other_tab" not in out["extra"]


def test_a_pending_challenge_in_another_tab_is_reported_too():
    """"Captcha is loading" in the signup tab must not read as an all-clear
    just because the tab we hold is quiet."""
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    quiet = _FakeTab([_FakeFrame("https://www.figma.com/education/")])
    loading = _FakeTab([_FakeFrame("https://www.figma.com/signup", {
        "kind": None, "site_key": None, "pending": True,
        "page_url": "https://www.figma.com/signup",
        "extra": {"pending_reason": "page says: captcha loading"}, "confidence": "medium",
    })])
    context = _FakeContext([quiet, loading])
    quiet.context = context
    loading.context = context

    out = asyncio.run(detect_in_playwright_page(quiet))
    assert out["kind"] is None
    assert out["pending"] is True
    assert out["extra"]["other_tab"] is True


def test_detector_js_matches_the_real_figma_markup():
    """Taken verbatim from a live transcript: the banner reads "Captcha is
    loading..." and the Arkose frame carries a title, not a src. The literal
    phrase list missed the first and the src-only selector missed the second,
    so the tool answered "no captcha" on the page that had one."""
    from plugins.browser.cloak._impl.captcha.detector import _DETECT_JS

    # The banner is matched by shape, not by an exact substring.
    assert r"/captcha\s+(?:is\s+)?loading/" in _DETECT_JS
    # ...and headings count, because that page is little else.
    assert 'querySelectorAll("h1, h2, [role=heading]")' in _DETECT_JS
    # A frame identified by title/name/id, not only by src.
    assert 'iframe[title*="arkose" i]' in _DETECT_JS
    assert 'iframe[name*="arkose" i]' in _DETECT_JS
    # A frame with no usable src and no public key is pending, not solvable.
    assert "the public key is not exposed yet" in _DETECT_JS
    assert "hasVendorSrc" in _DETECT_JS


def test_a_pending_banner_in_a_subframe_is_not_lost():
    """Figma's signup form and its "Captcha is loading..." banner live in a
    login iframe. Returning early only on a resolved kind let the main frame's
    clean read win, so the tool said "no captcha" about a page that had one."""
    import asyncio
    from plugins.browser.cloak._impl.captcha.detector import detect_in_playwright_page

    page = _FakePage([
        _FakeFrame("https://www.figma.com/education/"),
        _FakeFrame("https://www.figma.com/login_iframe?form_state=sign_up", {
            "kind": None, "site_key": None, "pending": True,
            "page_url": "https://www.figma.com/login_iframe?form_state=sign_up",
            "extra": {"vendor": "arkose",
                      "pending_reason": "Arkose frame present but the public key is not exposed yet"},
            "confidence": "medium",
        }),
    ])
    out = asyncio.run(detect_in_playwright_page(page))
    assert out["kind"] is None
    assert out["pending"] is True
    assert out["extra"]["in_iframe"] is True
    assert out["page_url"] == "https://www.figma.com/education/"


def test_detector_reads_the_arkose_key_from_the_enforcement_url():
    """Sites rarely expose data-pkey. Arkose carries the public key in the
    enforcement URL, and a solver cannot take the task without it — so the
    challenge was found and then never solvable."""
    from plugins.browser.cloak._impl.captcha.detector import _DETECT_JS

    assert "findPkey" in _DETECT_JS
    # .../v2/<PUBLIC_KEY>/api.js
    assert r"/\/v2\/([A-Za-z0-9-]{8,})\//" in _DETECT_JS
    # ...?pkey=<PUBLIC_KEY> / ?public_key=<PUBLIC_KEY>
    assert "public_key|pkey" in _DETECT_JS
    # Scripts count, not just iframes: the script loads first.
    assert 'script[src*="arkose" i]' in _DETECT_JS
    assert "arkoseConfig" in _DETECT_JS


def test_arkose_blob_reaches_every_solver():
    """Sites that gate Arkose on a per-session data-exchange blob are simply
    unsolvable without it, and 2captcha rejects a bare blob with ERROR_DATA —
    it has to arrive wrapped."""
    import json as _json
    from plugins.browser.cloak._impl.captcha.twocaptcha import _b_funcaptcha
    from plugins.browser.cloak._impl.captcha.anticaptcha import _build_task as anti_task
    from plugins.browser.cloak._impl.captcha.capsolver import _build_task as cap_task

    extra = {"blob": "BLOB-XYZ", "surl": "https://figma-api.arkoselabs.com"}

    two = _b_funcaptcha("PKEY", "https://www.figma.com/signup", extra)
    assert two["data"] == _json.dumps({"blob": "BLOB-XYZ"})
    assert two["surl"] == "https://figma-api.arkoselabs.com"

    anti = anti_task("funcaptcha", "PKEY", "https://www.figma.com/signup", extra)
    assert anti["data"] == _json.dumps({"blob": "BLOB-XYZ"})
    assert anti["funcaptchaApiJSSubdomain"] == "figma-api.arkoselabs.com"

    cap = cap_task("funcaptcha", "PKEY", "https://www.figma.com/signup", extra)
    assert cap["data"] == _json.dumps({"blob": "BLOB-XYZ"})

    # An already-wrapped payload passes through untouched.
    prewrapped = {"data": _json.dumps({"blob": "RAW"})}
    assert _b_funcaptcha("K", "u", prewrapped)["data"] == _json.dumps({"blob": "RAW"})
    # No blob at all: the field is simply absent, not an empty string.
    assert "data" not in _b_funcaptcha("K", "u", {})


def test_funcaptcha_tries_2captcha_before_capsolver():
    """CapSolver rejects some Arkose public keys outright, so leading with it
    spends a round trip to learn nothing."""
    from plugins.browser.cloak._impl.captcha import router as r

    assert r._PREFERRED["funcaptcha"] == ["2captcha", "anticaptcha", "capsolver"]


def test_solve_schema_offers_every_wired_provider():
    from plugins.browser.cloak._impl.tools_manage import SCHEMA_SOLVE_CAPTCHA

    providers = SCHEMA_SOLVE_CAPTCHA["properties"]["provider"]["enum"]
    assert providers == ["auto", "capsolver", "2captcha", "anticaptcha"]
    assert "blob" in SCHEMA_SOLVE_CAPTCHA["properties"]["extra"]["description"]
