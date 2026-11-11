"""Isolated, safety-first recursive *directory* copy and move engine.

This module is deliberately separate from :mod:`app.actions.file_transfer`.
That engine transfers exactly one regular file; this one transfers a whole
directory tree. Keeping them apart means directory recursion, tree limits,
atomic tree publication and cross-device tree removal cannot change the
behaviour of the file-only path.

The model never supplies an absolute filesystem path. Callers pass an approved
folder key understood by :class:`UserFolderResolver` plus a bounded relative
path for both ends of a transfer. Every mutating entry point is confirmation
gated and returns a structured, JSON-safe result dictionary. Preview helpers
run the same validation and produce the same plan without touching anything.

Safety rules
------------
* Directories only, recursively. Symbolic links, devices, sockets and FIFOs
  are refused anywhere in the tree.
* At most ``MAX_FILES`` regular files, ``MAX_TOTAL_BYTES`` total bytes,
  ``MAX_DEPTH`` nested levels below the source root, and ``MAX_PATH_BYTES``
  for any resulting path.
* A copy is written into a private temp sibling directory of the destination,
  every file and directory is fsynced, and the tree is published with one
  :func:`os.rename` inside the destination parent. The temp tree is always
  removed when the operation fails.
* A move uses one atomic rename on the same filesystem. Across filesystems it
  copies, publishes, verifies the file count and byte total, and only then
  removes the source. If source removal fails the verified destination is
  kept and the result is reported as partial.
* No content is read beyond the copy itself, and results never contain raw
  exception text, file contents or absolute paths.

Only ``os``, ``stat``, ``tempfile`` and ``pathlib`` are imported, and only for
filesystem work. There is no registry wiring, UI, model, network or Qt code
here. The tool layer integration contract is described at the bottom of this
docstring.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver

# One bounded chunk keeps memory flat regardless of tree size.
CHUNK_SIZE = 1_048_576
# Bounded tree budget: 1000 files and 8 GiB in total.
MAX_FILES = 1000
MAX_TOTAL_BYTES = 8 * 1024 ** 3
# Bounds mirror app/actions/file_transfer.py and app/actions/file_ops.py.
MAX_DEPTH = 32
MAX_PATH_LENGTH = 1024
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
# Published trees never inherit source permissions.
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
TEMP_PREFIX = ".misty-dir-"
BACKUP_PREFIX = ".misty-dir-backup-"
TEMP_SUFFIX = ".tmp"

_MESSAGES = {
    "confirmation_required": "Confirmation is required. Nothing was changed.",
    "invalid_folder": "The folder is not on the approved allowlist.",
    "invalid_relative_path": "The relative path is not valid.",
    "empty_relative_path": "The relative path must not be empty.",
    "absolute_path": "Absolute paths are not allowed.",
    "traversal": "The path must not contain '..'.",
    "scope_escape": "The path escapes the approved folder.",
    "path_too_long": "The path is too long.",
    "path_too_deep": "The path is too deep.",
    "missing_parent": "The parent directory does not exist.",
    "parent_not_directory": "A parent path component is not a directory.",
    "parent_unavailable": "A parent path component is unavailable.",
    "symlink_parent": "A parent path component is a symbolic link.",
    "source_not_found": "The source directory does not exist.",
    "source_unavailable": "The source directory is unavailable.",
    "source_symlink": "The source is a symbolic link.",
    "source_not_directory": "The source is not a directory.",
    "source_changed": "The source directory changed during the operation.",
    "symlink_entry": "The directory contains a symbolic link.",
    "special_entry": "The directory contains an unsupported file type.",
    "too_many_files": "The directory contains too many files.",
    "total_too_large": "The directory is larger than the transfer limit.",
    "destination_exists": "The destination already exists.",
    "destination_symlink": "The destination is a symbolic link.",
    "destination_not_directory": "The destination is not a directory.",
    "destination_unavailable": "The destination is unavailable.",
    "destination_inside_source": (
        "The destination is inside the source directory."
    ),
    "source_inside_destination": (
        "The source is inside the destination directory."
    ),
    "same_directory": "The source and destination are the same directory.",
    "copy_failed": "The directory could not be copied.",
    "move_failed": "The directory could not be moved.",
    "publish_failed": "The copied directory could not be published.",
    "source_remove_failed": (
        "The copy is verified, but the original could not be fully removed."
    ),
    "verification_failed": "The operation could not be verified.",
    "internal_error": "The operation could not be completed.",
}

__all__ = [
    "CHUNK_SIZE",
    "DIRECTORY_MODE",
    "FILE_MODE",
    "MAX_DEPTH",
    "MAX_FILES",
    "MAX_TOTAL_BYTES",
    "DirectoryTransferEngine",
]

# Integration note (tool layer, not wired here):
#
#   engine = DirectoryTransferEngine()               # or pass a resolver
#   engine.preview_copy(folder, relative_path, destination_folder,
#                       destination_relative_path, overwrite=False)
#   engine.preview_move(...same arguments...)
#   engine.copy(..., overwrite=False, confirmed=False)
#   engine.move(..., overwrite=False, confirmed=False)
#
# ``confirmed`` must be exactly True before anything is written. Any other
# value (including truthy non-booleans such as 1) returns
# ``confirmation_required`` with a populated ``preview`` and performs zero
# writes. Policy should gate these names with ``confirm=True`` and pass the
# broker's boolean through unchanged.
#
# Result keys are stable:
#   success, action, code, message, source, destination, files, bytes,
#   overwritten, partial, verified, preview
# ``requires_confirmation`` is the single additive field, mirroring
# app/actions/file_transfer.py so the tool layer can detect a gated mutation
# without inspecting ``code``. ``files`` and ``bytes`` describe the verified
# tree for a mutation and the planned tree for a preview; both are None when
# nothing was changed. ``preview`` is a JSON-safe plan dictionary or None.


def _message_for(error_code):
    return _MESSAGES.get(error_code, _MESSAGES["internal_error"])


class DirectoryTransferEngine:
    """Bounded recursive copy/move of directories inside approved folders.

    ``resolver`` defaults to the production :class:`UserFolderResolver`.
    The engine never creates the source or destination parent directories:
    both ends must already exist inside an approved folder.
    """

    def __init__(self, resolver=None):
        self.resolver = resolver or UserFolderResolver()

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
        """Return the exact directory copy plan without changing anything."""
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
        """Return the exact directory move plan without changing anything."""
        return self._preview_transfer(
            "move",
            folder,
            relative_path,
            destination_folder,
            destination_relative_path,
            overwrite,
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
        """Recursively copy one directory inside approved folders.

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
        """Recursively move one directory inside approved folders.

        Uses one atomic rename on the same filesystem. Across filesystems it
        copies and verifies the tree first, then removes the source.
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

    # ------------------------------------------------------------------
    # Shared result builders
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        action,
        success,
        code=None,
        *,
        message=None,
        source=None,
        destination=None,
        files=None,
        bytes_copied=None,
        overwritten=False,
        partial=False,
        verified=False,
        preview=None,
        requires_confirmation=False,
    ):
        return {
            "success": bool(success),
            "action": action,
            "code": code,
            "message": (
                message if message is not None else _message_for(code)
            ),
            "source": source,
            "destination": destination,
            "files": int(files) if files is not None else None,
            "bytes": (
                int(bytes_copied) if bytes_copied is not None else None
            ),
            "overwritten": bool(overwritten),
            "partial": bool(partial),
            "verified": bool(verified),
            "preview": preview,
            "requires_confirmation": bool(requires_confirmation),
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
            code,
            preview=preview,
            source=source,
            destination=destination,
            **extra,
        )

    # ------------------------------------------------------------------
    # Entry-point plumbing
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
        return self._result(
            action,
            True,
            message="Preview only. Nothing was changed.",
            source=str(context["source_relative"]),
            destination=str(context["destination_relative"]),
            files=context["files"],
            bytes_copied=context["bytes"],
            preview=preview,
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
                "confirmation_required",
                preview=preview,
                source=str(context["source_relative"]),
                destination=str(context["destination_relative"]),
                requires_confirmation=True,
            )
        try:
            source_error = self._revalidate_source(context)
            if source_error:
                return self._fail(
                    action,
                    source_error,
                    preview=preview,
                    source=str(context["source_relative"]),
                    destination=str(context["destination_relative"]),
                )
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

    # ------------------------------------------------------------------
    # Preparation (read-only)
    # ------------------------------------------------------------------

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
            "kind": "directory",
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

        source_path, error = self._full_path(source_base, source_relative)
        if error:
            return None, error, preview
        destination_path, error = self._full_path(
            destination_base, destination_relative
        )
        if error:
            return None, error, preview

        source, source_st, error = self._source_state(
            source_base, source_relative
        )
        if error:
            return None, error, preview

        destination, destination_st, error = self._destination_state(
            destination_base, destination_relative
        )
        if error:
            preview["destination_exists"] = False
            return None, error, preview

        preview["destination_exists"] = destination_st is not None
        preview["overwrite"] = bool(overwrite)

        # Structural conflicts are reported before the overwrite policy: a
        # destination that contains (or sits inside) the source must never be
        # replaced, even with overwrite=True.
        if self._same_directory(source, destination, destination_st):
            return None, "same_directory", preview
        if self._is_within(destination, source):
            return None, "destination_inside_source", preview
        if self._is_within(source, destination):
            return None, "source_inside_destination", preview

        error = self._destination_error(destination_st, bool(overwrite))
        if error:
            return None, error, preview

        plan, error = self._scan(source, source)
        preview.update(
            {
                "files": plan["files"],
                "bytes": plan["bytes"],
                "depth": plan["depth"],
                "max_files": MAX_FILES,
                "max_bytes": MAX_TOTAL_BYTES,
                "max_depth": MAX_DEPTH,
                "mode": "0o700/0o600",
                "destination_exists": destination_st is not None,
                "overwrite": bool(overwrite),
            }
        )
        if error:
            return None, error, preview

        same_filesystem = self._same_filesystem(source_st, destination)
        preview.update(
            {
                "same_filesystem": bool(same_filesystem),
                "atomic": bool(
                    operation == "copy" or same_filesystem
                ),
                "method": self._planned_method(
                    operation, same_filesystem
                ),
            }
        )
        context = {
            "operation": operation,
            "source_base": source_base,
            "source_relative": source_relative,
            "source": source,
            "source_stat": source_st,
            "destination_base": destination_base,
            "destination_relative": destination_relative,
            "destination": destination,
            "destination_state": destination_st,
            "overwrite": bool(overwrite),
            "files": int(plan["files"]),
            "bytes": int(plan["bytes"]),
        }
        return context, None, preview

    # ------------------------------------------------------------------
    # Mutation implementations
    # ------------------------------------------------------------------

    def _perform_copy(self, action, context, preview):
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        source = context["source"]
        destination = context["destination"]
        temp = None
        try:
            try:
                temp = Path(
                    tempfile.mkdtemp(
                        prefix=TEMP_PREFIX,
                        suffix=TEMP_SUFFIX,
                        dir=destination.parent,
                    )
                )
            except OSError:
                return self._fail(
                    action,
                    "destination_unavailable",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )
            try:
                os.chmod(temp, DIRECTORY_MODE)
            except OSError:
                pass

            counts, error = self._build_tree(source, temp, destination)
            if error:
                return self._fail(
                    action,
                    error,
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )
            self._fsync_tree(temp)

            if (
                counts["files"] != context["files"]
                or counts["bytes"] != context["bytes"]
            ):
                return self._fail(
                    action,
                    "source_changed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )

            error, overwritten = self._publish(temp, context)
            if error:
                return self._fail(
                    action,
                    error,
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )
            # The temp tree is now the published destination.
            temp = None
            self._fsync_directory(destination.parent)

            if not self._verify_destination(destination, context):
                return self._fail(
                    action,
                    "verification_failed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    files=context["files"],
                    bytes_copied=context["bytes"],
                    overwritten=overwritten,
                    partial=True,
                    verified=False,
                )
            return self._result(
                action,
                True,
                message="Directory copied.",
                source=source_text,
                destination=destination_text,
                files=context["files"],
                bytes_copied=context["bytes"],
                overwritten=overwritten,
                partial=False,
                verified=True,
                preview=preview,
            )
        finally:
            if temp is not None:
                self._remove_tree(temp)

    def _perform_move(self, action, context, preview):
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        source = context["source"]
        destination = context["destination"]
        overwrite = context["overwrite"]

        destination, destination_st, error = self._destination_state(
            context["destination_base"],
            context["destination_relative"],
        )
        if error:
            return self._fail(
                action,
                error,
                preview=preview,
                source=source_text,
                destination=destination_text,
            )
        context["destination"] = destination

        if destination_st is not None:
            error = self._destination_error(destination_st, overwrite)
            if error:
                return self._fail(
                    action,
                    error,
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )
            if not self._same_filesystem(
                context["source_stat"], destination
            ):
                return self._move_across_devices(
                    action, context, preview, overwritten=True
                )
            return self._rename_over_destination(action, context, preview)

        # Destination absent: one rename is both the move and the publish.
        try:
            os.rename(source, destination)
        except OSError as error:
            if error.errno == errno.EXDEV:
                return self._move_across_devices(
                    action, context, preview, overwritten=False
                )
            return self._fail(
                action,
                "move_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                partial=False,
                verified=False,
            )

        self._fsync_directory(destination.parent)
        if source.parent != destination.parent:
            self._fsync_directory(source.parent)
        return self._finish_rename(
            action, context, preview, overwritten=False
        )

    def _rename_over_destination(self, action, context, preview):
        """Same-filesystem move that replaces an existing directory."""
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        source = context["source"]
        destination = context["destination"]
        backup = None
        try:
            try:
                backup = Path(
                    tempfile.mkdtemp(
                        prefix=BACKUP_PREFIX,
                        suffix=TEMP_SUFFIX,
                        dir=destination.parent,
                    )
                )
            except OSError:
                return self._fail(
                    action,
                    "destination_unavailable",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )
            try:
                os.rename(destination, backup)
            except OSError:
                self._remove_tree(backup)
                return self._fail(
                    action,
                    "publish_failed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                )
            try:
                os.rename(source, destination)
            except OSError as error:
                try:
                    os.rename(backup, destination)
                except OSError:
                    backup = None  # keep the old tree for recovery
                if error.errno == errno.EXDEV:
                    return self._move_across_devices(
                        action, context, preview, overwritten=True
                    )
                return self._fail(
                    action,
                    "move_failed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    partial=False,
                    verified=False,
                )
            self._remove_tree(backup)
            backup = None

            self._fsync_directory(destination.parent)
            if source.parent != destination.parent:
                self._fsync_directory(source.parent)
            return self._finish_rename(
                action, context, preview, overwritten=True
            )
        finally:
            if backup is not None:
                self._remove_tree(backup)

    def _finish_rename(self, action, context, preview, overwritten):
        """Verify a same-filesystem rename by metadata only."""
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        source = context["source"]
        destination = context["destination"]

        verified = self._verify_destination(destination, context)
        try:
            remaining = os.path.lexists(source)
        except OSError:
            remaining = True
        if not verified or remaining:
            return self._fail(
                action,
                "verification_failed",
                preview=preview,
                source=source_text,
                destination=destination_text,
                files=context["files"],
                bytes_copied=context["bytes"],
                overwritten=overwritten,
                partial=True,
                verified=False,
            )
        return self._result(
            action,
            True,
            message="Directory moved.",
            source=source_text,
            destination=destination_text,
            files=context["files"],
            bytes_copied=context["bytes"],
            overwritten=overwritten,
            partial=False,
            verified=True,
            preview=preview,
        )

    def _move_across_devices(
        self, action, context, preview, overwritten
    ):
        """Copy first, verify the published tree, then remove the source."""
        source_text = str(context["source_relative"])
        destination_text = str(context["destination_relative"])
        source = context["source"]
        destination = context["destination"]
        temp = None
        try:
            try:
                temp = Path(
                    tempfile.mkdtemp(
                        prefix=TEMP_PREFIX,
                        suffix=TEMP_SUFFIX,
                        dir=destination.parent,
                    )
                )
            except OSError:
                return self._fail(
                    action,
                    "destination_unavailable",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    partial=False,
                    verified=False,
                )
            try:
                os.chmod(temp, DIRECTORY_MODE)
            except OSError:
                pass

            counts, error = self._build_tree(source, temp, destination)
            if error:
                return self._fail(
                    action,
                    error,
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    partial=False,
                    verified=False,
                )
            self._fsync_tree(temp)

            if (
                counts["files"] != context["files"]
                or counts["bytes"] != context["bytes"]
            ):
                return self._fail(
                    action,
                    "source_changed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    partial=False,
                    verified=False,
                )

            error, published_overwrite = self._publish(temp, context)
            if error:
                return self._fail(
                    action,
                    error,
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    partial=False,
                    verified=False,
                )
            temp = None
            self._fsync_directory(destination.parent)
            overwritten = bool(overwritten or published_overwrite)

            if not self._verify_destination(destination, context):
                return self._fail(
                    action,
                    "verification_failed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    files=context["files"],
                    bytes_copied=context["bytes"],
                    overwritten=overwritten,
                    partial=True,
                    verified=False,
                )

            if not self._remove_tree(source):
                return self._fail(
                    action,
                    "source_remove_failed",
                    preview=preview,
                    source=source_text,
                    destination=destination_text,
                    files=context["files"],
                    bytes_copied=context["bytes"],
                    overwritten=overwritten,
                    partial=True,
                    verified=True,
                )
            self._fsync_directory(source.parent)
            return self._result(
                action,
                True,
                message="Directory moved.",
                source=source_text,
                destination=destination_text,
                files=context["files"],
                bytes_copied=context["bytes"],
                overwritten=overwritten,
                partial=False,
                verified=True,
                preview=preview,
            )
        finally:
            if temp is not None:
                self._remove_tree(temp)

    # ------------------------------------------------------------------
    # Publish and verify
    # ------------------------------------------------------------------

    def _publish(self, temp, context):
        """Atomically publish ``temp`` as the destination tree.

        Returns ``(error_code, overwritten)``. The temporary tree is left in
        place on failure so the caller's cleanup path owns it; on success it
        has been renamed and no longer exists under its old name.
        """
        destination, destination_st, error = self._destination_state(
            context["destination_base"],
            context["destination_relative"],
        )
        if error:
            return error, False

        backup = None
        overwritten = False
        if destination_st is not None:
            if stat.S_ISLNK(destination_st.st_mode):
                return "destination_symlink", False
            if not stat.S_ISDIR(destination_st.st_mode):
                return "destination_not_directory", False
            if not context["overwrite"]:
                return "destination_exists", False
            try:
                backup = Path(
                    tempfile.mkdtemp(
                        prefix=BACKUP_PREFIX,
                        suffix=TEMP_SUFFIX,
                        dir=destination.parent,
                    )
                )
            except OSError:
                return "destination_unavailable", False
            try:
                # Replaces the empty backup directory atomically.
                os.rename(destination, backup)
            except OSError:
                self._remove_tree(backup)
                return "publish_failed", False
            overwritten = True

        try:
            os.rename(temp, destination)
        except OSError:
            if backup is not None:
                try:
                    os.rename(backup, destination)
                except OSError:
                    backup = None  # keep the old tree for recovery
            if backup is not None:
                self._remove_tree(backup)
            return "publish_failed", False

        if backup is not None:
            self._remove_tree(backup)
        return None, overwritten

    def _verify_destination(self, destination, context):
        """Confirm the published tree's file count and byte total."""
        try:
            destination_st = os.lstat(destination)
        except OSError:
            return False
        if not stat.S_ISDIR(destination_st.st_mode):
            return False
        plan, error = self._scan(destination, destination)
        if error:
            return False
        return (
            plan["files"] == context["files"]
            and plan["bytes"] == context["bytes"]
        )

    # ------------------------------------------------------------------
    # Tree walking
    # ------------------------------------------------------------------

    def _scan(self, root, path_root=None):
        """Walk a directory tree without following links or reading content.

        Returns ``({"files", "bytes", "depth"}, error_code)``. Counting stops
        at the first violation so hostile trees cannot consume unbounded time.
        """
        if path_root is None:
            path_root = root
        plan = {"files": 0, "bytes": 0, "depth": 0}
        pending = [(root, Path("."), 0)]
        while pending:
            directory, relative_prefix, depth = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError:
                return plan, "source_unavailable"

            for entry in entries:
                relative = self._join(relative_prefix, entry.name)
                entry_depth = depth + 1
                plan["depth"] = max(plan["depth"], entry_depth)
                if entry_depth > MAX_DEPTH:
                    return plan, "path_too_deep"
                if not self._entry_name_ok(entry.name):
                    return plan, "path_too_long"
                if not self._entry_path_ok(path_root, relative):
                    return plan, "path_too_long"
                try:
                    entry_st = os.lstat(entry.path)
                except OSError:
                    return plan, "source_unavailable"
                if stat.S_ISLNK(entry_st.st_mode):
                    return plan, "symlink_entry"
                if stat.S_ISDIR(entry_st.st_mode):
                    pending.append((entry.path, relative, entry_depth))
                    continue
                if stat.S_ISREG(entry_st.st_mode):
                    plan["files"] += 1
                    plan["bytes"] += int(entry_st.st_size)
                    if plan["files"] > MAX_FILES:
                        return plan, "too_many_files"
                    if plan["bytes"] > MAX_TOTAL_BYTES:
                        return plan, "total_too_large"
                    continue
                return plan, "special_entry"
        return plan, None

    def _build_tree(self, source_root, destination_root, final_root):
        """Copy a tree into the private ``destination_root``.

        Returns ``({"files", "bytes"}, error_code)``. Every created file is
        fsynced before this returns. The caller removes the temp tree on any
        error.
        """
        counts = {"files": 0, "bytes": 0}
        pending = [(source_root, destination_root, Path("."), 0)]
        while pending:
            source_directory, destination_directory, relative_prefix, depth = (
                pending.pop()
            )
            try:
                with os.scandir(source_directory) as iterator:
                    entries = list(iterator)
            except OSError:
                return counts, "source_unavailable"

            for entry in entries:
                relative = self._join(relative_prefix, entry.name)
                entry_depth = depth + 1
                if entry_depth > MAX_DEPTH:
                    return counts, "path_too_deep"
                if not self._entry_name_ok(entry.name):
                    return counts, "path_too_long"
                if not self._entry_path_ok(final_root, relative):
                    return counts, "path_too_long"
                try:
                    entry_st = os.lstat(entry.path)
                except OSError:
                    return counts, "source_unavailable"
                if stat.S_ISLNK(entry_st.st_mode):
                    return counts, "symlink_entry"

                if stat.S_ISDIR(entry_st.st_mode):
                    target_directory = destination_directory / entry.name
                    try:
                        os.mkdir(target_directory, DIRECTORY_MODE)
                        os.chmod(target_directory, DIRECTORY_MODE)
                    except OSError:
                        return counts, "copy_failed"
                    pending.append(
                        (
                            entry.path,
                            target_directory,
                            relative,
                            entry_depth,
                        )
                    )
                    continue

                if not stat.S_ISREG(entry_st.st_mode):
                    return counts, "special_entry"

                error, copied = self._copy_file(
                    entry.path, destination_directory / entry.name
                )
                if error:
                    return counts, error
                counts["files"] += 1
                counts["bytes"] += copied
                if counts["files"] > MAX_FILES:
                    return counts, "too_many_files"
                if counts["bytes"] > MAX_TOTAL_BYTES:
                    return counts, "total_too_large"
        return counts, None

    def _copy_file(self, source, destination):
        """Stream one regular file into the private temp tree.

        Returns ``(error_code, bytes_copied)``. Content is read only here.
        """
        source_fd = None
        destination_fd = None
        copied = 0
        try:
            try:
                source_fd = os.open(
                    source,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError:
                return "source_unavailable", 0

            source_st = os.fstat(source_fd)
            if stat.S_ISLNK(source_st.st_mode):
                return "symlink_entry", 0
            if not stat.S_ISREG(source_st.st_mode):
                return "special_entry", 0
            expected = int(source_st.st_size)
            if expected > MAX_TOTAL_BYTES:
                return "total_too_large", 0

            try:
                destination_fd = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    FILE_MODE,
                )
            except OSError:
                return "copy_failed", 0
            try:
                os.fchmod(destination_fd, FILE_MODE)
            except OSError:
                pass

            while True:
                try:
                    chunk = os.read(source_fd, CHUNK_SIZE)
                except OSError:
                    return "copy_failed", copied
                if not chunk:
                    break
                if copied + len(chunk) > MAX_TOTAL_BYTES:
                    return "total_too_large", copied
                try:
                    self._write_all(destination_fd, chunk)
                except OSError:
                    return "copy_failed", copied
                copied += len(chunk)

            if copied != expected:
                return "source_changed", copied

            try:
                os.fsync(destination_fd)
            except OSError:
                return "copy_failed", copied
            return None, copied
        except OSError:
            return "copy_failed", copied
        finally:
            self._close_quietly(source_fd)
            self._close_quietly(destination_fd)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _base_for(self, folder):
        try:
            if not isinstance(folder, str) or not folder:
                return None, "invalid_folder"
            base = Path(self.resolver.base_for_key(folder))
            base = base.resolve(strict=True)
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
        """Validate every parent in ``relative`` without following links."""
        current = base
        for part in relative.parts[:-1]:
            current = current / part
            try:
                parent_st = os.lstat(current)
            except FileNotFoundError:
                return None, "missing_parent", None
            except NotADirectoryError:
                return None, "parent_not_directory", None
            except OSError:
                return None, "parent_unavailable", None
            if stat.S_ISLNK(parent_st.st_mode):
                return None, "symlink_parent", None
            if not stat.S_ISDIR(parent_st.st_mode):
                return None, "parent_not_directory", None
        return current, None, None

    def _source_state(self, base, relative):
        parent, error, _ = self._parent_chain(base, relative)
        if error:
            return None, None, error

        target = base / relative
        try:
            target_st = os.lstat(target)
        except FileNotFoundError:
            return None, None, "source_not_found"
        except OSError:
            return None, None, "source_unavailable"

        if stat.S_ISLNK(target_st.st_mode):
            return target, target_st, "source_symlink"
        if not stat.S_ISDIR(target_st.st_mode):
            return target, target_st, "source_not_directory"

        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return target, target_st, "source_unavailable"
        if not resolved.is_relative_to(base):
            return target, target_st, "scope_escape"
        return resolved, target_st, None

    def _destination_state(self, base, relative):
        parent, error, _ = self._parent_chain(base, relative)
        if error:
            return None, None, error
        try:
            resolved_parent = parent.resolve(strict=True)
        except (OSError, RuntimeError):
            return None, None, "parent_unavailable"
        if not resolved_parent.is_relative_to(base):
            return None, None, "scope_escape"

        target = resolved_parent / relative.name
        try:
            target_st = os.lstat(target)
        except FileNotFoundError:
            target_st = None
        except NotADirectoryError:
            return None, None, "parent_not_directory"
        except OSError:
            return None, None, "destination_unavailable"
        return target, target_st, None

    @staticmethod
    def _destination_error(target_st, overwrite):
        if target_st is None:
            return None
        if stat.S_ISLNK(target_st.st_mode):
            return "destination_symlink"
        if not stat.S_ISDIR(target_st.st_mode):
            return "destination_not_directory"
        if not overwrite:
            return "destination_exists"
        return None

    @staticmethod
    def _same_directory(source, destination, destination_st=None):
        try:
            if source == destination:
                return True
        except (OSError, TypeError, ValueError):
            return True
        if destination_st is not None:
            try:
                source_st = os.lstat(source)
            except OSError:
                return False
            return (
                int(source_st.st_dev) == int(destination_st.st_dev)
                and int(source_st.st_ino) == int(destination_st.st_ino)
            )
        return False

    @staticmethod
    def _is_within(candidate, parent):
        try:
            return candidate == parent or candidate.is_relative_to(parent)
        except (OSError, TypeError, ValueError):
            return True

    def _same_filesystem(self, source_stat, destination):
        try:
            parent_st = os.stat(Path(destination).parent)
        except OSError:
            return False
        return int(source_stat.st_dev) == int(parent_st.st_dev)

    @staticmethod
    def _planned_method(operation, same_filesystem):
        if operation == "copy":
            return "copy"
        return "rename" if same_filesystem else "copy+remove"

    def _revalidate_source(self, context):
        """Reject a source that vanished or changed identity after preview."""
        try:
            source_st = os.lstat(context["source"])
        except FileNotFoundError:
            return "source_not_found"
        except OSError:
            return "source_unavailable"
        if stat.S_ISLNK(source_st.st_mode):
            return "source_symlink"
        if not stat.S_ISDIR(source_st.st_mode):
            return "source_not_directory"
        expected = context["source_stat"]
        if (
            int(source_st.st_dev),
            int(source_st.st_ino),
        ) != (
            int(expected.st_dev),
            int(expected.st_ino),
        ):
            return "source_changed"
        return None

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
    def _join(prefix, name):
        if not prefix.parts:
            return Path(name)
        return prefix / name

    @staticmethod
    def _entry_name_ok(name):
        try:
            return len(os.fsencode(name)) <= MAX_COMPONENT_BYTES
        except (OSError, RuntimeError, ValueError, TypeError):
            return False

    @staticmethod
    def _entry_path_ok(root, relative):
        try:
            return len(os.fsencode(root / relative)) <= MAX_PATH_BYTES
        except (OSError, RuntimeError, ValueError, TypeError):
            return False

    @staticmethod
    def _write_all(fd, chunk):
        view = memoryview(chunk)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            view = view[written:]

    def _fsync_tree(self, root):
        """Fsync every directory we created, children before parents."""
        pending = [root]
        directories = []
        while pending:
            directory = pending.pop()
            directories.append(directory)
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError:
                continue
            for entry in entries:
                try:
                    entry_st = os.lstat(entry.path)
                except OSError:
                    continue
                if stat.S_ISDIR(entry_st.st_mode):
                    pending.append(entry.path)
        for directory in reversed(directories):
            self._fsync_directory(directory)

    @staticmethod
    def _remove_tree(path):
        """Remove a private tree; never follow symbolic links.

        Returns True when the path no longer exists. Symlinks encountered
        inside the tree are unlinked, not traversed.
        """
        try:
            root_st = os.lstat(path)
        except FileNotFoundError:
            return True
        except OSError:
            return False

        if not stat.S_ISDIR(root_st.st_mode):
            try:
                os.unlink(path)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return True

        try:
            with os.scandir(path) as iterator:
                children = [entry.path for entry in iterator]
        except OSError:
            return False

        removed = True
        for child in children:
            try:
                child_st = os.lstat(child)
            except FileNotFoundError:
                continue
            except OSError:
                removed = False
                continue
            if stat.S_ISDIR(child_st.st_mode):
                if not DirectoryTransferEngine._remove_tree(child):
                    removed = False
                continue
            try:
                os.unlink(child)
            except FileNotFoundError:
                continue
            except OSError:
                removed = False
        if not removed:
            return False
        try:
            os.rmdir(path)
        except FileNotFoundError:
            return True
        except OSError:
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
