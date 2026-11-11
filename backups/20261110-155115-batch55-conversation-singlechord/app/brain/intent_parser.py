import re
import unicodedata


APPLICATIONS = {
    "firefox": "firefox",
    "mozilla firefox": "firefox",
    "chrome": "google-chrome-stable",
    "chromium": "chromium",
    "terminal": "konsole",
    "konsole": "konsole",
    "dolphin": "dolphin",
    "discord": "discord",
    "steam": "steam",
    "spotify": "spotify",
    "code": "code",
}

FOLDERS = {
    "home": "~",
    "downloads": "~/Pobrane",
    "download": "~/Pobrane",
    "documents": "~/Documents",
    "desktop": "~/Desktop",
    "pictures": "~/Pictures",
    "music": "~/Music",
    "videos": "~/Videos",
    "pobrane": "~/Pobrane",
    "dokumenty": "~/Documents",
    "pulpit": "~/Desktop",
    "obrazy": "~/Pictures",
    "muzyka": "~/Music",
    "wideo": "~/Videos",
    "katalog domowy": "~",
}

BROWSERS = {
    "browser",
    "the browser",
    "web browser",
    "the web browser",
    "default browser",
    "internet",
    "przeglądarkę",
    "przegladarke",
    "przeglądarka",
    "przegladarka",
    "domyślną przeglądarkę",
    "domyslna przegladarke",
}


def normalize(text):
    text = text.lower().strip()
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[.!?,;:]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def result(intent, requires_confirmation=False, **kwargs):
    return {
        "intent": intent,
        "arguments": kwargs,
        "requires_confirmation": requires_confirmation,
    }


def parse_intent(transcript):
    text = normalize(transcript)

    if not text:
        return result("unknown", original_text=transcript)

    # Time
    if re.search(
        r"\b(what time is it|tell me the time|current time|"
        r"która godzina|ktora godzina|podaj godzinę|podaj godzine)\b",
        text,
    ):
        return result("get_time")

    # System status
    if re.search(
        r"\b(system status|system information|"
        r"how is my computer|"
        r"stan systemu|informacje o systemie|"
        r"jak działa komputer|jak dziala komputer)\b",
        text,
    ):
        return result("get_system_status")

    # Volume
    if re.search(
        r"\b(turn up the volume|increase volume|volume up|"
        r"zwiększ głośność|zwieksz glosnosc|"
        r"podgłośnij|podglosnij)\b",
        text,
    ):
        return result("volume_up")

    if re.search(
        r"\b(turn down the volume|decrease volume|volume down|"
        r"zmniejsz głośność|zmniejsz glosnosc|"
        r"ścisz|scisz)\b",
        text,
    ):
        return result("volume_down")

    if re.search(
        r"\b(mute|mute volume|silence volume|"
        r"wycisz|wycisz dźwięk|wycisz dzwiek)\b",
        text,
    ):
        return result("volume_mute")

    # Close active window
    if re.fullmatch(
        r"(close window|close this window|"
        r"zamknij okno|zamknij to okno)",
        text,
    ):
        return result(
            "close_active_window",
            requires_confirmation=True,
        )

    # Open application or folder
    match = re.fullmatch(
        r"(?:open|launch|start|"
        r"otwórz|otworz|uruchom|włącz|wlacz)"
        r"\s+(.+)",
        text,
    )

    if match:
        target = match.group(1).strip()

        if target in BROWSERS:
            return result("open_default_browser")

        if target in APPLICATIONS:
            return result(
                "open_application",
                application=APPLICATIONS[target],
            )

        if target in FOLDERS:
            return result(
                "open_folder",
                path=FOLDERS[target],
            )

        return result(
            "unknown",
            original_text=transcript,
        )

    return result(
        "unknown",
        original_text=transcript,
    )
