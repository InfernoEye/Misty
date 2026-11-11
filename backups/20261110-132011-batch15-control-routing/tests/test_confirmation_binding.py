import unittest

from app.popup_process import PopupProcess


class FakePopup:
    def __init__(self):
        self.confirmation = None
        self.busy = False
        self.cleared = False

    def show_confirmation(self, title, description):
        self.confirmation = (title, description)

    def set_confirmation_busy(self):
        self.busy = True

    def clear_confirmation(self):
        self.cleared = True


class FakeIpc:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


class ConfirmationBindingTests(unittest.TestCase):
    def setUp(self):
        self.process = PopupProcess.__new__(PopupProcess)
        self.process.popup = FakePopup()
        self.process.ipc = FakeIpc()
        self.process._confirmation_request_id = None

    def test_popup_echoes_request_id_with_decision(self):
        self.process.handle_message({
            "type": "confirmation",
            "request_id": "request-42",
            "title": "Close window?",
            "description": "Closing may lose work.",
        })
        self.process.handle_confirmation_action(True)

        self.assertEqual(self.process.popup.confirmation, (
            "Close window?", "Closing may lose work."
        ))
        self.assertEqual(self.process.ipc.sent[-1], {
            "type": "confirmation_response",
            "approved": True,
            "request_id": "request-42",
        })
        self.assertIsNone(self.process._confirmation_request_id)

    def test_clear_discards_request_id(self):
        self.process.handle_message({
            "type": "confirmation",
            "request_id": "request-42",
            "title": "Close?",
            "description": "",
        })
        self.process.handle_message({"type": "confirmation_clear"})
        self.process.handle_confirmation_action(False)

        self.assertTrue(self.process.popup.cleared)
        self.assertNotIn("request_id", self.process.ipc.sent[-1])

    def test_missing_request_id_is_not_invented(self):
        self.process.handle_message({
            "type": "confirmation",
            "title": "Close?",
            "description": "",
        })
        self.process.handle_confirmation_action(True)

        self.assertEqual(self.process.ipc.sent[-1], {
            "type": "confirmation_response",
            "approved": True,
        })


if __name__ == "__main__":
    unittest.main()
