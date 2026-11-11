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
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver
from app.actions.file_search import FileSearchService


MAX_FILES = 200_000
MAX_GROUPS = 1_000
MAX_MIN_BYTES = 1024 ** 3
MAX_RESULT_PATHS = 5_000
MAX_TOTAL_HASH_BYTES = 16 * 1024 ** 3
MAX_DEPTH = 32
CHUNK_SIZE = 1024 * 1024

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

        try:
            root = self.resolver.resolve_key(str(folder), str(relative_path or ""))
        except (FileScopeError, OSError, RuntimeError, ValueError, TypeError):
            return _failure("invalid_folder")
        if not root.is_dir() or root.is_symlink():
            return _failure("invalid_folder")

        by_size = {}
        scanned = 0
        truncated = False
        reason = None

        try:
            for directory, dirnames, filenames in os.walk(
                root, followlinks=False
            ):
                depth = len(Path(directory).relative_to(root).parts)
                if depth >= MAX_DEPTH:
                    dirnames[:] = []
                    truncated = True
                    reason = reason or "depth budget"
                    continue
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if name.casefold() not in FileSearchService.EXCLUDED_NAMES
                    and not os.path.islink(os.path.join(directory, name))
                )
                for name in sorted(filenames):
                    if name.casefold() in FileSearchService.EXCLUDED_NAMES:
                        continue
                    path = Path(directory) / name
                    try:
                        entry = path.lstat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(entry.st_mode) or entry.st_size < min_bytes:
                        continue
                    if scanned >= max_files:
                        truncated = True
                        reason = reason or "file limit"
                        break
                    scanned += 1
                    by_size.setdefault(entry.st_size, []).append(path)
                if truncated and reason == "file limit":
                    break
        except OSError:
            return _failure("scan_failed")

        groups = []
        hashed_bytes = 0
        for size, paths in sorted(by_size.items()):
            if len(paths) < 2:
                continue
            by_hash = {}
            for path in paths:
                if hashed_bytes + size > MAX_TOTAL_HASH_BYTES:
                    truncated = True
                    reason = reason or "hashing budget"
                    break
                try:
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        while True:
                            chunk = handle.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            digest.update(chunk)
                            hashed_bytes += len(chunk)
                    by_hash.setdefault(digest.hexdigest(), []).append(path)
                except OSError:
                    continue
            for digest, matches in by_hash.items():
                if len(matches) < 2:
                    continue
                relative_paths = []
                for path in matches:
                    try:
                        relative_paths.append(
                            path.relative_to(root).as_posix()
                        )
                    except ValueError:
                        continue
                if len(relative_paths) < 2:
                    continue
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
