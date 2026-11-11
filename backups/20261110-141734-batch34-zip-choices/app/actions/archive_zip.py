"""Isolated, safety-first ZIP-only archive engine.

The model never supplies an absolute filesystem path. Callers pass one of the
approved folder keys understood by :class:`UserFolderResolver` plus bounded
relative paths. Every mutating entry point is confirmation gated and returns a
structured, JSON-safe result dictionary; preview helpers expose the same plan
without touching the filesystem.

Creation accepts regular files only (no directories, symbolic links, devices
or FIFOs), 1..100 entries and at most 8 GiB of source data. The archive is
written to a private temp file inside the destination folder, fsynced, checked
against the plan, and published with :func:`os.replace` using mode ``0600``.
An existing archive is never replaced unless ``overwrite=True`` is explicit.

Extraction reads the central directory before writing anything and rejects
encrypted entries, absolute paths, traversal, escaping backslashes, symbolic
links, unsupported entry types, duplicate names, more than 1000 entries, any
entry above 2 GiB, more than 8 GiB total, and entries whose compression ratio
exceeds 200x when the output is larger than 100 MiB. Data is extracted into a
fresh ``0700`` temp directory next to the requested destination; files are
created ``0600`` with ``O_EXCL|O_NOFOLLOW``. Nothing is published until entry
count, byte totals, types and sizes are verified, and a failed extraction
removes its partial directory. The destination must be a new directory.

Results carry ``success``, ``action``, ``requires_confirmation``, ``code``
(also ``error_code``), ``message``, ``archive``, ``entries`` (an integer
count), ``items`` (JSON-safe per-entry metadata), ``bytes``, ``destination``,
``preview`` and ``verified``. The tool layer formats ``preview["entries"]``
as a count, so ``items`` is the detailed list.

This module deliberately contains no registry wiring, UI, model, network or
Qt imports, and uses only the Python standard library. See the integration
note at the bottom of this docstring for the argument and confirmation
contract used by the tool layer.

Integration note (tool layer, not wired here)::

    preview_create(folder, relative_paths, archive_name, overwrite=False)
    create(folder, relative_paths, archive_name, overwrite=False,
           confirmed=False)
    preview_extract(folder, archive_relative_path, destination_folder,
                    destination_relative_path=None)
    extract(folder, archive_relative_path, destination_folder,
            destination_relative_path=None, confirmed=False)

``confirmed`` must be exactly ``True`` before anything is written; any other
value (including truthy non-booleans such as ``1``) returns
``confirmation_required`` with a populated ``preview`` and performs zero
writes and zero directory creation. The tool layer should gate these names in
policy with ``confirm=True`` and pass the broker's boolean through unchanged.
"""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from pathlib import Path

from app.actions.file_scope import FileScopeError, UserFolderResolver

# One bounded chunk keeps memory flat regardless of archive size.
CHUNK_SIZE = 1_048_576
# Creation bounds.
MAX_CREATE_ENTRIES = 100
# Extraction bounds.
MAX_EXTRACT_ENTRIES = 1000
MAX_TOTAL_BYTES = 8 * 1024 ** 3
MAX_ENTRY_BYTES = 2 * 1024 ** 3
MAX_COMPRESSION_RATIO = 200
RATIO_MIN_OUTPUT_BYTES = 100 * 1024 ** 2
# Full CRC re-read of a freshly written archive is bounded to keep creation
# linear; structural (name/count/size) verification always runs.
MAX_FULL_VERIFY_BYTES = 64 * 1024 ** 2
# Path bounds mirror app/actions/file_ops.py and file_transfer.py.
MAX_PATH_LENGTH = 1024
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_DEPTH = 32
MAX_NAME_BYTES = 255
ARCHIVE_MODE = 0o600
DIRECTORY_MODE = 0o700
TEMP_PREFIX = ".misty-zip-"
TEMP_SUFFIX = ".tmp"
EXTRACT_PREFIX = ".misty-extract-"
ARCHIVE_SUFFIX = ".zip"

