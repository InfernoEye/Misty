"""An opt-in ``InputBackend`` that drives ``ydotool`` (uinput) subprocesses.

This is the only implementation of :class:`app.desktop.input.InputBackend` in
the tree.  It is **never constructed by default**: no module here builds one,
the registry takes it through an explicit constructor argument, and nothing
enables an input backend implicitly.  Callers that want it must pass a ready
instance.

Safety properties
-----------------
* Text is written to ``ydotool type --file -`` on **stdin** and never appears
  in argv, so it is not visible in ``ps``.  Keys go to ``ydotool key`` as key
  codes derived from the closed vocabulary in :mod:`app.desktop.input`.
* Every subprocess runs with ``start_new_session=True`` and a hard timeout;
  a timed-out child is killed by ``subprocess.run`` and reported as
  ``timeout``.
* Input is bounded (the ``InputAction`` validators already cap text and key
  combinations); nothing typed is logged, and results carry fixed messages.
* When ``ydotool`` is missing, or ``/dev/uinput`` is not usable (the
  ``input`` group is usually required), every call fails closed with a fixed
  refusal code instead of raising.
* Only ``type_text`` and ``press_key`` are supported.  Pointer actions are
  refused rather than guessed, and no modifier is ever left pressed.

Refusal codes (:data:`REFUSAL_CODES`): ``invalid_action``,
``unsupported_action``, ``backend_unavailable`` (no ``ydotool``),
``uinput_unavailable`` (``/dev/uinput`` missing or not read/write),
``timeout``, ``command_failed``.  The ``detail`` field distinguishes
``uinput_missing`` from ``uinput_denied``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping

from app.desktop.input import MAX_TEXT_LENGTH, validate_action

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PROVIDER_YDOTOOL",
    "REFUSAL_CODES",
    "UINPUT_PATH",
    "YdotoolBackend",
    "key_codes",
]

DEFAULT_TIMEOUT_SECONDS = 5.0
UINPUT_PATH = "/dev/uinput"
PROVIDER_YDOTOOL = "ydotool"
POINTER_TOLERANCE_PX = 2
POINTER_CORRECTION_ROUNDS = 8
POINTER_SETTLE_SECONDS = 0.25
POINTER_MAX_STEP_PX = 400
POINTER_MIN_MEASURABLE_PX = 8

REFUSAL_CODES = frozenset({
    "backend_unavailable",
    "uinput_unavailable",
    "unsupported_action",
    "invalid_action",
    "timeout",
    "command_failed",
    "pointer_not_verified",
})

# linux/input-event-codes.h codes.  Letters map to their evdev letter key;
# the same code types lower and upper case depending on Shift.
_EVDEV_KEYS = {
    **dict(zip("qwertyuiop", range(16, 26))),
    **dict(zip("asdfghjkl", range(30, 39))),
    **dict(zip("zxcvbnm", range(44, 51))),
    **{str(digit): 1 + digit for digit in range(1, 10)},
    "0": 11,
    "enter": 28,
    "tab": 15,
    "space": 57,
    "backspace": 14,
    "delete": 111,
    "insert": 110,
    "escape": 1,
    "left": 105,
    "right": 106,
    "up": 103,
    "down": 108,
    "home": 102,
    "end": 107,
    "pageup": 104,
    "pagedown": 109,
    **{f"f{number}": 58 + number for number in range(1, 11)},
    "f11": 87,
    "f12": 88,
}
_EVDEV_MODIFIERS = {"ctrl": 29, "alt": 56, "shift": 42, "meta": 125}
# ydotool 1.0.4 click codes: 0xC0 = down then up; the low byte picks the button.
_BUTTON_CODES = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
_LEFT_DOWN = "0x40"
_LEFT_UP = "0x80"

_MESSAGES = {
    "invalid_action": "Action must be a canonical input action.",
    "unsupported_action": "Only text and key input are supported.",
    "backend_unavailable": "ydotool is not installed.",
    "uinput_unavailable": "The uinput device is not writable by this user.",
    "timeout": "ydotool timed out.",
    "pointer_not_verified": "The pointer did not reach the requested position.",
    "command_failed": "ydotool reported an error.",
    "success": "Input action executed.",
}


def key_codes(combo):
    """Return evdev codes for a validated ``PressKey`` combo.

    ``"ctrl+alt+t"`` -> ``[29, 56, 20]``.  A modifier held as its own key is
    emitted once so the press sequence can release it once.
    """
    codes = []
    for token in combo.split("+"):
        if token in _EVDEV_MODIFIERS:
            code = _EVDEV_MODIFIERS[token]
        elif token in _EVDEV_KEYS:
            code = _EVDEV_KEYS[token]
        else:  # validate_action already rejected unknown tokens
            raise ValueError("Unknown key token.")
        if code not in codes:
            codes.append(code)
    return codes


def _clamp_step(value):
    return round(max(-POINTER_MAX_STEP_PX, min(POINTER_MAX_STEP_PX, value)))


def _measured_gain(previous, observed, requested):
    if abs(requested) < POINTER_MIN_MEASURABLE_PX or observed == 0:
        return previous
    return observed / requested


def _failure(code, detail=None):
    result = {
        "success": False,
        "code": code,
        "message": _MESSAGES[code],
    }
    if detail is not None:
        result["detail"] = detail
    return result


class YdotoolBackend:
    """``InputBackend`` over the ``ydotool`` binary.

    ``which``, ``exists``, ``access`` and ``runner`` are injectable so tests
    never execute ``ydotool``.  ``runner(argv, input=..., timeout=...)`` must
    behave like ``subprocess.run`` (return an object with ``returncode``).
    """

    provider = PROVIDER_YDOTOOL

    def __init__(
        self,
        binary=None,
        *,
        which=None,
        exists=None,
        access=None,
        runner=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        cursor_reader=None,
    ):
        self._binary = binary
        self._which = which or shutil.which
        self._exists = exists or os.path.exists
        self._cursor_reader = cursor_reader
        self._access = access or os.access
        self._runner = runner or self._default_runner
        self._timeout = float(timeout)

    # -- discovery ----------------------------------------------------
    def _resolve_binary(self):
        if isinstance(self._binary, str) and self._binary:
            return self._binary
        found = self._which("ydotool")
        return found if isinstance(found, str) and found else None

    @staticmethod
    def _default_runner(argv, input=None, timeout=None):
        return subprocess.run(
            argv,
            input=input,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )

    def _uinput_problem(self):
        if not self._exists(UINPUT_PATH):
            return "uinput_missing"
        if not self._access(UINPUT_PATH, os.R_OK | os.W_OK):
            return "uinput_denied"
        return None

    def capabilities(self):
        """Return a JSON-safe capability report; performs no input."""
        binary = self._resolve_binary()
        problem = self._uinput_problem()
        return {
            "provider": PROVIDER_YDOTOOL,
            "available": binary is not None and problem is None,
            "binary": binary,
            "uinput": UINPUT_PATH,
            "uinput_problem": problem,
            "supported_kinds": ["type_text", "press_key", "pointer_move",
                                "pointer_click", "pointer_drag", "scroll"],
            "max_text_length": MAX_TEXT_LENGTH,
            "timeout_seconds": self._timeout,
        }

    # -- argv construction -------------------------------------------
    def type_argv(self, binary=None):
        """``ydotool type --file -``: text travels on stdin only."""
        return [(binary or self._resolve_binary()), "type", "--file", "-"]

    def key_argv(self, combo, binary=None):
        """``ydotool key`` takes ``code:state`` pairs (1 = down, 0 = up).

        Modifiers go down in order, the key is tapped, and modifiers are released
        in reverse so nothing stays pressed.
        """
        binary = binary or self._resolve_binary()
        codes = key_codes(combo)
        *modifiers, key = codes
        events = [f"{code}:1" for code in modifiers]
        events += [f"{key}:1", f"{key}:0"]
        events += [f"{code}:0" for code in reversed(modifiers)]
        return [binary, "key", *events]

    def _release_keys(self, binary, combo):
        """Release every key a failed press may have left held down."""
        codes = key_codes(combo)
        releases = [f"{code}:0" for code in reversed(codes)]
        self._exec([binary, "key", *releases])

    def move_argv(self, x, y, binary=None):
        """Absolute pointer move: ``ydotool mousemove --absolute -x X -y Y``."""
        binary = binary or self._resolve_binary()
        return [binary, "mousemove", "--absolute", "-x", str(int(x)),
                "-y", str(int(y))]

    def click_argv(self, button="left", count=1, binary=None):
        """Pointer click: ``ydotool click --repeat N <button>``."""
        binary = binary or self._resolve_binary()
        argv = [binary, "click"]
        count = int(count)
        if count > 1:
            argv += ["--repeat", str(count)]
        argv.append(_BUTTON_CODES[button])
        return argv

    def scroll_argv(self, dx, dy, binary=None):
        """Relative wheel move: ``ydotool mousemove --wheel -x DX -y DY``.

        ydotool reports this as "Move mouse pointer or wheel"; ``--wheel``
        makes the deltas a relative wheel move (positive Y scrolls up), which
        is what a physical wheel emits. No scroll at all is refused so the
        caller never records a no-op as success.
        """
        binary = binary or self._resolve_binary()
        dx = int(dx)
        dy = int(dy)
        if dx == 0 and dy == 0:
            return None
        return [binary, "mousemove", "--wheel", "-x", str(dx), "-y", str(dy)]

    # -- execution ----------------------------------------------------
    def _exec(self, argv, input_bytes=None):
        try:
            completed = self._runner(
                argv, input=input_bytes, timeout=self._timeout
            )
        except subprocess.TimeoutExpired:
            return "timeout"
        except Exception:
            return "command_failed"
        if getattr(completed, "returncode", None) == 0:
            return None
        return "command_failed"

    def execute(self, action):
        """Run one canonical action; always returns fixed-message fields."""
        try:
            canonical = validate_action(action)
        except Exception:
            return _failure("invalid_action")

        text = None
        cursor_argv = None
        if canonical.kind == "type_text":
            text = canonical.fields["text"]
        elif canonical.kind == "press_key":
            pass
        elif canonical.kind == "pointer_move":
            cursor_argv = self.move_argv(
                canonical.fields["x"], canonical.fields["y"]
            )
        elif canonical.kind == "pointer_drag":
            return self.drag(
                (canonical.fields["start_x"], canonical.fields["start_y"]),
                (canonical.fields["end_x"], canonical.fields["end_y"]),
            )
        elif canonical.kind == "pointer_click":
            # A click is a move followed by the button press: clicking
            # wherever the cursor happens to be would hit the wrong target.
            cursor_argv = [
                self.move_argv(
                    canonical.fields["x"], canonical.fields["y"]
                ),
                self.click_argv(
                    canonical.fields.get("button", "left"),
                    canonical.fields.get("count", 1),
                ),
            ]
        elif canonical.kind == "scroll":
            cursor_argv = self.scroll_argv(
                canonical.fields.get("dx", 0), canonical.fields.get("dy", 0)
            )
            if cursor_argv is None:
                return _failure("invalid_action")
        else:
            return _failure("unsupported_action")

        binary = self._resolve_binary()
        if binary is None:
            return _failure("backend_unavailable")
        problem = self._uinput_problem()
        if problem is not None:
            return _failure("uinput_unavailable", problem)

        verified = None
        if text is not None:
            failure = self._exec(
                self.type_argv(binary), text.encode("utf-8")
            )
        elif canonical.kind in ("pointer_move", "pointer_click"):
            target = (canonical.fields["x"], canonical.fields["y"])
            failure = self._exec(self.move_argv(*target))
            if failure is None:
                verified = self._verify_pointer(binary, target)
                if verified is False or (
                    verified is None and canonical.kind == "pointer_click"
                ):
                    return _failure("pointer_not_verified")
            if failure is None and canonical.kind == "pointer_click":
                failure = self._exec(
                    self.click_argv(
                        canonical.fields.get("button", "left"),
                        canonical.fields.get("count", 1),
                        binary=binary,
                    )
                )
        elif cursor_argv is not None:
            commands = (
                cursor_argv if isinstance(cursor_argv, list)
                and cursor_argv and isinstance(cursor_argv[0], list)
                else [cursor_argv]
            )
            failure = None
            for command in commands:
                failure = self._exec(command)
                if failure is not None:
                    break
        else:
            combo = canonical.fields["combo"]
            failure = self._exec(self.key_argv(combo, binary))
            if failure is not None:
                self._release_keys(binary, combo)

        if failure is not None:
            return _failure(failure)
        return {"success": True, "verified": verified, "message": _MESSAGES["success"]}

    def drag(self, start, end):
        """Left-button drag: verified move to ``start``, press, verified move to ``end``, release.

        The button is released on every path after the press, so a failure
        midway never leaves it held down.
        """
        binary = self._resolve_binary()
        if self._verify_pointer(binary, start) is not True:
            return _failure("pointer_not_verified")
        if self._exec([binary, "click", _LEFT_DOWN]) is not None:
            return _failure("command_failed")
        try:
            failure = self._exec(self.move_argv(*end, binary=binary))
            if failure is None and self._verify_pointer(binary, end) is not True:
                failure = "pointer_not_verified"
        finally:
            self._exec([binary, "click", _LEFT_UP])
        if failure is not None:
            return _failure(failure)
        return {"success": True, "verified": True, "message": _MESSAGES["success"]}

    def _verify_pointer(self, binary, target):
        """Move until the real cursor is within tolerance of ``target``.

        Returns ``None`` when no cursor reader is configured, so the caller
        can report that the position was not checked. Otherwise returns
        ``True`` once within tolerance, or ``False`` after the bounded
        number of correction rounds.
        """
        if self._cursor_reader is None:
            return None
        target_x, target_y = target
        time.sleep(POINTER_SETTLE_SECONDS)
        x, y = self._cursor_reader()
        gain_x = gain_y = 1.0
        for _ in range(POINTER_CORRECTION_ROUNDS):
            dx, dy = target_x - x, target_y - y
            if abs(dx) <= POINTER_TOLERANCE_PX and abs(dy) <= POINTER_TOLERANCE_PX:
                return True
            step_x = _clamp_step(dx / gain_x)
            step_y = _clamp_step(dy / gain_y)
            if self._exec(
                [binary, "mousemove", "-x", str(step_x), "-y", str(step_y)]
            ) is not None:
                return False
            time.sleep(POINTER_SETTLE_SECONDS)
            new_x, new_y = self._cursor_reader()
            gain_x = _measured_gain(gain_x, new_x - x, step_x)
            gain_y = _measured_gain(gain_y, new_y - y, step_y)
            x, y = new_x, new_y
        return abs(target_x - x) <= POINTER_TOLERANCE_PX and abs(target_y - y) <= POINTER_TOLERANCE_PX
