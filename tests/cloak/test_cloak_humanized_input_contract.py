"""Regression contracts for the Cloak humanized registration path."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "plugins" / "browser" / "cloak" / "_impl" / "tools_input.py"
MANAGE = ROOT / "plugins" / "browser" / "cloak" / "_impl" / "tools_manage.py"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == name)
    return ast.get_source_segment(source, node) or ""


def _normalizer():
    source = MANAGE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_normalize_profile_tags")
    namespace = {"Any": Any, "Dict": Dict, "List": List}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MANAGE), "exec"), namespace)
    return namespace["_normalize_profile_tags"]


def test_profile_tags_match_manager_tagcreate_shape() -> None:
    normalize = _normalizer()

    assert normalize(["registration", {"tag": "manual", "color": "blue"}]) == [
        {"tag": "registration"},
        {"tag": "manual", "color": "blue"},
    ]
    try:
        normalize([{}])
    except ValueError as exc:
        assert "non-empty tag" in str(exc)
    else:
        raise AssertionError("an empty Manager tag must be rejected")


def test_cloak_text_input_never_uses_native_or_instant_fill() -> None:
    typed = _function_source(INPUT, "browser_type")
    filled = _function_source(INPUT, "browser_fill")

    assert "humanized_selector_required" in typed
    assert "humanized_selector_required" in filled
    assert "_native_type(" not in typed
    assert "_native_type(" not in filled
    assert "await loc.fill(text" not in filled
    assert "await _clear_field(page, target, timeout_ms)" in filled
    assert "await loc.type(text, timeout=timeout_ms)" in filled