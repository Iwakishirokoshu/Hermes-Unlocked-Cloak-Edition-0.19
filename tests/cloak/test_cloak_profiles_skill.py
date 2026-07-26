"""Static contract for the model-driven Cloak profile skill.

The skill is the only place the model learns how the cloak_* tools actually
behave, so it has to track them. Every assertion here corresponds to a way the
agent got stuck in production.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "cloak-profiles" / "SKILL.md"


def test_skill_covers_the_real_profile_lifecycle() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "name: cloak-profiles" in text
    for tool in ("cloak_list_profiles", "cloak_create_profile", "cloak_launch",
                 "cloak_stop", "cloak_proxy_pool"):
        assert tool in text
    assert "cloak_launch_profile" not in text
    assert "humanize=true" in text
    assert 'tags=[{"tag": "registration"}]' in text
    # Switching by name is supported now; the skill must say so.
    assert "allow_profile_switch=true" in text


def test_skill_forbids_churning_profiles() -> None:
    """Twenty-nine profiles were created in one session because a failure was
    answered with a fresh fingerprint instead of a diagnosis."""
    text = SKILL.read_text(encoding="utf-8")

    assert "Не создавай новый профиль на каждую неудачу" in text
    assert "name-001" in text


def test_skill_documents_both_proxy_paths() -> None:
    """A pasted proxy and the shared pool are both first-class. The operator
    picks; the skill must not push one over the other."""
    text = SKILL.read_text(encoding="utf-8")

    assert 'proxy="socks5://user:pass@host:1080"' in text
    assert "use_pool=true" in text
    assert 'cloak_proxy_pool(action="add"' in text
    assert "всегда сильнее пула" in text
    assert "proxy_unavailable" in text
    assert "socks4" in text
    # The manual reserve/release dance is no longer the documented path.
    assert 'cloak_proxy_pool(action="next")' not in text


def test_skill_documents_both_ways_to_address_a_field() -> None:
    """Refs used to be refused for text input; they work now, and the old
    prohibition left the model with no legal move."""
    text = SKILL.read_text(encoding="utf-8")

    assert 'ref="e5"' in text
    assert "selector=\"input[type='email']\"" in text
    assert "verify=true" in text
    assert "CLOAK_MISTYPE_CHANCE=0" in text
    assert "humanized_selector_required" in text
    # The flow opens a second tab; the model has to expect it.
    assert "в новой вкладке" in text
    assert "other_tab" in text


def test_skill_separates_pending_from_no_captcha() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "cloak_detect_captcha" in text
    assert "cloak_solve_captcha" in text
    assert "pending: true" in text
    assert "wait_ms=15000" in text
    assert "url=<detected page_url>" in text
    assert "не применяет его к странице" in text
    assert "MANUAL_INTERVENTION_REQUIRED" in text
    assert "прямую инъекцию токенов" in text


def test_skill_lists_every_wired_solver() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "CAPSOLVER_API_KEY" in text
    assert "TWO_CAPTCHA_API_KEY" in text
    assert "TWOCAPTCHA_API_KEY" in text
    assert "ANTICAPTCHA_API_KEY" in text
    # Promising a provider the router cannot reach is worse than omitting it.
    assert "NOTLETTERS_API_KEY" not in text
