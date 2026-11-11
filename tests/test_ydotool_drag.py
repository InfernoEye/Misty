import unittest

from app.desktop.ydotool_backend import YdotoolBackend
from test_ydotool_backend import FakeRunner, PerfectCursor, UINPUT_PATH, _Completed

PRESS = "0x40"
RELEASE = "0x80"


def backend(runner, cursor_reader):
    return YdotoolBackend(
        runner=runner,
        which=lambda name: "/usr/bin/ydotool",
        exists=lambda path: path == UINPUT_PATH,
        access=lambda path, mode: True,
        cursor_reader=cursor_reader,
    )


def button_codes(runner):
    return [call[0][-1] for call in runner.calls if call[0][1] == "click"]


class DragTests(unittest.TestCase):
    def test_a_drag_presses_moves_then_releases_the_left_button(self):
        runner = FakeRunner()
        cursor = PerfectCursor(runner)
        result = backend(cursor, cursor.read).drag((100, 200), (500, 600))
        self.assertTrue(result["success"], result)
        self.assertEqual(button_codes(runner), [PRESS, RELEASE])
        self.assertEqual(cursor.read(), (500, 600))

    def test_the_button_is_released_when_the_move_to_the_end_fails(self):
        runner = FailsOnSecondMove()
        cursor = PerfectCursor(runner)
        result = backend(cursor, cursor.read).drag((100, 200), (500, 600))
        self.assertFalse(result["success"])
        self.assertEqual(button_codes(runner)[-1], RELEASE)

    def test_no_button_is_pressed_when_the_start_point_does_not_verify(self):
        runner = FakeRunner()
        result = backend(runner, lambda: (0, 0)).drag((100, 200), (500, 600))
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "pointer_not_verified")
        self.assertNotIn(PRESS, button_codes(runner))


class FailsOnSecondMove(FakeRunner):
    def __init__(self):
        super().__init__()
        self.moves = 0

    def __call__(self, argv, input=None, timeout=None):
        if argv[1] == "mousemove":
            self.moves += 1
            if self.moves == 2:
                self.calls.append((argv, input, timeout))
                return _Completed(1)
        return super().__call__(argv, input, timeout)


if __name__ == "__main__":
    unittest.main()
