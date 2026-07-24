"""Regression tests for Cloak Edition critical fixes.

Run from repo root:
  python -m pytest tests/cloak -q
"""
from __future__ import annotations

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

