"""HumanBehavior — pure-Python humanized mouse/typing primitives.

Hermes vendor slim build: the original ShadowHackrs implementation depended
on numpy+scipy for Bezier curve generation and used a single uniform-delay
"typo" path. We replace it with the pydoll-derived math that lives in
``hermes_plugin_cloak.humanize`` so that:

  - no scipy/numpy in the gmail-factory venv (saves ~150MB),
  - mouse paths use minimum-jerk timing + asymmetric Bezier + velocity-inverse
    Gaussian tremor (the same math Hermes uses everywhere else),
  - the keyboard path keeps a simple drop-in API (``natural_type(page,
    selector, text, make_typos)``) so the rest of the vendor code is
    unchanged.

We avoid importing from ``hermes_plugin_cloak`` directly here so that the
vendor stays standalone — the gmail-factory venv does not have Hermes on
its PYTHONPATH. The pydoll math is therefore inlined below. Attribution
in the module header.

Math attribution:
  - bezier_2d, minimum_jerk, random_control_points — pydoll
    (autoscrape-labs/pydoll, MIT). Same code lives in
    hermes_plugin_cloak/humanize/utils.py.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import List, Tuple

logger = logging.getLogger("gmail_factory_behavior")

Point = Tuple[float, float]


# ---------------------------------------------------------------------------
# Pydoll-derived math (inlined to keep vendor standalone).
# ---------------------------------------------------------------------------

def _minimum_jerk(t: float) -> float:
    t2 = t * t
    t3 = t2 * t
    t4 = t3 * t
    t5 = t4 * t
    return 10.0 * t3 - 15.0 * t4 + 6.0 * t5


def _bezier_2d(t: float, p0: Point, p1: Point, p2: Point, p3: Point) -> Point:
    u = 1.0 - t
    u2 = u * u
    u3 = u2 * u
    t2 = t * t
    t3 = t2 * t
    x = u3 * p0[0] + 3.0 * u2 * t * p1[0] + 3.0 * u * t2 * p2[0] + t3 * p3[0]
    y = u3 * p0[1] + 3.0 * u2 * t * p1[1] + 3.0 * u * t2 * p2[1] + t3 * p3[1]
    return (x, y)


def _random_control_points(
    start: Point,
    end: Point,
    curvature_min: float = 0.10,
    curvature_max: float = 0.30,
    curvature_asymmetry: float = 0.6,
    short_distance_threshold: float = 50.0,
) -> Tuple[Point, Point]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        return (start, end)
    perp = (-dy / distance, dx / distance)
    scale = min(1.0, distance / short_distance_threshold)
    offsets = (
        random.uniform(curvature_min, curvature_max) * distance * scale,
        random.uniform(curvature_min, curvature_max) * distance * scale,
    )
    sign = random.choice([-1.0, 1.0])
    t1 = random.uniform(0.2, curvature_asymmetry)
    t2 = random.uniform(curvature_asymmetry, 0.8)
    cp1 = (
        start[0] + dx * t1 + perp[0] * offsets[0] * sign,
        start[1] + dy * t1 + perp[1] * offsets[0] * sign,
    )
    counter = random.uniform(0.3, 1.0)
    cp2 = (
        start[0] + dx * t2 + perp[0] * offsets[1] * sign * counter,
        start[1] + dy * t2 + perp[1] * offsets[1] * sign * counter,
    )
    return (cp1, cp2)


# ---------------------------------------------------------------------------
# QWERTY adjacency for plausible "fat-finger" typos.
# Subset of hermes_plugin_cloak.humanize.constants.QWERTY_NEIGHBORS.
# ---------------------------------------------------------------------------

_QWERTY_NEIGHBORS = {
    'q': ['w', 'a', 's'], 'w': ['q', 'e', 'a', 's', 'd'],
    'e': ['w', 'r', 's', 'd', 'f'], 'r': ['e', 't', 'd', 'f', 'g'],
    't': ['r', 'y', 'f', 'g', 'h'], 'y': ['t', 'u', 'g', 'h', 'j'],
    'u': ['y', 'i', 'h', 'j', 'k'], 'i': ['u', 'o', 'j', 'k', 'l'],
    'o': ['i', 'p', 'k', 'l'], 'p': ['o', 'l'],
    'a': ['q', 'w', 's', 'z'], 's': ['w', 'a', 'd', 'z', 'x'],
    'd': ['e', 's', 'f', 'x', 'c'], 'f': ['r', 'd', 'g', 'c', 'v'],
    'g': ['t', 'f', 'h', 'v', 'b'], 'h': ['y', 'g', 'j', 'b', 'n'],
    'j': ['u', 'h', 'k', 'n', 'm'], 'k': ['i', 'j', 'l', 'm'],
    'l': ['o', 'p', 'k'], 'z': ['a', 's', 'x'],
    'x': ['z', 's', 'd', 'c'], 'c': ['x', 'd', 'f', 'v'],
    'v': ['c', 'f', 'g', 'b'], 'b': ['v', 'g', 'h', 'n'],
    'n': ['b', 'h', 'j', 'm'], 'm': ['n', 'j', 'k'],
}


def _nearby_key(ch: str) -> str:
    lower = ch.lower()
    neighbours = _QWERTY_NEIGHBORS.get(lower)
    if not neighbours:
        return ""
    pick = random.choice(neighbours)
    return pick.upper() if ch.isupper() else pick


# ---------------------------------------------------------------------------
# Public API — drop-in replacement for the original ShadowHackrs HumanBehavior.
# ---------------------------------------------------------------------------


class HumanBehavior:
    """Pure-Python humanized mouse + typing for Playwright pages.

    Public surface kept compatible with the original ShadowHackrs API
    consumed by the rest of the vendor (``stealth_browser.natural_type``,
    ``stealth_browser.natural_click``, ``warmup.WarmupEngine``):
      - ``generate_bezier_curve(start, end, num_points)`` -> list[(x, y)]
      - ``natural_type(page, selector, text, make_typos=True)``
      - ``human_scroll(page, min_scrolls=1, max_scrolls=3)``
    """

    @staticmethod
    def generate_bezier_curve(
        start_coords: Point,
        end_coords: Point,
        num_points: int = 20,
    ) -> List[Tuple[int, int]]:
        """Generate a list of integer-pixel waypoints on a humanized Bezier
        curve from ``start_coords`` to ``end_coords``.

        Uses asymmetric random control points so the path has a realistic
        ballistic/correction shape rather than a symmetric arc.
        """
        if num_points <= 1:
            return [(int(start_coords[0]), int(start_coords[1])),
                    (int(end_coords[0]), int(end_coords[1]))]

        cp1, cp2 = _random_control_points(start_coords, end_coords)
        points: List[Tuple[int, int]] = []
        # Equally-spaced t values in [0, 1] — pure Python, no numpy.
        for i in range(num_points):
            t = i / (num_points - 1)
            # Map raw t through min-jerk for a slow-fast-slow velocity profile.
            t_pos = _minimum_jerk(t)
            x, y = _bezier_2d(t_pos, start_coords, cp1, cp2, end_coords)
            points.append((int(round(x)), int(round(y))))
        return points

    @staticmethod
    async def natural_type(
        page,
        selector: str,
        text: str,
        make_typos: bool = True,
    ) -> bool:
        """Type ``text`` into ``selector`` with variable speed and
        occasional QWERTY-neighbour typos that are then corrected with
        Backspace.

        Returns True on success, False if the selector never appears.
        """
        try:
            element = await page.wait_for_selector(selector, timeout=5000)
            if not element:
                return False

            await element.click()
            await page.wait_for_timeout(random.randint(100, 300))

            # ~2% typo probability per character; ~0.5% in our pydoll math
            # but the original ShadowHackrs default was 2% so we keep that
            # as the user-visible behaviour. Caller can disable via make_typos.
            typo_prob = 0.02 if make_typos else 0.0

            for i, char in enumerate(text):
                if (
                    typo_prob > 0
                    and random.random() < typo_prob
                    and char.isalpha()
                ):
                    wrong = _nearby_key(char)
                    if wrong and wrong != char:
                        await page.keyboard.type(wrong)
                        await page.wait_for_timeout(random.randint(150, 350))
                        await page.keyboard.press("Backspace")
                        await page.wait_for_timeout(random.randint(80, 180))

                await page.keyboard.type(char)

                # Faster in the middle of words, slower at edges.
                delay = random.uniform(30, 120)
                if i == 0 or i == len(text) - 1:
                    delay += random.uniform(50, 150)
                await page.wait_for_timeout(int(delay))

            return True
        except Exception as e:
            logger.error("natural_type failed on %s: %s", selector, e)
            return False

    @staticmethod
    async def human_scroll(page, min_scrolls: int = 1, max_scrolls: int = 3) -> None:
        """Scroll the page a few times with random direction + magnitude."""
        scrolls = random.randint(min_scrolls, max_scrolls)
        for _ in range(scrolls):
            direction = 1 if random.random() > 0.2 else -1
            distance = random.randint(100, 500) * direction
            try:
                await page.mouse.wheel(0, distance)
            except Exception:
                pass
            await page.wait_for_timeout(random.randint(800, 2000))

    @staticmethod
    async def human_move_to(page, x: float, y: float, num_points: int = 24) -> None:
        """Move the mouse along a humanized Bezier from its current pos
        to (x, y). Uses page.mouse.move() for each waypoint.

        Note: Playwright doesn't expose current cursor position; we treat
        (0, 0) as a safe origin. Real callers should chain moves so the
        cursor is at a known point first.
        """
        try:
            start = (0.0, 0.0)
            end = (float(x), float(y))
            path = HumanBehavior.generate_bezier_curve(start, end, num_points)
            for px, py in path:
                await page.mouse.move(px, py)
                await asyncio.sleep(random.uniform(0.008, 0.020))
        except Exception as e:
            logger.debug("human_move_to fallback to direct move: %s", e)
            try:
                await page.mouse.move(x, y)
            except Exception:
                pass
