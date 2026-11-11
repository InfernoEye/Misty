"""Separate read-only window for full Misty responses and research sources."""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
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
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^\s*(#{1,3})\s+(.*)$")


def _inline_markup(value):
    value = html.escape(value, quote=False)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    return value


def _file_list_parts(paragraph):
    """Split 'intro: a, b, c' when it is clearly a filename list."""
    match = re.match(r"^(.*?:)\s*(.+)$", paragraph, flags=re.DOTALL)
    if not match:
        return None
    intro, rest = match.group(1), match.group(2)
    items = [item.strip() for item in rest.split(",")]
    if len(items) < 5:
        return None
    if any(not item or len(item) > 200 for item in items):
        return None
    if sum(len(item) <= 160 for item in items) / len(items) < 0.9:
        return None
    intro_lower = intro.casefold()
    file_context = any(
        word in intro_lower
        for word in (
            "file", "files", "folder", "directory", "downloads",
            "plik", "pliki", "katalog", "pobrane",
        )
    )
    extension_ratio = sum(
        bool(re.search(r"\.[A-Za-z0-9]{1,8}$", item)) for item in items
    ) / len(items)
    if not file_context and extension_ratio < 0.6:
        return None
    if items[-1].endswith(".") and not items[-1].endswith("..."):
        items[-1] = items[-1][:-1].rstrip()
    return intro, items


def _list_rows(items, monospace=False):
    rows = []
    for item in items:
        content = _inline_markup(item)
        if monospace:
            content = f"<code>{content}</code>"
        rows.append(
            '<p class="misty-item" style="margin:0 0 5px 18px; '
            'text-indent:-11px; line-height:145%;">'
            '<span style="color:#8fb3ff;">•</span> '
            f'{content}</p>'
        )
    return "".join(rows)


def format_response_html(text):
    """Render a response with readable paragraphs, bullets, and file lists."""
    if not isinstance(text, str):
        text = str(text)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    blocks = []
    paragraph = []
    bullets = []

    def flush_paragraph():
        if paragraph:
            blocks.append(("paragraph", "\n".join(paragraph)))
            paragraph.clear()

    def flush_bullets():
        if bullets:
            blocks.append(("bullets", list(bullets)))
            bullets.clear()

    for line in normalized.split("\n"):
        stripped = line.strip()
        bullet = _BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group(1).strip())
            continue
        flush_bullets()
        if not stripped:
            flush_paragraph()
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            blocks.append(("heading", heading.group(2).strip()))
            continue
        paragraph.append(line.rstrip())

    flush_bullets()
    flush_paragraph()

    parts = []
    for kind, value in blocks:
        if kind == "bullets":
            parts.append(_list_rows(value))
        elif kind == "heading":
            parts.append(f"<h2>{_inline_markup(value)}</h2>")
        else:
            file_list = _file_list_parts(value)
            if file_list:
                intro, items = file_list
                intro_html = _inline_markup(intro)
                parts.append(
                    f"<p>{intro_html}</p>"
                    + _list_rows(items, monospace=True)
                )
            else:
                body = "<br>".join(
                    _inline_markup(line) for line in value.split("\n")
                )
                parts.append(f"<p>{body}</p>")

    return (
        "<style>"
        "p { margin: 0 0 12px 0; line-height: 150%; }"
        "h2 { margin: 8px 0 8px 0; font-size: 16px; }"
        "code { font-family: 'DejaVu Sans Mono', monospace; "
        "color: #dce6ff; }"
        "</style>" + "".join(parts)
    )


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
    # Navigation has its own browser window; do not duplicate it here.
    if stripped.startswith(("Opening ", "Searching ")):
        return False
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
        self.open_button = QPushButton("Open link")
        self.open_button.clicked.connect(self._open_first_source)
        self.open_button.hide()
        buttons.addWidget(self.open_button)
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
        self.body.setHtml(format_response_html(text))

        urls = extract_source_urls(text)
        self.sources.clear()
        for url in urls:
            item = QListWidgetItem(url)
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.sources.addItem(item)
        self.sources_label.setVisible(bool(urls))
        self.sources.setVisible(bool(urls))
        self.open_button.setVisible(bool(urls))

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
            self.open_button.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def copy_response(self):
        if self._raw_text:
            QGuiApplication.clipboard().setText(self._raw_text)

    def _open_first_source(self):
        if self.sources.count():
            self._open_source(self.sources.item(0))

    def _open_source(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(url, str):
            return
        try:
            validated = validate_open_url(url)
        except ValueError:
            return
        QDesktopServices.openUrl(QUrl(validated))
