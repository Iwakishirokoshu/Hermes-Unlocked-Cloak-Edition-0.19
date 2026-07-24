"""UI helpers — slim version for Hermes vendor build.

The interactive TUI (banner, menu, progress, dashboard, etc.) was
removed: Hermes drives the factory non-interactively via
``hermes_runner.py`` and surfaces results through the agent. This module
keeps only the small surface that the rest of the vendor still imports:

  - ``console`` (Rich console used for child-process logging)
  - ``THEME`` (color dict; runners log through it)
  - ``print_success / print_error / print_warning / print_info``
  - ``show_session_summary`` (used by retry_engine/account_warmer when
    they finish a batch)
  - ``get_progress_context`` (returns a no-op context manager so existing
    ``with progress:`` blocks in runners.py keep working)

Anything else (show_banner, show_menu, ask_*, show_dashboard, show_settings,
show_accounts) was deleted — calling them would have been a UX dead-end
in a daemon process anyway.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

from rich.console import Console

if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# Send Rich output to stderr so stdout stays clean for JSON results
# coming out of hermes_runner.py.
console = Console(file=sys.stderr, force_terminal=False)


THEME = {
    "version": "vendor-slim",
    "app_name": "Gmail Factory (Hermes vendor)",
    "primary": "bright_cyan",
    "secondary": "bright_magenta",
    "accent": "bright_blue",
    "success": "bright_green",
    "error": "bright_red",
    "warning": "bright_yellow",
    "muted": "bright_black",
}


def print_success(msg: str) -> None:
    console.print(f"[{THEME['success']}]> {msg}[/]")


def print_error(msg: str) -> None:
    console.print(f"[{THEME['error']}]> {msg}[/]")


def print_warning(msg: str) -> None:
    console.print(f"[{THEME['warning']}]> {msg}[/]")


def print_info(msg: str) -> None:
    console.print(f"[{THEME['accent']}]> {msg}[/]")


def show_session_summary(total: int, successes: int, failures: int, duration_s: float) -> None:
    rate = (successes / total * 100) if total > 0 else 0
    console.print(f"\n[{THEME['primary']}]{'='*60}[/]")
    console.print("[bold]SESSION SUMMARY[/]")
    console.print(f"[{THEME['primary']}]{'='*60}[/]")
    console.print(f"  Total Attempts: {total}")
    console.print(f"  [{THEME['success']}]Successes: {successes}[/]")
    console.print(f"  [{THEME['error']}]Failures: {failures}[/]")
    console.print(f"  Success Rate: {rate:.1f}%")
    console.print(f"  Duration: {duration_s:.0f}s ({duration_s/60:.1f}m)")
    console.print(f"[{THEME['primary']}]{'='*60}[/]\n")


class _NullProgress:
    """No-op stand-in for rich.progress.Progress.

    runners.py / batch_runner.py call ``progress.add_task`` and
    ``progress.update`` while wrapping work in ``with get_progress_context()
    as progress:``. In Hermes-mode there is no terminal to render the bar
    into, so we accept the calls and discard them.
    """

    def add_task(self, description: str = "", total: float | None = None, **kwargs) -> int:  # noqa: ARG002
        return 0

    def update(self, task_id: int, **kwargs) -> None:  # noqa: ARG002
        return None

    def __enter__(self) -> "_NullProgress":
        return self

    def __exit__(self, *exc) -> None:
        return None


@contextmanager
def get_progress_context():
    yield _NullProgress()
