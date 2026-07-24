"""Shared utilities lifted from the original ``selenium_runner`` module.

The Selenium engine itself is removed from this vendor (Hermes is
Playwright-only), but a handful of small helpers were used elsewhere
(notably ``generate_name`` and ``generate_password``). They live here so
the rest of the vendor keeps importing them from a stable path:

    from core._legacy import generate_name, generate_password
"""
from __future__ import annotations

import os
import random
import string
from typing import List


def _names_file_path() -> str:
    # Resolve data/names.txt relative to the vendor root, not cwd.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "data", "names.txt")


def _load_names() -> List[str]:
    names: List[str] = []
    try:
        with open(_names_file_path(), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
    except FileNotFoundError:
        pass
    return names


_names_list = _load_names()


def generate_name() -> str:
    """Pick a random "First Last" from data/names.txt, or fall back to a
    pseudo name. Compatible with the original ShadowHackrs API."""
    if _names_list:
        return random.choice(_names_list)
    return f"User{random.randint(1000, 9999)}"


def generate_password(length: int = 14) -> str:
    """Generate a strong unique password.

    Composition: 3 uppercase + 5 lowercase + 3 digits + 2 specials + filler.
    Same recipe as the original ShadowHackrs ``generate_password``.
    """
    length = max(13, int(length))
    upper = random.choices(string.ascii_uppercase, k=3)
    lower = random.choices(string.ascii_lowercase, k=5)
    digits = random.choices(string.digits, k=3)
    specials = random.choices("!@#$%&*", k=2)
    filler = random.choices(string.ascii_letters + string.digits, k=length - 13)
    pool = upper + lower + digits + specials + filler
    random.shuffle(pool)
    return "".join(pool)


def validate_birthday(birthday_str: str):
    """Parse and clamp a "MM DD YYYY"-ish birthday string."""
    try:
        month, day, year = birthday_str.split()
        if not (1 <= int(month) <= 12):
            month = "1"
        if not (1 <= int(day) <= 31):
            day = "1"
        if not (1900 <= int(year) <= 2010):
            year = "1990"
        return str(int(month)), str(int(day)), str(int(year))
    except Exception:
        return "1", "1", "1990"


class _LegacyEngineNotAvailable(RuntimeError):
    """Raised when callers ask for the Selenium or Appium engine.

    This vendor build is Playwright-only — the Selenium / Appium engines
    were dropped to keep deps small (no selenium, no Appium client, no
    webdriver-manager). Hermes always drives Playwright through Cloak.
    """


def run_selenium_flow(*args, **kwargs):  # noqa: ARG001
    raise _LegacyEngineNotAvailable(
        "Selenium engine is not bundled in the Hermes vendor build. "
        "Use ENGINE_MODE=playwright."
    )


def create_driver(*args, **kwargs):  # noqa: ARG001
    raise _LegacyEngineNotAvailable(
        "Selenium engine is not bundled in the Hermes vendor build."
    )


class AppiumManager:
    """Stub: the Appium engine is not bundled in this vendor build."""

    def __init__(self) -> None:
        raise _LegacyEngineNotAvailable(
            "Appium engine is not bundled in the Hermes vendor build. "
            "Use ENGINE_MODE=playwright."
        )