_MESSAGES = {
    "confirmation_required": "Confirmation is required. Nothing was changed.",
    "invalid_folder": "The folder is not on the approved allowlist.",
    "invalid_relative_path": "The relative path is not valid.",
    "empty_relative_path": "The relative path must not be empty.",
    "absolute_path": "Absolute paths are not allowed.",
    "traversal": "The path must not contain '..'.",
    "path_too_long": "The path is too long.",
    "path_too_deep": "The path is too deep.",
    "invalid_entry_name": "An archive entry name is not valid.",
    "invalid_entries": "The entry list is not valid.",
    "no_entries": "At least one file must be selected.",
    "too_many_entries": "Too many entries were selected.",
    "duplicate_entry": "Duplicate archive names are not allowed.",
    "invalid_archive_name": "The archive name is not valid.",
    "archive_extension": "The archive name must end with .zip.",
    "archive_exists": "The archive already exists.",
    "archive_is_directory": "The archive path is a directory.",
    "archive_not_regular": "The archive is not a regular file.",
    "archive_symlink": "The archive path is a symbolic link.",
    "archive_not_found": "The archive does not exist.",
    "archive_unavailable": "The archive is unavailable.",
    "archive_changed": "The archive changed during the operation.",
    "invalid_archive": "The file is not a valid ZIP archive.",
    "source_not_found": "The source file does not exist.",
    "source_unavailable": "The source file is unavailable.",
    "source_is_directory": "The source is a directory.",
    "source_not_regular": "The source is not a regular file.",
    "symlink_target": "The source is a symbolic link.",
    "source_changed": "A source file changed during the operation.",
    "missing_parent": "The parent directory does not exist.",
    "parent_not_directory": "A parent path component is not a directory.",
    "parent_unavailable": "A parent path component is unavailable.",
    "symlink_parent": "A parent path component is a symbolic link.",
    "destination_exists": "The destination directory already exists.",
    "destination_symlink": "The destination is a symbolic link.",
    "destination_unavailable": "The destination is unavailable.",
    "total_too_large": "The selected files exceed the archive size limit.",
    "entry_too_large": "An archive entry exceeds the extraction size limit.",
    "archive_too_large": "The archive exceeds the extraction size limit.",
    "compression_ratio": "An archive entry has an unsafe compression ratio.",
    "encrypted_entry": "Encrypted archive entries are not supported.",
    "symlink_entry": "The archive contains a symbolic link.",
    "unsupported_entry": "The archive contains an unsupported entry type.",
    "create_failed": "The archive could not be created.",
    "extract_failed": "The archive could not be extracted.",
    "verification_failed": "The operation could not be verified.",
    "internal_error": "The operation could not be completed.",
}

__all__ = [
    "CHUNK_SIZE",
    "MAX_CREATE_ENTRIES",
    "MAX_EXTRACT_ENTRIES",
    "MAX_TOTAL_BYTES",
    "MAX_ENTRY_BYTES",
    "MAX_COMPRESSION_RATIO",
    "RATIO_MIN_OUTPUT_BYTES",
    "ZipEngine",
]


def _message_for(error_code):
    return _MESSAGES.get(error_code, _MESSAGES["internal_error"])


