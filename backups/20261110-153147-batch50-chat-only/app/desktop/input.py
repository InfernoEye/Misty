"""Validation-first desktop input abstraction.

This module deliberately contains no real input backend.  It defines a small,
closed vocabulary of pointer/keyboard actions, canonicalises and validates them,
and exposes an orchestrator (:class:`DesktopInput`) that delegates execution to
an injected backend.  Backends (uinput, portal, computer-use-linux, ...) are
expected to live elsewhere and to be injected explicitly.

Security properties:

* Only the allowlisted action kinds below can ever reach a backend.
* A backend receives a frozen, canonical :class:`InputAction`; it never sees raw
  model text, method names, or arbitrary keyword arguments.
* Validation happens before any backend call, including snapshot calls.
* Typed text and coordinates are never logged.  ``repr(InputAction)`` redacts all
  field values, and results carry fixed messages rather than backend text.
* Failures are reported with the fixed codes in :data:`FAILURE_CODES`.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Protocol, runtime_checkable

__all__ = [
    "ACTION_KINDS",
    "ALLOWED_BUTTONS",
    "ALLOWED_KEYS",
    "ALLOWED_MODIFIERS",
    "DesktopInput",
    "FAILURE_CODES",
    "InputAction",
    "InputBackend",
    "InputValidationError",
    "MAX_BATCH_ACTIONS",
    "MAX_BATCH_LIMIT",
    "MAX_CLICK_COUNT",
    "MAX_COMBO_LENGTH",
    "MAX_COORDINATE",
    "MAX_MODIFIERS",
    "MAX_SCREEN_LENGTH",
    "MAX_SCROLL_DELTA",
    "MAX_TEXT_LENGTH",
    "MAX_TIMEOUT_SECONDS",
    "MIN_CLICK_COUNT",
    "MIN_COORDINATE",
    "PointerClick",
    "PointerMove",
    "PressKey",
    "Scroll",
    "TypeText",
    "pointer_click",
    "pointer_move",
    "press_key",
    "scroll",
    "type_text",
    "validate_action",
    "validate_batch",
]

# ---------------------------------------------------------------------------
# Bounds and allowlists
# ---------------------------------------------------------------------------

MAX_COORDINATE = 100_000
# Symmetric bounds keep left/up monitors with negative virtual origins usable.
MIN_COORDINATE = -MAX_COORDINATE
MAX_CLICK_COUNT = 10
MIN_CLICK_COUNT = 1
MAX_SCROLL_DELTA = 1_000
MAX_TEXT_LENGTH = 2_000
MAX_MODIFIERS = 3
MAX_COMBO_LENGTH = 64
MAX_SCREEN_LENGTH = 64
MAX_BATCH_ACTIONS = 20
MAX_BATCH_LIMIT = 100
MAX_TIMEOUT_SECONDS = 30.0

ACTION_KINDS = frozenset(
    {"pointer_click", "pointer_move", "scroll", "type_text", "press_key"}
)
ALLOWED_BUTTONS = frozenset({"left", "middle", "right"})
MODIFIER_ORDER = ("ctrl", "alt", "shift", "meta")
ALLOWED_MODIFIERS = frozenset(MODIFIER_ORDER)

_NAMED_KEYS = frozenset(
    {
        "left",
        "right",
        "up",
        "down",
        "enter",
        "tab",
        "escape",
        "backspace",
        "delete",
        "home",
        "end",
        "pageup",
        "pagedown",
        "space",
    }
)
ALLOWED_KEYS = frozenset(
    {chr(code) for code in range(ord("a"), ord("z") + 1)}
    | {str(digit) for digit in range(10)}
    | {f"f{number}" for number in range(1, 13)}
    | _NAMED_KEYS
)

FAILURE_CODES = frozenset(
    {
        "invalid_action",
        "invalid_coordinates",
        "invalid_text",
        "invalid_key",
        "backend_error",
        "backend_failure",
        "verification_failed",
    }
)
_MESSAGES = {
    "invalid_action": "Action must be a canonical input action.",
    "unknown_kind": "Unknown input action kind.",
    "bad_fields": "Action fields do not match the action kind.",
    "invalid_coordinates": "Coordinates must be integers within the supported range.",
    "invalid_scroll": "Scroll deltas must be integers within the supported range.",
    "invalid_button": "Button must be left, middle, or right.",
    "invalid_count": "Click count must be an integer between 1 and 10.",
    "invalid_screen": "Screen identity must be a short printable string without control characters.",
    "invalid_text_type": "Text must be a string.",
    "invalid_text_control": "Text must not contain control characters other than newline and tab.",
    "invalid_text_length": "Text must not exceed 2000 characters.",
    "invalid_key_type": "Key combination must be a non-empty string.",
    "invalid_key_length": "Key combination is too long.",
    "invalid_key_token": "Key combination contains an empty or unknown key.",
    "invalid_key_modifiers": "Key combination contains too many modifiers.",
    "invalid_key_duplicate": "Key combination contains duplicate modifiers.",
    "invalid_key_shape": "Key combination must contain exactly one non-modifier key.",
    "invalid_batch": "Batch must be a sequence of input actions.",
    "invalid_batch_max": "max_actions must be an integer between 1 and 100.",
    "invalid_batch_size": "Batch contains too many actions.",
    "backend_error": "Input backend raised an unexpected error.",
    "backend_result": "Input backend returned an invalid result.",
    "backend_failure": "Input backend reported failure.",
    "verification_failed": "Input action could not be verified.",
    "success": "Input action executed.",
    "success_unverified": "Input action executed but could not be verified.",
}

# ---------------------------------------------------------------------------
# Errors and canonical action
# ---------------------------------------------------------------------------


class InputValidationError(ValueError):
    """A rejected action.  Messages are fixed and never echo user input."""

    def __init__(self, code: str, message: str) -> None:
        if code not in FAILURE_CODES:
            raise ValueError("Unknown input failure code.")
        self.code = code
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"InputValidationError(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class InputAction:
    """A canonical, immutable input action.

    Instances are normally produced by the factories below; :func:`validate_action`
    re-validates the kind and fields so forged instances cannot bypass checks.
    Field values are JSON-safe primitives.
    """

    kind: str
    fields: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def to_dict(self) -> dict[str, object]:
        """Return a fresh JSON-safe canonical dict."""
        return {"kind": self.kind, "fields": dict(self.fields)}

    def __hash__(self) -> int:
        return hash((self.kind, tuple(sorted(self.fields.items()))))

    def __repr__(self) -> str:  # Never expose typed text or coordinates.
        return f"InputAction(kind={self.kind!r}, fields=<redacted>)"


@dataclass(frozen=True, slots=True)
class PointerClick:
    x: int
    y: int
    button: str = "left"
    count: int = 1
    screen: str = ""

    def to_action(self) -> InputAction:
        return InputAction(
            "pointer_click",
            {
                "x": _valid_coordinate(self.x),
                "y": _valid_coordinate(self.y),
                "button": _valid_button(self.button),
                "count": _valid_count(self.count),
                "screen": _valid_screen(self.screen),
            },
        )


@dataclass(frozen=True, slots=True)
class PointerMove:
    x: int
    y: int
    screen: str = ""

    def to_action(self) -> InputAction:
        return InputAction(
            "pointer_move",
            {
                "x": _valid_coordinate(self.x),
                "y": _valid_coordinate(self.y),
                "screen": _valid_screen(self.screen),
            },
        )


@dataclass(frozen=True, slots=True)
class Scroll:
    dx: int
    dy: int

    def to_action(self) -> InputAction:
        return InputAction(
            "scroll",
            {
                "dx": _valid_scroll_delta(self.dx),
                "dy": _valid_scroll_delta(self.dy),
            },
        )


@dataclass(frozen=True, slots=True)
class TypeText:
    text: str

    def to_action(self) -> InputAction:
        return InputAction("type_text", {"text": _valid_text(self.text)})


@dataclass(frozen=True, slots=True)
class PressKey:
    combo: str

    def to_action(self) -> InputAction:
        return InputAction("press_key", {"combo": _valid_combo(self.combo)})


# ---------------------------------------------------------------------------
# Validators (fixed messages, no input reflection)
# ---------------------------------------------------------------------------


def _raise(code: str, message: str) -> None:
    raise InputValidationError(code, message)


def _valid_coordinate(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("invalid_coordinates", _MESSAGES["invalid_coordinates"])
    if value < MIN_COORDINATE or value > MAX_COORDINATE:
        _raise("invalid_coordinates", _MESSAGES["invalid_coordinates"])
    return value


def _valid_scroll_delta(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("invalid_coordinates", _MESSAGES["invalid_scroll"])
    if value < -MAX_SCROLL_DELTA or value > MAX_SCROLL_DELTA:
        _raise("invalid_coordinates", _MESSAGES["invalid_scroll"])
    return value


def _valid_button(value: object) -> str:
    if not isinstance(value, str) or value not in ALLOWED_BUTTONS:
        _raise("invalid_action", _MESSAGES["invalid_button"])
    return value


def _valid_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("invalid_action", _MESSAGES["invalid_count"])
    if value < MIN_CLICK_COUNT or value > MAX_CLICK_COUNT:
        _raise("invalid_action", _MESSAGES["invalid_count"])
    return value


def _valid_screen(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_SCREEN_LENGTH:
        _raise("invalid_action", _MESSAGES["invalid_screen"])
    for character in value:
        if unicodedata.category(character).startswith("C"):
            _raise("invalid_action", _MESSAGES["invalid_screen"])
    return value


def _valid_text(value: object) -> str:
    if not isinstance(value, str):
        _raise("invalid_text", _MESSAGES["invalid_text_type"])
    if len(value) > MAX_TEXT_LENGTH:
        _raise("invalid_text", _MESSAGES["invalid_text_length"])
    for character in value:
        if character in ("\n", "\t"):
            continue
        # Cc covers NUL and C0/C1 controls; Cs covers lone surrogates, which are
        # not reliably JSON-encodable.
        if unicodedata.category(character) in ("Cc", "Cs"):
            _raise("invalid_text", _MESSAGES["invalid_text_control"])
    return value


def _valid_combo(value: object) -> str:
    if not isinstance(value, str) or not value:
        _raise("invalid_key", _MESSAGES["invalid_key_type"])
    if len(value) > MAX_COMBO_LENGTH:
        _raise("invalid_key", _MESSAGES["invalid_key_length"])

    raw_tokens = value.split("+")
    tokens = [token.strip().lower() for token in raw_tokens]
    if any(not token for token in tokens):
        _raise("invalid_key", _MESSAGES["invalid_key_token"])

    modifiers: list[str] = []
    keys: list[str] = []
    for token in tokens:
        if token in ALLOWED_MODIFIERS:
            modifiers.append(token)
        else:
            keys.append(token)

    if len(modifiers) > MAX_MODIFIERS:
        _raise("invalid_key", _MESSAGES["invalid_key_modifiers"])
    if len(set(modifiers)) != len(modifiers):
        _raise("invalid_key", _MESSAGES["invalid_key_duplicate"])
    if len(keys) != 1:
        _raise("invalid_key", _MESSAGES["invalid_key_shape"])
    key = keys[0]
    if key not in ALLOWED_KEYS:
        _raise("invalid_key", _MESSAGES["invalid_key_token"])

    ordered = [modifier for modifier in MODIFIER_ORDER if modifier in modifiers]
    ordered.append(key)
    return "+".join(ordered)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def pointer_click(
    x: int, y: int, button: str = "left", count: int = 1, screen: str = ""
) -> InputAction:
    return PointerClick(x=x, y=y, button=button, count=count, screen=screen).to_action()


def pointer_move(x: int, y: int, screen: str = "") -> InputAction:
    return PointerMove(x=x, y=y, screen=screen).to_action()


def scroll(dx: int, dy: int) -> InputAction:
    return Scroll(dx=dx, dy=dy).to_action()


def type_text(text: str) -> InputAction:
    return TypeText(text=text).to_action()


def press_key(combo: str) -> InputAction:
    return PressKey(combo=combo).to_action()


# ---------------------------------------------------------------------------
# Canonical action validation
# ---------------------------------------------------------------------------

_FIELD_NAMES: dict[str, frozenset[str]] = {
    "pointer_click": frozenset({"x", "y", "button", "count", "screen"}),
    "pointer_move": frozenset({"x", "y", "screen"}),
    "scroll": frozenset({"dx", "dy"}),
    "type_text": frozenset({"text"}),
    "press_key": frozenset({"combo"}),
}

_SPEC_TYPES = (PointerClick, PointerMove, Scroll, TypeText, PressKey)


def _from_canonical(kind: object, fields: object) -> InputAction:
    if not isinstance(kind, str) or kind not in ACTION_KINDS:
        _raise("invalid_action", _MESSAGES["unknown_kind"])
    if not isinstance(fields, Mapping):
        _raise("invalid_action", _MESSAGES["bad_fields"])
    if any(not isinstance(key, str) for key in fields.keys()):
        _raise("invalid_action", _MESSAGES["bad_fields"])
    if frozenset(fields.keys()) != _FIELD_NAMES[kind]:
        _raise("invalid_action", _MESSAGES["bad_fields"])

    try:
        if kind == "pointer_click":
            return PointerClick(
                x=fields["x"],
                y=fields["y"],
                button=fields["button"],
                count=fields["count"],
                screen=fields["screen"],
            ).to_action()
        if kind == "pointer_move":
            return PointerMove(
                x=fields["x"], y=fields["y"], screen=fields["screen"]
            ).to_action()
        if kind == "scroll":
            return Scroll(dx=fields["dx"], dy=fields["dy"]).to_action()
        if kind == "type_text":
            return TypeText(text=fields["text"]).to_action()
        return PressKey(combo=fields["combo"]).to_action()
    except (KeyError, TypeError):
        # Malformed mappings must never leak internals.
        _raise("invalid_action", _MESSAGES["bad_fields"])


def validate_action(action: object) -> InputAction:
    """Return a fresh canonical action or raise :class:`InputValidationError`.

    Accepts a canonical :class:`InputAction`, one of the action spec dataclasses,
    or the canonical mapping shape ``{"kind": ..., "fields": {...}}``.
    """
    if isinstance(action, InputAction):
        return _from_canonical(action.kind, action.fields)
    if isinstance(action, _SPEC_TYPES):
        return action.to_action()
    if isinstance(action, Mapping):
        if frozenset(action.keys()) != frozenset({"kind", "fields"}):
            _raise("invalid_action", _MESSAGES["invalid_action"])
        return _from_canonical(action["kind"], action["fields"])
    _raise("invalid_action", _MESSAGES["invalid_action"])


_SPEC_KINDS: dict[type, str] = {
    PointerClick: "pointer_click",
    PointerMove: "pointer_move",
    Scroll: "scroll",
    TypeText: "type_text",
    PressKey: "press_key",
}


def _kind_hint(action: object) -> str | None:
    """Return a safe kind label for failure results; never raw input."""
    if isinstance(action, InputAction):
        kind = action.kind
        return kind if isinstance(kind, str) and kind in ACTION_KINDS else None
    for spec_type, kind in _SPEC_KINDS.items():
        if isinstance(action, spec_type):
            return kind
    if isinstance(action, Mapping):
        kind = action.get("kind")
        return kind if isinstance(kind, str) and kind in ACTION_KINDS else None
    return None


def validate_batch(
    actions: Iterable[object], max_actions: int = MAX_BATCH_ACTIONS
) -> tuple[InputAction, ...]:
    """Validate a whole batch before any of it is executed.

    The default cap is 20 actions; the hard cap is 100.  Invalid members abort the
    batch with their own structured error.
    """
    if (
        isinstance(max_actions, bool)
        or not isinstance(max_actions, int)
        or max_actions < 1
        or max_actions > MAX_BATCH_LIMIT
    ):
        _raise("invalid_action", _MESSAGES["invalid_batch_max"])
    if isinstance(actions, (str, bytes, bytearray, Mapping)):
        _raise("invalid_action", _MESSAGES["invalid_batch"])
    items: list[object] = []
    try:
        for action in actions:
            if len(items) >= max_actions:
                _raise("invalid_action", _MESSAGES["invalid_batch_size"])
            items.append(action)
    except TypeError:
        _raise("invalid_action", _MESSAGES["invalid_batch"])
    return tuple(validate_action(action) for action in items)


# ---------------------------------------------------------------------------
# Backend contract and orchestrator
# ---------------------------------------------------------------------------


@runtime_checkable
class InputBackend(Protocol):
    """Duck-typed backend contract.

    ``execute`` must return a mapping with a strict boolean ``success`` and may
    include ``message`` and ``verified``.  ``snapshot_state`` is optional and is
    only consulted when an independent verifier is configured.
    """

    def execute(self, action: InputAction) -> Mapping[str, object]: ...


Verifier = Callable[[InputAction, object, object], bool | None]


def _failure(
    code: str,
    message: str,
    *,
    kind: str | None = None,
    verified: bool | None = None,
) -> dict[str, object]:
    return {
        "success": False,
        "code": code,
        "message": message,
        "verified": verified,
        "kind": kind,
    }


def _success(
    *, kind: str, verified: bool | None, message: str = _MESSAGES["success"]
) -> dict[str, object]:
    return {
        "success": True,
        "code": None,
        "message": message,
        "verified": verified,
        "kind": kind,
    }


class DesktopInput:
    """Validate actions, then delegate them to an injected backend.

    The constructor accepts an advisory ``timeout``.  It is not passed to the
    backend because the backend contract stays deliberately small: backends own
    their own blocking/timeout behaviour, and this module starts no threads.
    """

    def __init__(
        self,
        backend: InputBackend,
        verifier: Verifier | None = None,
        timeout: float | None = None,
    ) -> None:
        if not callable(getattr(backend, "execute", None)):
            raise TypeError("backend must provide a callable execute(action).")
        if verifier is not None and not callable(verifier):
            raise TypeError("verifier must be callable or None.")
        if timeout is not None:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
                or timeout > MAX_TIMEOUT_SECONDS
            ):
                raise ValueError(
                    "timeout must be a number greater than 0 and at most 30."
                )
        self.backend = backend
        self.verifier = verifier
        self.timeout = float(timeout) if timeout is not None else None

    def execute(self, action: object) -> dict[str, object]:
        """Validate and execute one action.  Returns a JSON-safe result dict."""
        try:
            canonical = validate_action(action)
        except InputValidationError as error:
            return _failure(error.code, error.message, kind=_kind_hint(action))
        except Exception:
            return _failure("invalid_action", _MESSAGES["invalid_action"])

        before_state = self._snapshot() if self.verifier is not None else None

        try:
            raw_result = self.backend.execute(canonical)
        except Exception:  # Backend details are intentionally not reflected.
            return _failure(
                "backend_error", _MESSAGES["backend_error"], kind=canonical.kind
            )

        if not isinstance(raw_result, Mapping):
            return _failure(
                "backend_error", _MESSAGES["backend_result"], kind=canonical.kind
            )
        try:
            backend_success = raw_result.get("success")
        except Exception:
            return _failure(
                "backend_error", _MESSAGES["backend_result"], kind=canonical.kind
            )
        if not isinstance(backend_success, bool):
            return _failure(
                "backend_error", _MESSAGES["backend_result"], kind=canonical.kind
            )
        if not backend_success:
            return _failure(
                "backend_failure", _MESSAGES["backend_failure"], kind=canonical.kind
            )

        after_state = self._snapshot() if self.verifier is not None else None
        verified = self._verify(canonical, raw_result, before_state, after_state)

        if verified is False:
            return _failure(
                "verification_failed",
                _MESSAGES["verification_failed"],
                kind=canonical.kind,
                verified=False,
            )

        message = (
            _MESSAGES["success"]
            if verified is True
            else _MESSAGES["success_unverified"]
        )
        return _success(kind=canonical.kind, verified=verified, message=message)

    def _snapshot(self) -> object:
        snapshot = getattr(self.backend, "snapshot_state", None)
        if not callable(snapshot):
            return None
        try:
            return snapshot()
        except Exception:
            return None

    def _verify(
        self,
        action: InputAction,
        raw_result: Mapping[str, object],
        before_state: object,
        after_state: object,
    ) -> bool | None:
        if self.verifier is not None:
            try:
                outcome = self.verifier(action, before_state, after_state)
            except Exception:
                return None
            return outcome if isinstance(outcome, bool) else None

        # No independent verifier: accept a strict boolean self-report only.
        try:
            backend_verified = raw_result.get("verified")
        except Exception:
            return None
        return backend_verified if isinstance(backend_verified, bool) else None
