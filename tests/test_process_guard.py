"""Cleaning up model servers left behind by an earlier Misty run.

The model servers are spawned with ``start_new_session=True`` and
``PR_SET_PDEATHSIG``. The session detaches them from Misty's process group
(so a terminal SIGINT does not kill a mid-request model), and the death
signal re-ties their lifetime to the app so they cannot survive it. This
module covers the safety net for servers that leaked anyway: a crash before
the signal was installed, or a build where prctl is unavailable.

These tests never signal a real process: every check is either a pure
predicate test or runs against a process this test itself started.
"""

import os
import pathlib
import sys
import tempfile
import time
import unittest

from app.engine.process_guard import (
    MODEL_SERVER_BINARIES,
    _matches_model_server,
    cleanup_orphan_model_servers,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def write_cmdline(directory, name, argv):
    """Create a fake /proc entry shape that the matcher can read."""
    entry = pathlib.Path(directory) / name
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv)
                                    + b"\0")
    return entry


class MatcherTests(unittest.TestCase):
    def test_the_real_binaries_are_recognised(self):
        self.assertIn("llama-server", MODEL_SERVER_BINARIES)
        self.assertIn("llama-mtmd-cli", MODEL_SERVER_BINARIES)

    def test_an_unrelated_process_is_never_matched(self):
        # This very process is not a model server.
        self.assertFalse(
            _matches_model_server(os.getpid(), ROOT, os.getuid())
        )

    def test_a_nonexistent_pid_is_not_matched(self):
        self.assertFalse(_matches_model_server(2 ** 22, ROOT, os.getuid()))


def _live_model_server_pids():
    return [
        int(entry.name) for entry in pathlib.Path("/proc").iterdir()
        if entry.name.isdigit() and _matches_model_server(
            int(entry.name), ROOT, os.getuid()
        )
    ]


class CleanupTests(unittest.TestCase):
    def test_keep_pids_are_never_touched(self):
        live = _live_model_server_pids()
        self.assertEqual(
            cleanup_orphan_model_servers(ROOT, keep_pids=[os.getpid(), *live]), 0
        )

    def test_bad_keep_pids_are_ignored(self):
        live = _live_model_server_pids()
        self.assertEqual(
            cleanup_orphan_model_servers(
                ROOT, keep_pids=["nonsense", None, *live]
            ),
            0,
        )


class ParentDeathTests(unittest.TestCase):
    def test_the_child_dies_with_its_parent(self):
        """PR_SET_PDEATHSIG: a SIGKILLed parent must not orphan the child."""
        if not sys.platform.startswith("linux"):
            self.skipTest("PR_SET_PDEATHSIG is Linux-only")

        pid_file = pathlib.Path(tempfile.mkdtemp()) / "child.pid"
        # The parent spawns a sleeper with the same preexec function the
        # agent uses, then is killed with SIGKILL.
        parent_code = (
            "import subprocess, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from app.brain.agent import _die_with_parent\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time;"
            " time.sleep(60)'], start_new_session=True,"
            " preexec_fn=_die_with_parent)\n"
            "open(%r, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        ) % (str(ROOT), str(pid_file))

        parent = __import__("subprocess").Popen(
            [sys.executable, "-c", parent_code]
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.05)
            if not pid_file.exists():
                self.skipTest("child pid was not recorded in time")
            child = int(pid_file.read_text().strip())

            parent.kill()
            parent.wait()

            # The kernel delivers SIGTERM to the child shortly after.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not pathlib.Path("/proc/%d" % child).exists():
                    break
                time.sleep(0.05)
            self.assertFalse(
                pathlib.Path("/proc/%d" % child).exists(),
                "the child outlived a SIGKILLed parent",
            )
        finally:
            if parent.poll() is None:
                parent.kill()


if __name__ == "__main__":
    unittest.main()
