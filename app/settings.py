"""Small, validated user-settings helpers shared by the UI and engine."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHORTCUT_CONFIG = ROOT / "config" / "shortcut.json"
DEFAULT_SHORTCUT = "F8"
MAX_SHORTCUT_LENGTH = 64


_LOCALIZED_MEDIA_KEYS = {
    "uruchom": "Launch",
    "odtworz": "Play",
    "odtwórz": "Play",
    "wstrzymaj": "Pause",
    "nastepny": "Next",
    "następny": "Next",
    "poprzedni": "Previous",
    "zatrzymaj": "Stop",
    "wycisz": "Volume Mute",
    "glosniej": "Volume Up",
    "głośniej": "Volume Up",
    "ciszej": "Volume Down",
}


def normalize_shortcut(value):
    """Convert a locale-dependent key name to Qt's portable spelling."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = re.fullmatch(r"(?i)uruchom\s*\(\s*(\d+)\s*\)", text)
    if match:
        return f"Launch ({match.group(1)})"
    for localized, portable in _LOCALIZED_MEDIA_KEYS.items():
        if text.casefold() == localized.casefold():
            return portable
    return text


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
    if not _valid_shortcut(value):
        return DEFAULT_SHORTCUT
    return normalize_shortcut(value.strip())


def save_shortcut(sequence):
    sequence = normalize_shortcut(sequence)
    if not _valid_shortcut(sequence):
        raise ValueError("Shortcut must be 1 to 64 printable characters.")
    SHORTCUT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = SHORTCUT_CONFIG.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"sequence": sequence.strip()}, indent=4) + "\n",
        encoding="utf-8",
    )
    temporary.replace(SHORTCUT_CONFIG)


def load_desktop_backend(*, path=None, environ=None):
    """Explicit backend selection; malformed settings never fall back to input.

    Default remains legacy. Cua is a read-only capability gate; kwin is an
    explicitly selected native pilot whose controller owns capability/approval
    checks. Selecting either never authorizes legacy pointer-tool fallback.
    """
    environ = os.environ if environ is None else environ
    value = environ.get("MISTY_DESKTOP_BACKEND")
    if value is None:
        location = ROOT / "config" / "desktop.json" if path is None else Path(path)
        try:
            if location.stat().st_size > 4096:
                raise ValueError("Desktop settings exceed their size limit.")
            data = json.loads(location.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"backend": "legacy"}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Desktop backend settings could not be read.") from exc
        if not isinstance(data, dict) or set(data) - {"backend"}:
            raise ValueError("Desktop backend settings are malformed.")
        value = data.get("backend", "legacy")
    if not isinstance(value, str) or value.strip().casefold() not in {"legacy", "cua", "kwin"}:
        raise ValueError("Desktop backend must be explicitly legacy, cua or kwin.")
    return value.strip().casefold()