class _ZipFailure(Exception):
    """Internal, fixed-code extraction failure carrying no raw text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ZipEngine:
    """Bounded ZIP creation and extraction inside approved user folders.

    ``resolver`` defaults to the production :class:`UserFolderResolver`.
    """

    def __init__(self, resolver=None):
        self.resolver = resolver or UserFolderResolver()

    # ------------------------------------------------------------------
    # Previews (read-only)
    # ------------------------------------------------------------------

    def preview_create(
        self,
        folder,
        relative_paths,
        archive_name,
        overwrite=False,
    ):
        """Return the exact creation plan without writing anything."""
        action = "preview_create"
        try:
            context, error, preview = self._prepare_create(
                folder, relative_paths, archive_name, overwrite
            )
        except Exception:
            return self._error(action, "internal_error")
        if error:
            return self._error(
                action,
                error,
                archive=preview.get("archive"),
                entries=preview.get("entries"),
                destination=preview.get("archive"),
                preview=preview,
            )
        return self._result(
            action,
            True,
            "Preview only. No archive was created.",
            archive=str(context["archive_relative"]),
            entries=preview["entries"],
            bytes_written=preview["bytes"],
            destination=str(context["archive_relative"]),
            preview=preview,
            entry_count=preview["entry_count"],
        )

    def preview_extract(
        self,
        folder,
        archive_relative_path,
        destination_folder,
        destination_relative_path=None,
    ):
        """Return the exact extraction plan without writing anything."""
        action = "preview_extract"
        try:
            context, error, preview = self._prepare_extract(
                folder,
                archive_relative_path,
                destination_folder,
                destination_relative_path,
            )
        except Exception:
            return self._error(action, "internal_error")
        if error:
            return self._error(
                action,
                error,
                archive=preview.get("archive"),
                entries=preview.get("entries"),
                destination=preview.get("destination"),
                preview=preview,
            )
        return self._result(
            action,
            True,
            "Preview only. Nothing was extracted.",
            archive=str(context["archive_relative"]),
            entries=preview["entries"],
            bytes_written=preview["bytes"],
            destination=str(context["destination_relative"]),
            preview=preview,
            entry_count=preview["entry_count"],
        )

    # ------------------------------------------------------------------
    # Mutations (approval gated)
    # ------------------------------------------------------------------

    def create(
        self,
        folder,
        relative_paths,
        archive_name,
        overwrite=False,
        confirmed=False,
    ):
        """Create a ZIP archive from regular files inside one approved folder.

        Without ``confirmed=True`` the result has ``requires_confirmation``
        set and ``preview`` populated. Nothing is created or changed.
        """
        action = "create"
        try:
            context, error, preview = self._prepare_create(
                folder, relative_paths, archive_name, overwrite
            )
        except Exception:
            return self._error(action, "internal_error")
        if error:
            return self._error(
                action,
                error,
                archive=preview.get("archive"),
                entries=preview.get("entries"),
                destination=preview.get("archive"),
                preview=preview,
            )
        if confirmed is not True:
            return self._result(
                action,
                False,
                _message_for("confirmation_required"),
                code="confirmation_required",
                archive=str(context["archive_relative"]),
                entries=preview["entries"],
                destination=str(context["archive_relative"]),
                requires_confirmation=True,
                preview=preview,
            )
        try:
            return self._perform_create(action, context, preview)
        except Exception:
            return self._error(
                action,
                "internal_error",
                archive=str(context["archive_relative"]),
                entries=preview["entries"],
                destination=str(context["archive_relative"]),
                preview=preview,
            )

    def extract(
        self,
        folder,
        archive_relative_path,
        destination_folder,
        destination_relative_path=None,
        confirmed=False,
    ):
        """Extract a ZIP archive into a new directory inside an approved folder.

        Without ``confirmed=True`` the result has ``requires_confirmation``
        set and ``preview`` populated. Nothing is created or changed.
        """
        action = "extract"
        try:
            context, error, preview = self._prepare_extract(
                folder,
                archive_relative_path,
                destination_folder,
                destination_relative_path,
            )
        except Exception:
            return self._error(action, "internal_error")
        if error:
            return self._error(
                action,
                error,
                archive=preview.get("archive"),
                entries=preview.get("entries"),
                destination=preview.get("destination"),
                preview=preview,
            )
        if confirmed is not True:
            return self._result(
                action,
                False,
                _message_for("confirmation_required"),
                code="confirmation_required",
                archive=str(context["archive_relative"]),
                entries=preview["entries"],
                destination=str(context["destination_relative"]),
                requires_confirmation=True,
                preview=preview,
            )
        try:
            return self._perform_extract(action, context, preview)
        except Exception:
            return self._error(
                action,
                "internal_error",
                archive=str(context["archive_relative"]),
                entries=preview["entries"],
                destination=str(context["destination_relative"]),
                preview=preview,
            )

    # ------------------------------------------------------------------
    # Result builders
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        action,
        success,
        message,
        code=None,
        *,
        archive=None,
        entries=None,
        items=None,
        bytes_written=None,
        destination=None,
        requires_confirmation=False,
        preview=None,
        verified=False,
        entry_count=None,
    ):
        if entry_count is None:
            entry_count = (
                entries
                if isinstance(entries, int) and not isinstance(entries, bool)
                else 0
            )
        if items is None and isinstance(preview, dict):
            items = preview.get("items")
        return {
            "success": bool(success),
            "action": action,
            "requires_confirmation": bool(requires_confirmation),
            "code": code,
            "error_code": code,
            "message": message,
            "archive": archive,
            "entries": int(entry_count),
            "items": list(items or []),
            "entry_count": int(entry_count),
            "bytes": (
                int(bytes_written) if bytes_written is not None else None
            ),
            "destination": destination,
            "target": destination,
            "preview": preview,
            "verified": bool(verified),
        }

    def _error(
        self,
        action,
        code,
        *,
        archive=None,
        entries=None,
        destination=None,
        preview=None,
    ):
        return self._result(
            action,
            False,
            _message_for(code),
            code=code,
            archive=archive,
            entries=entries,
            destination=destination,
            preview=preview,
        )

    # ------------------------------------------------------------------
    # Creation preparation
    # ------------------------------------------------------------------

    def _prepare_create(self, folder, relative_paths, archive_name, overwrite):
        preview = {
            "operation": "create",
            "archive": self._echo(archive_name),
            "entries": 0,
            "items": [],
            "entry_count": 0,
            "bytes": 0,
            "overwrite": bool(overwrite),
        }

        base, error = self._base_for(folder)
        if error:
            return None, error, preview

        name, error = self._archive_name(archive_name)
        if error:
            return None, error, preview
        preview["archive"] = name
        preview["archive_name"] = name

        archive_relative = Path(name)
        target, error = self._full_path(base, archive_relative)
        if error:
            return None, error, preview
        preview["absolute_archive"] = str(target)

        if not isinstance(relative_paths, (list, tuple)):
            return None, "invalid_entries", preview
        items = list(relative_paths)
        if not items:
            return None, "no_entries", preview
        if len(items) > MAX_CREATE_ENTRIES:
            preview["entry_count"] = len(items)
            preview["entry_limit"] = MAX_CREATE_ENTRIES
            return None, "too_many_entries", preview

        planned = []
        arcnames = set()
        entries = []
        total = 0
        for item in items:
            relative, error = self._relative_path(item)
            if error:
                return None, error, preview
            if self._has_trailing_separator(item):
                return None, "invalid_relative_path", preview
            if "\\" in item:
                # A backslash would become a directory separator in other
                # ZIP consumers; refuse ambiguous archive names.
                return None, "invalid_relative_path", preview

            arcname = "/".join(relative.parts)
            if arcname in arcnames:
                return None, "duplicate_entry", preview
            arcnames.add(arcname)

            source, st, error = self._source_state(base, relative)
            if error:
                if st is not None:
                    preview["bytes"] = total
                return None, error, preview

            size = int(st.st_size)
            total += size
            if total > MAX_TOTAL_BYTES:
                preview["bytes"] = total
                preview["bytes_limit"] = MAX_TOTAL_BYTES
                return None, "total_too_large", preview
            planned.append(
                {
                    "relative": relative,
                    "source": source,
                    "arcname": arcname,
                    "size": size,
                    "stat": st,
                }
            )
            entries.append(
                {
                    "name": arcname,
                    "size": size,
                    "compressed_size": None,
                    "kind": "file",
                }
            )

        existing, error = self._archive_state(
            base, archive_relative, bool(overwrite)
        )
        preview.update(
            {
                "entries": len(entries),
                "items": entries,
                "entry_count": len(entries),
                "bytes": total,
                "bytes_limit": MAX_TOTAL_BYTES,
                "entry_limit": MAX_CREATE_ENTRIES,
                "archive_exists": existing is not None,
                "atomic": True,
                "mode": "0o600",
                "compression": "deflate",
            }
        )
        if error:
            return None, error, preview

        return (
            {
                "base": base,
                "archive_relative": archive_relative,
                "archive": target,
                "overwrite": bool(overwrite),
                "existing": existing,
                "entries": planned,
                "total": total,
            },
            None,
            preview,
        )

    # ------------------------------------------------------------------
    # Creation mutation
    # ------------------------------------------------------------------

    def _perform_create(self, action, context, preview):
        base = context["base"]
        target = context["archive"]
        archive_text = str(context["archive_relative"])

        existing, error = self._archive_state(
            base, context["archive_relative"], context["overwrite"]
        )
        if error:
            return self._error(
                action,
                error,
                archive=archive_text,
                entries=preview["entries"],
                destination=archive_text,
                preview=preview,
            )

        temp_fd = None
        temp_path = None
        handle = None
        try:
            try:
                temp_fd, temp_name = tempfile.mkstemp(
                    prefix=TEMP_PREFIX,
                    suffix=TEMP_SUFFIX,
                    dir=base,
                )
            except OSError:
                return self._error(
                    action,
                    "create_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=archive_text,
                    preview=preview,
                )
            temp_path = Path(temp_name)
            try:
                os.fchmod(temp_fd, ARCHIVE_MODE)
            except OSError:
                pass

            handle = os.fdopen(temp_fd, "w+b")
            temp_fd = None

            try:
                with zipfile.ZipFile(
                    handle,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as archive:
                    for entry in context["entries"]:
                        self._write_entry(archive, entry)
                handle.flush()
                os.fsync(handle.fileno())
            except _ZipFailure as failure:
                return self._error(
                    action,
                    failure.code,
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=archive_text,
                    preview=preview,
                )
            except (
                OSError,
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
                RuntimeError,
                ValueError,
            ):
                return self._error(
                    action,
                    "create_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=archive_text,
                    preview=preview,
                )
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
                    handle = None

            try:
                if not self._verify_archive(
                    temp_path, context["entries"], context["total"]
                ):
                    return self._error(
                        action,
                        "verification_failed",
                        archive=archive_text,
                        entries=preview["entries"],
                        destination=archive_text,
                        preview=preview,
                    )

                try:
                    temp_st = os.lstat(temp_path)
                except OSError:
                    return self._error(
                        action,
                        "verification_failed",
                        archive=archive_text,
                        entries=preview["entries"],
                        destination=archive_text,
                        preview=preview,
                    )
                if not stat.S_ISREG(temp_st.st_mode):
                    return self._error(
                        action,
                        "verification_failed",
                        archive=archive_text,
                        entries=preview["entries"],
                        destination=archive_text,
                        preview=preview,
                    )

                # Revalidate immediately before publishing the temp file.
                existing, error = self._archive_state(
                    base,
                    context["archive_relative"],
                    context["overwrite"],
                )
                if error:
                    return self._error(
                        action,
                        error,
                        archive=archive_text,
                        entries=preview["entries"],
                        destination=archive_text,
                        preview=preview,
                    )

                os.replace(temp_path, target)
                temp_path = None
            except OSError:
                return self._error(
                    action,
                    "create_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=archive_text,
                    preview=preview,
                )

            try:
                final_st = os.lstat(target)
            except OSError:
                return self._error(
                    action,
                    "verification_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=archive_text,
                    preview=preview,
                )
            if not stat.S_ISREG(final_st.st_mode) or (
                int(final_st.st_size) != int(temp_st.st_size)
            ):
                return self._error(
                    action,
                    "verification_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=archive_text,
                    preview=preview,
                )

            self._fsync_directory(base)
            return self._result(
                action,
                True,
                "Archive created and verified.",
                archive=archive_text,
                entries=preview["entries"],
                bytes_written=context["total"],
                destination=archive_text,
                preview=preview,
                verified=True,
            )
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_path is not None:
                self._remove_quietly(temp_path)

    def _write_entry(self, archive, entry):
        info = zipfile.ZipInfo(
            filename=entry["arcname"],
            date_time=(1980, 1, 1, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | ARCHIVE_MODE) << 16
        info.file_size = int(entry["size"])

        source_fd = None
        try:
            source_fd = os.open(
                entry["source"],
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            raise _ZipFailure("source_unavailable") from error

        try:
            opened = os.fstat(source_fd)
            expected = entry["stat"]
            if not stat.S_ISREG(opened.st_mode):
                raise _ZipFailure("source_not_regular")
            if (opened.st_dev, opened.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise _ZipFailure("source_changed")
            if int(opened.st_size) != int(entry["size"]):
                raise _ZipFailure("source_changed")

            copied = 0
            with archive.open(info, "w") as destination:
                while True:
                    try:
                        chunk = os.read(source_fd, CHUNK_SIZE)
                    except OSError as error:
                        raise _ZipFailure("source_unavailable") from error
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > int(entry["size"]):
                        raise _ZipFailure("source_changed")
                    try:
                        destination.write(chunk)
                    except (OSError, ValueError) as error:
                        raise _ZipFailure("create_failed") from error

            if copied != int(entry["size"]):
                raise _ZipFailure("source_changed")
        finally:
            if source_fd is not None:
                try:
                    os.close(source_fd)
                except OSError:
                    pass

        return copied

    def _verify_archive(self, path, entries, total):
        expected = [(entry["arcname"], int(entry["size"])) for entry in entries]
        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = archive.infolist()
                actual = [
                    (info.filename, int(info.file_size)) for info in infos
                ]
                if actual != expected:
                    return False
                if sum(size for _, size in actual) != int(total):
                    return False
                if int(total) <= MAX_FULL_VERIFY_BYTES:
                    if archive.testzip() is not None:
                        return False
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
            return False
        return True

    # ------------------------------------------------------------------
    # Extraction preparation
    # ------------------------------------------------------------------

    def _prepare_extract(
        self,
        folder,
        archive_relative_path,
        destination_folder,
        destination_relative_path,
    ):
        preview = {
            "operation": "extract",
            "archive": self._echo(archive_relative_path),
            "destination": self._echo(destination_relative_path),
            "entries": 0,
            "items": [],
            "entry_count": 0,
            "bytes": 0,
        }

        base, error = self._base_for(folder)
        if error:
            return None, error, preview
        destination_base, error = self._base_for(destination_folder)
        if error:
            return None, error, preview

        archive_relative, error = self._relative_path(archive_relative_path)
        if error:
            return None, error, preview
        preview["archive"] = str(archive_relative)
        if self._has_trailing_separator(archive_relative_path):
            return None, "invalid_relative_path", preview
        if not archive_relative.name.lower().endswith(ARCHIVE_SUFFIX):
            return None, "archive_extension", preview

        archive_parent, error, _ = self._parent_chain(base, archive_relative)
        if error:
            return None, error, preview

        archive_path, error = self._full_path(base, archive_relative)
        if error:
            return None, error, preview
        preview["absolute_archive"] = str(archive_path)

        archive_st, error = self._archive_input_state(archive_path)
        if error:
            return None, error, preview
        preview["archive_size"] = int(archive_st.st_size)

        destination_relative, error = self._destination_relative(
            destination_relative_path, archive_relative.name
        )
        if error:
            return None, error, preview
        preview["destination"] = str(destination_relative)

        destination, error = self._full_path(
            destination_base, destination_relative
        )
        if error:
            return None, error, preview
        preview["absolute_destination"] = str(destination)

        parent, error, _ = self._parent_chain(
            destination_base, destination_relative
        )
        if error:
            return None, error, preview

        try:
            destination_st = os.lstat(destination)
        except FileNotFoundError:
            destination_st = None
        except NotADirectoryError:
            return None, "parent_not_directory", preview
        except OSError:
            return None, "destination_unavailable", preview
        if destination_st is not None:
            if stat.S_ISLNK(destination_st.st_mode):
                return None, "destination_symlink", preview
            return None, "destination_exists", preview

        entries, total, error = self._inspect_path(archive_path)
        if error:
            return None, error, preview

        preview.update(
            {
                "entries": len(entries),
                "items": [entry["public"] for entry in entries],
                "entry_count": len(entries),
                "bytes": total,
                "entry_limit": MAX_EXTRACT_ENTRIES,
                "entry_bytes_limit": MAX_ENTRY_BYTES,
                "bytes_limit": MAX_TOTAL_BYTES,
                "max_compression_ratio": MAX_COMPRESSION_RATIO,
                "ratio_min_bytes": RATIO_MIN_OUTPUT_BYTES,
                "destination_exists": False,
                "new_directory": True,
                "atomic": True,
                "mode": "0o600",
                "directory_mode": "0o700",
            }
        )

        fingerprint = tuple(
            (
                entry["name"],
                int(entry["size"]),
                int(entry["info"].compress_size),
                int(entry["info"].CRC),
            )
            for entry in entries
        )
        return (
            {
                "archive_base": base,
                "archive_relative": archive_relative,
                "archive": archive_path,
                "archive_stat": archive_st,
                "destination_base": destination_base,
                "destination_relative": destination_relative,
                "destination": destination,
                "destination_parent": parent,
                "entries": entries,
                "total": total,
                "fingerprint": fingerprint,
            },
            None,
            preview,
        )

    def _destination_relative(self, destination_relative_path, archive_name):
        if destination_relative_path is None:
            stem = archive_name[: -len(ARCHIVE_SUFFIX)]
            if not stem:
                return None, "invalid_archive_name"
            relative, error = self._relative_path(stem)
            if error:
                return None, "invalid_archive_name"
            return relative, None

        relative, error = self._relative_path(destination_relative_path)
        if error:
            return None, error
        if self._has_trailing_separator(destination_relative_path):
            return None, "invalid_relative_path"
        return relative, None

    # ------------------------------------------------------------------
    # Extraction mutation
    # ------------------------------------------------------------------

    def _perform_extract(self, action, context, preview):
        archive_text = str(context["archive_relative"])
        destination_text = str(context["destination_relative"])
        destination_base = context["destination_base"]
        destination = context["destination"]

        temp_dir = None
        try:
            archive_st, error = self._archive_input_state(context["archive"])
            if error:
                return self._error(
                    action,
                    error,
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            if (
                archive_st.st_dev,
                archive_st.st_ino,
                int(archive_st.st_size),
            ) != (
                context["archive_stat"].st_dev,
                context["archive_stat"].st_ino,
                int(context["archive_stat"].st_size),
            ):
                return self._error(
                    action,
                    "archive_changed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )

            parent, error, _ = self._parent_chain(
                destination_base, context["destination_relative"]
            )
            if error:
                return self._error(
                    action,
                    error,
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            try:
                destination_st = os.lstat(destination)
            except FileNotFoundError:
                destination_st = None
            except NotADirectoryError:
                return self._error(
                    action,
                    "parent_not_directory",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            except OSError:
                return self._error(
                    action,
                    "destination_unavailable",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            if destination_st is not None:
                code = (
                    "destination_symlink"
                    if stat.S_ISLNK(destination_st.st_mode)
                    else "destination_exists"
                )
                return self._error(
                    action,
                    code,
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )

            try:
                temp_dir = Path(
                    tempfile.mkdtemp(prefix=EXTRACT_PREFIX, dir=parent)
                )
            except OSError:
                return self._error(
                    action,
                    "destination_unavailable",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            try:
                os.chmod(temp_dir, DIRECTORY_MODE)
            except OSError:
                pass

            archive_fd = None
            try:
                try:
                    archive_fd = os.open(
                        context["archive"],
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                    )
                except OSError:
                    return self._error(
                        action,
                        "archive_unavailable",
                        archive=archive_text,
                        entries=preview["entries"],
                        destination=destination_text,
                        preview=preview,
                    )
                handle = os.fdopen(archive_fd, "rb")
                archive_fd = None
                with handle:
                    try:
                        archive = zipfile.ZipFile(handle, "r")
                    except (OSError, zipfile.BadZipFile, ValueError):
                        return self._error(
                            action,
                            "invalid_archive",
                            archive=archive_text,
                            entries=preview["entries"],
                            destination=destination_text,
                            preview=preview,
                        )
                    with archive:
                        entries, total, error = self._inspect_zip(archive)
                        if error:
                            return self._error(
                                action,
                                error,
                                archive=archive_text,
                                entries=preview["entries"],
                                destination=destination_text,
                                preview=preview,
                            )
                        fingerprint = tuple(
                            (
                                entry["name"],
                                int(entry["size"]),
                                int(entry["info"].compress_size),
                                int(entry["info"].CRC),
                            )
                            for entry in entries
                        )
                        if fingerprint != context["fingerprint"]:
                            return self._error(
                                action,
                                "archive_changed",
                                archive=archive_text,
                                entries=preview["entries"],
                                destination=destination_text,
                                preview=preview,
                            )

                        written_bytes = 0
                        written_entries = 0
                        for entry in entries:
                            copied = self._extract_entry(archive, temp_dir, entry)
                            written_bytes += copied
                            written_entries += 1
                            if written_bytes > MAX_TOTAL_BYTES:
                                raise _ZipFailure("archive_too_large")

                if written_entries != len(entries):
                    raise _ZipFailure("verification_failed")
                if written_bytes != int(total):
                    raise _ZipFailure("verification_failed")
                if not self._verify_extraction(
                    temp_dir, entries, written_bytes
                ):
                    raise _ZipFailure("verification_failed")
            except _ZipFailure as failure:
                return self._error(
                    action,
                    failure.code,
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
                return self._error(
                    action,
                    "extract_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            finally:
                if archive_fd is not None:
                    try:
                        os.close(archive_fd)
                    except OSError:
                        pass

            try:
                destination_st = os.lstat(destination)
            except FileNotFoundError:
                destination_st = None
            except OSError:
                return self._error(
                    action,
                    "destination_unavailable",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            if destination_st is not None:
                return self._error(
                    action,
                    "destination_exists",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )

            try:
                os.replace(temp_dir, destination)
                temp_dir = None
            except OSError:
                return self._error(
                    action,
                    "extract_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )

            try:
                final_st = os.lstat(destination)
            except OSError:
                self._remove_tree(destination)
                return self._error(
                    action,
                    "verification_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )
            if not stat.S_ISDIR(final_st.st_mode) or stat.S_ISLNK(
                final_st.st_mode
            ):
                self._remove_tree(destination)
                return self._error(
                    action,
                    "verification_failed",
                    archive=archive_text,
                    entries=preview["entries"],
                    destination=destination_text,
                    preview=preview,
                )

            self._fsync_directory(parent)
            return self._result(
                action,
                True,
                "Archive extracted and verified.",
                archive=archive_text,
                entries=preview["entries"],
                bytes_written=int(total),
                destination=destination_text,
                preview=preview,
                verified=True,
            )
        finally:
            if temp_dir is not None:
                self._remove_tree(temp_dir)

    def _extract_entry(self, archive, root, entry):
        parts = entry["parts"]
        target = root.joinpath(*parts)

        if entry["kind"] == "directory":
            self._ensure_directory(root, parts)
            return 0

        self._ensure_directory(root, parts[:-1])
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(target, flags, ARCHIVE_MODE)
        except FileExistsError as error:
            raise _ZipFailure("unsupported_entry") from error
        except OSError as error:
            raise _ZipFailure("extract_failed") from error

        copied = 0
        try:
            try:
                os.fchmod(fd, ARCHIVE_MODE)
            except OSError:
                pass
            try:
                source = archive.open(entry["info"], "r")
            except RuntimeError as error:
                raise _ZipFailure("encrypted_entry") from error
            except (OSError, zipfile.BadZipFile) as error:
                raise _ZipFailure("invalid_archive") from error

            with os.fdopen(fd, "wb") as destination:
                fd = None
                with source:
                    while True:
                        try:
                            chunk = source.read(CHUNK_SIZE)
                        except RuntimeError as error:
                            raise _ZipFailure("encrypted_entry") from error
                        except (OSError, zipfile.BadZipFile) as error:
                            raise _ZipFailure("invalid_archive") from error
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > int(entry["size"]):
                            raise _ZipFailure("entry_too_large")
                        if copied > MAX_ENTRY_BYTES:
                            raise _ZipFailure("entry_too_large")
                        try:
                            destination.write(chunk)
                        except OSError as error:
                            raise _ZipFailure("extract_failed") from error

                if copied != int(entry["size"]):
                    raise _ZipFailure("verification_failed")
                if int(entry["size"]) > RATIO_MIN_OUTPUT_BYTES and (
                    int(entry["info"].compress_size) <= 0
                    or int(entry["size"])
                    > int(entry["info"].compress_size)
                    * MAX_COMPRESSION_RATIO
                ):
                    raise _ZipFailure("compression_ratio")
                try:
                    destination.flush()
                    os.fsync(destination.fileno())
                except OSError as error:
                    raise _ZipFailure("extract_failed") from error
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        try:
            st = os.lstat(target)
        except OSError as error:
            raise _ZipFailure("verification_failed") from error
        if not stat.S_ISREG(st.st_mode) or int(st.st_size) != int(
            entry["size"]
        ):
            raise _ZipFailure("verification_failed")
        return copied

    def _ensure_directory(self, root, parts):
        current = root
        for part in parts:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                try:
                    os.mkdir(current, DIRECTORY_MODE)
                except FileExistsError:
                    # Something appeared between lstat and mkdir; validate it
                    # rather than trusting it.
                    try:
                        st = os.lstat(current)
                    except OSError as error:
                        raise _ZipFailure("extract_failed") from error
                    if stat.S_ISLNK(st.st_mode):
                        raise _ZipFailure("symlink_entry")
                    if not stat.S_ISDIR(st.st_mode):
                        raise _ZipFailure("unsupported_entry")
                    continue
                except OSError as error:
                    raise _ZipFailure("extract_failed") from error
                try:
                    os.chmod(current, DIRECTORY_MODE)
                except OSError:
                    pass
                continue
            except NotADirectoryError as error:
                raise _ZipFailure("unsupported_entry") from error
            except OSError as error:
                raise _ZipFailure("extract_failed") from error
            if stat.S_ISLNK(st.st_mode):
                raise _ZipFailure("symlink_entry")
            if not stat.S_ISDIR(st.st_mode):
                raise _ZipFailure("unsupported_entry")
        return current

    def _verify_extraction(self, root, entries, total):
        actual_total = 0
        for entry in entries:
            target = root.joinpath(*entry["parts"])
            try:
                st = os.lstat(target)
            except OSError:
                return False
            if entry["kind"] == "directory":
                if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    return False
                continue
            if not stat.S_ISREG(st.st_mode):
                return False
            if stat.S_IMODE(st.st_mode) != ARCHIVE_MODE:
                return False
            if int(st.st_size) != int(entry["size"]):
                return False
            actual_total += int(st.st_size)
        return actual_total == int(total)

    # ------------------------------------------------------------------
    # Archive inspection (read-only)
    # ------------------------------------------------------------------

    def _inspect_path(self, path):
        fd = None
        try:
            try:
                fd = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError:
                return None, 0, "archive_unavailable"

            handle = os.fdopen(fd, "rb")
            fd = None
            with handle:
                try:
                    archive = zipfile.ZipFile(handle, "r")
                except (OSError, zipfile.BadZipFile, ValueError):
                    return None, 0, "invalid_archive"
                with archive:
                    return self._inspect_zip(archive)
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
            return None, 0, "invalid_archive"
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _inspect_zip(self, archive):
        try:
            infos = archive.infolist()
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
            return None, 0, "invalid_archive"

        if len(infos) > MAX_EXTRACT_ENTRIES:
            return None, 0, "too_many_entries"

        entries = []
        names = set()
        total = 0
        for info in infos:
            if int(getattr(info, "flag_bits", 0)) & 0x1:
                return None, 0, "encrypted_entry"

            parts, error = self._entry_parts(info.filename)
            if error:
                return None, 0, error
            name = "/".join(parts)
            if name in names:
                return None, 0, "duplicate_entry"
            names.add(name)

            mode = (int(getattr(info, "external_attr", 0)) >> 16) & 0xFFFF
            # Some writers store only permission bits in the high word, so a
            # zero file-type field is treated as a regular file, not rejected.
            file_type = mode & 0o170000
            if file_type == 0o120000:
                return None, 0, "symlink_entry"
            if file_type not in (0, 0o100000, 0o040000):
                return None, 0, "unsupported_entry"
            is_directory = info.is_dir() or file_type == 0o040000
            if not is_directory and int(info.compress_type) not in {
                zipfile.ZIP_STORED,
                zipfile.ZIP_DEFLATED,
                zipfile.ZIP_BZIP2,
                zipfile.ZIP_LZMA,
            }:
                return None, 0, "unsupported_entry"

            size = 0 if is_directory else int(info.file_size)
            compressed = 0 if is_directory else int(info.compress_size)
            if not is_directory and size > MAX_ENTRY_BYTES:
                return None, 0, "entry_too_large"
            total += size
            if total > MAX_TOTAL_BYTES:
                return None, 0, "archive_too_large"
            if not is_directory and size > RATIO_MIN_OUTPUT_BYTES:
                if compressed <= 0 or (
                    size > compressed * MAX_COMPRESSION_RATIO
                ):
                    return None, 0, "compression_ratio"

            kind = "directory" if is_directory else "file"
            entries.append(
                {
                    "name": name,
                    "parts": parts,
                    "kind": kind,
                    "size": size,
                    "info": info,
                    "public": {
                        "name": name,
                        "size": size,
                        "compressed_size": compressed,
                        "kind": kind,
                    },
                }
            )
        return entries, total, None

    @staticmethod
    def _entry_parts(filename):
        if not isinstance(filename, str) or not filename:
            return None, "invalid_entry_name"
        if "\x00" in filename:
            return None, "invalid_entry_name"

        normalized = filename.replace("\\", "/")
        if normalized.startswith("/"):
            return None, "absolute_path"
        if len(normalized) >= 2 and normalized[1] == ":" and (
            normalized[0].isalpha()
        ):
            return None, "absolute_path"

        raw_parts = normalized.split("/")
        parts = []
        for part in raw_parts:
            if part == "":
                continue
            if part == "..":
                return None, "traversal"
            if part == ".":
                return None, "invalid_entry_name"
            parts.append(part)
        if not parts:
            return None, "invalid_entry_name"
        if len(parts) > MAX_DEPTH:
            return None, "path_too_deep"
        if len(normalized) > MAX_PATH_LENGTH:
            return None, "path_too_long"
        try:
            if len(os.fsencode(normalized)) > MAX_PATH_BYTES:
                return None, "path_too_long"
            if any(
                len(os.fsencode(part)) > MAX_COMPONENT_BYTES
                for part in parts
            ):
                return None, "path_too_long"
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_entry_name"
        return tuple(parts), None

    # ------------------------------------------------------------------
    # Shared validation helpers
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
    def _archive_name(archive_name):
        if not isinstance(archive_name, str) or not archive_name:
            return None, "invalid_archive_name"
        if "\x00" in archive_name:
            return None, "invalid_archive_name"
        if archive_name in {".", ".."}:
            return None, "invalid_archive_name"
        if "/" in archive_name or "\\" in archive_name:
            return None, "invalid_archive_name"
        if os.sep in archive_name or (
            os.altsep and os.altsep in archive_name
        ):
            return None, "invalid_archive_name"
        if not archive_name.lower().endswith(ARCHIVE_SUFFIX):
            return None, "archive_extension"
        if archive_name == ARCHIVE_SUFFIX:
            return None, "invalid_archive_name"
        try:
            encoded = os.fsencode(archive_name)
        except (OSError, RuntimeError, ValueError, TypeError):
            return None, "invalid_archive_name"
        if (
            len(encoded) > MAX_NAME_BYTES
            or len(archive_name) > MAX_NAME_BYTES
        ):
            return None, "invalid_archive_name"
        return archive_name, None

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
        return target, st, None

    def _archive_state(self, base, relative, overwrite):
        target = base / relative
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return None, None
        except OSError:
            return None, "archive_unavailable"

        if stat.S_ISLNK(st.st_mode):
            return st, "archive_symlink"
        if stat.S_ISDIR(st.st_mode):
            return st, "archive_is_directory"
        if not stat.S_ISREG(st.st_mode):
            return st, "archive_not_regular"
        if not overwrite:
            return st, "archive_exists"
        return st, None

    def _archive_input_state(self, path):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None, "archive_not_found"
        except OSError:
            return None, "archive_unavailable"
        if stat.S_ISLNK(st.st_mode):
            return None, "archive_symlink"
        if stat.S_ISDIR(st.st_mode):
            return None, "archive_is_directory"
        if not stat.S_ISREG(st.st_mode):
            return None, "archive_not_regular"
        return st, None

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
    def _remove_quietly(path):
        try:
            if os.path.lexists(path):
                os.unlink(path)
        except OSError:
            pass

    @classmethod
    def _remove_tree(cls, root):
        """Remove a directory tree without ever following a symbolic link."""
        root = Path(root)
        try:
            st = os.lstat(root)
        except OSError:
            return
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return

        directories = []
        stack = [root]
        while stack:
            current = stack.pop()
            directories.append(current)
            try:
                children = list(os.scandir(current))
            except OSError:
                continue
            for child in children:
                try:
                    child_st = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(child_st.st_mode) and not stat.S_ISLNK(
                    child_st.st_mode
                ):
                    stack.append(Path(child.path))
                else:
                    try:
                        os.unlink(child.path)
                    except OSError:
                        pass
        for directory in reversed(directories):
            try:
                os.rmdir(directory)
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
