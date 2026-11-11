import json
from pathlib import Path

import sounddevice as sd
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "audio.json"


def load_audio_settings():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"mode": "system"}


class SettingsWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Misty Settings")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        title = QLabel("Misty settings")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(title)

        description = QLabel(
            "Configure how Misty interacts with your desktop. "
            "More settings will be added as the assistant develops."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        section = QLabel("AUDIO INPUT")
        section.setStyleSheet(
            "font-size: 10px; font-weight: 800; "
            "letter-spacing: 1px; color: #8994AE;"
        )
        layout.addWidget(section)
        layout.addWidget(QLabel("Microphone"))

        self.devices = QComboBox()
        self.devices.setMinimumHeight(36)
        layout.addWidget(self.devices)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh devices")
        refresh.clicked.connect(self.refresh_devices)
        save = QPushButton("Save settings")
        save.setDefault(True)
        save.clicked.connect(self.save_settings)

        buttons.addWidget(refresh)
        buttons.addStretch()
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.refresh_devices()

    def refresh_devices(self):
        previous = self.devices.currentData()
        if previous is None:
            previous = load_audio_settings()

        self.devices.clear()

        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            default_index = sd.default.device[0]

            default_name = "System default"
            if default_index is not None and default_index >= 0:
                default_name = sd.query_devices(default_index)["name"]

            self.devices.addItem(
                f"System default — {default_name}",
                {"mode": "system"},
            )

            for device in devices:
                if device["max_input_channels"] <= 0:
                    continue

                api = hostapis[device["hostapi"]]["name"]
                value = {
                    "mode": "device",
                    "name": device["name"],
                    "hostapi": api,
                }
                label = (
                    f'{device["name"]} — {api} '
                    f'({device["max_input_channels"]} input channels)'
                )
                self.devices.addItem(label, value)

            selected = self.devices.findData(previous)
            if selected >= 0:
                self.devices.setCurrentIndex(selected)

            self.status.setText(f"Detected {len(devices)} audio devices.")

        except Exception as error:
            self.status.setText(f"Could not enumerate audio devices: {error}")

    def save_settings(self):
        selected = self.devices.currentData()
        if not selected:
            return

        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temporary = CONFIG.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(selected, indent=4) + "\n",
                encoding="utf-8",
            )
            temporary.replace(CONFIG)
            self.status.setText(
                "Microphone settings saved. They apply to the next recording."
            )
        except OSError as error:
            QMessageBox.critical(
                self, "Misty settings", f"Could not save settings:\n{error}"
            )
