import json
import unittest
from dataclasses import FrozenInstanceError

from app.desktop.input import (
    ACTION_KINDS,
    ALLOWED_BUTTONS,
    ALLOWED_KEYS,
    ALLOWED_MODIFIERS,
    FAILURE_CODES,
    MAX_BATCH_ACTIONS,
    MAX_COORDINATE,
    MAX_SCROLL_DELTA,
    MIN_COORDINATE,
    MAX_TEXT_LENGTH,
    DesktopInput,
    InputAction,
    InputValidationError,
    PointerClick,
    PointerMove,
    PressKey,
    Scroll,
    TypeText,
    pointer_click,
    pointer_move,
    press_key,
    scroll,
    type_text,
    validate_action,
    validate_batch,
)


_DEFAULT = object()


class FakeBackend:
    """Records calls; performs no real input."""

    def __init__(self, result=_DEFAULT, error=None, before="before", after="after"):
        self.calls = []
        self.snapshot_calls = 0
        self.result = (
            {"success": True, "message": "fake ok", "verified": None}
            if result is _DEFAULT
            else result
        )
        self.error = error
        self.before = before
        self.after = after

    def execute(self, action):
        self.calls.append(action)
        if self.error is not None:
            raise self.error
        if callable(self.result):
            return self.result(action)
        return dict(self.result) if isinstance(self.result, dict) else self.result

    def snapshot_state(self):
        self.snapshot_calls += 1
        return self.before if self.snapshot_calls == 1 else self.after


class NoSnapshotBackend:
    def __init__(self):
        self.calls = []

    def execute(self, action):
        self.calls.append(action)
        return {"success": True}


