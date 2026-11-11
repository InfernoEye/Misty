import sys
from pathlib import Path

# Add the Misty project root to Python's import path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.intent_parser import parse_intent


TESTS = [
    ("Open Firefox", "open_application"),
    ("Open the browser", "open_default_browser"),
    ("Open Downloads", "open_folder"),
    ("Open terminal", "open_application"),
    ("What's the CPU temperature?", "unknown"),
    ("Turn up the volume", "volume_up"),
    ("Turn down the volume", "volume_down"),
    ("Mute", "volume_mute"),
    ("Close window", "close_active_window"),
    ("What time is it?", "get_time"),
    ("Otwórz przeglądarkę", "open_default_browser"),
    ("Otwórz Firefoksa", "unknown"),
    ("Open default browser", "open_default_browser"),
    ("Open web browser", "open_default_browser"),
    ("Otwórz terminal", "open_application"),
    ("Otwórz Pobrane", "open_folder"),
    ("Zwiększ głośność", "volume_up"),
    ("Zmniejsz głośność", "volume_down"),
    ("Wycisz", "volume_mute"),
    ("Zamknij okno", "close_active_window"),
    ("Która godzina?", "get_time"),
    ("Misty", "unknown"),
]


def main():
    passed = 0

    for phrase, expected in TESTS:
        parsed = parse_intent(phrase)
        actual = parsed["intent"]

        success = actual == expected

        print(
            f"{'PASS' if success else 'FAIL'} | "
            f"{phrase:35} | {actual}"
        )

        if success:
            passed += 1

    print(f"\nPassed: {passed}/{len(TESTS)}")

    if passed != len(TESTS):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
