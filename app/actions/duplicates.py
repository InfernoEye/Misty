"""Read-only, scope-confined duplicate-file finder.

Only paths inside an approved user folder are scanned. Symbolic links and
special files are ignored, excluded directories are skipped, and all reads are
bounded by file count and a total hashing budget. Results contain relative
paths, sizes, and hashes only; no file contents are returned or logged.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time

from app.actions.file_scope import FileScopeError, UserFolderResolver
from app.actions.file_search import FileSearchService


MAX_FILES = 200_000
MAX_GROUPS = 1_000
MAX_MIN_BYTES = 1024 ** 3
MAX_RESULT_PATHS = 5_000
MAX_TOTAL_HASH_BYTES = 16 * 1024 ** 3
MAX_DEPTH = 32
CHUNK_SIZE = 1024 * 1024
# Hard ceilings for one scan, independent of a large max_files argument:
# every visited entry (files, directories, excluded names) counts, and the
# walk stops at the wall-clock deadline.
MAX_VISITED_ENTRIES = 100_000
DEFAULT_SCAN_SECONDS = 60.0

_CODES = {
    "invalid_folder": "The folder is not an approved, accessible directory.",
    "invalid_limit": "One of the scan limits is invalid.",
    "scan_failed": "The duplicate scan could not be completed.",
    "too_many_files": "The scan exceeded the file limit.",
    "too_many_bytes": "The scan exceeded the hashing budget.",
    "no_duplicates": "No duplicate files were found.",
}


def _failure(code):
    return {
        "success": False,
        "code": code,
        "message": _CODES.get(code, "The duplicate scan failed."),
        "groups": [],
        "duplicate_groups": 0,
        "duplicate_files": 0,
        "wasted_bytes": 0,
        "scanned_files": 0,
        "truncated": False,
        "truncation_reason": None,
    }


class DuplicateFinder:
    def __init__(self, resolver=None):
        self.resolver = resolver or UserFolderResolver()

    def find(
        self,
        folder,
        relative_path="",
        min_bytes=1,
        max_files=50_000,
        max_groups=100,
        timeout=None,
    ):
        if (
            not isinstance(min_bytes, int)
            or isinstance(min_bytes, bool)
            or not 1 <= min_bytes <= MAX_MIN_BYTES
        ):
            return _failure("invalid_limit")
        if (
            not isinstance(max_files, int)
            or isinstance(max_files, bool)
            or not 1 <= max_files <= MAX_FILES
        ):
            return _failure("invalid_limit")
        if (
            not isinstance(max_groups, int)
            or isinstance(max_groups, bool)
            or not 1 <= max_groups <= MAX_GROUPS
        ):
            return _failure("invalid_limit")
        if timeout is None:
            timeout = DEFAULT_SCAN_SECONDS
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 86_400
        ):
            return _failure("invalid_limit")

        try:
            root = self.resolver.resolve_key(str(folder), str(relative_path or ""))
        except (FileScopeError, OSError, RuntimeError, ValueError, TypeError):
            return _failure("invalid_folder")
        if not root.is_dir() or root.is_symlink():
            return _failure("invalid_folder")

        deadline = time.monotonic() + float(timeout)
        entry_budget = min(MAX_VISITED_ENTRIES, max_files)
        by_size = {}
        scanned = 0
        visited = 0
        truncated = False
        reason = None

        # One incremental os.scandir walk: a directory is never listed as a
        # whole in one call, the walk is depth-first in name order (so the
        # reported groups stay stable), and every visited entry -- not only
        # qualifying files -- is charged against the budget.
        try:
            stack = [self._open_sorted(root)]
        except OSError:
            return _failure("scan_failed")
        try:
            while stack:
                if time.monotonic() >= deadline:
                    truncated = True
                    reason = reason or "time limit"
                    break
                try:
                    entry = stack[-1].next()
                except StopIteration:
                    stack.pop().close()
                    continue
                except OSError:
                    stack.pop().close()
                    continue
                visited += 1
                if visited > entry_budget:
                    truncated = True
                    reason = reason or "entry limit"
                    break
                depth = len(stack) - 1
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if (
                            depth + 1 >= MAX_DEPTH
                            or entry.name.casefold()
                            in FileSearchService.EXCLUDED_NAMES
                        ):
                            continue
                        try:
                            child = self._open_sorted(entry.path)
                        except OSError:
                            continue
                        stack.append(child)
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.name.casefold() in FileSearchService.EXCLUDED_NAMES:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size < min_bytes:
                    continue
                if scanned >= max_files:
                    truncated = True
                    reason = reason or "file limit"
                    break
                scanned += 1
                by_size.setdefault(int(st.st_size), []).append(
                    (entry.path, int(st.st_size), int(st.st_mtime_ns))
                )
        finally:
            for walker in stack:
                walker.close()

        groups = []
        hashed_bytes = 0
        for size, entries in sorted(by_size.items()):
            if len(entries) < 2:
                continue
            by_hash = {}
            for path, expected_size, expected_mtime in entries:
                if time.monotonic() >= deadline:
                    truncated = True
                    reason = reason or "time limit"
                    break
                if hashed_bytes + size > MAX_TOTAL_HASH_BYTES:
                    truncated = True
                    reason = reason or "hashing budget"
                    break
                digest = self._hash_stable(
                    path,
                    expected_size,
                    expected_mtime,
                    MAX_TOTAL_HASH_BYTES - hashed_bytes,
                    min(deadline, time.monotonic() + _per_file_seconds(size)),
                )
                if digest is None:
                    continue
                hashed_bytes += digest[1]
                by_hash.setdefault(digest[0], []).append(path)
            for digest, matches in by_hash.items():
                if len(matches) < 2:
                    continue
                relative_paths = []
                for path in matches:
                    try:
                        relative_paths.append(
                            os.path.relpath(path, root).replace(os.sep, "/")
                        )
                    except ValueError:
                        continue
                if len(relative_paths) < 2:
                    continue
                if len(relative_paths) > MAX_RESULT_PATHS:
                    truncated = True
                    reason = reason or "result path limit"
                groups.append({
                    "size": size,
                    "sha256": digest,
                    "paths": relative_paths[:MAX_RESULT_PATHS],
                })
                if len(groups) >= max_groups:
                    truncated = True
                    reason = reason or "group limit"
                    break
            if len(groups) >= max_groups:
                break

        groups.sort(
            key=lambda item: (item["size"] * (len(item["paths"]) - 1)),
            reverse=True,
        )
        duplicate_files = sum(len(item["paths"]) for item in groups)
        wasted = sum(
            item["size"] * (len(item["paths"]) - 1) for item in groups
        )
        if not groups:
            return {
                **_failure("no_duplicates"),
                "success": True,
                "code": None,
                "message": "No duplicate files were found.",
                "scanned_files": scanned,
                "truncated": truncated,
                "truncation_reason": reason,
            }
        return {
            "success": True,
            "code": None,
            "message": (
                f"Found {len(groups)} duplicate group(s), "
                f"{wasted} byte(s) reclaimable."
            ),
            "groups": groups,
            "duplicate_groups": len(groups),
            "duplicate_files": duplicate_files,
            "wasted_bytes": wasted,
            "scanned_files": scanned,
            "truncated": truncated,
            "truncation_reason": reason,
        }

    @staticmethod
    def _open_sorted(directory):
        return _SortedWalk(directory)

    @staticmethod
    def _hash_stable(path, expected_size, expected_mtime, budget, deadline):
        """Hash one file, re-checking identity and enforcing the budget.

        Returns ``(hexdigest, bytes_read)`` or ``None`` when the file grew,
        shrank, was replaced or modified while it was read, is not regular,
        exceeds ``budget`` or the per-file ``deadline``.
        """
        budget = int(max(0, budget))
        if budget <= 0:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(
            os, "O_CLOEXEC", 0
        )
        try:
            fd = os.open(path, flags)
        except OSError:
            return None
        digest = hashlib.sha256()
        read_bytes = 0
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return None
            if (
                int(opened.st_size) != int(expected_size)
                or int(opened.st_mtime_ns) != int(expected_mtime)
            ):
                return None
            while True:
                if time.monotonic() >= deadline:
                    return None
                want = min(CHUNK_SIZE, budget - read_bytes)
                if want <= 0:
                    # Reading one more byte proves the file is longer.
                    if os.read(fd, 1):
                        return None
                    break
                try:
                    chunk = os.read(fd, want)
                except OSError:
                    return None
                if not chunk:
                    break
                digest.update(chunk)
                read_bytes += len(chunk)
            after = os.fstat(fd)
        except OSError:
            return None
        finally:
            os.close(fd)
        if (
            int(after.st_size) != read_bytes
            or int(after.st_size) != int(expected_size)
            or int(after.st_mtime_ns) != int(expected_mtime)
        ):
            return None
        return digest.hexdigest(), read_bytes


class _SortedWalk:
    """Yield one directory level in name order, lazily.

    The level's names are read with one ``os.listdir`` call and sorted (a
    list of names, not of entries), then handed out in sorted order. Entries
    are stat'ed on demand from a ``scandir`` iteration that is advanced as
    names are requested, so the walk can stop between any two entries and no
    ``DirEntry`` list is ever built.
    """

    def __init__(self, directory):
        self.directory = os.fspath(directory)
        self.names = sorted(os.listdir(self.directory), reverse=True)
        self.iterator = None
        self.spare = {}
        self.pending = frozenset(self.names)

    def next(self):
        if not self.names:
            raise StopIteration
        wanted = self.names[-1]
        cached = self.spare.pop(wanted, None)
        if cached is not None:
            self.names.pop()
            return cached
        if self.iterator is None:
            self.iterator = os.scandir(self.directory)
        while True:
            try:
                entry = next(self.iterator)
            except StopIteration:
                # The name vanished after the listing.
                self.names.pop()
                return _VanishedEntry(wanted)
            if entry.name in self.spare:
                continue
            if entry.name in self.pending:
                self.spare[entry.name] = entry
                if entry.name == wanted:
                    self.names.pop()
                    return self.spare.pop(entry.name)
                continue
            # Created after this level was listed: not part of it.
            continue

    def close(self):
        self.names = []
        self.spare = {}
        if self.iterator is not None:
            try:
                self.iterator.close()
            except OSError:
                pass
            self.iterator = None


class _VanishedEntry:
    """Placeholder for a name that disappeared while walking."""

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def is_symlink(self):
        return False

    def is_dir(self, follow_symlinks=False):
        return False

    def stat(self, follow_symlinks=False):
        raise FileNotFoundError(self.name)


def _per_file_seconds(size):
    """Per-file deadline slice, generous for slow disks but bounded."""
    return max(2.0, min(120.0, 2.0 + int(size) / (32 * 1024 * 1024)))
