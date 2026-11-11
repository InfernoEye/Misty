"""Bounded, read-only, scope-confined full-text search over local files.

The model never chooses an arbitrary filesystem root. ``TextSearchService``
resolves an approved folder through :class:`UserFolderResolver`, then runs the
installed ``rg`` (ripgrep) executable with an argv list, no shell, a deadline,
and a hard output byte cap. Only the structured ``--json`` event stream is
read; stderr is discarded so no raw error text or file content can leak into a
result.

Results contain a path relative to the approved root, a line number, and at
most :data:`MAX_LINE_CHARS` characters of that line. Binary files, symlinks,
paths outside the root, and excluded directories are never returned.

This module performs no writes, starts no network or GUI components, and
imports no model, Qt, or desktop-automation code.
"""

from __future__ import annotations

import base64
import json
import os
import selectors
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver
from app.actions.file_search import FileSearchService


DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_LINE_CHARS = 500
MAX_QUERY_CHARS = 200
MIN_LIMIT = 1
MAX_LIMIT = 200
MAX_PER_FILE = 50
MAX_FILE_SIZE = "2M"

# The filename-search exclusions are reused verbatim so both discovery paths
# agree about caches, dependency trees, build output, and trash. A few cache
# and trash spellings are added for full-text search.
EXCLUDED_NAMES = frozenset(
    set(FileSearchService.EXCLUDED_NAMES)
    | {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".Trash",
        ".local/share/trash",
        "Trash",
    }
)


@dataclass(frozen=True, slots=True)
class _RunResult:
    """Outcome of one bounded ripgrep process.

    ``stopped`` means the caller's event consumer asked to stop early (the
    global result limit was reached), so the exit status is not meaningful.
    """

    returncode: int | None = None
    timed_out: bool = False
    too_large: bool = False
    launch_error: bool = False
    stopped: bool = False
    failed: bool = False


