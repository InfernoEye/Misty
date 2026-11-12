import html
import json
import os
import socket
import tomllib
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlsplit

import sounddevice as sd
from PySide6.QtCore import QEvent, Qt, QSize, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
    QListWidget, QListWidgetItem, QScrollArea, QTextBrowser,
    QKeySequenceEdit, QGridLayout,
)

ROOT = Path(__file__).resolve().parents[2]
AUDIO_CONFIG = ROOT / "config" / "audio.json"

from app.engine.activity_store import ActivityStore
from app.settings import load_shortcut, save_shortcut
from app.ui.task_card import TaskCard


STYLE = """
QMainWindow, QWidget#root {
    background: #11151e;
    color: #e8edf7;
    font-family: Inter, sans-serif;
}
QFrame#sidebar {
    background: #171d29;
    border-right: 1px solid #293247;
}
QLabel { color: #e8edf7; }
QLabel#muted { color: #8d9ab2; }
QLabel#brand {
    font-size: 21px;
    font-weight: 700;
    color: #f1f4ff;
}
QPushButton#nav, QPushButton#settingNav {
    outline: none;
    background: transparent;
    color: #aebbd1;
    border: none;
    border-radius: 9px;
    padding: 11px 14px;
    text-align: left;
    font-size: 14px;
}
QPushButton#nav:hover, QPushButton#settingNav:hover {
    background: #242d40;
    color: white;
}
QPushButton#nav[selected="true"],
QPushButton#settingNav[selected="true"] {
    background: #293957;
    color: white;
}
QFrame#topbar {
    background: #151b27;
    border-bottom: 1px solid #293247;
}
QFrame#contentCard {
    background: #192130;
    border: 1px solid #29364d;
    border-radius: 16px;
}
QLabel#pageTitle {
    font-size: 25px;
    font-weight: 650;
}
QLabel#heroTitle {
    font-size: 30px;
    font-weight: 650;
}
QLabel#status {
    color: #9bc8ad;
    background: #1c332c;
    border-radius: 10px;
    padding: 6px 10px;
    font-size: 12px;
}
QPushButton#primary {
    background: #718dce;
    color: #101827;
    border: none;
    border-radius: 9px;
    padding: 10px 18px;
    font-weight: 650;
}
QPushButton#primary:hover { background: #89a5e8; }
QComboBox {
    background: #111925;
    color: #e8edf7;
    border: 1px solid #34415a;
    border-radius: 8px;
    padding: 10px;
    min-height: 24px;
}
QComboBox QAbstractItemView {
    background: #192130;
    color: #e8edf7;
    selection-background-color: #344b70;
}
QPushButton#secondary {
    background: #263247;
    color: #dce5f5;
    border: 1px solid #3a4962;
    border-radius: 8px;
    padding: 9px 14px;
}
QKeySequenceEdit {
    background: #111925;
    color: #e8edf7;
    border: 1px solid #34415a;
    border-radius: 8px;
    padding: 8px 12px;
    min-height: 28px;
    font-size: 14px;
    outline: none;
}
QKeySequenceEdit:focus {
    border: 1px solid #4d6fa8;
}
QKeySequenceEdit QLineEdit {
    background: #111925;
    color: #e8edf7;
    border: none;
    min-height: 26px;
    padding: 4px 2px;
    font-size: 14px;
}
QScrollArea {
    background: transparent;
    border: none;
}
QWidget#scrollPage {
    background: #11151e;
}
QScrollBar:vertical {
    background: #11151e;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #34415a;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
"""


class ShortcutKeySequenceEdit(QKeySequenceEdit):
    """A key-sequence field that ignores OS auto-repeat while recording."""

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            event.accept()
            return
        super().keyReleaseEvent(event)


def load_audio_settings():
    try:
        return json.loads(AUDIO_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"mode": "system"}


