"""Popup feedback must not request activation; widget tests stay offscreen."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.ui.popup import MistyPopup
from tests.agent_fakes import qt_application


class PopupNoFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = qt_application()
        if not isinstance(cls.application, QApplication):
            raise unittest.SkipTest("Widget-capable QApplication is unavailable")

    def setUp(self):
        self.popup = MistyPopup()
        self.popup.timer.stop()

    def tearDown(self):
        self.popup.timer.stop()
        self.popup.close()
        self.popup.deleteLater()

    def test_window_requests_no_focus_and_show_without_activation(self):
        self.assertTrue(self.popup.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(self.popup.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertFalse(self.popup.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_show_keeps_feedback_visible_without_activate_request(self):
        with patch.object(self.popup, "show") as show, \
             patch.object(self.popup, "raise_") as raise_window, \
             patch.object(self.popup, "activateWindow") as activate:
            self.popup.show_popup()
        show.assert_called_once_with()
        raise_window.assert_called_once_with()
        activate.assert_not_called()

    def test_confirmation_mouse_buttons_still_emit_decisions_without_activation(self):
        decisions = []
        self.popup.confirmation_action.connect(decisions.append)
        with patch.object(self.popup, "show"), patch.object(self.popup, "raise_"), \
             patch.object(self.popup, "activateWindow") as activate:
            self.popup.show_confirmation("Approve action", "Review the requested target.")
            self.assertTrue(self.popup.confirm_button.isEnabled())
            self.assertTrue(self.popup.cancel_button.isEnabled())
            self.popup.confirm_button.click()
            self.popup.cancel_button.click()
        self.assertEqual(decisions, [True, False])
        activate.assert_not_called()

    def test_private_target_text_is_displayed_as_plain_text(self):
        self.assertEqual(self.popup.confirmation_title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.popup.confirmation_description.textFormat(), Qt.TextFormat.PlainText)

    def test_selection_rows_remain_available_without_activation(self):
        with patch.object(self.popup, "show"), patch.object(self.popup, "raise_"), \
             patch.object(self.popup, "activateWindow") as activate:
            self.popup.show_window_selection([{"title": "First window"}, {"title": "Second window"}])
        self.assertEqual(self.popup.state, "selection")
        # Two unchanged candidate rows and the trailing layout stretch.
        self.assertEqual(self.popup.selection_rows_layout.count(), 3)
        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
