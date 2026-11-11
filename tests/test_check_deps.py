"""Unit tests for scripts/check_deps.py.

Every test injects fake metadata, a fake ``shutil.which`` and a fake
filesystem, so nothing here depends on the real machine or the real venv.
"""

import importlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "misty_check_deps", ROOT / "scripts" / "check_deps.py"
)
assert _spec is not None and _spec.loader is not None
check_deps = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules[cls.__module__].
sys.modules[_spec.name] = check_deps
_spec.loader.exec_module(check_deps)


class ParseRequirementsTests(unittest.TestCase):
    def test_parses_only_pins_and_rejects_ranges(self):
        text = (
            "# header comment\n"
            "\n"
            "PySide6==6.12.0  # inline note\n"
            "dbus-python==1.5.1\n"
            "dbus_next==0.2.3\n"
        )
        self.assertEqual(
            check_deps.parse_requirements(text),
            [
                ("PySide6", "6.12.0"),
                ("dbus-python", "1.5.1"),
                ("dbus_next", "0.2.3"),
            ],
        )
        with self.assertRaises(ValueError):
            check_deps.parse_requirements("numpy>=2.0\n")


class ModuleMappingTests(unittest.TestCase):
    def test_distribution_names_map_to_import_names(self):
        expected = {
            "Pillow": "PIL",
            "beautifulsoup4": "bs4",
            "dbus-python": "dbus",
            "dbus_next": "dbus_next",
            "PyGObject": "gi",
            "PySide6": "PySide6",
            "numpy": "numpy",
        }
        for distribution, module in expected.items():
            with self.subTest(distribution=distribution):
                self.assertEqual(check_deps.module_for(distribution), module)


class CheckPythonPinsTests(unittest.TestCase):
    def _run(self, pins, versions, importable_modules):
        report = check_deps.Report()

        def version_of(name):
            return versions.get(check_deps.normalise(name))

        def importable(module):
            return module in importable_modules

        with mock.patch.object(
            check_deps.importlib.metadata, "version", side_effect=version_of
        ), mock.patch.object(
            importlib.util, "find_spec",
            side_effect=lambda name: object() if importable(name) else None,
        ):
            check_deps.check_python_pins(
                pins, report, version_of=version_of, importable=importable
            )
        return report

    def test_all_pins_match(self):
        report = self._run(
            [("Pillow", "13.0.0"), ("dbus-python", "1.5.1")],
            {"pillow": "13.0.0", "dbus-python": "1.5.1"},
            {"PIL", "dbus"},
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(len(report.results), 2)

    def test_version_mismatch_not_installed_and_unimportable_all_fail(self):
        report = self._run(
            [
                ("Pillow", "13.0.0"),
                ("psutil", "7.2.2"),
                ("PyGObject", "3.58.1"),
            ],
            {"pillow": "12.0.0", "pygobject": "3.58.1"},
            {"PIL"},
        )
        details = {item.name: item.detail for item in report.failures}
        self.assertEqual(len(details), 3)
        self.assertIn("installed 12.0.0", details["Pillow==13.0.0"])
        self.assertEqual(details["psutil==7.2.2"], "not installed")
        self.assertIn("`import gi` failed", details["PyGObject==3.58.1"])
        self.assertFalse(report.passed)


class CheckBinariesTests(unittest.TestCase):
    def test_required_missing_fails_optional_missing_does_not(self):
        report = check_deps.Report()
        present = {"ffmpeg": "/usr/bin/ffmpeg"}
        check_deps.check_binaries(
            [("ffmpeg", True), ("ydotool", True), ("ninja", False)],
            report,
            which=present.get,
        )
        self.assertEqual([item.name for item in report.failures], ["ydotool"])
        self.assertTrue(report.results[0].ok)
        self.assertEqual(report.results[0].detail, "/usr/bin/ffmpeg")
        self.assertTrue(report.results[2].ok)
        self.assertIn("optional", report.results[2].detail)


class RunChecksTests(unittest.TestCase):
    def test_end_to_end_aggregates_failures_and_print_report(self):
        """Fake metadata + fake FS: the runner wires all three checks and a
        single failing item makes the report (and the exit code) fail."""
        pins = "PySide6==6.12.0\npsutil==7.2.2\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(pins, encoding="utf-8")
            # Real files for two of the required paths; everything else is
            # missing, which is exactly what a fresh checkout looks like.
            (root / "config" / "shortcut.json").parent.mkdir(parents=True)
            (root / "config" / "shortcut.json").write_text("{}", "utf-8")
            report = check_deps.run_checks(
                root,
                version_of=lambda name: {
                    "pyside6": "6.12.0", "psutil": "7.2.1",
                }.get(name),
                importable=lambda module: True,
                which=lambda command: (
                    f"/usr/bin/{command}"
                    if command in {"ffmpeg", "git"} else None
                ),
            )
            stream = io.StringIO()
            check_deps.print_report(report, quiet=True, stream=stream)
            output = stream.getvalue()

        self.assertFalse(report.passed)
        keyed = {item.name: item for item in report.results}
        # Wrong installed version -> FAIL.
        self.assertFalse(keyed["psutil==7.2.2"].ok)
        self.assertIn("installed 7.2.1", keyed["psutil==7.2.2"].detail)
        # Missing required binary -> FAIL; present binary -> PASS.
        self.assertFalse(keyed["ydotool"].ok)
        self.assertTrue(keyed["git"].ok)
        # Real file passes the path check, absent model does not.
        self.assertTrue(keyed["shortcut config"].ok)
        self.assertFalse(keyed["whisper model"].ok)
        self.assertIn("check_deps: FAIL", output)
        self.assertIn("ydotool", output)
        self.assertNotIn("check_deps: PASS", output)


if __name__ == "__main__":
    unittest.main()
