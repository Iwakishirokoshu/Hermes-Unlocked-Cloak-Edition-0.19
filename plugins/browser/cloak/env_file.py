"""Shared helpers for reading Cloak env files (BOM-safe)."""
from __future__ import annotations

from typing import Dict


def parse_env_file(path: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Strips UTF-8 BOM from the first key if present."""
    out: Dict[str, str] = {}
    # utf-8-sig strips a leading BOM transparently on the first line.
    with open(path, encoding="utf-8-sig") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lstrip("\ufeff")
            value = value.strip().strip('"').strip("'")
            if key:
                out[key] = value
    return out
