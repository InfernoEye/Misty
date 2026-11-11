"""Small, validated user-settings helpers shared by the UI and engine."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHORTCUT_CONFIG = ROOT / "config" / "shortcut.json"
DEFAULT_SHORTCUT = "F8"
MAX_SHORTCUT_LENGTH = 64


def _valid_shortcut(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_SHORTCUT_LENGTH
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def load_shortcut():
    try:
        data = json.loads(SHORTCUT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_SHORTCUT
    value = data.get("sequence") if isinstance(data, dict) else None
    return value.strip() if _valid_shortcut(value) else DEFAULT_SHORTCUT


def save_shortcut(sequence):
    if not _valid_shortcut(sequence):
        raise ValueError("Shortcut must be 1 to 64 printable characters.")
    SHORTCUT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = SHORTCUT_CONFIG.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"sequence": sequence.strip()}, indent=4) + "\n",
        encoding="utf-8",
    )
    temporary.replace(SHORTCUT_CONFIG)
