"""Tests for the isolated system telemetry service.

All psutil calls used by deterministic tests are mocked.  The final test case
is an optional, read-only smoke test against the real machine and only checks
tolerant ranges and JSON safety.
"""

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import psutil

from app.system import telemetry
from app.system.telemetry import SystemTelemetry

BOOT_TIMESTAMP = 1_700_000_000.0
NOW_TIMESTAMP = BOOT_TIMESTAMP + 3_725.0
GIB = 1024**3

PROCESS_KEYS = {"pid", "name", "memory_percent", "cpu_percent"}


class FakeProcess:
    """Minimal psutil.Process stand-in exposing only ``info``.

    Accessing ``cmdline`` is a privacy failure, so it raises immediately.
    """

    def __init__(self, info, error=None):
        self._info = info
        self._error = error

    @property
    def info(self):
        if self._error is not None:
            raise self._error
        return self._info

    @property
    def cmdline(self):
        raise AssertionError("telemetry must never read process cmdline")


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.cpu_percent = self._start("cpu_percent", return_value=12.3456)
        self.cpu_count = self._start("cpu_count", return_value=8)
        self.getloadavg = self._start(
            "getloadavg", return_value=(1.234, 2.346, 3.456)
        )
        self.virtual_memory = self._start(
            "virtual_memory",
            return_value=SimpleNamespace(
                total=16 * GIB,
                available=8 * GIB,
                used=8 * GIB,
                percent=50.04,
            ),
        )
        self.swap_memory = self._start(
            "swap_memory",
            return_value=SimpleNamespace(
                total=4 * GIB,
                used=1 * GIB,
                percent=25.06,
            ),
        )
        self.boot_time = self._start(
            "boot_time", return_value=BOOT_TIMESTAMP
        )
        self.pids = self._start("pids", return_value=[1, 2, 3])
        self.disk_usage = self._start(
            "disk_usage",
            return_value=SimpleNamespace(
                total=500 * GIB,
                used=200 * GIB,
                free=300 * GIB,
                percent=42.44,
            ),
        )

        now_patch = mock.patch.object(
            telemetry.time, "time", return_value=NOW_TIMESTAMP
        )
        self.addCleanup(now_patch.stop)
        now_patch.start()

    def _start(self, name, **kwargs):
        patch = mock.patch.object(telemetry.psutil, name, **kwargs)
        self.addCleanup(patch.stop)
        return patch.start()

    def test_summary_structure_rounding_and_timestamps(self):
        result = SystemTelemetry().summary()

        self.assertTrue(result["success"], result)
        self.assertEqual(result["message"], "System telemetry retrieved.")
        self.assertEqual(result["cpu_percent"], 12.3)
        self.assertEqual(result["cpu_count"], 8)
        self.assertEqual(result["load_average"], [1.23, 2.35, 3.46])
        self.assertEqual(
            result["memory"],
            {
                "total": 16 * GIB,
                "available": 8 * GIB,
                "used": 8 * GIB,
                "percent": 50.0,
            },
        )
        self.assertEqual(
            result["swap"],
            {"total": 4 * GIB, "used": 1 * GIB, "percent": 25.1},
        )
        self.assertEqual(result["uptime_seconds"], 3_725)
        self.assertEqual(result["process_count"], 3)
        self.assertEqual(
            result["disk"],
            {
                "path": "/",
                "total": 500 * GIB,
                "used": 200 * GIB,
                "free": 300 * GIB,
                "percent": 42.4,
            },
        )
        self.assertEqual(
            result["boot_time"],
            datetime.fromtimestamp(
                BOOT_TIMESTAMP, tz=timezone.utc
            ).isoformat(),
        )

        timestamp = datetime.fromisoformat(result["timestamp"])
        self.assertIsNotNone(timestamp.tzinfo)
        self.assertEqual(timestamp.utcoffset().total_seconds(), 0)
        self.assertEqual(result["timestamp"][-6:], "+00:00")

        # Expanded JSON must be strict: no NaN/Infinity and no namedtuples.
        json.dumps(result, allow_nan=False)

    def test_summary_calls_psutil_safely(self):
        SystemTelemetry().summary()

        self.cpu_percent.assert_called_once_with(interval=None)
        self.cpu_count.assert_called_once_with(logical=True)
        self.disk_usage.assert_called_once_with("/")

    def test_summary_disk_failure_is_a_fixed_safe_dict(self):
        with mock.patch.object(
            telemetry.psutil,
            "disk_usage",
            side_effect=PermissionError("denied"),
        ):
            result = SystemTelemetry().summary()

        self.assertEqual(
            result,
            {"success": False, "message": "Disk usage is unavailable."},
        )

    def test_summary_psutil_failure_is_a_fixed_safe_dict(self):
        with mock.patch.object(
            telemetry.psutil,
            "cpu_percent",
            side_effect=psutil.Error("synthetic failure"),
        ):
            result = SystemTelemetry().summary()

        self.assertEqual(
            result,
            {
                "success": False,
                "message": "System telemetry is unavailable.",
            },
        )

    def test_summary_load_failure_is_a_fixed_safe_dict(self):
        with mock.patch.object(
            telemetry.psutil,
            "getloadavg",
            side_effect=OSError("unsupported"),
        ):
            result = SystemTelemetry().summary()

        self.assertEqual(
            result,
            {"success": False, "message": "Load average is unavailable."},
        )

    def test_summary_generic_failure_never_raises(self):
        with mock.patch.object(
            telemetry.psutil,
            "virtual_memory",
            side_effect=RuntimeError("synthetic failure"),
        ):
            result = SystemTelemetry().summary()

        self.assertFalse(result["success"])
        self.assertEqual(
            result["message"], "System telemetry is unavailable."
        )

    def test_summary_values_are_bounded(self):
        self.cpu_percent.return_value = 150.0
        self.cpu_count.return_value = 99_999
        self.getloadavg.return_value = (-1.0, 1e12, float("nan"))
        self.virtual_memory.return_value = SimpleNamespace(
            total=100,
            available=90,
            used=999,
            percent=150.0,
        )
        self.swap_memory.return_value = SimpleNamespace(
            total=100,
            used=999,
            percent=-25.0,
        )
        self.disk_usage.return_value = SimpleNamespace(
            total=100,
            used=999,
            free=999,
            percent=150.0,
        )
        with mock.patch.object(
            telemetry.time, "time", return_value=BOOT_TIMESTAMP - 100
        ):
            result = SystemTelemetry().summary()

        self.assertTrue(result["success"], result)
        self.assertEqual(result["cpu_percent"], 100.0)
        self.assertEqual(result["cpu_count"], 4096)
        self.assertEqual(
            result["load_average"], [0.0, 1_000_000.0, 0.0]
        )
        self.assertEqual(result["memory"]["used"], 100)
        self.assertEqual(result["memory"]["available"], 90)
        self.assertEqual(result["memory"]["percent"], 100.0)
        self.assertEqual(result["swap"]["used"], 100)
        self.assertEqual(result["swap"]["percent"], 0.0)
        self.assertEqual(result["disk"]["used"], 100)
        self.assertEqual(result["disk"]["free"], 100)
        self.assertEqual(result["disk"]["percent"], 100.0)
        self.assertEqual(result["uptime_seconds"], 0)
        json.dumps(result, allow_nan=False)