class TextSearchService:
    """Search file contents inside one approved folder.

    ``resolver`` defaults to :class:`UserFolderResolver` for the current user.
    ``rg_path`` defaults to ripgrep found on ``PATH``; an explicit path is
    accepted for an isolated installation or tests.
    """

    ERROR_MESSAGES = {
        "invalid_query": (
            "Search text must be 1-200 characters without control characters."
        ),
        "invalid_folder": "Folder is not an approved, accessible directory.",
        "invalid_limit": "Result limits must be within the allowed range.",
        "rg_missing": "The local ripgrep executable is unavailable.",
        "timeout": "The search timed out.",
        "output_too_large": "The search produced too much output.",
        "rg_error": "The search could not be completed.",
    }

    TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS
    MAX_OUTPUT_BYTES = MAX_OUTPUT_BYTES

    def __init__(self, resolver=None, rg_path=None):
        self.resolver = resolver or UserFolderResolver()
        self._rg_path = rg_path

    # -- public API -----------------------------------------------------

    def search(
        self,
        folder,
        query,
        limit=50,
        max_matches_per_file=10,
        case_sensitive=False,
        fixed_strings=False,
    ):
        """Search ``query`` in an approved ``folder``.

        ``folder`` may be an approved key (``"Documents"``), a key plus a
        relative path (``"Documents/Reports"``), or a path that resolves
        inside an approved folder. ``limit`` caps returned lines globally;
        ``max_matches_per_file`` caps ripgrep matches per file.
        """
        validated_query = self._validate_query(query)
        if validated_query is None:
            return self._failure("invalid_query")

        if not self._valid_limit(limit, MAX_LIMIT):
            return self._failure("invalid_limit")
        if not self._valid_limit(max_matches_per_file, MAX_PER_FILE):
            return self._failure("invalid_limit")

        try:
            root = self._resolve_root(folder)
        except (FileScopeError, OSError, RuntimeError, ValueError):
            return self._failure("invalid_folder")

        rg = self._rg_executable()
        if rg is None:
            return self._failure("rg_missing")

        argv = self._build_argv(
            rg,
            root,
            validated_query,
            max_matches_per_file,
            bool(case_sensitive),
            bool(fixed_strings),
        )

        state = {
            "results": [],
            "per_file": {},
            "paths": {},
            "binary": set(),
            "truncated": False,
            "truncation_reason": None,
        }

        def consume(raw_line):
            return self._consume_line(
                raw_line,
                state,
                root,
                limit,
                max_matches_per_file,
            )

        try:
            outcome = self._run(argv, consume)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Never let an operating-system detail or exception text reach
            # the caller; process failures are reported by stable code only.
            return self._failure("rg_error")

        if outcome.timed_out:
            return self._failure("timeout")
        if outcome.too_large:
            return self._failure("output_too_large")
        if outcome.launch_error:
            return self._failure("rg_missing")
        if outcome.failed:
            return self._failure("rg_error")
        if (
            not outcome.stopped
            and outcome.returncode is not None
            and outcome.returncode not in (0, 1)
        ):
            return self._failure("rg_error")

        results = state["results"]
        count = len(results)
        file_count = len({item["path"] for item in results})
        if count:
            message = f"Found {count} matching line(s) in {file_count} file(s)."
        else:
            message = "No matching text was found."
        if state["truncated"]:
            message += " Results were truncated."

        return {
            "success": True,
            "error": None,
            "message": message,
            "query": validated_query,
            "results": results,
            "count": count,
            "file_count": file_count,
            "truncated": state["truncated"],
            "truncation_reason": state["truncation_reason"],
        }

    # -- validation and scope ------------------------------------------

    @staticmethod
    def _valid_limit(value, maximum):
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and MIN_LIMIT <= value <= maximum
        )

    @staticmethod
    def _validate_query(query):
        if not isinstance(query, str):
            return None
        # Check the original value: stripping whitespace must not turn a
        # trailing control character into an acceptable query.
        for character in query:
            category = unicodedata.category(character)
            if category in {"Cc", "Cs"}:
                return None
        query = query.strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            return None
        return query

    def _resolve_root(self, folder):
        """Return a canonical directory inside an approved folder.

        Key-style values go through ``UserFolderResolver.child`` so parent
        traversal and symlink escapes are refused. An already-resolved path is
        mapped back to its approved base and rechecked through the resolver.
        """
        if isinstance(folder, Path):
            return self._resolve_absolute_root(folder)
        if not isinstance(folder, str):
            raise FileScopeError("Folder is not on the filesystem allowlist.")

        value = folder.strip()
        if not value:
            raise FileScopeError("Folder is not on the filesystem allowlist.")

        if value.startswith("~") or os.path.isabs(value):
            return self._resolve_absolute_root(Path(value))

        key, separator, relative = value.partition("/")
        if key not in self.resolver.folder_paths:
            raise FileScopeError("Folder is not on the filesystem allowlist.")
        if not separator:
            relative = ""

        base = self.resolver.base_for_key(key)
        if relative:
            self._refuse_symlink_path(base, relative)
        return self.resolver.child(base, relative)

    def _resolve_absolute_root(self, folder):
        raw = Path(folder).expanduser()
        try:
            if raw.is_symlink():
                raise FileScopeError("Symlinked roots are not allowed.")
            target = raw.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise FileScopeError("The folder is unavailable.") from error
        if not target.is_dir():
            raise FileScopeError("The folder is not a directory.")

        home = Path(self.resolver.home).expanduser().resolve(strict=True)
        if not target.is_relative_to(home):
            raise FileScopeError("The folder is outside the home directory.")

        # The resolved target hides any symlink that was used to reach it.
        # Recheck the caller's lexical path, from the configured home (or the
        # canonical home when the caller already supplied a resolved path).
        try:
            raw_absolute = Path(os.path.abspath(os.fspath(raw)))
            configured_home = Path(
                os.path.abspath(
                    os.fspath(Path(self.resolver.home).expanduser())
                )
            )
        except (OSError, ValueError, TypeError) as error:
            raise FileScopeError("The folder is unavailable.") from error
        if raw_absolute.is_relative_to(configured_home):
            self._refuse_symlink_between(configured_home, raw_absolute)
        elif raw_absolute.is_relative_to(home):
            self._refuse_symlink_between(home, raw_absolute)

        for key in self.resolver.folder_paths:
            try:
                base = self.resolver.base_for_key(key)
            except (FileScopeError, OSError, RuntimeError):
                continue
            if target == base or target.is_relative_to(base):
                relative = target.relative_to(base)
                return self.resolver.child(
                    base, relative.as_posix() if relative.parts else ""
                )
        raise FileScopeError("The folder is not inside an approved folder.")

    @staticmethod
    def _refuse_symlink_path(base, relative):
        candidate = Path(base) / Path(relative)
        current = Path(base)
        for part in candidate.relative_to(base).parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise FileScopeError("Symlinked paths are not allowed.")
            except OSError as error:
                raise FileScopeError("The folder is unavailable.") from error

    @staticmethod
    def _refuse_symlink_between(start, target):
        try:
            relative = Path(target).relative_to(start)
        except ValueError:
            return
        current = Path(start)
        for part in relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise FileScopeError("Symlinked paths are not allowed.")
            except OSError as error:
                raise FileScopeError("The folder is unavailable.") from error

    def _rg_executable(self):
        if self._rg_path is None:
            return shutil.which("rg")

        try:
            value = os.fspath(self._rg_path)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, str) or not value:
            return None
        value = os.path.expanduser(value)
        if os.sep in value or (os.altsep and os.altsep in value):
            if os.path.isfile(value) and os.access(value, os.X_OK):
                return value
            return None
        return shutil.which(value)

    # -- command construction ------------------------------------------

    @staticmethod
    def _build_argv(
        rg,
        root,
        query,
        max_matches_per_file,
        case_sensitive,
        fixed_strings,
    ):
        argv = [
            rg,
            "--json",
            "--no-messages",
            "--no-config",
            "--max-count",
            str(max_matches_per_file),
            "--max-filesize",
            MAX_FILE_SIZE,
            "--no-follow",
            "--hidden",
            "--no-ignore",
        ]
        for name in sorted(EXCLUDED_NAMES):
            argv.extend(["--glob", f"!{name}", "--glob", f"!{name}/**"])
        argv.append("--case-sensitive" if case_sensitive else "--ignore-case")
        if fixed_strings:
            argv.append("--fixed-strings")
        # ``--`` makes queries beginning with '-' inert and keeps the pattern
        # and root out of option parsing.
        argv.extend(["--", query, str(root)])
        return argv

    # -- bounded process execution -------------------------------------

    def _run(self, argv, consume):
        """Run one argv list, feeding JSON lines to ``consume``.

        This is the single subprocess seam. Tests inject a fake runner by
        overriding this method; production always uses an argv list, no shell,
        DEVNULL stdin, a piped stdout, and discarded stderr.
        """
        deadline = time.monotonic() + self.TIMEOUT_SECONDS
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                bufsize=0,
                close_fds=True,
            )
        except (OSError, ValueError):
            return _RunResult(launch_error=True)

        selector = selectors.DefaultSelector()
        buffer = bytearray()
        total = 0
        timed_out = False
        too_large = False
        stopped = False
        failed = False
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = selector.select(remaining)
                if not events:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    continue
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except OSError:
                    break
                if not chunk:
                    break
                total += len(chunk)
                if total > self.MAX_OUTPUT_BYTES:
                    too_large = True
                    break
                buffer.extend(chunk)
                if not self._consume_buffer(buffer, consume, final=False):
                    stopped = True
                    break

            if not timed_out and not too_large and not stopped:
                self._consume_buffer(buffer, consume, final=True)
        except (OSError, ValueError):
            failed = True
        finally:
            selector.close()
            if timed_out or too_large or stopped or failed:
                self._terminate(process)
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass

        returncode = None
        if not timed_out and not too_large and not stopped and not failed:
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                timed_out = True
        else:
            returncode = process.poll()

        return _RunResult(
            returncode=returncode,
            timed_out=timed_out,
            too_large=too_large,
            stopped=stopped,
            failed=failed,
        )

    @staticmethod
    def _consume_buffer(buffer, consume, final):
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if final and buffer:
                    raw = bytes(buffer)
                    buffer.clear()
                    return consume(raw)
                return True
            raw = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if not consume(raw):
                return False

    @staticmethod
    def _terminate(process):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # -- JSON event parsing --------------------------------------------

    def _consume_line(self, raw_line, state, root, limit, per_file):
        try:
            event = json.loads(raw_line)
        except (ValueError, TypeError, UnicodeDecodeError):
            return True
        if not isinstance(event, dict):
            return True

        event_type = event.get("type")
        if event_type == "begin" or event_type == "summary":
            return True
        data = event.get("data")
        if not isinstance(data, dict):
            return True

        if event_type == "end":
            self._handle_end(data, state, root)
            return True
        if event_type != "match":
            return True

        raw_path = self._event_text(data.get("path"))
        if raw_path is None:
            return True
        relative = self._cached_relative(raw_path, state, root)
        if relative is None or relative in state["binary"]:
            return True

        line_number = data.get("line_number")
        if (
            not isinstance(line_number, int)
            or isinstance(line_number, bool)
            or line_number < 1
        ):
            return True

        raw_text = self._event_text(data.get("lines"))
        if raw_text is None:
            return True
        if "\x00" in raw_text:
            self._mark_binary(relative, state)
            return True
        if "\x00" in raw_path:
            return True

        text, clipped = self._bounded_text(raw_text)
        if clipped:
            self._truncate(state, "line length")

        used = state["per_file"].get(relative, 0)
        if used >= per_file:
            self._truncate(state, "per-file limit")
            return True

        if len(state["results"]) >= limit:
            self._truncate(state, "result limit", force=True)
            return False

        state["results"].append(
            {
                "path": relative,
                "line_number": line_number,
                "text": text,
            }
        )
        state["per_file"][relative] = used + 1

        if len(state["results"]) >= limit:
            self._truncate(state, "result limit", force=True)
            return False
        return True

    def _handle_end(self, data, state, root):
        raw_path = self._event_text(data.get("path"))
        if raw_path is None:
            return
        relative = self._cached_relative(raw_path, state, root)
        if relative is None:
            return
        if data.get("binary_offset") is not None:
            self._mark_binary(relative, state)

    @staticmethod
    def _mark_binary(relative, state):
        state["binary"].add(relative)
        state["results"][:] = [
            item for item in state["results"] if item["path"] != relative
        ]
        state["per_file"].pop(relative, None)

    @staticmethod
    def _truncate(state, reason, force=False):
        state["truncated"] = True
        if force or state["truncation_reason"] is None:
            state["truncation_reason"] = reason

    @staticmethod
    def _event_text(value):
        if not isinstance(value, dict):
            return None
        text = value.get("text")
        if isinstance(text, str):
            return text
        encoded = value.get("bytes")
        if isinstance(encoded, str):
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                return None
            return raw.decode("utf-8", "replace")
        return None

    def _cached_relative(self, raw_path, state, root):
        cache = state["paths"]
        if raw_path in cache:
            return cache[raw_path]
        relative = self._relative_path(raw_path, root)
        cache[raw_path] = relative
        return relative

    def _relative_path(self, raw_path, root):
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            return None
        try:
            path = Path(raw_path)
        except (TypeError, ValueError):
            return None

        if path.is_absolute():
            try:
                relative = path.relative_to(root)
            except ValueError:
                try:
                    relative = Path(os.path.relpath(str(path), str(root)))
                except (ValueError, OSError):
                    return None
        else:
            relative = path

        if relative.is_absolute() or not relative.parts:
            return None
        if any(part in {"..", ""} for part in relative.parts):
            return None

        candidate = root / relative
        try:
            if candidate.is_symlink():
                return None
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_relative_to(root):
            return None
        return relative.as_posix()

    @staticmethod
    def _bounded_text(raw_text):
        text = raw_text
        if text.endswith("\n"):
            text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
        if len(text) > MAX_LINE_CHARS:
            return text[:MAX_LINE_CHARS], True
        return text, False

    @staticmethod
    def _failure(code):
        return {
            "success": False,
            "error": code,
            "message": TextSearchService.ERROR_MESSAGES[code],
            "results": [],
            "count": 0,
            "file_count": 0,
            "truncated": False,
            "truncation_reason": None,
        }
