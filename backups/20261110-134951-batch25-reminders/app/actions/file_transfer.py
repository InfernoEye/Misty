"""Isolated, safety-first file copy, move and rename engine.

The model never supplies an absolute filesystem path. Callers pass one of the
approved folder keys understood by :class:`UserFolderResolver` plus a bounded
relative path for both ends of a transfer. Every mutating entry point is
confirmation gated and returns a structured, JSON-safe result dictionary;
preview helpers expose the same plan without touching the filesystem.

Files only: directories, symbolic links, devices and FIFOs are refused. Data
is copied in bounded chunks, fsynced to a private temp file, and published
with :func:`os.replace`; a same-filesystem move is a single atomic rename and
a cross-device move falls back to copy-then-unlink only after the copied file
and its directory are durable.

This module deliberately contains no registry wiring, UI, model, network or
Qt imports. See the module-level integration note at the bottom of this
docstring for the argument and confirmation contract used by the tool layer.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver

# One bounded chunk keeps memory flat regardless of file size.
CHUNK_SIZE = 1_048_576
# Ceiling for a single copied file (8 GiB).
MAX_FILE_BYTES = 8 * 1024 ** 3
# Path bounds mirror app/actions/file_ops.py so both engines agree.
MAX_PATH_LENGTH = 1024
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_DEPTH = 32
MAX_NAME_BYTES = 255
COPY_MODE = 0o600
TEMP_PREFIX = ".misty-transfer-"
TEMP_SUFFIX = ".tmp"

_MESSAGES = {
    "confirmation_required": "Confirmation is required. Nothing was changed.",
    "invalid_folder": "The folder is not on the approved allowlist.",
    "invalid_relative_path": "The relative path is not valid.",
    "empty_relative_path": "The relative path must not be empty.",
    "absolute_path": "Absolute paths are not allowed.",
    "traversal": "The path must not contain '..'.",
    "path_too_long": "The path is too long.",
    "path_too_deep": "The path is too deep.",
    "missing_parent": "The parent directory does not exist.",
    "parent_not_directory": "A parent path component is not a directory.",
    "parent_unavailable": "A parent path component is unavailable.",
    "symlink_parent": "A parent path component is a symbolic link.",
    "source_not_found": "The source file does not exist.",
    "source_unavailable": "The source file is unavailable.",
    "source_is_directory": "The source is a directory.",
    "source_not_regular": "The source is not a regular file.",
    "source_changed": "The source file changed during the operation.",
    "symlink_target": "The source is a symbolic link.",
    "same_file": "The source and destination are the same file.",
    "destination_exists": "The destination already exists.",
    "destination_symlink": "The destination is a symbolic link.",
    "destination_is_directory": "The destination is a directory.",
    "destination_not_regular": "The destination is not a regular file.",
    "destination_unavailable": "The destination is unavailable.",
    "file_too_large": "The file is larger than the transfer limit.",
    "copy_failed": "The file could not be copied.",
    "move_failed": "The file could not be moved.",
    "rename_failed": "The file could not be renamed.",
    "unlink_failed": (
        "The file was copied, but the original could not be removed."
    ),
    "verification_failed": "The operation could not be verified.",
    "invalid_name": "The new name is not valid.",
    "name_too_long": "The new name is too long.",
    "internal_error": "The operation could not be completed.",
}

__all__ = [
    "CHUNK_SIZE",
    "MAX_FILE_BYTES",
    "FileTransferEngine",
]

# Integration note (tool layer, not wired here):
#
#   preview_copy(folder, relative_path, destination_folder,
#                destination_relative_path, overwrite=False)
#   preview_move(...same arguments...)
#   preview_rename(folder, relative_path, new_name)
#   copy(...), move(...), rename(...)  # each also accepts confirmed=False
#
# ``confirmed`` must be exactly True before anything is written; any other
# value (including truthy non-booleans) returns ``confirmation_required``
# with a populated ``preview`` and performs zero writes. The tool layer should
# gate these names in policy with ``confirm=True`` and pass the broker's
# boolean through unchanged.


def _message_for(error_code):
    return _MESSAGES.get(error_code, _MESSAGES["internal_error"])


class FileTransferEngine:
    """Bounded copy/move/rename operations inside approved user folders.

    ``resolver`` defaults to the production :class:`UserFolderResolver`.
    ``quarantine_dir`` is accepted for API symmetry with
    :class:`~app.actions.file_ops.FileOpsEngine`; copy, move and rename never
    write to it, never require it, and never create it. It is reserved for
    future partial-move recovery and is otherwise inert.
    """

    def __init__(self, resolver=None, quarantine_dir=None):
        self.resolver = resolver or UserFolderResolver()
        self.quarantine_dir = (
            Path(quarantine_dir).expanduser()
            if quarantine_dir is not None
            else None
        )

    # ------------------------------------------------------------------
    # Previews (read-only)
    # ------------------------------------------------------------------

    def preview_copy(
        self,
        folder,
        relative_path,
        destination_folder,
        destination_relative_path,
        overwrite=False,
    ):
        """Return the exact copy plan without creating or changing anything."""
        return self._preview_transfer(
            "copy",
            folder,
            relative_path,
            destination_folder,
            destination_relative_path,
            overwrite,
        )

    def preview_move(
        self,
        folder,
        relative_path,
        destination_folder,
        destination_relative_path,
        overwrite=False,
    ):
        """Return the exact move plan without creating or changing anything."""
        return self._preview_transfer(
            "move",
            folder,
            relative_path,
            destination_folder,
            destination_relative_path,
            overwrite,
        )

    def preview_rename(self, folder, relative_path, new_name):
        """Return the exact rename plan without renaming anything."""
        action = "preview_rename"
        try:
            context, error, preview = self._prepare_rename(
                folder, relative_path, new_name
            )
        except Exception:
            return self._fail(
                action,
                "internal_error",
                preview=None,
                source=self._echo(relative_path),
            )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=preview.get("source"),
                destination=preview.get("destination"),
            )
        return self._result(
            action,
            True,
            "Preview only. No file was renamed.",
            None,
            preview=preview,
            source=str(context["source_relative"]),
            destination=str(context["destination_relative"]),
            atomic=True,
            method="rename-in-place",
        )

    # ------------------------------------------------------------------
    # Mutations (approval gated)
    # ------------------------------------------------------------------

    def copy(
        self,
        folder,
        relative_path,
        destination_folder,
        destination_relative_path,
        overwrite=False,
        confirmed=False,
    ):
        """Copy one regular file inside approved folders.

        Without ``confirmed=True`` the result has ``requires_confirmation``
        set and ``preview`` populated. Nothing is created or changed.
        """
        return self._mutate_transfer(
            "copy",
            folder,
            relative_path,
            destination_folder,
            destination_relative_path,
            overwrite,
            confirmed,
        )

    def move(
        self,
        folder,
        relative_path,
        destination_folder,
        destination_relative_path,
        overwrite=False,
        confirmed=False,
    ):
        """Move one regular file inside approved folders.

        Uses an atomic rename on one filesystem. Across filesystems it copies,
        makes the copy durable, and only then unlinks the source.
        """
        return self._mutate_transfer(
            "move",
            folder,
            relative_path,
            destination_folder,
            destination_relative_path,
            overwrite,
            confirmed,
        )

    def rename(self, folder, relative_path, new_name, confirmed=False):
        """Rename one regular file in place; the destination never exists."""
        action = "rename"
        try:
            context, error, preview = self._prepare_rename(
                folder, relative_path, new_name
            )
        except Exception:
            return self._fail(
                action,
                "internal_error",
                preview=None,
                source=self._echo(relative_path),
            )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=preview.get("source"),
                destination=preview.get("destination"),
            )
        if confirmed is not True:
            return self._result(
                action,
                False,
                _message_for("confirmation_required"),
                "confirmation_required",
                requires_confirmation=True,
                preview=preview,
                source=str(context["source_relative"]),
                destination=str(context["destination_relative"]),
                atomic=True,
                method="rename-in-place",
            )
        try:
            return self._perform_rename(action, context, preview)
        except Exception:
            return self._fail(
                action,
                "internal_error",
                preview=preview,
                source=str(context["source_relative"]),
                destination=str(context["destination_relative"]),
            )

    # ------------------------------------------------------------------
    # Shared result builders
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        action,
        success,
        message,
        code=None,
        *,
        requires_confirmation=False,
        preview=None,
        source=None,
        destination=None,
        bytes_copied=None,
        overwritten=False,
        durable=False,
        partial=False,
        atomic=False,
        method=None,
        verified=False,
        mtime_preserved=False,
    ):
        return {
            "success": bool(success),
            "action": action,
            "requires_confirmation": bool(requires_confirmation),
            "message": message,
            "code": code,
            "error_code": code,
            "source": source,
            "destination": destination,
            "target": destination,
            "preview": preview,
            "bytes": (
                int(bytes_copied) if bytes_copied is not None else None
            ),
            "overwritten": bool(overwritten),
            "durable": bool(durable),
            "partial": bool(partial),
            "atomic": bool(atomic),
            "method": method,
            "verified": bool(verified),
            "mtime_preserved": bool(mtime_preserved),
        }

    def _fail(
        self,
        action,
        code,
        *,
        preview=None,
        source=None,
        destination=None,
        **extra,
    ):
        return self._result(
            action,
            False,
            _message_for(code),
            code,
            preview=preview,
            source=source,
            destination=destination,
            **extra,
        )

    # ------------------------------------------------------------------
    # Preparation (read-only)
    # ------------------------------------------------------------------

    def _preview_transfer(
        self,
        operation,
        folder,
        relative_path,
        destination_folder,
        destination_relative_path,
        overwrite,
    ):
        action = "preview_" + operation
        try:
            context, error, preview = self._prepare_transfer(
                operation,
                folder,
                relative_path,
                destination_folder,
                destination_relative_path,
                overwrite,
            )
        except Exception:
            return self._fail(
                action,
                "internal_error",
                preview=None,
                source=self._echo(relative_path),
                destination=self._echo(destination_relative_path),
            )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=preview.get("source"),
                destination=preview.get("destination"),
            )
        message = (
            "Preview only. No file was copied."
            if operation == "copy"
            else "Preview only. No file was moved."
        )
        return self._result(
            action,
            True,
            message,
            None,
            preview=preview,
            source=str(context["source_relative"]),
            destination=str(context["destination_relative"]),
            atomic=preview["atomic"],
            method=preview["method"],
        )

    def _mutate_transfer(
        self,
        operation,
        folder,
        relative_path,
        destination_folder,
        destination_relative_path,
        overwrite,
        confirmed,
    ):
        action = operation
        try:
            context, error, preview = self._prepare_transfer(
                operation,
                folder,
                relative_path,
                destination_folder,
                destination_relative_path,
                overwrite,
            )
        except Exception:
            return self._fail(
                action,
                "internal_error",
                preview=None,
                source=self._echo(relative_path),
                destination=self._echo(destination_relative_path),
            )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=preview.get("source"),
                destination=preview.get("destination"),
            )
        if confirmed is not True:
            return self._result(
                action,
                False,
                _message_for("confirmation_required"),
                "confirmation_required",
                requires_confirmation=True,
                preview=preview,
                source=str(context["source_relative"]),
                destination=str(context["destination_relative"]),
                atomic=preview["atomic"],
                method=preview["method"],
            )
        try:
            if operation == "copy":
                return self._perform_copy(action, context, preview)
            return self._perform_move(action, context, preview)
        except Exception:
            return self._fail(
                action,
                "internal_error",
                preview=preview,
                source=str(context["source_relative"]),
                destination=str(context["destination_relative"]),
            )

    def _prepare_transfer(
        self,
        operation,
        folder,
        relative_path,
        destination_folder,
        destination_relative_path,
        overwrite,
    ):
        preview = {
            "operation": operation,
            "source": self._echo(relative_path),
            "destination": self._echo(destination_relative_path),
        }

        source_base, error = self._base_for(folder)
        if error:
            return None, error, preview
        destination_base, error = self._base_for(destination_folder)
        if error:
            return None, error, preview

        source_relative, error = self._relative_path(relative_path)
        if error:
            return None, error, preview
        destination_relative, error = self._relative_path(
            destination_relative_path
        )
        if error:
            return None, error, preview

        preview["source"] = str(source_relative)
        preview["destination"] = str(destination_relative)

        if self._has_trailing_separator(
            relative_path
        ) or self._has_trailing_separator(destination_relative_path):
            return None, "invalid_relative_path", preview

        source, error = self._full_path(source_base, source_relative)
        if error:
            return None, error, preview
        destination, error = self._full_path(
            destination_base, destination_relative
        )
        if error:
            return None, error, preview

        preview["absolute_source"] = str(source)
        preview["absolute_destination"] = str(destination)

        source_target, source_st, error = self._source_state(
            source_base, source_relative
        )
        if error:
            if source_st is not None:
                preview["size"] = int(source_st.st_size)
                preview["max_bytes"] = MAX_FILE_BYTES
            return None, error, preview

        destination_target, destination_st, error = self._destination_lstat(
            destination_base, destination_relative
        )
        if error:
            return None, error, preview

        preview["destination_exists"] = destination_st is not None
        preview["overwrite"] = bool(overwrite)

        if self._same_file(source_target, destination_target, destination_st):
            return None, "same_file", preview

        error = self._destination_error(destination_st, bool(overwrite))
        if error:
            return None, error, preview

        same_filesystem = self._same_filesystem(
            source_st, destination_target
        )
        preview.update(
            {
                "size": int(source_st.st_size),
                "bytes": int(source_st.st_size),
                "same_filesystem": bool(same_filesystem),
                "chunk_bytes": CHUNK_SIZE,
                "max_bytes": MAX_FILE_BYTES,
                "mode": "0o600",
                "atomic": bool(
                    operation == "copy" or same_filesystem
                ),
                "method": self._planned_method(
                    operation, same_filesystem
                ),
                "mtime_preserved": bool(
                    operation == "copy" or same_filesystem
                ),
            }
        )
        context = {
            "operation": operation,
            "source_base": source_base,
            "source_relative": source_relative,
            "source": source_target,
            "destination_base": destination_base,
            "destination_relative": destination_relative,
            "destination": destination_target,
            "overwrite": bool(overwrite),
            "stat": source_st,
            "size": int(source_st.st_size),
        }
        return context, None, preview

    def _prepare_rename(self, folder, relative_path, new_name):
        preview = {
            "operation": "rename",
            "source": self._echo(relative_path),
            "new_name": self._echo(new_name),
            "destination": None,
        }

        base, error = self._base_for(folder)
        if error:
            return None, error, preview

        relative, error = self._relative_path(relative_path)
        if error:
            return None, error, preview
        preview["source"] = str(relative)

        if self._has_trailing_separator(relative_path):
            return None, "invalid_relative_path", preview

        name, error = self._new_name(new_name)
        if error:
            return None, error, preview
        preview["new_name"] = name

        destination_relative = (
            Path(name) if relative.parent == Path(".") else relative.parent / name
        )
        preview["destination"] = str(destination_relative)

        source, error = self._full_path(base, relative)
        if error:
            return None, error, preview
        destination, error = self._full_path(base, destination_relative)
        if error:
            return None, error, preview

        preview["absolute_source"] = str(source)
        preview["absolute_destination"] = str(destination)

        source_target, source_st, error = self._source_state(base, relative)
        if error:
            if source_st is not None:
                preview["size"] = int(source_st.st_size)
                preview["max_bytes"] = MAX_FILE_BYTES
            return None, error, preview

        destination_target, destination_st, error = self._destination_lstat(
            base, destination_relative
        )
        if error:
            return None, error, preview

        preview["destination_exists"] = destination_st is not None
        preview["overwrite"] = False

        if self._same_file(source_target, destination_target, destination_st):
            return None, "same_file", preview

        error = self._destination_error(destination_st, False)
        if error:
            return None, error, preview

        preview.update(
            {
                "size": int(source_st.st_size),
                "bytes": int(source_st.st_size),
                "atomic": True,
                "method": "rename-in-place",
                "mode": "preserve",
                "max_bytes": MAX_FILE_BYTES,
            }
        )
        context = {
            "base": base,
            "source_relative": relative,
            "source": source_target,
            "destination_relative": destination_relative,
            "destination": destination_target,
            "stat": source_st,
            "size": int(source_st.st_size),
        }
        return context, None, preview

    # ------------------------------------------------------------------
    # Mutation implementations
    # ------------------------------------------------------------------

    def _perform_copy(self, action, context, preview):
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        outcome = self._stream_copy(context)
        if outcome["error"]:
            return self._fail(
                action,
                outcome["error"],
                preview=preview,
                source=source_text,
                destination=destination_text,
                atomic=True,
                method="stream",
            )
        return self._result(
            action,
            True,
            "File copied.",
            None,
            preview=preview,
            source=source_text,
            destination=destination_text,
            bytes_copied=outcome["bytes"],
            overwritten=outcome["overwritten"],
            durable=True,
            partial=False,
            atomic=True,
            method="stream",
            verified=True,
            mtime_preserved=outcome["mtime_preserved"],
        )

    def _perform_move(self, action, context, preview):
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        source = context["source"]
        destination = context["destination"]
        expected = context["stat"]

        source_target, source_st, error = self._source_state(
            context["source_base"], context["source_relative"]
        )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        destination_target, destination_st, error = (
            self._destination_lstat(
                context["destination_base"],
                context["destination_relative"],
            )
        )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        if self._same_file(source_target, destination_target, destination_st):
            return self._fail(
                action,
                "same_file",
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        error = self._destination_error(
            destination_st, context["overwrite"]
        )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        if (
            source_st.st_dev,
            source_st.st_ino,
            source_st.st_size,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
        ):
            return self._fail(
                action,
                "source_changed",
                preview=preview,
                source=source_text,
                destination=destination_text,
            )

        overwritten = destination_st is not None
        try:
            os.replace(source, destination)
        except OSError as error:
            if error.errno != errno.EXDEV:
                return self._fail(
                    action,
                    "move_failed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )
            return self._move_across_devices(
                action, context, preview, overwritten
            )

        self._fsync_directory(destination.parent)
        if source.parent != destination.parent:
            self._fsync_directory(source.parent)

        try:
            final_st = os.lstat(destination)
            remaining = os.path.lexists(source)
        except OSError:
            return self._fail(
                action,
                "verification_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                durable=True,
                partial=True,
                atomic=True,
                method="rename",
            )
        if remaining or not stat.S_ISREG(final_st.st_mode):
            return self._fail(
                action,
                "verification_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                durable=True,
                partial=True,
                atomic=True,
                method="rename",
            )
        return self._result(
            action,
            True,
            "File moved.",
            None,
            preview=preview,
            source=source_text,
            destination=destination_text,
            bytes_copied=int(expected.st_size),
            overwritten=overwritten,
            durable=True,
            partial=False,
            atomic=True,
            method="rename",
            verified=True,
            mtime_preserved=True,
        )

    def _move_across_devices(
        self, action, context, preview, overwritten
    ):
        """Copy first, publish durably, then unlink the original."""
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        source = context["source"]
        destination = context["destination"]

        outcome = self._stream_copy(context)
        if outcome["error"]:
            return self._fail(
                action,
                outcome["error"],
                preview=preview,
                source=source_text,
                destination=destination_text,
                atomic=False,
                method="copy+unlink",
            )

        try:
            os.unlink(source)
        except OSError:
            return self._fail(
                action,
                "unlink_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                bytes_copied=outcome["bytes"],
                overwritten=outcome["overwritten"],
                durable=True,
                partial=True,
                atomic=False,
                method="copy+unlink",
                mtime_preserved=outcome["mtime_preserved"],
            )

        try:
            remaining = os.path.lexists(source)
            present = os.path.lexists(destination)
        except OSError:
            remaining, present = True, True
        if remaining or not present:
            return self._fail(
                action,
                "unlink_failed" if remaining else "verification_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                bytes_copied=outcome["bytes"],
                overwritten=outcome["overwritten"],
                durable=True,
                partial=True,
                atomic=False,
                method="copy+unlink",
                mtime_preserved=outcome["mtime_preserved"],
            )

        self._fsync_directory(source.parent)
        return self._result(
            action,
            True,
            "File moved.",
            None,
            preview=preview,
            source=source_text,
            destination=destination_text,
            bytes_copied=outcome["bytes"],
            overwritten=outcome["overwritten"],
            durable=True,
            partial=False,
            atomic=False,
            method="copy+unlink",
            verified=True,
            mtime_preserved=outcome["mtime_preserved"],
        )

    def _perform_rename(self, action, context, preview):
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        base = context["base"]
        source = context["source"]
        destination = context["destination"]
        expected = context["stat"]

        source_target, source_st, error = self._source_state(
            base, context["source_relative"]
        )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        destination_target, destination_st, error = (
            self._destination_lstat(
                base, context["destination_relative"]
            )
        )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        if self._same_file(source_target, destination_target, destination_st):
            return self._fail(
                action,
                "same_file",
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        error = self._destination_error(destination_st, False)
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        if (
            source_st.st_dev,
            source_st.st_ino,
            source_st.st_size,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
        ):
            return self._fail(
                action,
                "source_changed",
                preview=preview,
                source=source_text,
                destination=destination_text,
            )

        try:
            os.replace(source, destination)
        except OSError:
            return self._fail(
                action,
                "rename_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                atomic=True,
                method="rename-in-place",
            )

        self._fsync_directory(base)
        try:
            final_st = os.lstat(destination)
            remaining = os.path.lexists(source)
        except OSError:
            return self._fail(
                action,
                "verification_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                durable=True,
                partial=True,
                atomic=True,
                method="rename-in-place",
            )
        if remaining or not stat.S_ISREG(final_st.st_mode):
            return self._fail(
                action,
                "verification_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                durable=True,
                partial=True,
                atomic=True,
                method="rename-in-place",
            )
        return self._result(
            action,
            True,
            "File renamed.",
            None,
            preview=preview,
            source=source_text,
            destination=destination_text,
            bytes_copied=int(expected.st_size),
            overwritten=False,
            durable=True,
            partial=False,
            atomic=True,
            method="rename-in-place",
            verified=True,
            mtime_preserved=True,
        )

    def _stream_copy(self, context):
        """Chunked copy to a fsynced temp file followed by os.replace.

        Returns ``{"error", "bytes", "overwritten", "mtime_preserved"}``.
        Temp files are always removed on failure.
        """
        outcome = {
            "error": None,
            "bytes": None,
            "overwritten": False,
            "mtime_preserved": False,
        }
        source = context["source"]
        destination = context["destination"]
        expected = context["stat"]

        source_target, source_st, error = self._source_state(
            context["source_base"], context["source_relative"]
        )
        if error:
            outcome["error"] = error
            return outcome
        destination_target, destination_st, error = (
            self._destination_lstat(
                context["destination_base"],
                context["destination_relative"],
            )
        )
        if error:
            outcome["error"] = error
            return outcome
        if self._same_file(source_target, destination_target, destination_st):
            outcome["error"] = "same_file"
            return outcome
        error = self._destination_error(
            destination_st, context["overwrite"]
        )
        if error:
            outcome["error"] = error
            return outcome
        if (
            source_st.st_dev,
            source_st.st_ino,
            source_st.st_size,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
        ):
            outcome["error"] = "source_changed"
            return outcome

        source_fd = None
        temp_fd = None
        temp_path = None
        try:
            try:
                source_fd = os.open(
                    source,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError:
                outcome["error"] = "source_unavailable"
                return outcome
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode):
                outcome["error"] = "source_not_regular"
                return outcome
            if (opened.st_dev, opened.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                outcome["error"] = "source_changed"
                return outcome
            if opened.st_size > MAX_FILE_BYTES:
                outcome["error"] = "file_too_large"
                return outcome

            try:
                temp_fd, temp_name = tempfile.mkstemp(
                    prefix=TEMP_PREFIX,
                    suffix=TEMP_SUFFIX,
                    dir=destination.parent,
                )
            except OSError:
                outcome["error"] = "destination_unavailable"
                return outcome
            temp_path = Path(temp_name)
            try:
                os.fchmod(temp_fd, COPY_MODE)
            except OSError:
                pass

            copied = 0
            while True:
                try:
                    chunk = os.read(source_fd, CHUNK_SIZE)
                except OSError:
                    outcome["error"] = "copy_failed"
                    return outcome
                if not chunk:
                    break
                if copied + len(chunk) > MAX_FILE_BYTES:
                    outcome["error"] = "file_too_large"
                    return outcome
                try:
                    self._write_all(temp_fd, chunk)
                except OSError:
                    outcome["error"] = "copy_failed"
                    return outcome
                copied += len(chunk)

            if copied != expected.st_size:
                outcome["error"] = "source_changed"
                return outcome

            try:
                os.fsync(temp_fd)
            except OSError:
                outcome["error"] = "copy_failed"
                return outcome

            # Revalidate immediately before publishing the temp file.
            destination_target, destination_st, error = (
                self._destination_lstat(
                    context["destination_base"],
                    context["destination_relative"],
                )
            )
            if error:
                outcome["error"] = error
                return outcome
            if self._same_file(
                source_target, destination_target, destination_st
            ):
                outcome["error"] = "same_file"
                return outcome
            error = self._destination_error(
                destination_st, context["overwrite"]
            )
            if error:
                outcome["error"] = error
                return outcome

            try:
                os.replace(temp_path, destination)
            except OSError:
                outcome["error"] = "copy_failed"
                return outcome
            temp_path = None

            try:
                temp_st = os.fstat(temp_fd)
                final_st = os.lstat(destination)
            except OSError:
                outcome["error"] = "verification_failed"
                return outcome
            if not stat.S_ISREG(final_st.st_mode) or (
                final_st.st_dev,
                final_st.st_ino,
            ) != (temp_st.st_dev, temp_st.st_ino):
                outcome["error"] = "verification_failed"
                return outcome

            outcome["mtime_preserved"] = self._preserve_mtime(
                destination, expected
            )
            outcome["bytes"] = copied
            outcome["overwritten"] = destination_st is not None
            self._fsync_directory(destination.parent)
            return outcome
        except OSError:
            outcome["error"] = "copy_failed"
            return outcome
        finally:
            self._close_quietly(source_fd)
            self._close_quietly(temp_fd)
            if temp_path is not None:
                self._remove_quietly(temp_path)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _base_for(self, folder):
        try:
            if not isinstance(folder, str) or not folder:
                return None, "invalid_folder"
            base = Path(self.resolver.base_for_key(folder))
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
            if not base.is_dir() or base.is_symlink():
                return None, "invalid_folder"
        except (OSError, RuntimeError, ValueError):
            return None, "invalid_folder"

        home = getattr(self.resolver, "home", None)
        if home is not None:
            try:
                home_path = Path(home).expanduser().resolve(strict=True)
                if base != home_path and not base.is_relative_to(home_path):
                    return None, "invalid_folder"
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
            ):
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
    def _new_name(new_name):
        if not isinstance(new_name, str) or not new_name:
            return None, "invalid_name"
        if "\x00" in new_name:
            return None, "invalid_name"
        if new_name in {".", ".."}:
            return None, "invalid_name"
        if ".." in new_name:
            return None, "invalid_name"
        separators = {"/", "\\"}
        for separator in (os.sep, os.altsep):
            if separator:
                separators.add(separator)
        if any(separator in new_name for separator in separators):
            return None, "invalid_name"
        try:
            encoded = os.fsencode(new_name)
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_name"
        if (
            len(encoded) > MAX_NAME_BYTES
            or len(new_name) > MAX_NAME_BYTES
        ):
            return None, "name_too_long"
        return new_name, None

    @staticmethod
    def _has_trailing_separator(value):
        if not isinstance(value, str) or not value:
            return False
        separators = {"/"}
        for separator in (os.sep, os.altsep):
            if separator:
                separators.add(separator)
        return value.endswith(tuple(sorted(separators)))

    @staticmethod
    def _full_path(base, relative):
        target = base / relative
        try:
            if len(os.fsencode(target)) > MAX_PATH_BYTES:
                return None, "path_too_long"
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_relative_path"
        return target, None

    @staticmethod
    def _parent_chain(base, relative):
        """Validate every parent in ``relative`` without following symlinks."""
        current = base
        for part in relative.parts[:-1]:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                return None, "missing_parent", None
            except OSError:
                return None, "parent_unavailable", None
            if stat.S_ISLNK(st.st_mode):
                return None, "symlink_parent", None
            if not stat.S_ISDIR(st.st_mode):
                return None, "parent_not_directory", None
        return current, None, None

    def _source_state(self, base, relative):
        parent, error, _ = self._parent_chain(base, relative)
        if error:
            return None, None, error

        target = base / relative
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return None, None, "source_not_found"
        except OSError:
            return None, None, "source_unavailable"

        if stat.S_ISLNK(st.st_mode):
            return target, st, "symlink_target"
        if stat.S_ISDIR(st.st_mode):
            return target, st, "source_is_directory"
        if not stat.S_ISREG(st.st_mode):
            return target, st, "source_not_regular"
        if int(st.st_size) > MAX_FILE_BYTES:
            return target, st, "file_too_large"
        return target, st, None

    def _destination_lstat(self, base, relative):
        parent, error, _ = self._parent_chain(base, relative)
        if error:
            return None, None, error

        target = base / relative
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return target, None, None
        except NotADirectoryError:
            return None, None, "parent_not_directory"
        except OSError:
            return None, None, "destination_unavailable"
        return target, st, None

    @staticmethod
    def _destination_error(st, overwrite):
        if st is None:
            return None
        if stat.S_ISLNK(st.st_mode):
            return "destination_symlink"
        if stat.S_ISDIR(st.st_mode):
            return "destination_is_directory"
        if not stat.S_ISREG(st.st_mode):
            return "destination_not_regular"
        if not overwrite:
            return "destination_exists"
        return None

    @staticmethod
    def _same_file(source, destination, destination_stat=None):
        try:
            if os.path.abspath(os.fspath(source)) == os.path.abspath(
                os.fspath(destination)
            ):
                return True
        except (OSError, TypeError, ValueError):
            return True
        if destination_stat is not None:
            try:
                source_stat = os.lstat(source)
            except OSError:
                return False
            return (
                source_stat.st_dev == destination_stat.st_dev
                and source_stat.st_ino == destination_stat.st_ino
            )
        return False

    def _same_filesystem(self, source_stat, destination):
        try:
            parent_stat = os.stat(destination.parent)
        except OSError:
            return False
        return int(source_stat.st_dev) == int(parent_stat.st_dev)

    @staticmethod
    def _planned_method(operation, same_filesystem):
        if operation == "copy":
            return "stream"
        return "rename" if same_filesystem else "copy+unlink"

    @staticmethod
    def _echo(value):
        if not isinstance(value, str):
            return None
        if len(value) <= MAX_PATH_LENGTH:
            return value
        return value[:MAX_PATH_LENGTH]

    # ------------------------------------------------------------------
    # Small filesystem helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_all(fd, chunk):
        view = memoryview(chunk)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            view = view[written:]

    @staticmethod
    def _preserve_mtime(path, source_stat):
        """Best-effort mtime preservation after the inode was verified."""
        mtime_ns = getattr(source_stat, "st_mtime_ns", None)
        atime_ns = getattr(source_stat, "st_atime_ns", None)
        if mtime_ns is None or atime_ns is None:
            return False
        if mtime_ns < 0 or atime_ns < 0:
            return False
        try:
            os.utime(
                path,
                ns=(int(atime_ns), int(mtime_ns)),
                follow_symlinks=False,
            )
        except (OSError, ValueError, OverflowError, TypeError):
            return False
        return True

    @staticmethod
    def _close_quietly(fd):
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def _remove_quietly(path):
        try:
            if os.path.lexists(path):
                os.unlink(path)
        except OSError:
            pass

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