class TopProcessesTests(unittest.TestCase):
    def _process(self, pid, name, memory, cpu):
        return FakeProcess(
            {
                "pid": pid,
                "name": name,
                "memory_percent": memory,
                "cpu_percent": cpu,
            }
        )

    def test_sorted_by_memory_with_limit_and_exact_keys(self):
        processes = [
            self._process(1, "one", 1.04, 9.0),
            self._process(2, "two", 5.06, 1.0),
            self._process(3, "three", 5.06, 2.0),
        ]
        with mock.patch.object(
            telemetry.psutil, "process_iter", return_value=processes
        ) as process_iter:
            result = SystemTelemetry().top_processes(
                limit=2, sort="memory"
            )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["sort"], "memory")
        self.assertEqual(result["limit"], 2)
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [item["pid"] for item in result["processes"]], [2, 3]
        )
        for item in result["processes"]:
            self.assertEqual(set(item), PROCESS_KEYS)
            self.assertEqual(item["memory_percent"], 5.1)
        process_iter.assert_called_once_with(
            attrs=["pid", "name", "memory_percent", "cpu_percent"]
        )
        json.dumps(result, allow_nan=False)

    def test_sorted_by_cpu_snapshot(self):
        processes = [
            self._process(1, "one", 1.0, 9.04),
            self._process(2, "two", 5.0, 1.0),
            self._process(3, "three", 3.0, 2.0),
        ]
        with mock.patch.object(
            telemetry.psutil, "process_iter", return_value=processes
        ):
            result = SystemTelemetry().top_processes(
                limit=3, sort="cpu"
            )

        self.assertTrue(result["success"], result)
        self.assertEqual(
            [item["pid"] for item in result["processes"]], [1, 3, 2]
        )
        self.assertEqual(result["processes"][0]["cpu_percent"], 9.0)

    def test_limit_validation(self):
        with mock.patch.object(
            telemetry.psutil, "process_iter"
        ) as process_iter:
            for limit in (0, -1, 21, 1.0, 20.0, "5", True, None, []):
                with self.subTest(limit=limit):
                    result = SystemTelemetry().top_processes(limit=limit)
                    self.assertEqual(
                        result,
                        {
                            "success": False,
                            "message": (
                                "Process limit must be an integer "
                                "between 1 and 20."
                            ),
                        },
                    )
                    process_iter.assert_not_called()

    def test_limit_bounds_are_accepted(self):
        processes = [self._process(1, "one", 1.0, 1.0)]
        with mock.patch.object(
            telemetry.psutil, "process_iter", return_value=processes
        ):
            for limit in (1, 20):
                with self.subTest(limit=limit):
                    result = SystemTelemetry().top_processes(limit=limit)
                    self.assertTrue(result["success"], result)
                    self.assertLessEqual(result["count"], limit)

    def test_sort_validation(self):
        with mock.patch.object(
            telemetry.psutil, "process_iter"
        ) as process_iter:
            for sort in ("pid", "", "Memory", "CPU", None, 1, "memory "):
                with self.subTest(sort=sort):
                    result = SystemTelemetry().top_processes(sort=sort)
                    self.assertEqual(
                        result,
                        {
                            "success": False,
                            "message": "Sort must be 'memory' or 'cpu'.",
                        },
                    )
                    process_iter.assert_not_called()

    def test_plain_dict_process_doubles_are_supported(self):
        processes = [
            {
                "pid": 9,
                "name": "dict-process",
                "memory_percent": 3.04,
                "cpu_percent": 4.04,
                "cmdline": ["--secret"],
            }
        ]
        with mock.patch.object(
            telemetry.psutil, "process_iter", return_value=processes
        ):
            result = SystemTelemetry().top_processes()

        self.assertTrue(result["success"], result)
        self.assertEqual(
            result["processes"],
            [
                {
                    "pid": 9,
                    "name": "dict-process",
                    "memory_percent": 3.0,
                    "cpu_percent": 4.0,
                }
            ],
        )
        self.assertNotIn("cmdline", result["processes"][0])

    def test_missing_and_denied_processes_are_skipped(self):
        processes = [
            self._process(10, "kept", 2.0, 1.0),
            FakeProcess(None, error=psutil.NoSuchProcess(pid=11)),
            FakeProcess(None, error=psutil.AccessDenied(pid=12)),
            FakeProcess(None, error=psutil.Error("synthetic")),
            FakeProcess({"pid": True, "name": "bool", "memory_percent": 1}),
            FakeProcess({"pid": "13", "name": "string", "memory_percent": 1}),
            FakeProcess(
                {
                    "pid": 14,
                    "name": "also kept",
                    "memory_percent": "nan",
                    "cpu_percent": None,
                }
            ),
            "not a process",
        ]
        with mock.patch.object(
            telemetry.psutil, "process_iter", return_value=processes
        ):
            result = SystemTelemetry().top_processes(limit=20)

        self.assertTrue(result["success"], result)
        self.assertEqual(
            [item["pid"] for item in result["processes"]], [10, 14]
        )
        self.assertEqual(result["processes"][1]["memory_percent"], 0.0)
        self.assertEqual(result["processes"][1]["cpu_percent"], 0.0)

    def test_name_is_sanitized_truncated_and_cmdline_never_read(self):
        long_name = "A" * 200 + "\nB"
        control_name = "line\nbreak\tname"
        processes = [
            FakeProcess(
                {
                    "pid": 1,
                    "name": long_name,
                    "memory_percent": 2.0,
                    "cpu_percent": 1.0,
                    "cmdline": ["--secret", "token"],
                }
            ),
            FakeProcess(
                {
                    "pid": 2,
                    "name": control_name,
                    "memory_percent": 1.0,
                    "cpu_percent": 1.0,
                }
            ),
            FakeProcess(
                {
                    "pid": 3,
                    "name": None,
                    "memory_percent": 1.0,
                    "cpu_percent": 1.0,
                }
            ),
            FakeProcess(
                {
                    "pid": 4,
                    "name": b"bytes-name",
                    "memory_percent": 1.0,
                    "cpu_percent": 1.0,
                }
            ),
        ]
        with mock.patch.object(
            telemetry.psutil, "process_iter", return_value=processes
        ):
            result = SystemTelemetry().top_processes(limit=20)

        names = {
            item["pid"]: item["name"] for item in result["processes"]
        }
        self.assertEqual(len(names[1]), 128)
        self.assertEqual(names[1], "A" * 128)
        self.assertEqual(names[2], "line break name")
        self.assertEqual(names[3], "unknown")
        self.assertEqual(names[4], "bytes-name")
        for item in result["processes"]:
            self.assertEqual(set(item), PROCESS_KEYS)
            self.assertNotIn("cmdline", item)

    def test_process_iter_failure_returns_safe_dict(self):
        with mock.patch.object(
            telemetry.psutil,
            "process_iter",
            side_effect=psutil.Error("synthetic failure"),
        ):
            result = SystemTelemetry().top_processes()

        self.assertEqual(
            result,
            {
                "success": False,
                "message": "Process information is unavailable.",
            },
        )

    def test_iteration_failure_returns_safe_dict(self):
        def broken():
            yield self._process(1, "one", 1.0, 1.0)
            raise psutil.Error("synthetic failure")

        with mock.patch.object(
            telemetry.psutil, "process_iter", side_effect=broken
        ):
            result = SystemTelemetry().top_processes()

        self.assertEqual(
            result,
            {
                "success": False,
                "message": "Process information is unavailable.",
            },
        )