class MainWindow(QMainWindow):
    text_submitted = Signal(str)
    shortcut_changed = Signal(str)
    #: Explicit Apply/Retry of the saved shortcut (restarts the helper even
    #: when the saved value did not change).
    shortcut_apply_requested = Signal(str)
    #: Recording starts/stops; the global binding is suspended meanwhile.
    shortcut_recording_started = Signal()
    shortcut_recording_finished = Signal()
    #: "Forget conversation context": main.py clears the session context.
    forget_context_requested = Signal()
    def __init__(self):
        super().__init__()
        self.activity_store = ActivityStore()
        self.activity_list = None
        self._last_typed_text = ""
        self._shortcut_recording = False
        self._shortcut_adjusting = False
        self._shortcut_before_record = ""
        self._shortcut_suspended = False
        self._shortcut_applying = False
        self._shortcut_pending_save = None
        self._shortcut_status_override = ""
        self.chat_view = None
        self.chat_input = None
        self.shortcut_hint = None
        #: Task progress card shown above the chat input (see TaskCard).
        self.task_card = None
        self.setWindowTitle("Misty Desktop")
        self.setMinimumSize(950, 650)
        self.resize(1120, 740)
        self.setStyleSheet(STYLE)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_sidebar())

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.section_title = QLabel("Chat")
        self.section_title.setStyleSheet(
            "font-size: 16px; font-weight: 600;"
        )
        right_layout.addWidget(self._build_topbar())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_chat_page())
        self.pages.addWidget(self._build_activity_page())
        self.pages.addWidget(self._build_settings_page())

        right_layout.addWidget(self.pages, 1)
        outer.addWidget(right, 1)

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(17, 25, 17, 18)
        layout.setSpacing(9)

        brand = QLabel("✦  Misty")
        brand.setObjectName("brand")
        layout.addWidget(brand)

        subtitle = QLabel("YOUR LOCAL AI ASSISTANT")
        subtitle.setObjectName("muted")
        subtitle.setStyleSheet("font-size: 9px; letter-spacing: 1.4px;")
        layout.addWidget(subtitle)
        layout.addSpacing(30)

        self.nav_buttons = []
        for label, page in (
            ("◉   Chat", 0),
            ("◷   Activity", 1),
            ("⚙   Settings", 2),
        ):
            button = QPushButton(label)
            button.setObjectName("nav")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, p=page: self.show_page(p))
            layout.addWidget(button)
            self.nav_buttons.append(button)

        layout.addStretch()

        version = QLabel("Misty · Local edition")
        version.setObjectName("muted")
        version.setStyleSheet("font-size: 11px;")
        layout.addWidget(version)
        return sidebar

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(66)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(27, 0, 27, 0)
        layout.addWidget(self.section_title)
        layout.addStretch()

        status = QLabel("●  LOCAL")
        status.setObjectName("status")
        status.setFixedHeight(30)
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(status)
        return bar

    def show_task_card(self):
        """Show the task card now (main.py, on task_started)."""
        if self.task_card is not None:
            self.task_card.show()
            self.task_card.raise_()

    def forget_conversation_context(self):
        """Emit the forget request (main.py owns the controller).

        The controller clears its ``SessionContext``; the button only
        announces the request, so no stored history is touched.
        """
        self.forget_context_requested.emit()

    def show_page(self, page):
        self.pages.setCurrentIndex(page)
        titles = ("Chat", "Activity", "Settings")
        self.section_title.setText(titles[page])

        for index, button in enumerate(self.nav_buttons):
            button.setProperty("selected", index == page)
            button.style().unpolish(button)
            button.style().polish(button)

    def _build_chat_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(42, 30, 42, 24)
        layout.setSpacing(14)

        heading = QLabel("Chat with Misty")
        heading.setObjectName("heroTitle")
        layout.addWidget(heading)

        description = QLabel(
            "Type a request or hold the voice shortcut. Answers stay local; "
            "long results open in the full-response window."
        )
        description.setObjectName("muted")
        description.setWordWrap(True)
        description.setStyleSheet("font-size: 13px;")
        layout.addWidget(description)

        self.chat_view = QTextBrowser()
        self.chat_view.setReadOnly(True)
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setStyleSheet("""
            QTextBrowser {
                background: #192130;
                border: 1px solid #29364d;
                border-radius: 14px;
                padding: 16px;
                color: #e8edf7;
                font-size: 14px;
            }
        """)
        layout.addWidget(self.chat_view, 1)

        # Progress of the request in flight: steps, elapsed time, Cancel and
        # Retry. It is a pure view; main.py wires it to the controller.
        self.task_card = TaskCard()
        layout.addWidget(self.task_card)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText(
            "Ask Misty to open something, research, list files, summarize…"
        )
        self.chat_input.setMinimumHeight(46)
        self.chat_input.setStyleSheet("""
            QLineEdit {
                background: #111925;
                color: #e8edf7;
                border: 1px solid #34415a;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 14px;
            }
        """)
        self.chat_input.returnPressed.connect(self.send_chat_message)
        row.addWidget(self.chat_input, 1)

        send = QPushButton("Send")
        send.setObjectName("primary")
        send.setMinimumHeight(46)
        send.clicked.connect(self.send_chat_message)
        row.addWidget(send)
        layout.addLayout(row)

        self.shortcut_hint = QLabel()
        self.shortcut_hint.setObjectName("muted")
        self.shortcut_hint.setStyleSheet(
            "font-size: 11px; letter-spacing: 0.5px;"
        )
        layout.addWidget(self.shortcut_hint)
        self.refresh_shortcut_hint()

        self.append_chat_message(
            "Misty",
            "I'm ready. Ask me to open a page, research something, "
            "summarize what you're seeing, or work with your local files.",
            "#8fb3ff",
        )
        return page

    def append_chat_message(self, author, text, color="#8fb3ff"):
        if self.chat_view is None:
            return
        safe = html.escape(str(text)).replace("\n", "<br>")
        self.chat_view.append(
            f'<div style="margin-bottom:10px;">'
            f'<span style="color:{color}; font-weight:700;">{html.escape(author)}</span>'
            f'<br><span>{safe}</span></div>'
        )
        bar = self.chat_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_user_message(self, text):
        self.append_chat_message("You", text, "#9bc8ad")

    def append_assistant_message(self, text):
        self.append_chat_message("Misty", text, "#8fb3ff")

    def append_assistant_error(self, text):
        """Show a failure in chat (never only in the popup/state)."""
        message = str(text or "").strip()
        if not message:
            return
        self.append_chat_message("Misty · error", message, "#f0a07c")

    def restore_chat_input(self, text):
        """Put a rejected request back so the user can retry it."""
        if self.chat_input is None:
            return
        self._last_typed_text = ""
        if str(text or "").strip():
            self.chat_input.setText(str(text))
            self.chat_input.setFocus()

    def focus_chat_reply_target(self):
        if self.chat_input is not None:
            self.chat_input.setFocus()

    def append_voice_transcript(self, text):
        if not isinstance(text, str) or not text.strip():
            return
        typed = self._last_typed_text
        self._last_typed_text = ""
        if typed and text.strip() == typed:
            return
        self.append_user_message(text.strip())

    def send_chat_message(self):
        if self.chat_input is None:
            return
        text = self.chat_input.text().strip()
        if not text:
            return
        # Do not echo the bubble or clear the input until the controller
        # admits the request; a rejection restores this text.
        self._last_typed_text = text
        self.chat_input.clear()
        self.text_submitted.emit(text)

    @staticmethod
    def _display_shortcut(sequence):
        parsed = QKeySequence(sequence)
        return (
            parsed.toString(QKeySequence.SequenceFormat.NativeText).strip()
            or str(sequence).strip()
        )

    def refresh_shortcut_hint(self):
        if self.shortcut_hint is not None:
            self.shortcut_hint.setText(
                "QUICK VOICE ACCESS     Hold "
                f"{self._display_shortcut(load_shortcut())} to talk"
            )

    def _build_activity_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(42, 35, 42, 30)
        layout.setSpacing(16)

        heading = QLabel("Activity")
        heading.setObjectName("heroTitle")
        layout.addWidget(heading)

        description = QLabel(
            "A local history of what Misty hears, decides to do and completes."
        )
        description.setObjectName("muted")
        description.setWordWrap(True)
        layout.addWidget(description)

        self.activity_list = QListWidget()
        self.activity_list.setWordWrap(True)
        self.activity_list.setSpacing(7)
        self.activity_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.activity_list.verticalScrollBar().setSingleStep(18)
        self.activity_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.activity_list.setStyleSheet("""
            QListWidget {
                background: #192130;
                border: 1px solid #29364d;
                border-radius: 14px;
                padding: 12px;
                outline: none;
            }
            QListWidget::item {
                background: #202b3d;
                color: #e8edf7;
                border-radius: 9px;
                padding: 12px;
                margin: 3px;
            }
            QListWidget::item:selected {
                background: #293957;
            }
        """)
        layout.addWidget(self.activity_list, 1)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.activity_purge_button = QPushButton("Delete older than 30 days")
        self.activity_purge_button.setObjectName("secondary")
        self.activity_purge_button.setToolTip(
            "Remove old local activity entries. Live data and settings are "
            "untouched."
        )
        self.activity_purge_button.clicked.connect(self.purge_old_activity)
        controls.addWidget(self.activity_purge_button)

        self.activity_clear_button = QPushButton("Clear activity history")
        self.activity_clear_button.setObjectName("secondary")
        self.activity_clear_button.setToolTip(
            "Delete every locally stored activity entry, including this list."
        )
        self.activity_clear_button.clicked.connect(self.clear_activity_history)
        controls.addWidget(self.activity_clear_button)

        # Forgetting the conversation context touches no stored history; it
        # only drops the in-memory references ("the one from May", "open it").
        self.forget_context_button = QPushButton("Forget conversation context")
        self.forget_context_button.setObjectName("secondary")
        self.forget_context_button.setToolTip(
            "Forget the last few requests and answers used to resolve "
            "follow-ups such as \"open it\". This is done at shutdown as well."
        )
        self.forget_context_button.clicked.connect(
            self.forget_conversation_context
        )
        controls.addWidget(self.forget_context_button)
        controls.addStretch()
        layout.addLayout(controls)

        self._refresh_activity()
        return page

    #: Days of activity history the "delete older" button keeps.
    ACTIVITY_RETENTION_DAYS = 30

    def purge_old_activity(self):
        """Delete activity older than :attr:`ACTIVITY_RETENTION_DAYS`.

        ``purge()`` without an argument falls back to the store's configured
        retention, which is unset by default, so the window is passed here.
        """
        try:
            removed = self.activity_store.purge(
                self.ACTIVITY_RETENTION_DAYS
            )
        except Exception as error:
            print(f"[Misty] Activity purge failed: {error}", flush=True)
            return 0
        self._refresh_activity()
        return removed

    def clear_activity_history(self):
        """Delete every stored activity entry after an explicit confirmation."""
        answer = QMessageBox.question(
            self,
            "Clear activity history?",
            "This permanently deletes the local activity history. Your files, "
            "memory entries and settings are not affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return 0
        try:
            removed = self.activity_store.clear()
        except Exception as error:
            print(f"[Misty] Activity clear failed: {error}", flush=True)
            return 0
        self._refresh_activity()
        return removed

    def add_activity_event(self, event):
        """Persist an event and refresh the visible timeline."""
        self.activity_store.add_event(event)
        self._refresh_activity()

    def _refresh_activity(self):
        if self.activity_list is None:
            return

        self.activity_list.clear()
        events = self.activity_store.recent_events(100)

        def readable_details(raw_details):
            details = (raw_details or "").strip()
            details = details.replace("\\n", "\n")

            if not details:
                return ""

            if details.startswith("Arguments:"):
                arguments_text, separator, result_text = details.partition(
                    "\nResult:"
                )
                arguments_raw = arguments_text[len("Arguments:"):].strip()

                try:
                    arguments = json.loads(arguments_raw)
                    arguments_display = (
                        "No arguments" if not arguments
                        else json.dumps(arguments, ensure_ascii=False, indent=2)
                    )
                except (ValueError, TypeError):
                    arguments_display = arguments_raw

                parts = ["Arguments", arguments_display]

                if separator:
                    try:
                        result = json.loads(result_text.strip())
                        if isinstance(result, dict):
                            result_display = result.get("message") or json.dumps(
                                result, ensure_ascii=False, indent=2
                            )
                        else:
                            result_display = json.dumps(
                                result, ensure_ascii=False, indent=2
                            )
                    except (ValueError, TypeError):
                        result_display = result_text.strip()

                    parts.extend(["", "Result", result_display])

                return "\n".join(parts)

            return details

        def add_selectable_entry(text, height=90):
            item = QListWidgetItem()
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            label.setContentsMargins(12, 0, 12, 7)
            label.setStyleSheet(
                "QLabel { background: transparent; color: #e8edf7; }"
                "QLabel::selection { background: #45618d; color: #ffffff; }"
            )

            # Measure every wrapped line before assigning the card height.
            available_width = max(
                120, self.activity_list.viewport().width()
            )
            text_width = max(80, available_width - 24)

            bounds = label.fontMetrics().boundingRect(
                0, 0, text_width, 100000,
                int(Qt.TextFlag.TextWordWrap),
                text,
            )

            # Generous vertical padding prevents text from touching or
            # overflowing the card background.
            required_height = max(120, height, bounds.height() + 60)

            label.setGeometry(
                0, -10, available_width, required_height
            )

            self.activity_list.addItem(item)
            self.activity_list.setItemWidget(item, label)
            item.setSizeHint(QSize(available_width, required_height))
            item.setToolTip(text)

        if not events:
            add_selectable_entry(
                "Nothing here yet\n\n"
                f"Hold {self._display_shortcut(load_shortcut())} and "
                "talk to Misty. Your requests and actions will appear here.",
                90,
            )
            return

        for event in events:
            timestamp = event["timestamp"].replace("T", " ")
            status = event["status"].upper()
            title = event["title"]
            details = readable_details(event.get("details", ""))

            text = f"{title}    ·    {status}\n{timestamp}"
            if details:
                text += f"\n\n{details}"

            index = self.activity_list.count()
            add_selectable_entry(text)

            item = self.activity_list.item(index)
            item.setData(Qt.ItemDataRole.UserRole, event)

    def _build_settings_page(self):
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(28, 25, 28, 25)
        outer.setSpacing(24)

        categories = QVBoxLayout()
        categories.setSpacing(7)
        categories.setContentsMargins(0, 5, 0, 0)

        self.settings_pages = QStackedWidget()
        self.settings_nav = []

        entries = [
            ("Voice", self._build_voice_settings),
            ("AI && Models", self._build_ai_models_settings),
            ("Privacy && Permissions", self._build_privacy_settings),
            ("Integrations", self._build_integrations_settings),
        ]

        for index, (name, builder) in enumerate(entries):
            button = QPushButton(name)
            button.setObjectName("settingNav")
            button.setMinimumWidth(175)
            button.clicked.connect(
                lambda checked=False, i=index: self.show_settings_category(i)
            )
            categories.addWidget(button)
            self.settings_nav.append(button)
            self.settings_pages.addWidget(builder())

        categories.addStretch()
        outer.addLayout(categories)
        outer.addWidget(self.settings_pages, 1)

        self.show_settings_category(0)
        return page

    def show_settings_category(self, index):
        self.settings_pages.setCurrentIndex(index)
        for i, button in enumerate(self.settings_nav):
            button.setProperty("selected", i == index)
            button.style().unpolish(button)
            button.style().polish(button)

    def _section_card(self, title, description):
        card = QFrame()
        card.setObjectName("contentCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(23, 22, 23, 22)
        layout.setSpacing(9)

        heading = QLabel(title)
        heading.setStyleSheet("font-size: 18px; font-weight: 650;")
        layout.addWidget(heading)

        text = QLabel(description)
        text.setObjectName("muted")
        text.setWordWrap(True)
        layout.addWidget(text)
        return card, layout

    def _build_voice_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(17)

        heading = QLabel("Voice")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        intro = QLabel(
            "Configure how Misty listens to you. Changes to the microphone "
            "apply to the next recording."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        shortcut_card, shortcut_layout = self._section_card(
            "Voice shortcut",
            "Hold this key anywhere to talk. Click Record, press one key or "
            "chord, then Save. KDE global shortcuts support one chord, not "
            "sequential multi-step keys."
        )
        shortcut_layout.addSpacing(5)
        shortcut_row = QHBoxLayout()
        self.shortcut_edit = ShortcutKeySequenceEdit(
            QKeySequence(load_shortcut())
        )
        self.shortcut_edit.setMinimumHeight(44)
        self.shortcut_edit.setMaximumSequenceLength(1)
        self.shortcut_edit.editingFinished.connect(
            self._finish_shortcut_recording
        )
        self.shortcut_edit.keySequenceChanged.connect(
            self._shortcut_key_changed
        )
        self.shortcut_edit.installEventFilter(self)
        for child in self.shortcut_edit.findChildren(QLineEdit):
            child.installEventFilter(self)
        shortcut_row.addWidget(self.shortcut_edit, 1)
        shortcut_layout.addLayout(shortcut_row)

        shortcut_buttons = QHBoxLayout()
        self.shortcut_record = QPushButton("Record")
        self.shortcut_record.setObjectName("secondary")
        self.shortcut_record.clicked.connect(self.toggle_shortcut_recording)
        shortcut_buttons.addWidget(self.shortcut_record)
        shortcut_save = QPushButton("Save shortcut")
        shortcut_save.setObjectName("primary")
        shortcut_save.clicked.connect(self.save_shortcut_setting)
        shortcut_buttons.addWidget(shortcut_save)
        self.shortcut_apply = QPushButton("Apply / Retry")
        self.shortcut_apply.setObjectName("secondary")
        self.shortcut_apply.setToolTip(
            "Restart the KDE helper and bind the saved shortcut again."
        )
        self.shortcut_apply.clicked.connect(self.apply_shortcut_setting)
        self.shortcut_apply.setVisible(False)
        shortcut_buttons.addWidget(self.shortcut_apply)
        shortcut_buttons.addStretch()
        shortcut_layout.addLayout(shortcut_buttons)
        self.shortcut_status = QLabel(
            "Current shortcut: "
            + self._display_shortcut(load_shortcut())
        )
        self.shortcut_status.setObjectName("muted")
        self.shortcut_status.setWordWrap(True)
        shortcut_layout.addWidget(self.shortcut_status)
        layout.addWidget(shortcut_card)

        card, card_layout = self._section_card(
            "Audio input",
            "Choose which microphone Misty uses for voice commands."
        )
        card_layout.addSpacing(5)

        label = QLabel("Microphone device")
        label.setStyleSheet("font-weight: 600;")
        card_layout.addWidget(label)

        self.devices = QComboBox()
        self.devices.setMinimumHeight(42)
        card_layout.addWidget(self.devices)

        self.audio_status = QLabel("")
        self.audio_status.setObjectName("muted")
        self.audio_status.setWordWrap(True)
        card_layout.addWidget(self.audio_status)

        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh devices")
        refresh.setObjectName("secondary")
        refresh.clicked.connect(self.refresh_devices)

        test = QPushButton("Test input")
        test.setObjectName("secondary")
        test.clicked.connect(self.test_audio_device)

        save = QPushButton("Save changes")
        save.setObjectName("primary")
        save.clicked.connect(self.save_audio_settings)

        buttons.addWidget(refresh)
        buttons.addWidget(test)
        buttons.addStretch()
        buttons.addWidget(save)
        card_layout.addLayout(buttons)

        layout.addWidget(card)
        layout.addStretch()

        self.refresh_devices()
        return page

    def eventFilter(self, watched, event):
        if (
            self._shortcut_recording
            and event.type() in (
                QEvent.Type.KeyPress, QEvent.Type.KeyRelease
            )
            and isinstance(event, QKeyEvent)
            and event.isAutoRepeat()
        ):
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def toggle_shortcut_recording(self):
        if self._shortcut_recording:
            # Cancel: re-arm the global binding and drop the draft.
            self._shortcut_end_recording()
            self.shortcut_edit.setKeySequence(
                QKeySequence(self._shortcut_before_record)
            )
            self.shortcut_status.setText(
                "Current shortcut: "
                + self._display_shortcut(load_shortcut())
            )
            return
        self._shortcut_before_record = (
            self.shortcut_edit.keySequence().toString(
                QKeySequence.SequenceFormat.PortableText
            )
        )
        self._shortcut_recording = True
        self.shortcut_record.setText("Cancel")
        self.shortcut_edit.clear()
        self.shortcut_edit.setFocus()
        self.shortcut_status.setText(
            "Listening for the next shortcut. Press one key or chord, "
            "then release it."
        )
        self._shortcut_suspend_binding()

    def _shortcut_suspend_binding(self):
        """Stop the global chord while recording so it cannot trigger voice."""
        if self._shortcut_suspended:
            return
        self._shortcut_suspended = True
        self.shortcut_recording_started.emit()

    def _shortcut_resume_binding(self):
        if not self._shortcut_suspended:
            return
        self._shortcut_suspended = False
        self.shortcut_recording_finished.emit()

    def _shortcut_end_recording(self):
        """Leave recording mode; the binding resumes exactly once."""
        if not self._shortcut_recording:
            return
        self._shortcut_recording = False
        self.shortcut_record.setText("Record")
        self._shortcut_resume_binding()

    def _shortcut_display_saved(self):
        state = self.shortcut_state()
        saved = state.get("saved") if isinstance(state, dict) else None
        return self._display_shortcut(saved or load_shortcut())

    def shortcut_state(self):
        """Saved-vs-active view from the controller (fallbacks if absent)."""
        getter = getattr(self, "_shortcut_state_provider", None)
        if callable(getter):
            try:
                state = getter()
            except Exception:
                state = None
            if isinstance(state, dict):
                return dict(state)
        saved = load_shortcut()
        return {
            "saved": saved,
            "active": None,
            "suspended": self._shortcut_suspended,
            "status": "suspended" if self._shortcut_suspended else "unknown",
            "needs_apply": False,
        }

    def _shortcut_render_state(self):
        if self._shortcut_status_override:
            self.shortcut_status.setText(self._shortcut_status_override)
        else:
            state = self.shortcut_state()
            saved = self._display_shortcut(state.get("saved") or "")
            active = state.get("active")
            status = state.get("status")
            if status == "suspended":
                self.shortcut_status.setText(
                    f"Saved: {saved}. Global shortcut is paused while "
                    "recording."
                )
            elif active:
                active_text = self._display_shortcut(active)
                if status == "active":
                    self.shortcut_status.setText(
                        f"Active shortcut: {saved}"
                    )
                else:
                    self.shortcut_status.setText(
                        f"Saved {saved}; the helper currently has "
                        f"{active_text} bound. Use Apply / Retry."
                    )
            else:
                self.shortcut_status.setText(
                    f"Saved: {saved}. The helper is not running, so the "
                    "shortcut is not active. Use Apply / Retry."
                )
        needs_apply = bool(
            self.shortcut_state().get("needs_apply")
        ) or bool(self._shortcut_status_override)
        if hasattr(self, "shortcut_apply") and self.shortcut_apply is not None:
            self.shortcut_apply.setVisible(needs_apply)

    def shortcut_applied(self, sequence):
        """The controller confirmed READY; only now persist the value."""
        pending = self._shortcut_pending_save
        self._shortcut_status_override = ""
        self._shortcut_applying = False
        self._shortcut_pending_save = None
        if pending:
            try:
                save_shortcut(pending)
            except (OSError, ValueError) as error:
                self._shortcut_status_override = (
                    f"Active {self._display_shortcut(pending)}, but it could "
                    f"not be saved: {error}"
                )
        self._shortcut_render_state()
        self.refresh_shortcut_hint()

    def show_shortcut_failure(self, reason):
        """Registration failed: the saved value is unchanged (not active)."""
        self._shortcut_applying = False
        self._shortcut_status_override = (
            "Not saved — registration failed: "
            f"{reason or 'unknown reason'}. Use Apply / Retry."
        )
        self._shortcut_pending_save = None
        self._shortcut_render_state()

    def _shortcut_key_changed(self, sequence):
        if not self._shortcut_recording or self._shortcut_adjusting:
            return
        portable = sequence.toString(
            QKeySequence.SequenceFormat.PortableText
        ).strip()
        chords = [
            chord.strip() for chord in portable.split(",") if chord.strip()
        ]
        deduplicated = []
        for chord in chords:
            if not deduplicated or (
                chord.casefold() != deduplicated[-1].casefold()
            ):
                deduplicated.append(chord)
        if len(deduplicated) != len(chords):
            self._shortcut_adjusting = True
            self.shortcut_edit.setKeySequence(
                QKeySequence(", ".join(deduplicated))
            )
            self._shortcut_adjusting = False
            portable = ", ".join(deduplicated)
        text = self._display_shortcut(portable)
        if text:
            self.shortcut_status.setText(
                f"Captured {text}. Click Save shortcut to apply, or Cancel."
            )

    def _finish_shortcut_recording(self):
        if not self._shortcut_recording:
            return
        portable = self.shortcut_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText
        ).strip()
        text = self._display_shortcut(portable)
        self._shortcut_end_recording()
        if portable:
            self.shortcut_status.setText(
                f"Captured {text}. Click Save shortcut to apply."
            )
        else:
            self.shortcut_edit.setKeySequence(
                QKeySequence(self._shortcut_before_record)
            )
            self.shortcut_status.setText("No shortcut captured.")

    def save_shortcut_setting(self):
        if self._shortcut_recording:
            self._finish_shortcut_recording()
        sequence = self.shortcut_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText
        ).strip()
        if not sequence:
            self.shortcut_status.setText(
                "That key is not recognized. Press a normal key, chord, "
                "or a supported media key."
            )
            return
        if "," in sequence:
            self.shortcut_status.setText(
                "KDE global shortcuts support one chord. Use a single key "
                "or one combination such as Ctrl+Alt+M."
            )
            return
        state = self.shortcut_state()
        already_active = (
            state.get("status") == "active"
            and str(state.get("saved", "")).strip() == sequence
        )
        if already_active and not self._shortcut_status_override:
            self.shortcut_status.setText(
                f"{self._display_shortcut(sequence)} is already active."
            )
            return
        # Hand the request to the app, which applies it first and only
        # persists the new value when the controller reports success.
        self._shortcut_applying = True
        self._shortcut_pending_save = sequence
        self._shortcut_status_override = ""
        self.shortcut_status.setText(
            f"Applying {self._display_shortcut(sequence)}…"
        )
        self.shortcut_changed.emit(sequence)
        return True

    def apply_shortcut_setting(self):
        """Explicit Apply/Retry for the saved value (restarts the helper)."""
        saved = self.shortcut_state().get("saved") or load_shortcut()
        if self._shortcut_recording:
            self._finish_shortcut_recording()
        self._shortcut_applying = True
        self._shortcut_status_override = ""
        self.shortcut_status.setText(
            f"Re-applying {self._display_shortcut(saved)}…"
        )
        self.shortcut_apply_requested.emit(str(saved))
        return True

    def test_audio_device(self):
        selected = self.devices.currentData()
        if not selected:
            return
        mode = selected.get("mode")
        if mode == "system":
            self.audio_status.setText(
                "System default microphone is selected; it will be used "
                "for the next recording."
            )
            return
        try:
            devices = sd.query_devices()
            target = None
            for index, device in enumerate(devices):
                if (
                    device["name"] == selected.get("name")
                    and sd.query_hostapis(device["hostapi"])["name"]
                    == selected.get("hostapi")
                    and device["max_input_channels"] > 0
                ):
                    target = index
                    break
            if target is None:
                self.audio_status.setText("That microphone is no longer available.")
                return
            sd.check_input_settings(
                device=target, channels=1, samplerate=16000
            )
            self.audio_status.setText(
                "Selected microphone supports 16 kHz mono input."
            )
        except Exception as error:
            self.audio_status.setText(f"Microphone test failed: {error}")

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

            input_count = 0
            for device in devices:
                if device["max_input_channels"] <= 0:
                    continue

                input_count += 1
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

            self.audio_status.setText(
                f"Found {input_count} microphone input device(s)."
            )

        except Exception as error:
            self.audio_status.setText(
                f"Could not enumerate audio devices: {error}"
            )

    def save_audio_settings(self):
        selected = self.devices.currentData()
        if not selected:
            return

        try:
            AUDIO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temporary = AUDIO_CONFIG.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(selected, indent=4) + "\n",
                encoding="utf-8",
            )
            temporary.replace(AUDIO_CONFIG)
            self.audio_status.setText(
                "Microphone settings saved. They apply to the next recording."
            )
        except OSError as error:
            self.audio_status.setText(f"Could not save settings: {error}")


    def _build_ai_models_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(17)

        heading = QLabel("AI & Models")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        intro = QLabel(
            "Manage Misty's local AI models and inference engine. "
            "Checking status does not load models or use GPU memory."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.model_status_labels = {}

        models = [
            (
                "reasoning",
                "Qwen3-VL 8B",
                "Local planning, tool calling and vision on one shared model.",
                ROOT / "models" / "vision" / "qwen3-vl-8b" / "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
            ),
            (
                "speech",
                "Whisper large-v3-turbo",
                "Local speech recognition for English and Polish.",
                ROOT / "models" / "ggml-large-v3-turbo-q5_0.bin",
            ),
        ]

        for key, title, description, model_path in models:
            card, card_layout = self._section_card(title, description)

            status = QLabel()
            status.setWordWrap(True)
            status.setMinimumHeight(76)
            status.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            card_layout.addWidget(status)

            self.model_status_labels[key] = (status, model_path)
            layout.addWidget(card)

        card, card_layout = self._section_card(
            "Inference engine",
            "The local software responsible for running Misty's models.",
        )

        self.model_status_labels["llama"] = (
            QLabel(),
            ROOT / "third_party" / "llama.cpp" / "build" / "bin" / "llama-server",
        )
        self.model_status_labels["whisper_cli"] = (
            QLabel(),
            ROOT / "third_party" / "whisper.cpp" / "build" / "bin" / "whisper-cli",
        )

        for key, title in (
            ("llama", "llama.cpp server"),
            ("whisper_cli", "Whisper executable"),
        ):
            label, _ = self.model_status_labels[key]
            label.setWordWrap(True)
            label.setMinimumHeight(76)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            card_layout.addWidget(QLabel(title))
            card_layout.addWidget(label)

        self.model_backend_label = QLabel()
        self.model_backend_label.setWordWrap(True)
        card_layout.addWidget(QLabel("GPU backend"))
        card_layout.addWidget(self.model_backend_label)

        layout.addWidget(card)

        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh status")
        refresh.setObjectName("primary")
        refresh.clicked.connect(self.refresh_model_status)
        buttons.addWidget(refresh)
        buttons.addStretch()
        layout.addLayout(buttons)
        layout.addStretch()

        self.refresh_model_status()

        # Keep the full page accessible when its content exceeds
        # the available window height.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        page.setObjectName("scrollPage")
        scroll.setWidget(page)
        return scroll

    def refresh_model_status(self):
        for key, (label, file_path) in self.model_status_labels.items():
            if file_path.is_file():
                size = file_path.stat().st_size
                size_mb = size / (1024 * 1024)
                if size_mb >= 1024:
                    size_text = f"{size_mb / 1024:.2f} GB"
                else:
                    size_text = f"{size_mb:.0f} MB"

                label.setText(
                    f"Available  ·  {size_text}\\n{file_path}"
                )
                label.setStyleSheet("color: #9bc8ad;")
            else:
                label.setText(f"Not found\\n{file_path}")
                label.setStyleSheet("color: #e6a0a0;")

        try:
            with (ROOT / "config.toml").open("rb") as config_file:
                config = tomllib.load(config_file)
            backend = config.get("decision", {}).get(
                "gpu_backend", "Not configured"
            )
        except (OSError, tomllib.TOMLDecodeError):
            backend = "Could not read config.toml"

        self.model_backend_label.setText(
            f"{backend.upper()} (configured in config.toml)"
            if backend != "Could not read config.toml"
            else backend
        )

    def _build_privacy_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(17)

        heading = QLabel("Privacy & Permissions")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        intro = QLabel(
            "An overview of what Misty currently does to protect your "
            "privacy. These indicators describe enforced behavior, not "
            "just configuration preferences."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        sections = [
            (
                "Local AI processing",
                "ACTIVE",
                "Speech recognition runs through local Whisper and reasoning "
                "runs through the local Qwen3-VL 8B model using llama.cpp. No cloud AI service "
                "is used by the current voice pipeline.",
            ),
            (
                "Microphone access",
                "HOLD TO TALK",
                "The microphone records only while the configured "
                "hold-to-talk shortcut is pressed. Misty does not use an "
                "always-on microphone listener.",
            ),
            (
                "Model memory",
                "UNLOADED AFTER REQUEST",
                "Whisper is unloaded and the shared model server is stopped "
                "during request cleanup, releasing their model memory.",
            ),
            (
                "Internet access",
                "LIMITED",
                "Web search and research use the local SearXNG service, "
                "then read public HTTP(S) pages. Private files, clipboard "
                "contents, and desktop screenshots are never uploaded. "
                "Public page text is treated as untrusted data.",
            ),
            (
                "Desktop action permissions",
                "ALLOWLISTED",
                "The agent can only request tools registered in its trusted "
                "tool registry. Deletions, file writes, archives, installs, "
                "messages, purchases, and other sensitive actions require "
                "an explicit popup approval; denied actions are not run.",
            ),
            (
                "Local activity history",
                "STORED LOCALLY",
                "Recognized speech, planned actions and tool results are "
                "recorded in data/activity.sqlite3 on this computer. "
                "This history is not automatically uploaded by Misty.",
            ),
        ]

        for title, status, description in sections:
            card, card_layout = self._section_card(title, description)

            status_label = QLabel(status)
            status_label.setStyleSheet(
                "font-size: 12px; font-weight: 700; "
                "color: #91c7a5; letter-spacing: 0.5px;"
            )
            card_layout.addSpacing(5)
            card_layout.addWidget(status_label)
            layout.addWidget(card)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        page.setObjectName("scrollPage")
        scroll.setWidget(page)
        return scroll

    def _build_integrations_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        heading = QLabel("Integrations")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        intro = QLabel(
            "Live status of the local components Misty can use. Nothing here "
            "is cloud-hosted."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        card = QFrame()
        card.setObjectName("contentCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(22, 18, 22, 18)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("COMPONENT"), 0, 0)
        grid.addWidget(QLabel("DETAIL"), 0, 1)
        grid.addWidget(QLabel("STATUS"), 0, 2)

        self.integration_items = []
        rows = [
            ("Reasoning model", ROOT / "models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
            ("Vision projector", ROOT / "models/vision/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"),
            ("llama.cpp server", ROOT / "third_party/llama.cpp/build/bin/llama-server"),
            ("Whisper binary", ROOT / "third_party/whisper.cpp/build/bin/whisper-cli"),
            ("Whisper model", ROOT / "models/ggml-large-v3-turbo-q5_0.bin"),
            ("OCRmyPDF", ROOT / "data/bin/ocrmypdf"),
            ("Playwright Chromium", ROOT / "data/playwright-browsers"),
        ]
        for row, (title, path) in enumerate(rows, 1):
            self._add_integration_row(
                grid, row, title, str(path), path.exists()
            )

        searxng_host, searxng_port, searxng_label = (
            self._searxng_endpoint()
        )
        searxng = self._port_open(searxng_host, searxng_port)
        self._add_integration_row(
            grid, len(rows) + 1, "SearXNG search",
            searxng_label, searxng,
        )
        self._add_integration_row(
            grid, len(rows) + 2, "KDE / KWin",
            os.environ.get("XDG_CURRENT_DESKTOP", "KDE"),
            "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper(),
        )
        self._add_integration_row(
            grid, len(rows) + 3, "Local tools",
            "files, zip, calendar, grep, duplicates, media",
            all(
                find_spec(name) is not None
                for name in (
                    "app.actions.file_transfer",
                    "app.actions.archive_zip",
                    "app.actions.calendar_ics",
                    "app.actions.duplicates",
                    "app.actions.text_search",
                    "app.desktop.media",
                )
            ),
        )
        layout.addWidget(card)

        refresh = QPushButton("Refresh status")
        refresh.setObjectName("secondary")
        refresh.clicked.connect(self.refresh_integrations)
        layout.addWidget(refresh, 0, Qt.AlignmentFlag.AlignRight)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        page.setObjectName("scrollPage")
        scroll.setWidget(page)
        return scroll

    def _add_integration_row(self, grid, row, title, detail, available):
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: 600;")
        detail_label = QLabel(detail)
        detail_label.setObjectName("muted")
        detail_label.setWordWrap(True)
        status = QLabel("AVAILABLE" if available else "MISSING")
        status.setStyleSheet(
            "font-size: 11px; font-weight: 700; color: "
            + ("#91c7a5" if available else "#e0a0a0")
        )
        grid.addWidget(title_label, row, 0)
        grid.addWidget(detail_label, row, 1)
        grid.addWidget(status, row, 2)
        self.integration_items.append((title, detail, status, available))

    @staticmethod
    def _searxng_endpoint():
        value = os.environ.get(
            "MISTY_SEARXNG_URL", "http://127.0.0.1:8080"
        ).strip()
        parts = urlsplit(value)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return host, port, f"{host}:{port}"

    @staticmethod
    def _port_open(host, port, timeout=0.25):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def refresh_integrations(self):
        checks = {
            "Reasoning model": (ROOT / "models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf").exists(),
            "Vision projector": (ROOT / "models/vision/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf").exists(),
            "llama.cpp server": (ROOT / "third_party/llama.cpp/build/bin/llama-server").exists(),
            "Whisper binary": (ROOT / "third_party/whisper.cpp/build/bin/whisper-cli").exists(),
            "Whisper model": (ROOT / "models/ggml-large-v3-turbo-q5_0.bin").exists(),
            "OCRmyPDF": (ROOT / "data/bin/ocrmypdf").exists(),
            "Playwright Chromium": (ROOT / "data/playwright-browsers").exists(),
            "SearXNG search": self._port_open(
                *self._searxng_endpoint()[:2]
            ),
            "KDE / KWin": "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper(),
        }
        for title, _detail, status, _old in self.integration_items:
            available = checks.get(title)
            if available is None:
                continue
            status.setText("AVAILABLE" if available else "MISSING")
            status.setStyleSheet(
                "font-size: 11px; font-weight: 700; color: "
                + ("#91c7a5" if available else "#e0a0a0")
            )

    def _placeholder_settings(self, title, description):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(17)

        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        card, card_layout = self._section_card(title, description)
        coming = QLabel("More options will appear here as Misty develops.")
        coming.setObjectName("muted")
        card_layout.addSpacing(8)
        card_layout.addWidget(coming)

        layout.addWidget(card)
        layout.addStretch()
        return page

    def prompt_for_text(self):
        """Bring the main chat forward and focus the text input."""
        self.show_page(0)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.chat_input is not None:
            self.chat_input.setFocus()

    def closeEvent(self, event):
        # Closing the window mid-record must not leave voice suspended.
        self._shortcut_resume_binding()
        self.hide()
        event.ignore()