class ValidatorTests(unittest.TestCase):
    def assert_invalid(self, code, factory, *args, **kwargs):
        with self.assertRaises(InputValidationError) as caught:
            factory(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertIn(code, FAILURE_CODES)
        self.assertTrue(caught.exception.message)

    # -- pointer_click -----------------------------------------------------

    def test_pointer_click_defaults_and_fields(self):
        action = pointer_click(10, 20)
        self.assertEqual(action.kind, "pointer_click")
        self.assertEqual(
            action.to_dict(),
            {
                "kind": "pointer_click",
                "fields": {
                    "x": 10,
                    "y": 20,
                    "button": "left",
                    "count": 1,
                    "screen": "",
                },
            },
        )

    def test_pointer_click_bounds_and_options(self):
        for x, y in ((0, 0), (-1, -1), (MIN_COORDINATE, MAX_COORDINATE)):
            action = pointer_click(x, y, button="right", count=10, screen="DP-1")
            self.assertEqual(action.fields["x"], x)
            self.assertEqual(action.fields["y"], y)
            self.assertEqual(action.fields["button"], "right")
            self.assertEqual(action.fields["count"], 10)
            self.assertEqual(action.fields["screen"], "DP-1")

    def test_pointer_click_rejects_non_integer_coordinates(self):
        for bad in (1.0, True, False, "5", None, [5], (5,)):
            with self.subTest(value=bad):
                self.assert_invalid("invalid_coordinates", pointer_click, bad, 0)

    def test_pointer_click_rejects_out_of_range_coordinates(self):
        for x, y in (
            (MIN_COORDINATE - 1, 0),
            (0, MIN_COORDINATE - 1),
            (MAX_COORDINATE + 1, 0),
            (0, MAX_COORDINATE + 1),
        ):
            with self.subTest(x=x, y=y):
                self.assert_invalid("invalid_coordinates", pointer_click, x, y)

    def test_pointer_click_rejects_bad_button(self):
        for bad in ("LEFT", "primary", "", None, 1, True):
            with self.subTest(value=bad):
                self.assert_invalid("invalid_action", pointer_click, 1, 2, bad)

    def test_pointer_click_rejects_bad_count(self):
        for bad in (0, -1, 11, True, False, 1.0, "1", None):
            with self.subTest(value=bad):
                self.assert_invalid("invalid_action", pointer_click, 1, 2, "left", bad)

    def test_pointer_click_rejects_bad_screen(self):
        for bad in (None, 4, True, "DP-1\n", "DP-1\x85", "S" * 65):
            with self.subTest(value=bad):
                self.assert_invalid(
                    "invalid_action", pointer_click, 1, 2, "left", 1, bad
                )

    def test_pointer_click_accepts_empty_and_max_screen(self):
        self.assertEqual(pointer_click(0, 0, screen="").fields["screen"], "")
        self.assertEqual(pointer_click(0, 0, screen="S" * 64).fields["screen"], "S" * 64)

    # -- pointer_move ------------------------------------------------------

    def test_pointer_move_valid(self):
        action = pointer_move(100, 200, screen="DP-2")
        self.assertEqual(action.kind, "pointer_move")
        self.assertEqual(action.fields, {"x": 100, "y": 200, "screen": "DP-2"})

    def test_pointer_move_rejects_bad_values(self):
        self.assert_invalid("invalid_coordinates", pointer_move, 10.0, 0)
        self.assert_invalid("invalid_coordinates", pointer_move, True, 0)
        self.assert_invalid("invalid_coordinates", pointer_move, 0, MAX_COORDINATE + 1)
        self.assert_invalid("invalid_action", pointer_move, 0, 0, screen=5)

    # -- scroll ------------------------------------------------------------

    def test_scroll_valid_bounds(self):
        for dx, dy in ((0, 0), (MAX_SCROLL_DELTA, -MAX_SCROLL_DELTA), (-5, 120)):
            action = scroll(dx, dy)
            self.assertEqual(action.kind, "scroll")
            self.assertEqual(action.fields, {"dx": dx, "dy": dy})

    def test_scroll_rejects_bad_values(self):
        for dx, dy in (
            (MAX_SCROLL_DELTA + 1, 0),
            (0, -MAX_SCROLL_DELTA - 1),
            (1.0, 0),
            (True, 0),
            ("1", 0),
            (None, 0),
        ):
            with self.subTest(dx=dx, dy=dy):
                self.assert_invalid("invalid_coordinates", scroll, dx, dy)

    # -- type_text ---------------------------------------------------------

    def test_type_text_accepts_newline_and_tab(self):
        text = "linia 1\nlinia 2\tZazółć 🙂"
        action = type_text(text)
        self.assertEqual(action.kind, "type_text")
        self.assertEqual(action.fields["text"], text)

    def test_type_text_accepts_empty_and_max_length(self):
        self.assertEqual(type_text("").fields["text"], "")
        self.assertEqual(
            type_text("a" * MAX_TEXT_LENGTH).fields["text"], "a" * MAX_TEXT_LENGTH
        )

    def test_type_text_rejects_non_string(self):
        for bad in (None, 123, True, b"bytes", ["x"]):
            with self.subTest(value=bad):
                self.assert_invalid("invalid_text", type_text, bad)

    def test_type_text_rejects_control_characters(self):
        for bad in ("\x00", "a\x01b", "\x1f", "\x7f", "\x85", "\r", "\ud800"):
            with self.subTest(text=repr(bad)):
                self.assert_invalid("invalid_text", type_text, bad)

    def test_type_text_rejects_oversize(self):
        self.assert_invalid("invalid_text", type_text, "a" * (MAX_TEXT_LENGTH + 1))

    def test_type_text_does_not_filter_secrets(self):
        action = type_text("password=hunter2")
        self.assertEqual(action.fields["text"], "password=hunter2")

    # -- press_key ---------------------------------------------------------

    def test_every_allowed_key_is_accepted(self):
        self.assertEqual(len(ALLOWED_KEYS), 62)
        for key in sorted(ALLOWED_KEYS):
            with self.subTest(key=key):
                self.assertEqual(press_key(key).fields["combo"], key)

    def test_press_key_modifiers_are_canonicalised(self):
        cases = {
            "Ctrl + Shift + T": "ctrl+shift+t",
            "SHIFT+CTRL+f12": "ctrl+shift+f12",
            "meta+alt+shift+pageup": "alt+shift+meta+pageup",
            "ctrl+alt+shift+enter": "ctrl+alt+shift+enter",
            " PageDown ": "pagedown",
        }
        for raw, expected in cases.items():
            with self.subTest(combo=raw):
                self.assertEqual(press_key(raw).fields["combo"], expected)

    def test_press_key_rejects_unknown_keys(self):
        for bad in ("f13", "nope", "ctrl+f0", "ctrl+deletee", "hyper+x"):
            with self.subTest(combo=bad):
                self.assert_invalid("invalid_key", press_key, bad)

    def test_super_is_an_alias_for_meta(self):
        # Desktop shortcuts are often written "Super+T" or "Win+R"; both mean
        # the meta modifier, so they are accepted and canonicalised.
        for raw, expected in (("super+t", "meta+t"), ("win+r", "meta+r"),
                              ("cmd+space", "meta+space")):
            with self.subTest(combo=raw):
                self.assertEqual(press_key(raw).fields["combo"], expected)

    def test_friendly_key_spellings_are_normalised(self):
        cases = {
            "Return": "enter",
            "esc": "escape",
            "page_up": "pageup",
            "Ctrl + Return": "ctrl+enter",
        }
        for raw, expected in cases.items():
            with self.subTest(combo=raw):
                self.assertEqual(press_key(raw).fields["combo"], expected)

    def test_press_key_rejects_too_many_modifiers(self):
        self.assert_invalid("invalid_key", press_key, "ctrl+alt+shift+meta+t")

    def test_press_key_rejects_duplicate_modifiers(self):
        self.assert_invalid("invalid_key", press_key, "ctrl+ctrl+t")

    def test_press_key_rejects_wrong_shape(self):
        for bad in ("ctrl", "ctrl+alt", "ctrl+t+u", "+", "ctrl++t", "ctrl+t+"):
            with self.subTest(combo=bad):
                self.assert_invalid("invalid_key", press_key, bad)

    def test_press_key_rejects_non_string_and_empty(self):
        for bad in (None, 1, True, b"ctrl+t", []):
            with self.subTest(value=bad):
                self.assert_invalid("invalid_key", press_key, bad)

    def test_press_key_rejects_oversize_combo(self):
        self.assert_invalid("invalid_key", press_key, "ctrl+" + "a" * 64)

    # -- shared error behaviour -------------------------------------------

    def test_validation_messages_do_not_reflect_input(self):
        marker = "do-not-reflect-me"
        with self.assertRaises(InputValidationError) as caught:
            type_text(marker + "\x00")
        self.assertEqual(caught.exception.code, "invalid_text")
        self.assertNotIn(marker, str(caught.exception))
        self.assertNotIn(marker, repr(caught.exception))

    def test_validation_error_rejects_unknown_codes(self):
        with self.assertRaises(ValueError):
            InputValidationError("not_a_code", "message")

    def test_all_factories_return_canonical_actions(self):
        for action in (
            pointer_click(1, 2),
            pointer_move(3, 4),
            scroll(5, -6),
            type_text("hello"),
            press_key("ctrl+enter"),
        ):
            self.assertIsInstance(action, InputAction)
            self.assertIn(action.kind, ACTION_KINDS)

    def test_spec_dataclasses_convert(self):
        self.assertEqual(PointerClick(1, 2).to_action().kind, "pointer_click")
        self.assertEqual(PointerMove(3, 4).to_action().kind, "pointer_move")
        self.assertEqual(Scroll(1, 2).to_action().kind, "scroll")
        self.assertEqual(TypeText("x").to_action().kind, "type_text")
        self.assertEqual(PressKey("enter").to_action().kind, "press_key")

    def test_allowlist_constants_are_bounded(self):
        self.assertEqual(ALLOWED_BUTTONS, frozenset({"left", "middle", "right"}))
        self.assertEqual(ALLOWED_MODIFIERS, frozenset({"ctrl", "alt", "shift", "meta"}))
        self.assertIn("enter", ALLOWED_KEYS)
        self.assertNotIn("insert", ALLOWED_KEYS)


class CanonicalActionTests(unittest.TestCase):
    def test_to_dict_is_json_safe_for_every_kind(self):
        actions = (
            pointer_click(1, 2, button="middle", count=2, screen="DP-1"),
            pointer_move(3, 4),
            scroll(-5, 6),
            type_text("line\nnext\t✓"),
            press_key("ctrl+shift+escape"),
        )
        for action in actions:
            with self.subTest(kind=action.kind):
                payload = action.to_dict()
                json.dumps(payload)
                self.assertEqual(set(payload), {"kind", "fields"})
                json.dumps(dict(action.fields))

    def test_to_dict_returns_a_fresh_copy(self):
        action = pointer_click(10, 20)
        payload = action.to_dict()
        payload["fields"]["x"] = 999
        payload["kind"] = "scroll"
        self.assertEqual(action.fields["x"], 10)
        self.assertEqual(action.kind, "pointer_click")

    def test_actions_are_hashable_and_equal_by_value(self):
        first = pointer_click(1, 2)
        second = validate_action(first)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_action_is_frozen(self):
        action = type_text("hello")
        with self.assertRaises(FrozenInstanceError):
            action.kind = "pointer_move"
        with self.assertRaises(FrozenInstanceError):
            action.fields = {}
        with self.assertRaises(TypeError):
            action.fields["text"] = "changed"

    def test_repr_hides_text_and_coordinates(self):
        text_action = type_text("super-secret-password")
        for rendered in (repr(text_action), str(text_action)):
            self.assertNotIn("super-secret-password", rendered)
            self.assertIn("redacted", rendered)

        click_action = pointer_click(12345, 67890)
        for rendered in (repr(click_action), str(click_action)):
            self.assertNotIn("12345", rendered)
            self.assertNotIn("67890", rendered)

    def test_validate_action_accepts_actions_specs_and_mapping(self):
        canonical = press_key("ctrl+t")
        self.assertEqual(validate_action(canonical).to_dict(), canonical.to_dict())
        self.assertEqual(validate_action(TypeText("x")).kind, "type_text")
        from_mapping = validate_action(
            {"kind": "pointer_move", "fields": {"x": 1, "y": 2, "screen": ""}}
        )
        self.assertEqual(from_mapping.kind, "pointer_move")

    def test_validate_action_revalidates_forged_actions(self):
        forged = InputAction("pointer_click", {"x": 1.5, "y": 2, "button": "left", "count": 1, "screen": ""})
        with self.assertRaises(InputValidationError) as caught:
            validate_action(forged)
        self.assertEqual(caught.exception.code, "invalid_coordinates")

        extra = InputAction("type_text", {"text": "x", "extra": "y"})
        with self.assertRaises(InputValidationError) as caught:
            validate_action(extra)
        self.assertEqual(caught.exception.code, "invalid_action")

    def test_validate_action_rejects_unknown_kind(self):
        with self.assertRaises(InputValidationError) as caught:
            validate_action({"kind": "shell_exec", "fields": {"command": "ls"}})
        self.assertEqual(caught.exception.code, "invalid_action")

    def test_validate_action_rejects_bad_envelopes(self):
        for bad in (
            None,
            "pointer_click",
            ["pointer_click", {}],
            {},
            {"kind": "pointer_click"},
            {"fields": {}},
            {"kind": "pointer_click", "fields": {}, "extra": 1},
            {"kind": 5, "fields": {}},
            {"kind": "pointer_click", "fields": []},
        ):
            with self.subTest(value=bad):
                with self.assertRaises(InputValidationError) as caught:
                    validate_action(bad)
                self.assertEqual(caught.exception.code, "invalid_action")

    def test_validate_action_rejects_missing_and_extra_fields(self):
        cases = (
            {"kind": "scroll", "fields": {"dx": 1}},
            {"kind": "scroll", "fields": {"dx": 1, "dy": 2, "dz": 3}},
            {"kind": "pointer_click", "fields": {"x": 1, "y": 2}},
            {"kind": "pointer_move", "fields": {"x": 1, "y": 2, "button": "left"}},
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(InputValidationError) as caught:
                    validate_action(case)
                self.assertEqual(caught.exception.code, "invalid_action")


class BatchTests(unittest.TestCase):
    def test_validate_batch_returns_canonical_tuple(self):
        batch = validate_batch([pointer_click(1, 2), type_text("x"), press_key("enter")])
        self.assertIsInstance(batch, tuple)
        self.assertEqual([action.kind for action in batch], ["pointer_click", "type_text", "press_key"])

    def test_validate_batch_default_limit(self):
        actions = [pointer_move(0, 0) for _ in range(MAX_BATCH_ACTIONS)]
        self.assertEqual(len(validate_batch(actions)), MAX_BATCH_ACTIONS)
        with self.assertRaises(InputValidationError) as caught:
            validate_batch(actions + [pointer_move(0, 0)])
        self.assertEqual(caught.exception.code, "invalid_action")

    def test_validate_batch_custom_limit(self):
        self.assertEqual(len(validate_batch([pointer_move(0, 0)], max_actions=1)), 1)
        with self.assertRaises(InputValidationError):
            validate_batch([pointer_move(0, 0), pointer_move(1, 1)], max_actions=1)

    def test_validate_batch_propagates_member_error(self):
        with self.assertRaises(InputValidationError) as caught:
            validate_batch([pointer_move(0, 0), press_key("not-a-key")])
        self.assertEqual(caught.exception.code, "invalid_key")

    def test_validate_batch_validates_max_actions(self):
        for bad in (0, -1, 101, True, 1.5, "20", None):
            with self.subTest(value=bad):
                with self.assertRaises(InputValidationError) as caught:
                    validate_batch([], max_actions=bad)
                self.assertEqual(caught.exception.code, "invalid_action")

    def test_validate_batch_rejects_non_sequences(self):
        for bad in ("abc", b"abc", {"kind": "scroll"}):
            with self.subTest(value=bad):
                with self.assertRaises(InputValidationError) as caught:
                    validate_batch(bad)
                self.assertEqual(caught.exception.code, "invalid_action")

    def test_validate_batch_accepts_generator(self):
        batch = validate_batch((pointer_move(i, i) for i in range(3)))
        self.assertEqual(len(batch), 3)

    def test_validate_batch_stops_endless_generators_at_the_cap(self):
        def endless():
            while True:
                yield pointer_move(0, 0)

        with self.assertRaises(InputValidationError) as caught:
            validate_batch(endless())
        self.assertEqual(caught.exception.code, "invalid_action")


class DesktopInputTests(unittest.TestCase):
    def assert_failure(self, result, code, kind=None, verified=None):
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], code)
        self.assertEqual(result["verified"], verified)
        if kind is not None:
            self.assertEqual(result["kind"], kind)
        json.dumps(result)

    def test_invalid_action_never_calls_backend(self):
        backend = FakeBackend()
        gateway = DesktopInput(backend, verifier=lambda *args: True)
        result = gateway.execute(
            {
                "kind": "pointer_click",
                "fields": {
                    "x": MAX_COORDINATE + 1,
                    "y": 2,
                    "button": "left",
                    "count": 1,
                    "screen": "",
                },
            }
        )
        self.assert_failure(result, "invalid_coordinates", kind="pointer_click")
        self.assertEqual(backend.calls, [])
        self.assertEqual(backend.snapshot_calls, 0)

    def test_invalid_action_never_calls_backend_without_verifier(self):
        backend = FakeBackend()
        gateway = DesktopInput(backend)
        cases = (
            (
                {
                    "kind": "pointer_click",
                    "fields": {
                        "x": 1.5,
                        "y": 2,
                        "button": "left",
                        "count": 1,
                        "screen": "",
                    },
                },
                "invalid_coordinates",
                "pointer_click",
            ),
            (
                {"kind": "type_text", "fields": {"text": "bad\x00"}},
                "invalid_text",
                "type_text",
            ),
            (
                {"kind": "press_key", "fields": {"combo": "hyper+t"}},
                "invalid_key",
                "press_key",
            ),
            ({"kind": "shell_exec", "fields": {}}, "invalid_action", None),
            ("raw model text", "invalid_action", None),
        )
        for action, code, kind in cases:
            with self.subTest(code=code):
                result = gateway.execute(action)
                self.assert_failure(result, code, kind=kind)
                self.assertEqual(backend.calls, [])
                self.assertEqual(backend.snapshot_calls, 0)

    def test_forged_non_string_kind_is_rejected_safely(self):
        backend = FakeBackend()
        result = DesktopInput(backend).execute(InputAction(["bad"], {}))
        self.assert_failure(result, "invalid_action")
        self.assertEqual(backend.calls, [])

    def test_backend_receives_frozen_canonical_action(self):
        backend = FakeBackend()
        gateway = DesktopInput(backend)
        result = gateway.execute({"kind": "pointer_click", "fields": {"x": 5, "y": 6, "button": "left", "count": 1, "screen": ""}})
        self.assertTrue(result["success"])
        self.assertEqual(len(backend.calls), 1)
        received = backend.calls[0]
        self.assertIsInstance(received, InputAction)
        self.assertEqual(received.to_dict()["fields"]["x"], 5)
        with self.assertRaises(TypeError):
            received.fields["x"] = 999
        with self.assertRaises(FrozenInstanceError):
            received.kind = "scroll"

    def test_backend_success_without_verifier(self):
        backend = FakeBackend(result={"success": True, "message": "ok"})
        result = DesktopInput(backend).execute(pointer_move(1, 2))
        self.assertTrue(result["success"])
        self.assertIsNone(result["code"])
        self.assertIsNone(result["verified"])
        self.assertEqual(result["kind"], "pointer_move")
        self.assertEqual(backend.snapshot_calls, 0)

    def test_backend_reported_verified_fallback(self):
        cases = ((True, True, True), (False, False, False), (None, True, None), ("yes", True, None), (1, True, None))
        for backend_verified, expected_success, expected_verified in cases:
            with self.subTest(backend_verified=backend_verified):
                backend = FakeBackend(
                    result={"success": True, "verified": backend_verified}
                )
                result = DesktopInput(backend).execute(pointer_move(1, 2))
                self.assertEqual(result["success"], expected_success)
                self.assertEqual(result["verified"], expected_verified)
                if not expected_success:
                    self.assertEqual(result["code"], "verification_failed")

    def test_backend_failure_mapping(self):
        backend = FakeBackend(
            result={"success": False, "message": "boom", "verified": True}
        )
        result = DesktopInput(backend).execute(pointer_move(1, 2))
        self.assert_failure(result, "backend_failure", kind="pointer_move", verified=None)
        self.assertNotIn("boom", result["message"])

    def test_backend_exception_is_safe(self):
        secret = "secret-text-and-9999"
        backend = FakeBackend(error=RuntimeError(secret))
        result = DesktopInput(backend).execute(type_text(secret))
        self.assert_failure(result, "backend_error", kind="type_text")
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, repr(result))

    def test_backend_invalid_results(self):
        for bad in (
            None,
            "ok",
            [],
            {},
            {"success": 1},
            {"success": "true"},
            {"success": None},
        ):
            with self.subTest(result=bad):
                backend = FakeBackend(result=bad)
                result = DesktopInput(backend).execute(pointer_move(1, 2))
                self.assert_failure(result, "backend_error", kind="pointer_move")

    def test_backend_message_is_not_reflected(self):
        backend = FakeBackend(
            result={"success": True, "message": "typed super-secret", "verified": None}
        )
        result = DesktopInput(backend).execute(type_text("super-secret"))
        self.assertNotIn("super-secret", json.dumps(result))
        self.assertEqual(result["message"], "Input action executed but could not be verified.")

    def test_verifier_true_semantics(self):
        seen = []

        def verifier(action, before, after):
            seen.append((action, before, after))
            return True

        backend = FakeBackend(before="B", after="A")
        result = DesktopInput(backend, verifier=verifier).execute(pointer_move(1, 2))
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertIsNone(result["code"])
        self.assertEqual(backend.snapshot_calls, 2)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0][0], InputAction)
        self.assertEqual(seen[0][1], "B")
        self.assertEqual(seen[0][2], "A")

    def test_verifier_false_semantics(self):
        backend = FakeBackend(result={"success": True, "verified": True})
        result = DesktopInput(backend, verifier=lambda *args: False).execute(
            pointer_move(1, 2)
        )
        self.assert_failure(
            result, "verification_failed", kind="pointer_move", verified=False
        )

    def test_verifier_none_semantics(self):
        backend = FakeBackend(result={"success": True, "verified": True})
        result = DesktopInput(backend, verifier=lambda *args: None).execute(
            pointer_move(1, 2)
        )
        self.assertTrue(result["success"])
        self.assertIsNone(result["verified"])
        self.assertIsNone(result["code"])
        self.assertIn("could not be verified", result["message"])

    def test_verifier_exception_yields_unverified(self):
        def verifier(*args):
            raise RuntimeError("verifier exploded")

        result = DesktopInput(FakeBackend(), verifier=verifier).execute(
            pointer_move(1, 2)
        )
        self.assertTrue(result["success"])
        self.assertIsNone(result["verified"])
        self.assertIsNone(result["code"])

    def test_verifier_truthy_non_boolean_is_not_trusted(self):
        for outcome in ("yes", 1, [True], object()):
            with self.subTest(outcome=outcome):
                result = DesktopInput(
                    FakeBackend(), verifier=lambda *args: outcome
                ).execute(pointer_move(1, 2))
                self.assertTrue(result["success"])
                self.assertIsNone(result["verified"])

    def test_verifier_not_called_when_backend_fails(self):
        calls = []

        def verifier(*args):
            calls.append(args)
            return True

        result = DesktopInput(
            FakeBackend(result={"success": False}), verifier=verifier
        ).execute(pointer_move(1, 2))
        self.assert_failure(result, "backend_failure", kind="pointer_move")
        self.assertEqual(calls, [])

    def test_verifier_overrides_backend_verified(self):
        backend = FakeBackend(result={"success": True, "verified": False})
        result = DesktopInput(backend, verifier=lambda *args: True).execute(
            pointer_move(1, 2)
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])

        backend = FakeBackend(result={"success": True, "verified": True})
        result = DesktopInput(backend, verifier=lambda *args: None).execute(
            pointer_move(1, 2)
        )
        self.assertTrue(result["success"])
        self.assertIsNone(result["verified"])

    def test_snapshot_failure_is_ignored(self):
        class BrokenSnapshotBackend(FakeBackend):
            def snapshot_state(self):
                self.snapshot_calls += 1
                raise RuntimeError("no snapshot")

        backend = BrokenSnapshotBackend()
        result = DesktopInput(backend, verifier=lambda action, before, after: before is None and after is None).execute(
            pointer_move(1, 2)
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertEqual(backend.snapshot_calls, 2)

    def test_backend_without_snapshot_support(self):
        backend = NoSnapshotBackend()
        result = DesktopInput(backend, verifier=lambda action, before, after: before is None and after is None).execute(
            pointer_move(1, 2)
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])

    def test_constructor_validation(self):
        with self.assertRaises(TypeError):
            DesktopInput(object())
        with self.assertRaises(TypeError):
            DesktopInput(type("B", (), {"execute": 5})())
        with self.assertRaises(TypeError):
            DesktopInput(FakeBackend(), verifier="not callable")

    def test_timeout_validation_and_storage(self):
        for bad in (0, -1, 30.5, 31, True, "5", float("nan"), float("inf")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    DesktopInput(FakeBackend(), timeout=bad)
        gateway = DesktopInput(FakeBackend(), timeout=2)
        self.assertEqual(gateway.timeout, 2.0)
        self.assertIsNone(DesktopInput(FakeBackend()).timeout)

    def test_timeout_is_not_passed_to_backend(self):
        backend = FakeBackend()
        result = DesktopInput(backend, timeout=5).execute(pointer_move(1, 2))
        self.assertTrue(result["success"])

    def test_result_is_json_safe(self):
        for result in (
            DesktopInput(FakeBackend()).execute(pointer_move(1, 2)),
            DesktopInput(FakeBackend(error=RuntimeError())).execute(pointer_move(1, 2)),
            DesktopInput(FakeBackend()).execute(
                {"kind": "type_text", "fields": {"text": "bad\x00"}}
            ),
            DesktopInput(FakeBackend(), verifier=lambda *args: False).execute(
                pointer_move(1, 2)
            ),
        ):
            json.dumps(result)
            self.assertEqual(
                set(result), {"success", "code", "message", "verified", "kind"}
            )

    def test_verified_failure_never_claims_success(self):
        class Backend:
            def execute(self, action):
                return {"success": False}

        result = DesktopInput(Backend(), verifier=lambda *args: True).execute(
            pointer_move(1, 2)
        )
        self.assertFalse(result["success"])
        self.assertIsNone(result["verified"])


if __name__ == "__main__":
    unittest.main()
