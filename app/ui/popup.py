"""Misty's animated KDE desktop popup."""

import random

from PySide6.QtCore import Qt, QTimer, Signal, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QFrame, QSizePolicy, QScrollArea, QPushButton
)


from app.settings import load_shortcut


class MistyPopup(QWidget):
    hover_changed = Signal(bool)
    confirmation_action = Signal(bool)

    STATES = {
        "ready": ("READY", "#8B95A7"),
        "listening": ("LISTENING", "#8DA9FF"),
        "transcribing": ("TRANSCRIBING", "#C4A7FF"),
        "thinking": ("THINKING", "#C4A7FF"),
        "acting": ("WORKING", "#F0C674"),
        "complete": ("COMPLETE", "#8BD5A5"),
        "error": ("ERROR", "#F28B82"),
        "confirmation": ("NEEDS CONFIRMATION", "#F0C674"),
        "selection": ("CHOOSE AN OPTION", "#8DA9FF"),
    }

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Misty")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # Voice feedback and mouse approval must not replace the user's
        # foreground target. This is not a guarantee of compositor behavior;
        # native input still rechecks the exact active target before dispatch.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setFixedWidth(390)
        self.setMaximumHeight(760)
        self.is_hovered = False

        self._build_ui()
        self._set_style()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(110)

        self.set_state("ready")
        self.set_transcript("Your commands will appear here.")
        self.set_response("I'm ready whenever you are.")

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        self.panel = QFrame()
        self.panel.setObjectName("panel")
        outer.addWidget(self.panel)

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(15)

        # Header
        header = QHBoxLayout()
        header.setSpacing(11)

        self.logo = QLabel("M")
        self.logo.setObjectName("logo")
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo.setFixedSize(42, 42)

        title_column = QVBoxLayout()
        title_column.setSpacing(1)

        title = QLabel("Misty")
        title.setObjectName("title")

        subtitle = QLabel("YOUR LOCAL ASSISTANT")
        subtitle.setObjectName("subtitle")

        title_column.addWidget(title)
        title_column.addWidget(subtitle)

        header.addWidget(self.logo)
        header.addLayout(title_column, 1)

        self.status = QLabel()
        self.status.setObjectName("status")
        header.addWidget(self.status)

        layout.addLayout(header)

        # Audio visualization
        visual = QFrame()
        visual.setObjectName("visual")
        visual.setFixedHeight(88)
        visual_layout = QVBoxLayout(visual)
        visual_layout.setContentsMargins(12, 13, 12, 13)
        visual_layout.setSpacing(7)

        self.state_label = QLabel("READY")
        self.state_label.setObjectName("stateLabel")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setFixedHeight(16)
        self.state_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed
        )
        visual_layout.addWidget(self.state_label)

        bar_container = QWidget()
        bar_container.setFixedHeight(30)

        bars_layout = QHBoxLayout(bar_container)
        bars_layout.setContentsMargins(0, 0, 0, 0)
        bars_layout.setSpacing(5)
        bars_layout.addStretch()

        self.bars = []
        for _ in range(17):
            bar = QFrame()
            bar.setObjectName("bar")
            bar.setFixedWidth(5)
            bar.setFixedHeight(5)
            self.bars.append(bar)
            bars_layout.addWidget(
                bar, 0, Qt.AlignmentFlag.AlignVCenter
            )

        bars_layout.addStretch()
        visual_layout.addWidget(bar_container)

        layout.addWidget(visual)

        # Recognized speech
        self.transcript_card = self._make_card(
            "YOU SAID", "transcript"
        )
        layout.addWidget(self.transcript_card)

        # Assistant response
        self.response_card = self._make_card(
            "MISTY", "response"
        )
        layout.addWidget(self.response_card)

        # Dedicated window-choice panel. Choices are individual rows,
        # not compressed into the ordinary response paragraph.
        self.selection_card = QFrame()
        self.selection_card.setObjectName("selectionCard")
        selection_layout = QVBoxLayout(self.selection_card)
        selection_layout.setContentsMargins(12, 12, 12, 12)
        selection_layout.setSpacing(9)

        self.selection_title = QLabel("CHOOSE AN OPTION")
        self.selection_title.setObjectName("selectionTitle")

        self.shortcut_text = load_shortcut()
        self.selection_message = QLabel(self._selection_instruction())
        self.selection_message.setObjectName("selectionMessage")
        self.selection_message.setWordWrap(True)

        self.selection_scroll = QScrollArea()
        self.selection_scroll.setObjectName("selectionScroll")
        self.selection_scroll.setWidgetResizable(True)
        self.selection_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.selection_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.selection_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.selection_scroll.setMaximumHeight(300)

        self.selection_rows_widget = QWidget()
        self.selection_rows_layout = QVBoxLayout(
            self.selection_rows_widget
        )
        self.selection_rows_layout.setContentsMargins(0, 2, 0, 2)
        self.selection_rows_layout.setSpacing(6)
        self.selection_scroll.setWidget(self.selection_rows_widget)

        selection_layout.addWidget(self.selection_title)
        selection_layout.addWidget(self.selection_message)
        selection_layout.addWidget(self.selection_scroll)

        layout.addWidget(self.selection_card)
        self.selection_card.hide()

        self.confirmation_card = QFrame()
        self.confirmation_card.setObjectName("confirmationCard")
        confirmation_layout = QVBoxLayout(self.confirmation_card)
        confirmation_layout.setContentsMargins(12, 11, 12, 12)
        confirmation_layout.setSpacing(8)

        self.confirmation_title = QLabel("Confirm this action?")
        self.confirmation_title.setObjectName("confirmationTitle")
        self.confirmation_title.setWordWrap(True)
        self.confirmation_title.setTextFormat(Qt.TextFormat.PlainText)

        self.confirmation_description = QLabel()
        self.confirmation_description.setObjectName(
            "confirmationDescription"
        )
        self.confirmation_description.setWordWrap(True)
        self.confirmation_description.setTextFormat(Qt.TextFormat.PlainText)
        self.confirmation_description.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )

        confirmation_buttons = QHBoxLayout()
        confirmation_buttons.setSpacing(8)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.setObjectName("confirmButton")

        confirmation_buttons.addWidget(self.cancel_button)
        confirmation_buttons.addWidget(self.confirm_button)

        confirmation_layout.addWidget(self.confirmation_title)
        confirmation_layout.addWidget(self.confirmation_description)
        confirmation_layout.addLayout(confirmation_buttons)

        layout.addWidget(self.confirmation_card)
        self.confirmation_card.hide()

        self.confirm_button.clicked.connect(
            lambda: self.confirmation_action.emit(True)
        )
        self.cancel_button.clicked.connect(
            lambda: self.confirmation_action.emit(False)
        )

        footer = QHBoxLayout()
        footer.setSpacing(5)

        self.footer_label = QLabel(
            f"HOLD {self.shortcut_text.upper()} TO TALK"
        )
        self.footer_label.setObjectName("footer")

        hint = QLabel("VOICE / MOUSE")
        hint.setObjectName("footer")

        footer.addWidget(self.footer_label)
        footer.addStretch()
        footer.addWidget(hint)

        layout.addLayout(footer)
        self.set_shortcut(load_shortcut())

    def set_shortcut(self, text):
        value = str(text or "").strip() or "F8"
        self.shortcut_text = value
        if hasattr(self, "footer_label"):
            self.footer_label.setText(f"HOLD {value.upper()} TO TALK")
        if hasattr(self, "selection_message"):
            self.selection_message.setText(self._selection_instruction())

    def _selection_instruction(self):
        return (
            f"Hold {self.shortcut_text} and say the number or title. "
            "Say cancel to stop."
        )

    def show_window_selection(self, candidates):
        # Rebuild the rows so the displayed numbers always match the
        # exact candidate order used by the voice resolver.
        while self.selection_rows_layout.count():
            item = self.selection_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for number, candidate in enumerate(candidates, 1):
            row = QFrame()
            row.setObjectName("selectionOption")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(9, 4, 10, 4)
            row_layout.setSpacing(10)

            number_label = QLabel(str(number))
            number_label.setObjectName("selectionNumber")
            number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number_label.setFixedSize(24, 24)

            title_label = QLabel(
                self._prepare_display_text(
                    str(candidate.get("title") or "Untitled window")
                )
            )
            title_label.setObjectName("selectionWindowTitle")
            title_label.setWordWrap(True)
            title_label.setMinimumWidth(0)
            title_label.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )

            row_layout.addWidget(number_label, 0, Qt.AlignmentFlag.AlignTop)
            row_layout.addWidget(title_label, 1)
            self.selection_rows_layout.addWidget(row)

        self.selection_rows_layout.addStretch(1)
        self.selection_message.setText(self._selection_instruction())
        self.response_card.hide()
        self.transcript_card.hide()
        self.selection_card.show()
        self.set_state("selection")
        self.adjustSize()
        self.show_popup()

    def set_window_selection_message(self, message):
        self.selection_message.setText(
            self._prepare_display_text(str(message))
        )
        self.selection_message.updateGeometry()
        self.adjustSize()

    def clear_window_selection(self):
        self.selection_card.hide()
        self.transcript_card.show()
        self.response_card.show()
        self.adjustSize()

    def show_confirmation(self, title, description):
        self.confirmation_title.setText(
            self._prepare_display_text(title)
        )
        self.confirmation_description.setText(
            self._prepare_display_text(description)
        )
        self.confirm_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.confirmation_card.show()
        self.set_state("confirmation")
        self.adjustSize()
        self.show_popup()

    def set_confirmation_busy(self):
        self.confirm_button.setEnabled(False)
        self.cancel_button.setEnabled(False)

    def clear_confirmation(self):
        self.confirmation_card.hide()
        self.confirm_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.adjustSize()

    def eventFilter(self, watched, event):
        if event.type() == event.Type.Resize:
            for content in (getattr(self, "transcript", None),
                            getattr(self, "response", None)):
                if content is not None and watched is content.parentWidget():
                    content.setFixedWidth(max(1, watched.width()))

        return super().eventFilter(watched, event)

    def _make_card(self, heading, attribute):
        card = QFrame()
        card.setObjectName("messageCard")

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 11)
        card_layout.setSpacing(5)

        label = QLabel(heading)
        label.setObjectName("cardHeading")

        content = QLabel()
        content.setObjectName("cardContent")
        content.setContentsMargins(5, 0, 5, 0)
        content.setWordWrap(True)
        content.setMinimumHeight(24)
        content.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum
        )

        scroll = QScrollArea()
        scroll.setObjectName("messageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        # Keep the message label within the viewport so word wrapping
        # respects its left and right content margins.
        content.setMinimumWidth(0)
        content.setMaximumWidth(16777215)
        scroll.viewport().installEventFilter(self)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setMinimumHeight(48 if attribute != "response" else 60)
        scroll.setMaximumHeight(90 if attribute != "response" else 340)
        scroll.setWidget(content)

        if attribute == "response":
            self.response_scroll = scroll

        card_layout.addWidget(label)
        card_layout.addWidget(scroll)

        setattr(self, attribute, content)
        return card

    def _set_style(self):
        self.setStyleSheet("""
            QWidget {
                color: #E8ECF5;
                font-family: "Inter", "Noto Sans", sans-serif;
            }

            QFrame#panel {
                background-color: #171B29;
                border: 1px solid #343D55;
                border-radius: 21px;
            }

            QLabel#logo {
                background-color: #34466F;
                color: #DCE5FF;
                border: 1px solid #657DB5;
                border-radius: 14px;
                font-size: 23px;
                font-weight: 800;
            }

            QLabel#title {
                font-size: 19px;
                font-weight: 700;
                color: #F3F5FC;
            }

            QLabel#subtitle {
                font-size: 8px;
                letter-spacing: 0.6px;
                color: #8994AE;
                font-weight: 600;
            }

            QLabel#status {
                background-color: #293348;
                color: #AFC2F8;
                border-radius: 9px;
                padding: 6px 7px;
                font-size: 9px;
                font-weight: 800;
            }

            QFrame#visual {
                background-color: #20283A;
                border: 1px solid #34415C;
                border-radius: 14px;
            }

            QLabel#stateLabel {
                color: #AFC2F8;
                font-size: 9px;
                font-weight: 800;
                letter-spacing: 2px;
            }

            QFrame#bar {
                background-color: #8DA9FF;
                border: none;
                border-radius: 3px;
            }

            QFrame#messageCard {
                background-color: #202536;
                border: 1px solid #30394F;
                border-radius: 12px;
            }

            QLabel#cardHeading {
                color: #8F9CB9;
                font-size: 9px;
                font-weight: 800;
                letter-spacing: 1.3px;
            }

            QLabel#cardContent {
                background-color: transparent;
                color: #EDF0F8;
                font-size: 13px;
            }

            QFrame#selectionCard {
                background-color: #202B40;
                border: 1px solid #526A9B;
                border-radius: 12px;
            }

            QLabel#selectionTitle {
                color: #AFC2F8;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 1.2px;
            }

            QLabel#selectionMessage {
                color: #D5DDF0;
                font-size: 11px;
            }

            QFrame#selectionOption {
                background-color: #29354C;
                border: 1px solid #3D4D6B;
                border-radius: 9px;
            }

            QLabel#selectionNumber {
                background-color: #8DA9FF;
                color: #172238;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 900;
            }

            QLabel#selectionWindowTitle {
                color: #F0F3FC;
                font-size: 11px;
                font-weight: 600;
            }

            QScrollArea#selectionScroll {
                background: transparent;
                border: none;
            }

            QFrame#confirmationCard {
                background-color: #292B35;
                border: 1px solid #8A7446;
                border-radius: 12px;
            }

            QLabel#confirmationTitle {
                color: #F0C674;
                font-size: 13px;
                font-weight: 800;
            }

            QLabel#confirmationDescription {
                color: #EDF0F8;
                font-size: 12px;
            }

            QPushButton#confirmButton {
                background-color: #8BD5A5;
                color: #17251D;
                border: none;
                border-radius: 8px;
                padding: 9px 12px;
                font-size: 11px;
                font-weight: 800;
            }

            QPushButton#cancelButton {
                background-color: #343D52;
                color: #EDF0F8;
                border: 1px solid #53617C;
                border-radius: 8px;
                padding: 9px 12px;
                font-size: 11px;
                font-weight: 800;
            }

            QScrollArea#messageScroll {
                background: transparent;
                border: none;
            }

            QScrollBar:vertical {
                background: #171D2B;
                width: 7px;
                margin: 2px 0;
                border-radius: 3px;
            }

            QScrollBar::handle:vertical {
                background: #536482;
                min-height: 22px;
                border-radius: 3px;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }

            QLabel#footer {
                color: #75819B;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 0.7px;
            }
        """)

    def set_state(self, state):
        if state not in self.STATES:
            raise ValueError(f"Unknown Misty state: {state}")

        label, color = self.STATES[state]

        self.state = state

        if state == "listening" and hasattr(self, "response_scroll"):
            self.response.setMinimumHeight(24)
            self.response.setMaximumHeight(16777215)
            self.response_scroll.setMinimumHeight(60)
            self.response_scroll.setMaximumHeight(340)
            self.response_scroll.setFixedHeight(60)
            self.response_scroll.verticalScrollBar().setValue(0)

        self.status.setText(label)
        self.state_label.setText(label)
        self.status.setStyleSheet(
            f"background-color: #293348; color: {color}; "
            "border-radius: 9px; padding: 6px 7px; "
            "font-size: 9px; font-weight: 800;"
        )
        self.state_label.setStyleSheet(
            f"color: {color}; font-size: 9px; "
            "font-weight: 800; letter-spacing: 2px;"
        )

        self._animate()

    @staticmethod
    def _prepare_display_text(text):
        import re

        # Allow long filenames and uninterrupted strings to wrap
        # after every 34 characters without changing their appearance.
        return re.sub(
            r"\S{34}(?=\S)",
            lambda match: match.group(0) + "\u200b",
            str(text)
        )

    def set_transcript(self, text):
        self.transcript.setText(self._prepare_display_text(text))

    def set_response(self, text):
        self.response.setText(self._prepare_display_text(text))
        QTimer.singleShot(0, self._fit_response_scroll)

    def _fit_response_scroll(self):
        if not hasattr(self, "response_scroll"):
            return

        scroll = self.response_scroll

        # Remove the previous response's height constraint before measuring.
        self.response.setMinimumHeight(24)
        self.response.updateGeometry()
        scroll.ensurePolished()

        width = scroll.viewport().width()
        if width <= 0:
            width = max(200, scroll.width() - 16)
        if width <= 0:
            width = 280

        # Account for the 5 px left and right text insets when measuring.
        margins = self.response.contentsMargins()
        width -= margins.left() + margins.right()
        width = max(1, width)

        text_rect = self.response.fontMetrics().boundingRect(
            QRect(0, 0, width, 100000),
            Qt.TextFlag.TextWordWrap,
            self.response.text()
        )
        content_height = max(24, text_rect.height())

        # Preserve the full content; the scroll area clips long responses.
        self.response.setMinimumHeight(content_height)

        # Keep the response area between 60px and 340px.
        target_height = max(60, min(340, content_height + 16))
        scroll.setFixedHeight(target_height)

        # Recalculate the card and top-level popup size in both directions.
        self.response.updateGeometry()
        scroll.updateGeometry()
        self.response_card.updateGeometry()
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self.adjustSize()

        # Start each new response at the top.
        scroll.verticalScrollBar().setValue(0)

    def hide(self):
        # Restore compact geometry before the popup is hidden.
        if hasattr(self, "response_scroll"):
            self.response.setText("I'm ready whenever you are.")
            self.response.setMinimumHeight(24)
            self.response_scroll.setFixedHeight(60)
            self.response_scroll.verticalScrollBar().setValue(0)

            self.response.updateGeometry()
            self.response_scroll.updateGeometry()
            self.response_card.updateGeometry()

            layout = self.layout()
            if layout is not None:
                layout.activate()

            self.adjustSize()

        super().hide()

    def show_popup(self):
        screen = QGuiApplication.primaryScreen()

        if screen is not None:
            area = screen.availableGeometry()
            self.adjustSize()
            margin = 24
            x = area.x() + area.width() - self.width() - margin
            y = area.y() + margin
            self.move(x, y)

        self.show()
        self.raise_()
        # Keep keyboard focus with the application the user was operating.
        # Confirmation buttons remain mouse-operable on this non-focus window.

    def _animate(self):
        active = self.state in ("listening", "transcribing", "thinking", "acting")

        for bar in self.bars:
            if active:
                height = random.randint(7, 30)
            else:
                height = random.randint(4, 9)

            bar.setFixedHeight(height)

    def enterEvent(self, event):
        self.is_hovered = True
        self.hover_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_hovered = False
        self.hover_changed.emit(False)
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return

        super().keyPressEvent(event)
