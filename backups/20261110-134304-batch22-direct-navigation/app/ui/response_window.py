"""Separate read-only window for full Misty responses and research sources."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.actions.web_launch import validate_open_url


AUTO_SHOW_MIN_CHARS = 280
MAX_SOURCES = 20
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")
_TRAILING = ".,;:!?"


def extract_source_urls(text, limit=MAX_SOURCES):
    """Return unique, validated public URLs found in a response."""
    if not isinstance(text, str) or limit <= 0:
        return []
    found = []
    seen = set()
    for match in _URL_RE.findall(text):
        url = match.rstrip(_TRAILING)
        if url in seen:
            continue
        try:
            validated = validate_open_url(url)
        except ValueError:
            continue
        seen.add(url)
        found.append(validated)
        if len(found) >= limit:
            break
    return found


def should_auto_open(text):
    """Open the full window for long answers or answers with sources."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) >= AUTO_SHOW_MIN_CHARS:
        return True
    return bool(extract_source_urls(stripped, limit=1))


class MistyResponseWindow(QMainWindow):
    """Non-modal, selectable full-response viewer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Misty — Full response")
        self.resize(920, 680)
        self._raw_text = ""
        self._transcript = ""

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        self.header = QLabel("Full response")
        self.header.setStyleSheet(
            "font-size: 18px; font-weight: 700; color: #eef2ff;"
        )
        layout.addWidget(self.header)

        self.transcript = QLabel("")
        self.transcript.setWordWrap(True)
        self.transcript.setStyleSheet("color: #9aa8c2; font-size: 12px;")
        layout.addWidget(self.transcript)

        self.body = QTextBrowser()
        self.body.setReadOnly(True)
        self.body.setOpenExternalLinks(False)
        self.body.setStyleSheet(
            "QTextBrowser {"
            " background: #151b27; color: #e8edf7;"
            " border: 1px solid #293247; border-radius: 10px;"
            " padding: 14px; font-size: 14px;"
            "}"
        )
        layout.addWidget(self.body, 1)

        self.sources_label = QLabel("Sources (double-click to open)")
        self.sources_label.setStyleSheet(
            "color: #9aa8c2; font-size: 12px; font-weight: 600;"
        )
        layout.addWidget(self.sources_label)
        self.sources = QListWidget()
        self.sources.setMaximumHeight(130)
        self.sources.setStyleSheet(
            "QListWidget {"
            " background: #151b27; color: #cfe0ff;"
            " border: 1px solid #293247; border-radius: 8px;"
            "}"
        )
        self.sources.itemDoubleClicked.connect(self._open_source)
        layout.addWidget(self.sources)
        self.sources_label.hide()
        self.sources.hide()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.copy_button = QPushButton("Copy response")
        self.copy_button.clicked.connect(self.copy_response)
        buttons.addWidget(self.copy_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.hide)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.setCentralWidget(root)
        self.setStyleSheet("QMainWindow { background: #11151e; }")

    def set_response(self, response, transcript=""):
        text = response if isinstance(response, str) else str(response)
        self._raw_text = text
        self._transcript = transcript if isinstance(transcript, str) else ""
        self.header.setText("Full response")
        self.transcript.setText(
            f"You asked: {self._transcript}" if self._transcript else ""
        )
        self.transcript.setVisible(bool(self._transcript))
        self.body.setPlainText(text)

        urls = extract_source_urls(text)
        self.sources.clear()
        for url in urls:
            item = QListWidgetItem(url)
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.sources.addItem(item)
        self.sources_label.setVisible(bool(urls))
        self.sources.setVisible(bool(urls))

    def show_response(self, response, transcript=""):
        self.set_response(response, transcript)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def show_last(self):
        if not self._raw_text:
            self.header.setText("No full response yet")
            self.body.setPlainText(
                "Ask Misty something or run a research request first."
            )
            self.transcript.hide()
            self.sources.hide()
            self.sources_label.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def copy_response(self):
        if self._raw_text:
            self.body.selectAll()
            self.body.copy()
            self.body.moveCursor(QTextCursor.MoveOperation.Start)

    def _open_source(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(url, str):
            return
        try:
            validated = validate_open_url(url)
        except ValueError:
            return
        QDesktopServices.openUrl(QUrl(validated))
