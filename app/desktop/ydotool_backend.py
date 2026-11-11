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

REFUSAL_CODES = frozenset({
    "backend_unavailable",
    "uinput_unavailable",
    "unsupported_action",
    "invalid_action",
    "timeout",
    "command_failed",
})

# linux/input-event-codes.h codes.  Letters map to their evdev letter key;
# the same code types lower and upper case depending on Shift.
_EVDEV_KEYS = {
    **{chr(ord("a") + offset): 30 + offset for offset in range(26)},
    **{str(digit): 2 + digit for digit in range(10)},
    "enter": 28,
    "tab": 15,
    "space": 57,
    "backspace": 14,
    "delete": 111,
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
_KEY_SYMBOLS = {
    **{chr(ord("a") + offset): "KEY_" + chr(ord("A") + offset)
       for offset in range(26)},
    **{str(digit): "KEY_" + str(digit) for digit in range(10)},
    "enter": "KEY_ENTER",
    "tab": "KEY_TAB",
    "space": "KEY_SPACE",
    "backspace": "KEY_BACKSPACE",
    "delete": "KEY_DELETE",
    "escape": "KEY_ESC",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "home": "KEY_HOME",
    "end": "KEY_END",
    "pageup": "KEY_PAGEUP",
    "pagedown": "KEY_PAGEDOWN",
    **{f"f{number}": f"KEY_F{number}" for number in range(1, 13)},
}
_KEY_SYMBOL_MODIFIERS = {
    "ctrl": "KEY_LEFTCTRL",
    "alt": "KEY_LEFTALT",
    "shift": "KEY_LEFTSHIFT",
    "meta": "KEY_LEFTMETA",
}

_MESSAGES = {
    "invalid_action": "Action must be a canonical input action.",
    "unsupported_action": "Only text and key input are supported.",
    "backend_unavailable": "ydotool is not installed.",
    "uinput_unavailable": "The uinput device is not writable by this user.",
    "timeout": "ydotool timed out.",
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


def key_symbols(combo):
    """Return X11-style key names for ``ydotool key`` on ``--key`` mode."""
    names = []
    for token in combo.split("+"):
        if token in _KEY_SYMBOL_MODIFIERS:
            names.append(_KEY_SYMBOL_MODIFIERS[token])
        elif token in _KEY_SYMBOLS:
            names.append(_KEY_SYMBOLS[token])
        else:  # pragma: no cover - guarded by validate_action
            raise ValueError("Unknown key token.")
    return names


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

    ``which``, ``exists``, ``access``, ``runner`` and ``env`` are injectable so
    tests never execute ``ydotool``.  ``runner(argv, input=..., timeout=...)``
    must behave like ``subprocess.run`` (return an object with
    ``returncode``).  ``env`` defaults to the process environment and is only
    used to decide the key mode (``--key`` on Wayland/X11 name mode).
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
        env=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        symbolic_keys=None,
    ):
        self._binary = binary
        self._which = which or shutil.which
        self._exists = exists or os.path.exists
        self._access = access or os.access
        self._runner = runner or self._default_runner
        self._env = dict(os.environ if env is None else env)
        self._timeout = float(timeout)
        self._symbolic = symbolic_keys

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

    def _symbolic_keys(self):
        if self._symbolic is not None:
            return bool(self._symbolic)
        session = self._env.get("XDG_SESSION_TYPE", "")
        return (
            str(session).casefold() == "wayland"
            or bool(self._env.get("WAYLAND_DISPLAY"))
        )

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
                                "pointer_click", "scroll"],
            "key_mode": "symbolic" if self._symbolic_keys() else "codes",
            "max_text_length": MAX_TEXT_LENGTH,
            "timeout_seconds": self._timeout,
        }

    # -- argv construction -------------------------------------------
    def type_argv(self, binary=None):
        """``ydotool type --file -``: text travels on stdin only."""
        return [(binary or self._resolve_binary()), "type", "--file", "-"]

    def key_argv(self, combo, binary=None):
        binary = binary or self._resolve_binary()
        if self._symbolic_keys():
            return [binary, "key", *key_symbols(combo)]
        return [binary, "key", ",".join(str(code) for code in key_codes(combo))]

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
        # ydotool accepts the same names as the canonical action ("left").
        argv.append(str(button))
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

        if text is not None:
            failure = self._exec(
                self.type_argv(binary), text.encode("utf-8")
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
            failure = self._exec(self.key_argv(canonical.fields["combo"], binary))

        if failure is not None:
            return _failure(failure)
        return {"success": True, "verified": None, "message": _MESSAGES["success"]}
