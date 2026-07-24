"""Regression coverage for the Cloak dashboard writable-state and auth rendering."""

from __future__ import annotations

import ast
import copy
import os
from pathlib import Path
from typing import Any, Dict
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_SOURCE = ROOT / "hermes_cli" / "cloak_dashboard.py"


def _load_path_writable():
    """Load the dependency-free helper without importing the FastAPI module."""
    module = ast.parse(DASHBOARD_SOURCE.read_text(encoding="utf-8"), filename=str(DASHBOARD_SOURCE))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_path_writable"
    )
    namespace = {"os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(DASHBOARD_SOURCE), "exec"), namespace)
    return namespace["_path_writable"]


def _load_manager_browser_url():
    """Load the URL helper without importing the FastAPI module."""
    module = ast.parse(DASHBOARD_SOURCE.read_text(encoding="utf-8"), filename=str(DASHBOARD_SOURCE))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_manager_browser_url"
    )
    namespace = {"os": os, "_bootstrap_env": lambda: None, "_manager_url": lambda: "http://manager:8080"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(DASHBOARD_SOURCE), "exec"), namespace)
    return namespace["_manager_browser_url"]


def _load_token_fields():
    """Compile the token helper without importing the FastAPI dashboard module."""
    module = ast.parse(DASHBOARD_SOURCE.read_text(encoding="utf-8"), filename=str(DASHBOARD_SOURCE))
    functions = [
        next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)
        for name in ("_mask_token", "_token_fields")
    ]
    namespace = {"Any": Any, "Dict": Dict}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(DASHBOARD_SOURCE), "exec"), namespace)
    return namespace["_token_fields"]


def _render_cloak_panel(*, auth_required: bool, legacy_token: str) -> str:
    """Exercise the panel token decision without importing FastAPI."""
    module = ast.parse(DASHBOARD_SOURCE.read_text(encoding="utf-8"), filename=str(DASHBOARD_SOURCE))
    function = copy.deepcopy(next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "cloak_panel"
    ))
    function.decorator_list = []
    namespace = {
        "Request": object,
        "HTMLResponse": lambda html: html,
        "_PANEL_HTML": "token=__CLOAK_DASH_TOKEN__",
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(DASHBOARD_SOURCE), "exec"), namespace)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_required=auth_required,
                legacy_session_token=legacy_token,
            )
        )
    )
    return namespace["cloak_panel"](request)


def test_path_writable_accepts_a_missing_nested_directory(tmp_path):
    """Fresh Compose volumes can create the Cloak directory on their first save."""
    path = tmp_path / "data" / ".hermes" / "cloak" / "manager.env"

    assert _load_path_writable()(str(path)) is True


def test_path_writable_rejects_a_missing_path_under_a_file(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")

    assert _load_path_writable()(str(blocker / "cloak" / "manager.env")) is False


def test_manager_browser_url_prefers_the_windows_reachable_address(monkeypatch):
    monkeypatch.setenv("CLOAK_MANAGER_BROWSER_URL", "http://127.0.0.1:8180/")

    assert _load_manager_browser_url()() == "http://127.0.0.1:8180"


def test_manager_browser_url_falls_back_to_the_manager_address(monkeypatch):
    monkeypatch.delenv("CLOAK_MANAGER_BROWSER_URL", raising=False)

    assert _load_manager_browser_url()() == "http://manager:8080"


def test_cloak_panel_uses_cookie_session_when_dashboard_auth_is_enabled():
    assert _render_cloak_panel(auth_required=True, legacy_token="stale-loopback-token") == "token="
    assert _render_cloak_panel(auth_required=False, legacy_token="loopback-token") == "token=loopback-token"

def test_cloak_token_is_masked_by_default_and_revealed_only_explicitly():
    token_fields = _load_token_fields()
    secret = "manager-secret-token"

    masked = token_fields(secret, reveal=False)
    assert masked["has_token"] is True
    assert masked["token_masked"] != secret
    assert "token" not in masked

    revealed = token_fields(secret, reveal=True)
    assert revealed["token"] == secret
    assert "token" not in token_fields("", reveal=True)


def test_cloak_token_reveal_is_explicit_and_not_cached():
    source = DASHBOARD_SOURCE.read_text(encoding="utf-8")

    assert "**_token_fields(token, reveal)," in source
    assert 'headers={"Cache-Control": "no-store"}' in source
    assert 'id="toggle-token"' in source
    assert '"?reveal=1"' in source
    assert 'if (!TOKEN_REVEALED) load(false);' in source
