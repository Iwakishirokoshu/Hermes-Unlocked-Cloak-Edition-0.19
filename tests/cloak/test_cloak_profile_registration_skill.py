"""Static contract for the model-driven Cloak registration skill."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "cloak-profile-registration" / "SKILL.md"


def test_registration_skill_uses_the_real_cloak_lifecycle() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "name: cloak-profile-registration" in text
    assert "cloak_create_profile" in text
    assert "cloak_launch" in text
    assert "cloak_launch_profile" not in text
    assert "humanize=true" in text
    assert 'tags=[{"tag": "registration"}]' in text
    assert "humanized_selector_required" in text
    assert "use_pool=false" in text
    assert "use_pool=true" in text
    assert 'cloak_proxy_pool(action="next")' in text
    assert "browser_snapshot" in text
    assert "browser_click" in text
    assert "verify=true" in text


def test_registration_skill_has_a_real_captcha_and_manual_gate() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "cloak_detect_captcha" in text
    assert "cloak_solve_captcha" in text
    assert "url=<detected page_url>" in text
    assert "не применяет его к странице" in text
    assert "MANUAL_INTERVENTION_REQUIRED" in text
    assert "CAPSOLVER_API_KEY" in text
    assert "TWO_CAPTCHA_API_KEY" in text
    assert "TWOCAPTCHA_API_KEY" in text
    assert "NOTLETTERS_API_KEY" not in text
    assert "прямую инъекцию токенов" in text