@unittest.skipUnless(psutil is not None, "psutil is not available")
class RealTelemetrySmokeTests(unittest.TestCase):
    """Tolerant, read-only checks against the real machine."""

    def test_real_summary_smoke(self):
        result = SystemTelemetry().summary()
        self.assertIsInstance(result, dict)
        self.assertIn("success", result)
        self.assertIn("message", result)
        if not result["success"]:
            self.assertIsInstance(result["message"], str)
            return

        self.assertGreaterEqual(result["cpu_percent"], 0.0)
        self.assertLessEqual(result["cpu_percent"], 100.0)
        self.assertGreaterEqual(result["cpu_count"], 0)
        self.assertEqual(len(result["load_average"]), 3)
        self.assertGreaterEqual(result["memory"]["total"], 0)
        self.assertGreaterEqual(result["memory"]["used"], 0)
        self.assertGreaterEqual(result["swap"]["total"], 0)
        self.assertGreaterEqual(result["uptime_seconds"], 0)
        self.assertGreaterEqual(result["process_count"], 1)
        self.assertEqual(result["disk"]["path"], "/")
        self.assertGreaterEqual(result["disk"]["total"], 0)
        self.assertGreaterEqual(result["disk"]["percent"], 0.0)
        self.assertLessEqual(result["disk"]["percent"], 100.0)
        datetime.fromisoformat(result["boot_time"])
        datetime.fromisoformat(result["timestamp"])
        json.dumps(result, allow_nan=False)

    def test_real_top_processes_smoke(self):
        result = SystemTelemetry().top_processes(limit=3, sort="memory")
        self.assertIsInstance(result, dict)
        self.assertIn("success", result)
        if not result["success"]:
            self.assertIsInstance(result["message"], str)
            return

        self.assertLessEqual(len(result["processes"]), 3)
        for item in result["processes"]:
            self.assertEqual(set(item), PROCESS_KEYS)
            self.assertGreaterEqual(item["pid"], 1)
            self.assertIsInstance(item["name"], str)
            self.assertLessEqual(len(item["name"]), 128)
            self.assertGreaterEqual(item["memory_percent"], 0.0)
            self.assertLessEqual(item["memory_percent"], 100.0)
            self.assertGreaterEqual(item["cpu_percent"], 0.0)
            self.assertLessEqual(item["cpu_percent"], 100.0)
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
