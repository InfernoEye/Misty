"""Isolated, safety-first mutating file operations.

The model never supplies an absolute filesystem path. Callers pass one of the
approved folder keys understood by :class:`UserFolderResolver` plus a bounded
relative path. Every mutating entry point is confirmation gated and returns a
structured result dictionary; preview helpers expose the same plan without
touching the filesystem.

This module deliberately contains no registry wiring, UI, or model imports. It
is intended to be integrated later behind the existing confirmation broker.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver


MAX_CONTENT_BYTES = 1_048_576
MAX_PATH_LENGTH = 1024
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_DEPTH = 32
MAX_QUARANTINE_ATTEMPTS = 10

_MESSAGES = {
    "confirmation_required": "Confirmation is required. Nothing was changed.",
    "invalid_folder": "The folder is not on the approved allowlist.",
    "invalid_relative_path": "The relative path is not valid.",
    "empty_relative_path": "The relative path must not be empty.",
    "absolute_path": "Absolute paths are not allowed.",
    "traversal": "The path must not contain '..'.",
    "path_too_long": "The path is too long.",
    "path_too_deep": "The path is too deep.",
    "invalid_content": "The content must be text.",
    "content_encoding_error": "The content cannot be encoded as UTF-8.",
    "content_too_large": "The content is too large.",
    "missing_parent": "The parent directory does not exist.",
    "parent_not_directory": "A parent path component is not a directory.",
    "parent_unavailable": "A parent path component is unavailable.",
    "symlink_parent": "A parent path component is a symbolic link.",
    "symlink_target": "The target is a symbolic link.",
    "target_is_directory": "The target is a directory.",
    "target_is_file": "The target is a file.",
    "target_not_regular": "The target is not a regular file.",
    "target_exists": "The target already exists.",
    "target_not_found": "The target does not exist.",
    "target_unavailable": "The target is unavailable.",
    "non_empty_directory": "The directory is not empty.",
    "quarantine_unavailable": "The quarantine directory is not configured.",
    "invalid_quarantine_root": "The quarantine directory is not valid.",
    "quarantine_failed": "The quarantine directory could not be prepared.",
    "manifest_failed": "The quarantine manifest could not be written.",
    "write_failed": "The file could not be written.",
    "verification_failed": "The written file could not be verified.",
    "mkdir_failed": "The directory could not be created.",
    "trash_failed": "The item could not be moved to quarantine.",
    "cross_device": "The item cannot be moved across filesystems safely.",
    "internal_error": "The operation could not be completed.",
}


def _message_for(error_code):
    return _MESSAGES.get(error_code, _MESSAGES["internal_error"])


def _target_text(relative_path):
    return relative_path if isinstance(relative_path, str) else None


class FileOpsEngine:
    """Bounded mutating operations inside approved user folders.

    ``resolver`` defaults to the production :class:`UserFolderResolver`.
    ``quarantine_root`` is intentionally caller supplied so tests and callers
    can choose a private directory; it must be an absolute path.
    """

    def __init__(self, resolver=None, quarantine_root=None):
        self.resolver = resolver or UserFolderResolver()
        self.quarantine_root = (
            Path(quarantine_root).expanduser()
            if quarantine_root is not None
            else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def preview_write(self, folder, relative_path, content, overwrite=False):
        """Return the exact write plan without creating or changing anything."""
        action = "preview_write"
        try:
            context, error, preview = self._prepare_write(
                folder, relative_path, content, overwrite
            )
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                _target_text(relative_path),
                "internal_error",
            )
        if error:
            return self._error(
                action,
                _message_for(error),
                _target_text(relative_path),
                error,
                preview,
            )
        return self._result(
            action,
            True,
            "Preview only. No file was written.",
            str(context["relative"]),
            requires_confirmation=False,
            preview=preview,
            verified=False,
        )

    def preview_mkdir(self, folder, relative_path, parents=False):
        """Return the exact mkdir plan without creating anything."""
        action = "preview_mkdir"
        try:
            context, error, preview = self._prepare_mkdir(
                folder, relative_path, parents
            )
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                _target_text(relative_path),
                "internal_error",
            )
        if error:
            return self._error(
                action,
                _message_for(error),
                _target_text(relative_path),
                error,
                preview,
            )
        return self._result(
            action,
            True,
            "Preview only. No directory was created.",
            str(context["relative"]),
            requires_confirmation=False,
            preview=preview,
            verified=False,
        )

    def preview_trash(self, folder, relative_path, recursive=False):
        """Return the exact quarantine plan without moving anything."""
        action = "preview_trash"
        try:
            context, error, preview = self._prepare_trash(
                folder, relative_path, recursive
            )
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                _target_text(relative_path),
                "internal_error",
            )
        if error:
            return self._error(
                action,
                _message_for(error),
                _target_text(relative_path),
                error,
                preview,
            )
        return self._result(
            action,
            True,
            "Preview only. Nothing was moved.",
            str(context["relative"]),
            requires_confirmation=False,
            preview=preview,
            verified=False,
        )

    def write_text(
        self,
        folder,
        relative_path,
        content,
        overwrite=False,
        confirmed=False,
    ):
        """Atomically write UTF-8 text inside an approved folder.

        Without ``confirmed=True`` the result has ``requires_confirmation``
        set and ``preview`` populated. Nothing is created or changed.
        """
        action = "write_text"
        try:
            context, error, preview = self._prepare_write(
                folder, relative_path, content, overwrite
            )
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                _target_text(relative_path),
                "internal_error",
            )
        if error:
            return self._error(
                action,
                _message_for(error),
                _target_text(relative_path),
                error,
                preview,
            )
        if not confirmed:
            return self._result(
                action,
                False,
                _message_for("confirmation_required"),
                str(context["relative"]),
                requires_confirmation=True,
                preview=preview,
                verified=False,
                error_code="confirmation_required",
            )

        try:
            return self._perform_write(action, context, preview)
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                str(context["relative"]),
                "internal_error",
                preview,
            )

    def mkdir(
        self,
        folder,
        relative_path,
        parents=False,
        confirmed=False,
    ):
        """Create one directory, optionally creating missing parents."""
        action = "mkdir"
        try:
            context, error, preview = self._prepare_mkdir(
                folder, relative_path, parents
            )
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                _target_text(relative_path),
                "internal_error",
            )
        if error:
            return self._error(
                action,
                _message_for(error),
                _target_text(relative_path),
                error,
                preview,
            )
        if not confirmed:
            return self._result(
                action,
                False,
                _message_for("confirmation_required"),
                str(context["relative"]),
                requires_confirmation=True,
                preview=preview,
                verified=False,
                error_code="confirmation_required",
            )

        try:
            return self._perform_mkdir(action, context, preview)
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                str(context["relative"]),
                "internal_error",
                preview,
            )

    def trash(
        self,
        folder,
        relative_path,
        confirmed=False,
        recursive=False,
    ):
        """Move a file or directory into a private quarantine.

        Empty directories are moved by default. Non-empty directories are
        refused unless ``recursive=True`` and ``confirmed=True``. Nothing is
        ever permanently deleted here.
        """
        action = "trash"
        try:
            context, error, preview = self._prepare_trash(
                folder, relative_path, recursive
            )
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                _target_text(relative_path),
                "internal_error",
            )
        if error:
            return self._error(
                action,
                _message_for(error),
                _target_text(relative_path),
                error,
                preview,
            )
        if not confirmed:
            return self._result(
                action,
                False,
                _message_for("confirmation_required"),
                str(context["relative"]),
                requires_confirmation=True,
                preview=preview,
                verified=False,
                error_code="confirmation_required",
            )

        try:
            return self._perform_trash(action, context, preview)
        except Exception:
            return self._error(
                action,
                _message_for("internal_error"),
                str(context["relative"]),
                "internal_error",
                preview,
            )

    # ------------------------------------------------------------------
    # Validation and preparation
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        action,
        success,
        message,
        target=None,
        requires_confirmation=False,
        preview=None,
        verified=False,
        error_code=None,
    ):
        return {
            "success": bool(success),
            "action": action,
            "requires_confirmation": bool(requires_confirmation),
            "message": message,
            "target": target,
            "preview": preview,
            "verified": bool(verified),
            "error_code": error_code,
        }

    def _error(self, action, message, target, error_code, preview=None):
        return self._result(
            action,
            False,
            message,
            target=target,
            requires_confirmation=False,
            preview=preview,
            verified=False,
            error_code=error_code,
        )

    def _base_for(self, folder):
        try:
            if not isinstance(folder, str) or not folder:
                return None, "invalid_folder"
            base = self.resolver.base_for_key(folder)
        except (
            FileScopeError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            return None, "invalid_folder"
        except Exception:
            return None, "invalid_folder"

        try:
            base = Path(base)
            if not base.is_dir():
                return None, "invalid_folder"
            if base.is_symlink():
                return None, "invalid_folder"
        except (OSError, RuntimeError, ValueError):
            return None, "invalid_folder"
        return base, None

    @staticmethod
    def _relative_path(relative_path):
        if not isinstance(relative_path, str):
            return None, "invalid_relative_path"
        if not relative_path:
            return None, "empty_relative_path"
        if len(relative_path) > MAX_PATH_LENGTH:
            return None, "path_too_long"
        if "\x00" in relative_path:
            return None, "invalid_relative_path"
        try:
            if len(os.fsencode(relative_path)) > MAX_PATH_BYTES:
                return None, "path_too_long"
            path = Path(relative_path)
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_relative_path"

        if path.is_absolute():
            return None, "absolute_path"
        if not path.parts:
            return None, "invalid_relative_path"
        if ".." in path.parts:
            return None, "traversal"
        if len(path.parts) > MAX_DEPTH:
            return None, "path_too_deep"
        try:
            if any(
                len(os.fsencode(part)) > MAX_COMPONENT_BYTES
                for part in path.parts
            ):
                return None, "path_too_long"
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_relative_path"
        return path, None

    @staticmethod
    def _parent_chain(base, relative, allow_missing=False):
        """Validate every parent in ``relative`` without following symlinks."""
        current = base
        parts = list(relative.parts[:-1])
        for index, part in enumerate(parts):
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                if allow_missing:
                    return current, None, parts[index:]
                return None, "missing_parent", None
            except OSError:
                return None, "parent_unavailable", None
            if stat.S_ISLNK(st.st_mode):
                return None, "symlink_parent", None
            if not stat.S_ISDIR(st.st_mode):
                return None, "parent_not_directory", None
        return current, None, []

    def _validate_write_target(self, base, relative, target, overwrite):
        parent, error, _ = self._parent_chain(base, relative)
        if error:
            return None, error

        try:
            st = os.lstat(target)
        except FileNotFoundError:
            st = None
        except OSError:
            return None, "target_unavailable"

        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                return st, "symlink_target"
            if stat.S_ISDIR(st.st_mode):
                return st, "target_is_directory"
            if not stat.S_ISREG(st.st_mode):
                return st, "target_not_regular"
            if not overwrite:
                return st, "target_exists"
        return st, None

    def _validate_mkdir_target(self, base, relative, parents):
        parent, error, missing = self._parent_chain(
            base, relative, allow_missing=bool(parents)
        )
        if error:
            return None, error, None

        try:
            st = os.lstat(base / relative)
        except (FileNotFoundError, NotADirectoryError):
            st = None
        except OSError:
            return None, "target_unavailable", None

        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                return None, "symlink_target", None
            if stat.S_ISDIR(st.st_mode):
                return None, "target_exists", None
            return None, "target_is_file", None

        return parent, None, missing

    def _validate_trash_target(self, base, relative, target):
        parent, error, _ = self._parent_chain(base, relative)
        if error:
            return None, error

        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return None, "target_not_found"
        except OSError:
            return None, "target_unavailable"

        if stat.S_ISLNK(st.st_mode):
            return None, "symlink_target"
        if stat.S_ISDIR(st.st_mode):
            return st, None
        if not stat.S_ISREG(st.st_mode):
            return None, "target_not_regular"
        return st, None

    def _prepare_write(self, folder, relative_path, content, overwrite):
        base, error = self._base_for(folder)
        if error:
            return None, error, {"target": _target_text(relative_path)}

        relative, error = self._relative_path(relative_path)
        if error:
            return None, error, {"target": _target_text(relative_path)}

        if isinstance(relative_path, str) and relative_path.endswith("/"):
            return None, "invalid_relative_path", {"target": str(relative)}

        if not isinstance(content, str):
            return (
                None,
                "invalid_content",
                {
                    "target": str(relative),
                    "content_type": type(content).__name__,
                },
            )
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            return (
                None,
                "content_encoding_error",
                {"target": str(relative), "content_type": "text"},
            )
        if len(data) > MAX_CONTENT_BYTES:
            return (
                None,
                "content_too_large",
                {
                    "target": str(relative),
                    "content_type": "text",
                    "content_bytes": len(data),
                    "content_limit": MAX_CONTENT_BYTES,
                },
            )

        target = base / relative
        try:
            if len(os.fsencode(target)) > MAX_PATH_BYTES:
                return None, "path_too_long", {"target": str(relative)}
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_relative_path", {"target": str(relative)}

        existing, error = self._validate_write_target(
            base, relative, target, bool(overwrite)
        )
        preview = {
            "target": str(relative),
            "absolute_target": str(target),
            "overwrite": bool(overwrite),
            "target_exists": existing is not None,
            "content_type": "text",
            "content_bytes": len(data),
            "content_sha256": hashlib.sha256(data).hexdigest(),
            "content": content,
            "content_length": len(content),
            "atomic": True,
            "mode": "0o600",
        }
        if error:
            return None, error, preview

        return (
            {
                "base": base,
                "relative": relative,
                "target": target,
                "data": data,
                "content": content,
                "overwrite": bool(overwrite),
                "existing": existing,
            },
            None,
            preview,
        )

    def _prepare_mkdir(self, folder, relative_path, parents):
        base, error = self._base_for(folder)
        if error:
            return None, error, {"target": _target_text(relative_path)}

        relative, error = self._relative_path(relative_path)
        if error:
            return None, error, {"target": _target_text(relative_path)}

        target = base / relative
        try:
            if len(os.fsencode(target)) > MAX_PATH_BYTES:
                return None, "path_too_long", {"target": str(relative)}
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_relative_path", {"target": str(relative)}

        parent, error, missing = self._validate_mkdir_target(
            base, relative, bool(parents)
        )
        preview = {
            "target": str(relative),
            "absolute_target": str(target),
            "parents": bool(parents),
            "missing_parents": [str(part) for part in (missing or [])],
            "target_exists": error
            in {"target_exists", "target_is_file", "symlink_target"},
            "mode": "0o700",
        }
        if error:
            return None, error, preview

        return (
            {
                "base": base,
                "relative": relative,
                "target": target,
                "parents": bool(parents),
                "missing": missing,
            },
            None,
            preview,
        )

    @staticmethod
    def _quarantine_state(path):
        if path is None:
            return None, "quarantine_unavailable"
        path = Path(path)
        if not path.is_absolute():
            return None, "invalid_quarantine_root"
        if path == Path(path.anchor):
            return None, "invalid_quarantine_root"
        if ".." in path.parts:
            return None, "invalid_quarantine_root"

        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        except OSError:
            return None, "invalid_quarantine_root"

        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                return None, "invalid_quarantine_root"
            if not stat.S_ISDIR(st.st_mode):
                return None, "invalid_quarantine_root"
            return {"path": path, "exists": True}, None

        parent = path.parent
        try:
            parent_st = os.lstat(parent)
        except FileNotFoundError:
            parent_st = None
        except OSError:
            return None, "invalid_quarantine_root"

        if parent_st is not None:
            if stat.S_ISLNK(parent_st.st_mode):
                return None, "invalid_quarantine_root"
            if not stat.S_ISDIR(parent_st.st_mode):
                return None, "invalid_quarantine_root"

        return {"path": path, "exists": False}, None

    def _prepare_trash(self, folder, relative_path, recursive):
        base, error = self._base_for(folder)
        if error:
            return None, error, {"target": _target_text(relative_path)}

        relative, error = self._relative_path(relative_path)
        if error:
            return None, error, {"target": _target_text(relative_path)}

        if isinstance(relative_path, str) and relative_path.endswith("/"):
            return None, "invalid_relative_path", {"target": str(relative)}

        target = base / relative
        try:
            if len(os.fsencode(target)) > MAX_PATH_BYTES:
                return None, "path_too_long", {"target": str(relative)}
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_relative_path", {"target": str(relative)}

        st, error = self._validate_trash_target(base, relative, target)
        if error:
            return (
                None,
                error,
                {"target": str(relative), "absolute_target": str(target)},
            )

        if stat.S_ISDIR(st.st_mode):
            item_type = "directory"
            try:
                entries = os.listdir(target)
            except OSError:
                return (
                    None,
                    "target_unavailable",
                    {
                        "target": str(relative),
                        "absolute_target": str(target),
                        "type": item_type,
                    },
                )
            entry_count = len(entries)
            non_empty = entry_count > 0
            if non_empty and not recursive:
                return (
                    None,
                    "non_empty_directory",
                    {
                        "target": str(relative),
                        "absolute_target": str(target),
                        "type": item_type,
                        "size": int(st.st_size),
                        "entry_count": entry_count,
                        "non_empty": True,
                        "recursive": False,
                    },
                )
        else:
            item_type = "file"
            entry_count = None
            non_empty = False

        quarantine, error = self._quarantine_state(self.quarantine_root)
        preview = {
            "target": str(relative),
            "absolute_target": str(target),
            "type": item_type,
            "size": int(st.st_size),
            "entry_count": entry_count,
            "non_empty": non_empty,
            "recursive": bool(recursive),
            "quarantine_root": (
                str(self.quarantine_root)
                if self.quarantine_root is not None
                else None
            ),
            "quarantine_root_exists": (
                bool(quarantine["exists"]) if quarantine else False
            ),
            "will_quarantine": True,
            "permanent_delete": False,
        }
        if error:
            return None, error, preview

        return (
            {
                "base": base,
                "folder": folder,
                "relative": relative,
                "target": target,
                "type": item_type,
                "size": int(st.st_size),
                "entry_count": entry_count,
                "non_empty": non_empty,
                "recursive": bool(recursive),
                "quarantine_root": quarantine["path"],
            },
            None,
            preview,
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def _perform_write(self, action, context, preview):
        relative = str(context["relative"])
        target = context["target"]
        data = context["data"]

        existing, error = self._validate_write_target(
            context["base"],
            context["relative"],
            target,
            context["overwrite"],
        )
        if error:
            return self._error(
                action, _message_for(error), relative, error, preview
            )

        parent = target.parent
        temp_path = None
        fd = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=".misty-",
                suffix=".tmp",
                dir=parent,
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

            try:
                temp_bytes = self._read_bytes(temp_path)
            except OSError:
                return self._error(
                    action,
                    _message_for("verification_failed"),
                    relative,
                    "verification_failed",
                    preview,
                )
            if temp_bytes != data:
                return self._error(
                    action,
                    _message_for("verification_failed"),
                    relative,
                    "verification_failed",
                    preview,
                )

            # Revalidate immediately before the atomic replacement.
            existing, error = self._validate_write_target(
                context["base"],
                context["relative"],
                target,
                context["overwrite"],
            )
            if error:
                return self._error(
                    action, _message_for(error), relative, error, preview
                )

            os.replace(temp_path, target)
            temp_path = None

            try:
                actual = self._read_bytes(target)
            except OSError:
                return self._error(
                    action,
                    _message_for("verification_failed"),
                    relative,
                    "verification_failed",
                    preview,
                )
            if actual != data:
                return self._error(
                    action,
                    _message_for("verification_failed"),
                    relative,
                    "verification_failed",
                    preview,
                )

            self._fsync_directory(parent)
            return self._result(
                action,
                True,
                "File written and verified.",
                relative,
                requires_confirmation=False,
                preview=preview,
                verified=True,
            )
        except OSError:
            return self._error(
                action,
                _message_for("write_failed"),
                relative,
                "write_failed",
                preview,
            )
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass

    def _perform_mkdir(self, action, context, preview):
        relative = str(context["relative"])
        target = context["target"]

        parent, error, _ = self._validate_mkdir_target(
            context["base"], context["relative"], context["parents"]
        )
        if error:
            return self._error(
                action, _message_for(error), relative, error, preview
            )

        try:
            current = context["base"]
            for part in context["relative"].parts[:-1]:
                current = current / part
                try:
                    st = os.lstat(current)
                except FileNotFoundError:
                    if not context["parents"]:
                        return self._error(
                            action,
                            _message_for("missing_parent"),
                            relative,
                            "missing_parent",
                            preview,
                        )
                    try:
                        os.mkdir(current, 0o700)
                    except FileExistsError:
                        try:
                            st = os.lstat(current)
                        except OSError:
                            return self._error(
                                action,
                                _message_for("mkdir_failed"),
                                relative,
                                "mkdir_failed",
                                preview,
                            )
                        if stat.S_ISLNK(st.st_mode):
                            return self._error(
                                action,
                                _message_for("symlink_parent"),
                                relative,
                                "symlink_parent",
                                preview,
                            )
                        if not stat.S_ISDIR(st.st_mode):
                            return self._error(
                                action,
                                _message_for("parent_not_directory"),
                                relative,
                                "parent_not_directory",
                                preview,
                            )
                    except OSError:
                        return self._error(
                            action,
                            _message_for("mkdir_failed"),
                            relative,
                            "mkdir_failed",
                            preview,
                        )
                except OSError:
                    return self._error(
                        action,
                        _message_for("parent_unavailable"),
                        relative,
                        "parent_unavailable",
                        preview,
                    )
                else:
                    if stat.S_ISLNK(st.st_mode):
                        return self._error(
                            action,
                            _message_for("symlink_parent"),
                            relative,
                            "symlink_parent",
                            preview,
                        )
                    if not stat.S_ISDIR(st.st_mode):
                        return self._error(
                            action,
                            _message_for("parent_not_directory"),
                            relative,
                            "parent_not_directory",
                            preview,
                        )

            try:
                os.mkdir(target, 0o700)
            except FileExistsError:
                try:
                    st = os.lstat(target)
                except OSError:
                    return self._error(
                        action,
                        _message_for("mkdir_failed"),
                        relative,
                        "mkdir_failed",
                        preview,
                    )
                if stat.S_ISLNK(st.st_mode):
                    return self._error(
                        action,
                        _message_for("symlink_target"),
                        relative,
                        "symlink_target",
                        preview,
                    )
                if stat.S_ISDIR(st.st_mode):
                    return self._error(
                        action,
                        _message_for("target_exists"),
                        relative,
                        "target_exists",
                        preview,
                    )
                return self._error(
                    action,
                    _message_for("target_is_file"),
                    relative,
                    "target_is_file",
                    preview,
                )
            except OSError:
                return self._error(
                    action,
                    _message_for("mkdir_failed"),
                    relative,
                    "mkdir_failed",
                    preview,
                )

            try:
                st = os.lstat(target)
            except OSError:
                return self._error(
                    action,
                    _message_for("mkdir_failed"),
                    relative,
                    "mkdir_failed",
                    preview,
                )
            if not stat.S_ISDIR(st.st_mode):
                return self._error(
                    action,
                    _message_for("mkdir_failed"),
                    relative,
                    "mkdir_failed",
                    preview,
                )

            self._fsync_directory(target.parent)
            return self._result(
                action,
                True,
                "Directory created.",
                relative,
                requires_confirmation=False,
                preview=preview,
                verified=True,
            )
        except OSError:
            return self._error(
                action,
                _message_for("mkdir_failed"),
                relative,
                "mkdir_failed",
                preview,
            )

    def _perform_trash(self, action, context, preview):
        relative = str(context["relative"])
        target = context["target"]

        st, error = self._validate_trash_target(
            context["base"], context["relative"], target
        )
        if error:
            return self._error(
                action, _message_for(error), relative, error, preview
            )

        if stat.S_ISDIR(st.st_mode):
            try:
                entries = os.listdir(target)
            except OSError:
                return self._error(
                    action,
                    _message_for("target_unavailable"),
                    relative,
                    "target_unavailable",
                    preview,
                )
            if entries and not context["recursive"]:
                return self._error(
                    action,
                    _message_for("non_empty_directory"),
                    relative,
                    "non_empty_directory",
                    preview,
                )

        quarantine, error = self._ensure_private_directory(
            context["quarantine_root"]
        )
        if error:
            return self._error(
                action, _message_for(error), relative, error, preview
            )

        container = None
        for _ in range(MAX_QUARANTINE_ATTEMPTS):
            name = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                + "-"
                + uuid.uuid4().hex
            )
            candidate = quarantine / name
            try:
                os.mkdir(candidate, 0o700)
                container = candidate
                break
            except FileExistsError:
                continue
            except OSError:
                return self._error(
                    action,
                    _message_for("quarantine_failed"),
                    relative,
                    "quarantine_failed",
                    preview,
                )
        if container is None:
            return self._error(
                action,
                _message_for("quarantine_failed"),
                relative,
                "quarantine_failed",
                preview,
            )

        item_dir = container / "item"
        try:
            os.mkdir(item_dir, 0o700)
        except OSError:
            self._cleanup_container(container)
            return self._error(
                action,
                _message_for("quarantine_failed"),
                relative,
                "quarantine_failed",
                preview,
            )

        destination = item_dir / target.name
        manifest_path = container / "manifest.json"
        manifest = {
            "original_relative_path": relative,
            "trashed_path": str(destination),
            "time": datetime.now(timezone.utc).isoformat(),
            "size": int(context["size"]),
            "type": context["type"],
            "entry_count": context["entry_count"],
            "non_empty": bool(context["non_empty"]),
            "recursive": bool(context["recursive"]),
            "folder": context["folder"],
            "original_name": target.name,
            "quarantine_relative_path": str(container.relative_to(quarantine)),
        }

        try:
            self._write_manifest(manifest_path, manifest)
        except Exception:
            self._cleanup_container(container)
            return self._error(
                action,
                _message_for("manifest_failed"),
                relative,
                "manifest_failed",
                preview,
            )

        try:
            if os.path.lexists(destination):
                self._cleanup_container(container)
                return self._error(
                    action,
                    _message_for("quarantine_failed"),
                    relative,
                    "quarantine_failed",
                    preview,
                )
            os.replace(target, destination)
        except OSError as error:
            self._cleanup_container(container)
            code = (
                "cross_device"
                if error.errno == errno.EXDEV
                else "trash_failed"
            )
            return self._error(
                action, _message_for(code), relative, code, preview
            )

        try:
            if os.path.lexists(target) or not os.path.lexists(destination):
                return self._error(
                    action,
                    _message_for("trash_failed"),
                    relative,
                    "trash_failed",
                    preview,
                )
        except OSError:
            return self._error(
                action,
                _message_for("trash_failed"),
                relative,
                "trash_failed",
                preview,
            )

        self._fsync_directory(item_dir)
        result = self._result(
            action,
            True,
            "Item moved to quarantine.",
            relative,
            requires_confirmation=False,
            preview=preview,
            verified=True,
        )
        result["quarantine_path"] = str(container)
        result["manifest_path"] = str(manifest_path)
        result["trashed_path"] = str(destination)
        result["manifest"] = manifest
        return result

    # ------------------------------------------------------------------
    # Small filesystem helpers
    # ------------------------------------------------------------------

    def _ensure_private_directory(self, path):
        path = Path(path)
        if not path.is_absolute():
            return None, "invalid_quarantine_root"
        if path == Path(path.anchor):
            return None, "invalid_quarantine_root"
        if ".." in path.parts:
            return None, "invalid_quarantine_root"

        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                try:
                    os.mkdir(current, 0o700)
                except FileExistsError:
                    try:
                        st = os.lstat(current)
                    except OSError:
                        return None, "invalid_quarantine_root"
                    if stat.S_ISLNK(st.st_mode):
                        return None, "invalid_quarantine_root"
                    if not stat.S_ISDIR(st.st_mode):
                        return None, "invalid_quarantine_root"
                    continue
                except OSError:
                    return None, "invalid_quarantine_root"
                continue
            except OSError:
                return None, "invalid_quarantine_root"
            if stat.S_ISLNK(st.st_mode):
                return None, "invalid_quarantine_root"
            if not stat.S_ISDIR(st.st_mode):
                return None, "invalid_quarantine_root"
        return path, None

    def _write_manifest(self, path, data):
        text = json.dumps(
            data, ensure_ascii=False, indent=2, sort_keys=True
        )
        fd = None
        temp_path = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=".manifest.", suffix=".tmp", dir=path.parent
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _cleanup_container(container):
        try:
            manifest = container / "manifest.json"
            if manifest.is_file():
                manifest.unlink()
            item_dir = container / "item"
            if item_dir.is_dir() and not item_dir.is_symlink():
                try:
                    item_dir.rmdir()
                except OSError:
                    pass
            try:
                container.rmdir()
            except OSError:
                pass
        except OSError:
            pass

    @staticmethod
    def _read_bytes(path):
        return Path(path).read_bytes()

    @staticmethod
    def _fsync_directory(directory):
        try:
            fd = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
