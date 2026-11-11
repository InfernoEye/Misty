"""Tests for the opt-in ydotool input backend.

Nothing executes ``ydotool``: a fake runner records argv and stdin, and the
binary/uinput probes are injected.  The tests assert that typed text never
reaches argv and that every missing prerequisite fails closed.
"""

import json
import subprocess
import unittest

from app.desktop.input import (
    DesktopInput,
    MAX_TEXT_LENGTH,
    press_key,
    pointer_click,
    scroll,
    type_text,
)
from app.desktop.input import InputBackend
from app.desktop.ydotool_backend import (
    REFUSAL_CODES,
    UINPUT_PATH,
    YdotoolBackend,
    key_codes,
)


class _Completed:
    def __init__(self, returncode=0):
        self.returncode = returncode


class FakeRunner:
    """Records ``(argv, input, timeout)``; can fail or time out."""

    def __init__(self, returncode=0, raises=None):
        self.calls = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, argv, input=None, timeout=None):
        self.calls.append((argv, input, timeout))
        if self.raises is not None:
            raise self.raises
        return _Completed(self.returncode)


def backend(**overrides):
    options = dict(
        which=lambda name: "/usr/bin/ydotool",
        exists=lambda path: path == UINPUT_PATH,
        access=lambda path, mode: True,
        env={"XDG_SESSION_TYPE": "x11"},
    )
    options.update(overrides)
    runner = options.pop("runner", None) or FakeRunner()
    return YdotoolBackend(runner=runner, **options), runner


SECRET = "SECRET-9f3a-typed-text"


