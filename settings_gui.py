import json
import sounddevice as sd

from pathlib import Path
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QPushButton,
    QMessageBox,
)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config" / "audio.json"


def load_settings():
    try:
        return json.loads(CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"mode": "system"}


class AudioSettings(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Misty - Audio Settings")
        self.setMinimumWidth(480)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(14)

        title = QLabel("Misty microphone settings")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")

        description = QLabel(
            "Choose which microphone Misty should use. "
            "System default follows your KDE/PipeWire audio settings."
        )
        description.setWordWrap(True)

        layout.addWidget(title)
        layout.addWidget(description)

        layout.addWidget(QLabel("Input device:"))

        self.devices = QComboBox()
        self.devices.setMinimumWidth(420)
        layout.addWidget(self.devices)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()

        refresh = QPushButton("Refresh devices")
        refresh.clicked.connect(self.refresh_devices)

        save = QPushButton("Save settings")
        save.clicked.connect(self.save_settings)

        buttons.addWidget(refresh)
        buttons.addWidget(save)

        layout.addLayout(buttons)

        self.setCentralWidget(container)

        self.refresh_devices()

    def refresh_devices(self):
        previous = self.devices.currentData()
        self.devices.clear()

        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()

            default_index = sd.default.device[0]

            if default_index is not None and default_index >= 0:
                default_info = sd.query_devices(default_index)
                default_name = default_info["name"]
            else:
                default_name = "Not currently available"

            self.devices.addItem(
                f"System default — {default_name}",
                {"mode": "system"}
            )

            for index, device in enumerate(devices):
                if device["max_input_channels"] <= 0:
                    continue

                api = hostapis[device["hostapi"]]["name"]

                config = {
                    "mode": "device",
                    "name": device["name"],
                    "hostapi": api,
                }

                label = (
                    f'{device["name"]} — {api} '
                    f'({device["max_input_channels"]} input channels)'
                )

                self.devices.addItem(label, config)

            selected = self.devices.findData(previous)

            if selected >= 0:
                self.devices.setCurrentIndex(selected)

            elif previous and previous.get("mode") == "device":
                self.status.setText(
                    "Previously selected microphone is unavailable. "
                    "System default is selected instead."
                )
            else:
                self.status.setText(
                    f"Detected {len(devices)} audio devices."
                )

        except Exception as error:
            QMessageBox.critical(
                self,
                "Audio error",
                str(error)
            )

    def save_settings(self):
        selected = self.devices.currentData()

        if not selected:
            return

        CONFIG.parent.mkdir(parents=True, exist_ok=True)

        CONFIG.write_text(
            json.dumps(selected, indent=4) + "\n"
        )

        self.status.setText("Audio settings saved successfully.")

        QMessageBox.information(
            self,
            "Misty",
            "Microphone settings saved."
        )


if __name__ == "__main__":
    app = QApplication([])
    window = AudioSettings()
    window.show()
    app.exec()
