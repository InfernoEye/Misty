"""Visual smoke-check contract; tests never launch a real model or desktop tool."""
import contextlib
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.brain.visual_decision import VisualDecisionCancelled, VisualDecisionTimeout
from scripts.check_visual_runtime import main, probe


class VisualCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        paths = {}
        for name in ("server_path", "model_path", "projector_path"):
            paths[name] = Path(self.temp.name) / name
            paths[name].write_bytes(b"test artifact, not executable")
        self.config = SimpleNamespace(**paths)
        self.runtime = Mock()
        self.runtime.start.return_value = SimpleNamespace(
            image_input=True, structured_output=True, probe_passed=True, grounding_verified=True)
        self.factory = Mock(return_value=self.runtime)

    def check(self, **kwargs):
        return probe(config=self.config, runtime_factory=self.factory, **kwargs)

    def test_default_checks_only_paths_and_starts_nothing(self):
        summary, code = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(summary["mode"], "artifacts_only")
        self.assertIsNone(summary["image_input"])
        self.assertFalse(summary["model_probe_attempted"])
        self.factory.assert_not_called()

    def test_missing_artifact_does_not_start_even_with_flags(self):
        self.config.projector_path = Path(self.temp.name) / "missing"
        summary, code = self.check(run_model=True, confirm_models_stopped=True)
        self.assertEqual(code, 2)
        self.assertEqual(summary["phase_gate"], "artifacts_missing")
        self.factory.assert_not_called()

    def test_model_requires_explicit_stopped_models_acknowledgement(self):
        with self.assertRaises(ValueError):
            self.check(run_model=True)
        self.factory.assert_not_called()

    def test_real_probe_success_is_not_desktop_grounding_or_input_readiness(self):
        summary, code = self.check(run_model=True, confirm_models_stopped=True)
        self.assertEqual(code, 0)
        self.assertTrue(summary["probe_passed"])
        self.assertFalse(summary["input_ready"])
        self.assertFalse(summary["grounding_verified"])
        self.runtime.close.assert_called_once()
        call = self.runtime.start.call_args
        self.assertIn("deadline", call.kwargs)
        self.assertIn("cancel", call.kwargs)

    def test_failure_timeout_and_cancel_all_close(self):
        for error, gate, code in ((RuntimeError("private detail"), "visual_probe_failed", 2),
                                  (VisualDecisionTimeout("private timeout"), "timeout", 2),
                                  (VisualDecisionCancelled("private cancel"), "canceled", 130)):
            with self.subTest(gate=gate):
                self.runtime.reset_mock()
                self.runtime.start.side_effect = error
                summary, actual = self.check(run_model=True, confirm_models_stopped=True)
                self.assertEqual(actual, code)
                self.assertEqual(summary["phase_gate"], gate)
                self.assertNotIn("private", str(summary))
                self.runtime.close.assert_called_once()

    def test_cleanup_failure_revokes_success(self):
        self.runtime.close.side_effect = RuntimeError("private cleanup")
        summary, code = self.check(run_model=True, confirm_models_stopped=True)
        self.assertEqual(code, 2)
        self.assertFalse(summary["cleanup_confirmed"])
        self.assertEqual(summary["phase_gate"], "cleanup_unconfirmed")

    def test_invalid_deadlines_are_rejected_without_launch(self):
        for timeout in (True, float("nan"), float("inf"), 0, 301):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.check(timeout=timeout)
        self.factory.assert_not_called()

    def test_cli_rejects_a_single_run_flag(self):
        for args in (["--run-model"], ["--confirm-models-stopped"]):
            with self.subTest(args=args), contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(args)


if __name__ == "__main__":
    unittest.main()
