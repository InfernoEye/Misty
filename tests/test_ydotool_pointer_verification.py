import unittest

from app.desktop.input import pointer_click, pointer_move
from app.desktop.ydotool_backend import YdotoolBackend
from test_ydotool_backend import TEST_LOG


class SimulatedPointer:
    """Pointer that lands each absolute move with a fixed offset, and tracks
    relative corrections, so the verification loop sees realistic drift."""

    def __init__(self, offset=(0, 0), start=(0, 0)):
        self.x, self.y = start
        self.offset = offset
        self.commands = []

    def runner(self, argv, input=None, timeout=None):
        self.commands.append(argv)
        if "--absolute" in argv:
            x = int(argv[argv.index("-x") + 1])
            y = int(argv[argv.index("-y") + 1])
            self.x = x + self.offset[0]
            self.y = y + self.offset[1]
        elif "mousemove" in argv:
            self.x += int(argv[argv.index("-x") + 1])
            self.y += int(argv[argv.index("-y") + 1])
        return _Done()

    def cursor(self):
        return self.x, self.y


class _Done:
    returncode = 0


def backend(pointer, **overrides):
    options = dict(
        which=lambda name: "/usr/bin/ydotool",
        exists=lambda path: True,
        access=lambda path, mode: True,
        runner=pointer.runner,
        cursor_reader=pointer.cursor,
        log_path=TEST_LOG,
    )
    options.update(overrides)
    return YdotoolBackend(**options)


def clicks(pointer):
    return [c for c in pointer.commands if "click" in c]


class VerifiedPointerMoveTests(unittest.TestCase):
    def test_a_move_that_lands_exactly_is_verified_without_correction(self):
        pointer = SimulatedPointer()
        result = backend(pointer).execute(pointer_move(600, 400))
        self.assertTrue(result["success"])
        self.assertIs(result["verified"], True)
        self.assertEqual(pointer.cursor(), (600, 400))
        self.assertEqual(len(pointer.commands), 1)

    def test_a_missed_move_is_corrected_then_verified(self):
        pointer = SimulatedPointer(offset=(-30, 12))
        result = backend(pointer).execute(pointer_move(600, 400))
        self.assertTrue(result["success"])
        self.assertIs(result["verified"], True)
        self.assertLessEqual(abs(pointer.cursor()[0] - 600), 2)
        self.assertLessEqual(abs(pointer.cursor()[1] - 400), 2)

    def test_a_move_that_never_converges_is_refused(self):
        class Stuck(SimulatedPointer):
            def runner(self, argv, input=None, timeout=None):
                self.commands.append(argv)
                return _Done()

        pointer = Stuck(start=(0, 0))
        result = backend(pointer).execute(pointer_move(600, 400))
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "pointer_not_verified")

    def test_a_click_never_happens_when_the_move_is_not_verified(self):
        class Stuck(SimulatedPointer):
            def runner(self, argv, input=None, timeout=None):
                self.commands.append(argv)
                return _Done()

        pointer = Stuck(start=(0, 0))
        result = backend(pointer).execute(pointer_click(600, 400))
        self.assertFalse(result["success"])
        self.assertEqual(clicks(pointer), [])

    def test_a_click_happens_only_after_the_pointer_is_verified(self):
        pointer = SimulatedPointer(offset=(-30, 12))
        result = backend(pointer).execute(pointer_click(600, 400))
        self.assertTrue(result["success"])
        self.assertEqual(len(clicks(pointer)), 1)

    def test_a_click_is_refused_without_a_cursor_reader(self):
        pointer = SimulatedPointer()
        result = backend(pointer, cursor_reader=None).execute(pointer_click(600, 400))
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "pointer_not_verified")
        self.assertEqual(clicks(pointer), [])

    def test_without_a_cursor_reader_the_move_is_unverified_not_faked(self):
        pointer = SimulatedPointer()
        result = backend(pointer, cursor_reader=None).execute(pointer_move(600, 400))
        self.assertTrue(result["success"])
        self.assertIsNone(result["verified"])
        self.assertEqual(len(pointer.commands), 1)

    def test_converges_when_relative_moves_are_accelerated(self):
        class Accelerated(SimulatedPointer):
            def runner(self, argv, input=None, timeout=None):
                if "mousemove" in argv and "--absolute" not in argv:
                    self.commands.append(argv)
                    dx = int(argv[argv.index("-x") + 1])
                    dy = int(argv[argv.index("-y") + 1])
                    self.x += round(dx * (1.9 if abs(dx) > 50 else 1.0))
                    self.y += round(dy * (1.9 if abs(dy) > 50 else 1.0))
                    return _Done()
                return super().runner(argv, input, timeout)

        pointer = Accelerated(offset=(-200, 150))
        result = backend(pointer).execute(pointer_move(600, 400))
        self.assertTrue(result["success"])
        self.assertIs(result["verified"], True)
        self.assertLessEqual(abs(pointer.cursor()[0] - 600), 2)
        self.assertLessEqual(abs(pointer.cursor()[1] - 400), 2)

    def test_correction_is_bounded(self):
        class Drifting(SimulatedPointer):
            def runner(self, argv, input=None, timeout=None):
                self.commands.append(argv)
                if "mousemove" in argv and "--absolute" not in argv:
                    self.x += int(argv[argv.index("-x") + 1]) // 2
                    self.y += int(argv[argv.index("-y") + 1]) // 2
                    return _Done()
                return super().runner(argv, input, timeout)

        pointer = Drifting(offset=(-500, -500))
        backend(pointer).execute(pointer_move(600, 400))
        self.assertLessEqual(len(pointer.commands), 8)


if __name__ == "__main__":
    unittest.main()