class TypeTextTests(unittest.TestCase):
    def test_text_goes_to_stdin_never_to_argv(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        result = client.execute(type_text(SECRET))

        self.assertTrue(result["success"])
        self.assertEqual(len(runner.calls), 1)
        argv, payload, timeout = runner.calls[0]
        self.assertEqual(argv, ["/usr/bin/ydotool", "type", "--file", "-"])
        self.assertEqual(payload, SECRET.encode("utf-8"))
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertGreater(timeout, 0)

    def test_newline_and_unicode_are_preserved_on_stdin(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        client.execute(type_text("a\u017c\nb\tc"))
        self.assertEqual(runner.calls[0][1], "a\u017c\nb\tc".encode("utf-8"))

    def test_language_placeholder_substitution_is_impossible(self):
        # Literal braces must pass through untouched: the text is raw stdin.
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        client.execute(type_text("{0} %s $HOME"))
        self.assertEqual(runner.calls[0][1], b"{0} %s $HOME")

    def test_input_validation_still_applies(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        long_action = {
            "kind": "type_text",
            "fields": {"text": "x" * (MAX_TEXT_LENGTH + 1)},
        }
        result = client.execute(long_action)
        self.assertEqual(result["code"], "invalid_action")
        self.assertEqual(runner.calls, [])

    def test_control_characters_are_rejected_before_running(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        control_action = {"kind": "type_text", "fields": {"text": "a\x01b"}}
        result = client.execute(control_action)
        self.assertEqual(result["code"], "invalid_action")
        self.assertEqual(runner.calls, [])


class KeyTests(unittest.TestCase):
    def test_ctrl_v_uses_evdev_codes(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        result = client.execute(press_key("ctrl+v"))

        self.assertTrue(result["success"])
        self.assertEqual(
            runner.calls[0][0], ["/usr/bin/ydotool", "key", "29,51"]
        )
        self.assertIsNone(runner.calls[0][1])

    def test_key_codes_helper(self):
        self.assertEqual(key_codes("ctrl+alt+t"), [29, 56, 49])
        self.assertEqual(key_codes("f12"), [88])
        self.assertEqual(key_codes("enter"), [28])

    def test_wayland_session_uses_symbolic_keys(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner, env={"XDG_SESSION_TYPE": "wayland"})
        client.execute(press_key("ctrl+shift+v"))
        self.assertEqual(
            runner.calls[0][0],
            ["/usr/bin/ydotool", "key",
             "KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_V"],
        )

    def test_pointer_actions_are_translated(self):
        # Pointer control is supported now: it must produce real ydotool
        # calls, not a refusal.
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        result = client.execute(scroll(1, 2))
        self.assertTrue(result["success"], result)
        self.assertEqual(runner.calls[0][0][1:3], ["mousemove", "--wheel"])

    def test_a_click_moves_before_it_clicks(self):
        # Clicking wherever the cursor happens to be would hit the wrong
        # target, so a click is always a move followed by the press.
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        client.execute(pointer_click(40, 50))
        verbs = [call[0][1] for call in runner.calls]
        self.assertEqual(verbs, ["mousemove", "click"])

    def test_an_empty_scroll_is_refused(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        result = client.execute(scroll(0, 0))
        self.assertFalse(result["success"])
        self.assertEqual(runner.calls, [])

    def test_partial_actions_are_refused(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        result = client.execute(
            {"kind": "press_key", "fields": {"combo": "hyper+x"}}
        )
        self.assertEqual(result["code"], "invalid_action")
        self.assertEqual(runner.calls, [])


class PrerequisiteTests(unittest.TestCase):
    def test_missing_ydotool_is_a_fixed_refusal(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner, which=lambda name: None)
        for action in (type_text(SECRET), press_key("ctrl+v")):
            result = client.execute(action)
            self.assertFalse(result["success"])
            self.assertEqual(result["code"], "backend_unavailable")
            self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(runner.calls, [])

    def test_missing_uinput_is_a_fixed_refusal(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner, exists=lambda path: False)
        result = client.execute(type_text(SECRET))
        self.assertEqual(result["code"], "uinput_unavailable")
        self.assertEqual(result["detail"], "uinput_missing")
        self.assertEqual(runner.calls, [])

    def test_unwritable_uinput_is_a_fixed_refusal(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner, access=lambda path, mode: False)
        result = client.execute(press_key("ctrl+v"))
        self.assertEqual(result["code"], "uinput_unavailable")
        self.assertEqual(result["detail"], "uinput_denied")
        self.assertEqual(runner.calls, [])

    def test_capabilities_report(self):
        client, _ = backend()
        report = client.capabilities()
        self.assertTrue(report["available"])
        self.assertEqual(report["provider"], "ydotool")
        self.assertEqual(report["binary"], "/usr/bin/ydotool")
        self.assertIsNone(report["uinput_problem"])
        self.assertIn("type_text", report["supported_kinds"])
        self.assertIsInstance(report["timeout_seconds"], float)

        broken, _ = backend(exists=lambda path: False)
        self.assertFalse(broken.capabilities()["available"])

    def test_refusal_codes_are_documented(self):
        self.assertEqual(
            REFUSAL_CODES,
            {
                "backend_unavailable", "uinput_unavailable",
                "unsupported_action", "invalid_action",
                "timeout", "command_failed",
            },
        )


class FailureTests(unittest.TestCase):
    def test_nonzero_exit_is_a_command_failure(self):
        runner = FakeRunner(returncode=2)
        client, _ = backend(runner=runner)
        result = client.execute(type_text(SECRET))
        self.assertEqual(result["code"], "command_failed")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_timeout_is_reported(self):
        runner = FakeRunner(raises=subprocess.TimeoutExpired("ydotool", 5))
        client, _ = backend(runner=runner)
        result = client.execute(type_text(SECRET))
        self.assertEqual(result["code"], "timeout")

    def test_unexpected_runner_error_is_contained(self):
        runner = FakeRunner(raises=OSError("boom"))
        client, _ = backend(runner=runner)
        result = client.execute(type_text(SECRET))
        self.assertEqual(result["code"], "command_failed")
        self.assertNotIn("boom", json.dumps(result))

    def test_default_runner_uses_a_new_session_and_no_shell(self):
        import inspect

        source = inspect.getsource(YdotoolBackend._default_runner)
        self.assertIn("start_new_session=True", source)
        self.assertNotIn("shell=True", source)
        self.assertIn("timeout", source)


class IntegrationWithDesktopInputTests(unittest.TestCase):
    def test_backend_satisfies_the_input_protocol(self):
        client, _ = backend()
        self.assertIsInstance(client, InputBackend)

    def test_desktop_input_reports_backend_refusals(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner, which=lambda name: None)
        result = DesktopInput(client).execute(press_key("ctrl+v"))
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "backend_failure")
        self.assertEqual(runner.calls, [])

    def test_desktop_input_success_is_forwarded(self):
        runner = FakeRunner()
        client, _ = backend(runner=runner)
        result = DesktopInput(client).execute(press_key("ctrl+v"))
        self.assertTrue(result["success"])
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
