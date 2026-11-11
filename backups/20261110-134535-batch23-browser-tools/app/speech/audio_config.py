import json
import sounddevice as sd

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "audio.json"


def get_input_device():
    try:
        settings = json.loads(CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {"mode": "system"}

    if settings.get("mode") != "device":
        print("[Misty] Using system default microphone.")
        return None

    target_name = settings.get("name")
    target_api = settings.get("hostapi")

    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    for index, device in enumerate(devices):
        api = hostapis[device["hostapi"]]["name"]

        if (
            device["name"] == target_name
            and api == target_api
            and device["max_input_channels"] > 0
        ):
            print(f"[Misty] Using microphone: {target_name}")
            return index

    print(
        "[Misty] Saved microphone unavailable. "
        "Falling back to system default."
    )

    return None
