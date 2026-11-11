"""Carefully scoped cleanup of orphaned Misty popup processes."""

import os
import signal
import sys
import time
from pathlib import Path


def _matches_popup_process(pid: int, root: Path, uid: int) -> bool:
    """Match only this user's popup worker launched from this project."""
    if pid == os.getpid():
        return False

    proc = Path("/proc") / str(pid)

    try:
        if proc.stat().st_uid != uid:
            return False

        cwd = Path(os.readlink(proc / "cwd")).resolve()
        if cwd != root.resolve():
            return False

        argv = (proc / "cmdline").read_bytes().decode(
            errors="replace"
        ).split("\0")
        argv = [part for part in argv if part]

        if len(argv) < 3 or argv[1:3] != ["-m", "app.popup_process"]:
            return False

        running_python = Path(argv[0]).resolve()
        expected_python = Path(sys.executable).resolve()
        return running_python == expected_python

    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return False


def cleanup_orphan_popup_processes(root: Path, timeout: float = 2.0) -> int:
    """Terminate matching old popup workers; never use broad process matching."""
    uid = os.getuid()
    found = []

    try:
        entries = list(Path("/proc").iterdir())
    except OSError as error:
        print(f"[Misty] Could not inspect processes: {error}", flush=True)
        return 0

    for entry in entries:
        if not entry.name.isdigit():
            continue

        pid = int(entry.name)
        if _matches_popup_process(pid, root, uid):
            found.append(pid)

    terminated = 0

    for pid in found:
        # Revalidate immediately before signalling to avoid PID-reuse mistakes.
        if not _matches_popup_process(pid, root, uid):
            continue

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            print(f"[Misty] Cannot stop stale popup PID {pid}.", flush=True)
            continue

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _matches_popup_process(pid, root, uid):
                break
            time.sleep(0.05)

        if _matches_popup_process(pid, root, uid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        terminated += 1
        print(f"[Misty] Cleaned up stale popup process {pid}.", flush=True)

    if not found:
        print("[Misty] No stale popup processes found.", flush=True)

    return terminated
