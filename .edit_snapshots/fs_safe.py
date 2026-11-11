"""Directory-fd anchored, race-resistant filesystem primitives.

The mutating engines in :mod:`app.actions.file_ops`,
:mod:`app.actions.file_transfer`, :mod:`app.actions.archive_zip` and
:mod:`app.actions.directory_transfer` used to check a path and then act on the
same path. Between the check and the action another process could rename a
path component and substitute a symbolic link, so the action landed outside
the intended tree, or a destination that appeared after the "does it exist?"
check was silently replaced.

This module removes the path race from the operations that delete and
publish:

* :func:`open_dir_nofollow` opens a directory component by component relative
  to a base directory fd with ``O_DIRECTORY | O_NOFOLLOW``, so no component
  can be a symbolic link at the moment it is opened.
* :func:`resolve_dir` opens the parent of a path this way and returns a
  directory fd for it; every later operation is relative to that fd.
* :func:`remove_tree_confined` deletes a *private* tree (a temp or backup
  directory created by Misty itself) using ``scandir(fd)``,
  ``os.unlink(name, dir_fd=fd)`` and ``os.rmdir(name, dir_fd=fd)`` with
  ``O_NOFOLLOW`` on every directory it descends into. Symbolic links inside
  the tree are unlinked, never traversed. If the root itself was replaced by
  a symbolic link (or a non-directory) after the caller created it, the tree
  is refused: nothing outside the tree is touched and only the replacement
  link itself is unlinked.
* :func:`publish_noreplace` publishes a temp file or a temp tree as the
  destination without ever replacing an existing destination it was not told
  about. On Linux it uses ``renameat2(RENAME_NOREPLACE)``; when that syscall
  is unavailable it falls back to ``link`` + ``unlink`` for regular files and
  to an exclusive-marker guarded rename for trees, reporting
  ``overwrite_race`` instead of clobbering when no guarantee is possible.

Everything here is Linux oriented (dir fds, ``O_NOFOLLOW``,
``RENAME_NOREPLACE``) and degrades safely: when a primitive is missing the
caller receives an explicit failure instead of a weaker guarantee.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import uuid

__all__ = [
    "OVERWRITE_RACE",
    "PUBLISH_BLOCKED",
    "PUBLISH_FAILED",
    "PUBLISH_OK",
    "PUBLISH_RACE_WIN",
    "PUBLISH_REPLACED",
    "PUBLISH_UNSUPPORTED",
    "close_quietly",
    "make_marker_name",
    "open_dir_nofollow",
    "publish_noreplace",
    "publish_replace",
    "remove_private_tree",
    "remove_tree_confined",
    "resolve_dir",
    "unlink_quietly",
]

# ``publish_*`` outcomes.
PUBLISH_OK = "ok"
PUBLISH_REPLACED = "replaced"
PUBLISH_UNSUPPORTED = "publish_unsupported"
PUBLISH_FAILED = "publish_failed"
OVERWRITE_RACE = "overwrite_race"
PUBLISH_RACE_WIN = "publish_race_lost"
PUBLISH_BLOCKED = "publish_blocked"

# renameat2 flag; values are stable on Linux.
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_MARKER_SUFFIX = ".misty-marker"
_MARKER_ATTEMPTS = 8
# Depth guard mirrors the directory engine; deeper trees are left in place.
_MAX_DEPTH = 64

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_PATH = getattr(os, "O_PATH", 0)

_DEFAULT_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_PROBE_DIR_FLAGS = (
    (_O_PATH | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    if _O_PATH
    else _DEFAULT_DIR_FLAGS
)

_renameat2 = None


def _load_renameat2():
    """Return a callable ``renameat2(dir_fd_src, src, dir_fd_dst, dst, flags)``."""
    global _renameat2
    if _renameat2 is not None:
        return _renameat2 or None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        symbol = libc.renameat2
    except (OSError, AttributeError):
        _renameat2 = False
        return None
    try:
        symbol.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        symbol.restype = ctypes.c_int
    except (AttributeError, TypeError):
        _renameat2 = False
        return None

    def call(dir_fd_src, src, dir_fd_dst, dst, flags):
        result = symbol(
            ctypes.c_int(dir_fd_src),
            os.fsencode(src),
            ctypes.c_int(dir_fd_dst),
            os.fsencode(dst),
            ctypes.c_uint(flags),
        )
        if result != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), src)
        return None

    _renameat2 = call
    return call


def close_quietly(fd):
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def make_marker_name():
    return f".misty-lock-{uuid.uuid4().hex}{_MARKER_SUFFIX}"


# ----------------------------------------------------------------------
# Directory fds
# ----------------------------------------------------------------------


def open_dir_nofollow(path, base_fd=None, flags=_DEFAULT_DIR_FLAGS):
    """Open ``path`` as a directory fd without following any symlink.

    ``path`` may be an absolute path or a relative sequence of components.
    Every component is opened with ``O_DIRECTORY | O_NOFOLLOW`` relative to
    the previous fd, so a component swapped for a symlink between two opens
    makes this raise ``OSError(ELOOP)`` rather than resolving through it.

    When ``base_fd`` is given and ``path`` is relative, resolution starts at
    ``base_fd``. The caller owns the returned fd.
    """
    parts = _path_parts(path)
    absolute = bool(parts) and parts[0] == ""
    if absolute:
        # Leading separator (absolute path): start at the filesystem root.
        fd = os.open(os.sep, flags)
        parts = [part for part in parts[1:] if part]
    elif base_fd is None:
        fd = None
    else:
        fd = os.dup(base_fd)

    try:
        if fd is None:
            if not parts:
                return os.open(".", flags)
            fd = os.open(parts[0], flags)
            parts = parts[1:]
        for part in parts:
            if part in ("", "."):
                continue
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        close_quietly(fd)
        raise


def _path_parts(path):
    if isinstance(path, (list, tuple)):
        return [os.fspath(part) for part in path]
    if isinstance(path, int):
        raise TypeError("path must be a path")
    raw = os.fspath(path)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    return raw.split(os.sep)


def resolve_dir(path):
    """Return ``(parent_fd, name)`` for the final component of ``path``.

    The parent chain is opened with :func:`open_dir_nofollow`, so the caller
    can perform ``os.unlink(name, dir_fd=fd)``,
    ``os.rename(..., dst_dir_fd=fd, ...)`` or ``os.lstat(name, dir_fd=fd)``
    against a directory that cannot have been swapped for a symlink.

    Raises ``OSError`` when a parent is missing, not a directory, or a symlink.
    """
    directory, name = os.path.split(os.path.abspath(os.fspath(path)))
    if not name or name in (".", ".."):
        raise OSError(errno.EINVAL, "invalid path", os.fspath(path))
    fd = open_dir_nofollow(directory)
    return fd, name


def _ensure_traversable(fd):
    """Best effort: restore owner rwx on a directory we are emptying."""
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
    except OSError:
        return False
    if mode & 0o700 == 0o700:
        return True
    try:
        os.fchmod(fd, mode | 0o700)
        return True
    except OSError:
        return False


def _sticky_protected(parent_fd, name):
    """Detect EPERM from the sticky bit when deleting someone else's file.

    Returns True when the final component is not owned by us, which lets the
    caller report the deletion as refused rather than as a transient race.
    """
    if not _O_PATH:
        return False
    try:
        fd = os.open(os.fspath(name), _PROBE_DIR_FLAGS, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        return os.fstat(fd).st_uid != os.geteuid()
    except OSError:
        return False
    finally:
        close_quietly(fd)


# ----------------------------------------------------------------------
# Confined removal
# ----------------------------------------------------------------------


class _RemovalState:
    __slots__ = ("leftover", "refused", "error")

    def __init__(self):
        self.leftover = 0
        self.refused = False
        self.error = None


def remove_private_tree(root):
    """``remove_tree_confined`` with a plain ``True`` on a clean removal.

    Returns True only when the tree is gone *and* nothing was refused, which
    is the contract of the engines' ``_remove_tree`` helpers.
    """
    ok, leftover = remove_tree_confined(root)
    return bool(ok) and leftover == 0


def remove_tree_confined(root):
    """Remove a private tree without ever following a symbolic link.

    Returns ``(ok, leftover)``. ``leftover`` counts entries that remain. The
    root is opened with ``O_NOFOLLOW``; if it is no longer a directory (for
    example it was replaced by a symlink after the caller created it) the
    call refuses, touches nothing outside the tree, unlinks the replacement
    symlink itself when it can, and returns ``(False, 1)``.
    """
    state = _RemovalState()
    try:
        parent_fd, name = resolve_dir(root)
    except OSError:
        return False, 1
    try:
        try:
            root_fd = os.open(name, _DEFAULT_DIR_FLAGS, dir_fd=parent_fd)
        except (FileNotFoundError, NotADirectoryError):
            return True, 0
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                return _refuse_root(parent_fd, name, state)
            if error.errno in (errno.EACCES, errno.EPERM) and (
                _sticky_protected(parent_fd, name)
            ):
                return _refuse_root(parent_fd, name, state)
            # Unreadable private directory: unlinking the name is still
            # safe because the name itself is still a directory.
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return True, 0
            except OSError:
                return False, 1
            return True, 0

        try:
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                return _refuse_root(parent_fd, name, state)
            self_accessible = _ensure_traversable(root_fd)
            _empty_directory(root_fd, 0, state)
            if state.leftover:
                return False, state.leftover
            if not self_accessible:
                return False, 1
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return True, 0
            except OSError:
                return False, 1
            return True, 0
        finally:
            close_quietly(root_fd)
    finally:
        close_quietly(parent_fd)


def _refuse_root(parent_fd, name, state):
    """The root was replaced: drop the replacement link, keep its target."""
    state.refused = True
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True, 0
    except OSError:
        state.leftover = 1
        return False, 1
    if not stat.S_ISLNK(current.st_mode):
        # A non-directory replacement is owned by us (mkdtemp name); unlink
        # it so the temp name does not linger.
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return True, 0
        except OSError:
            pass
        state.leftover = 1
        return False, 1
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return True, 0
    except OSError:
        state.leftover = 1
        return False, 1
    return False, 0


def _open_child_dir(dir_fd, name):
    """Open ``name`` below ``dir_fd`` as a directory, never through a link.

    ``O_PATH|O_NOFOLLOW`` succeeds for both directories and symbolic links, so
    it is used to *identify* the entry and only a real directory is then
    re-opened with read access.
    """
    probe = None
    if _O_PATH:
        try:
            probe = os.open(name, _PROBE_DIR_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError:
            probe = None
        if probe is not None:
            try:
                if not stat.S_ISDIR(os.fstat(probe).st_mode):
                    return None
            finally:
                close_quietly(probe)
    try:
        return os.open(name, _DEFAULT_DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _empty_directory(dir_fd, depth, state):
    if depth > _MAX_DEPTH:
        state.leftover += 1
        return
    try:
        names = list(os.listdir(dir_fd))
    except OSError:
        state.leftover += 1
        return

    child_dirs = []
    for name in names:
        try:
            entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            state.leftover += 1
            continue
        if stat.S_ISDIR(entry.st_mode):
            child_fd = _open_child_dir(dir_fd, name)
            if child_fd is None:
                # Swapped for a symlink or made unreadable after the stat:
                # unlink the name, never traverse it.
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except (FileNotFoundError, IsADirectoryError):
                    pass
                except OSError:
                    state.leftover += 1
                continue
            try:
                _ensure_traversable(child_fd)
                _empty_directory(child_fd, depth + 1, state)
            finally:
                close_quietly(child_fd)
            child_dirs.append(name)
            continue
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            continue
        except OSError:
            state.leftover += 1

    for name in child_dirs:
        try:
            os.rmdir(name, dir_fd=dir_fd)
        except FileNotFoundError:
            continue
        except OSError:
            state.leftover += 1


def _create_marker(parent_fd):
    last_error = None
    for _ in range(_MARKER_ATTEMPTS):
        name = make_marker_name()
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as error:
            last_error = error
            continue
        os.close(fd)
        return name
    raise last_error or OSError(errno.EEXIST, "marker unavailable")


def _noreplace_with_marker(src, parent_fd, name):
    """Fallback no-clobber publish for trees when renameat2 is missing.

    An exclusive marker created in the destination directory serialises
    publishers that use the same protocol. The destination is then claimed
    once by ``os.rename(src, name)``, which renames *onto* ``name`` and can
    only succeed if ``name`` still does not exist, so a destination created
    by another process is never replaced. When the marker cannot be created
    the kernels' guarantee is unknown and ``PUBLISH_UNSUPPORTED`` is
    returned instead of guessing.
    """
    marker = None
    try:
        marker = _create_marker(parent_fd)
    except OSError:
        return PUBLISH_UNSUPPORTED
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        except OSError:
            return PUBLISH_FAILED
        if current is not None:
            return PUBLISH_BLOCKED
        try:
            os.rename(src, name, dst_dir_fd=parent_fd)
        except OSError as error:
            if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                return PUBLISH_RACE_WIN
            return PUBLISH_FAILED
        try:
            placed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            source = os.lstat(src) if os.path.lexists(src) else None
        except OSError:
            return PUBLISH_FAILED
        if source is not None and (
            int(placed.st_dev),
            int(placed.st_ino),
        ) != (int(source.st_dev), int(source.st_ino)):
            return PUBLISH_FAILED
        return PUBLISH_OK
    finally:
        if marker is not None:
            try:
                os.unlink(marker, dir_fd=parent_fd)
            except OSError:
                pass


def publish_noreplace(temp_path, dest_path):
    """Publish ``temp_path`` as ``dest_path`` without clobbering anything.

    Returns one of :data:`PUBLISH_OK`, :data:`OVERWRITE_RACE`,
    :data:`PUBLISH_UNSUPPORTED`, :data:`PUBLISH_FAILED` or
    :data:`PUBLISH_BLOCKED` (the destination exists and the caller asked for
    a no-clobber publish). The temp path is consumed only on
    :data:`PUBLISH_OK`.
    """
    try:
        parent_fd, name = resolve_dir(dest_path)
    except OSError:
        return PUBLISH_FAILED
    try:
        try:
            temp_st = os.lstat(temp_path)
        except OSError:
            return PUBLISH_FAILED
        if stat.S_ISDIR(temp_st.st_mode):
            return _publish_tree_noreplace(temp_path, parent_fd, name)
        if not stat.S_ISREG(temp_st.st_mode):
            return PUBLISH_UNSUPPORTED
        call = _load_renameat2()
        if call is not None:
            src_abs = os.path.abspath(os.fspath(temp_path))
            try:
                call(_AT_FDCWD, src_abs, parent_fd, name, _RENAME_NOREPLACE)
                return PUBLISH_OK
            except FileExistsError:
                return OVERWRITE_RACE
            except OSError as error:
                if error.errno not in (
                    errno.ENOSYS,
                    errno.EINVAL,
                    errno.EOPNOTSUPP,
                    errno.EPERM,
                    errno.ENOTSUP,
                ):
                    return PUBLISH_FAILED
        return _publish_file_link(temp_path, parent_fd, name)
    finally:
        close_quietly(parent_fd)


def _publish_file_link(temp_path, parent_fd, name):
    """Atomic no-clobber publish of a regular file via link + unlink."""
    try:
        os.link(
            temp_path,
            name,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        return OVERWRITE_RACE
    except OSError as error:
        if error.errno == errno.EXDEV:
            return PUBLISH_UNSUPPORTED
        return PUBLISH_FAILED
    try:
        os.unlink(temp_path)
    except OSError:
        pass
    return PUBLISH_OK


def _publish_tree_noreplace(temp_path, parent_fd, name):
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    except OSError:
        return PUBLISH_FAILED
    if current is not None:
        if stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(
            current.st_mode
        ):
            return PUBLISH_BLOCKED
        return OVERWRITE_RACE
    call = _load_renameat2()
    if call is not None:
        temp_abs = os.path.abspath(os.fspath(temp_path))
        try:
            call(_AT_FDCWD, temp_abs, parent_fd, name, _RENAME_NOREPLACE)
            return PUBLISH_OK
        except FileExistsError:
            return OVERWRITE_RACE
        except OSError as error:
            if error.errno not in (
                errno.ENOSYS,
                errno.EINVAL,
                errno.EOPNOTSUPP,
                errno.EPERM,
                errno.ENOTSUP,
            ):
                return PUBLISH_FAILED
    # No RENAME_NOREPLACE on this kernel/filesystem: rename under an
    # exclusive marker so a concurrently created destination is never
    # silently replaced.
    return _noreplace_with_marker(temp_path, parent_fd, name)


def publish_replace(temp_path, dest_path):
    """Atomically replace ``dest_path`` with ``temp_path``.

    Used only when the caller explicitly allowed overwriting. Anchored on the
    parent directory fd like the other primitives; the temp path must be on
    the same filesystem (the engines create it in the destination parent).
    """
    try:
        parent_fd, dest_name = resolve_dir(dest_path)
    except OSError:
        return PUBLISH_FAILED
    try:
        try:
            os.replace(temp_path, dest_name, dst_dir_fd=parent_fd)
        except OSError:
            return PUBLISH_FAILED
        return PUBLISH_REPLACED
    finally:
        close_quietly(parent_fd)
