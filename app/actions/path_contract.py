"""Lexical/policy contract for caller-supplied relative paths.

Single source of truth for the validation helpers that used to be copied
into ``file_ops``, ``file_transfer``, ``archive_zip`` and
``directory_transfer``.  The module is pure and stdlib-only: no file
descriptors are opened and nothing is written.  ``parent_chain`` does call
``os.lstat`` (that is its contract), every other function is lexical.

Stable error codes
------------------
``invalid_relative_path``, ``empty_relative_path``, ``path_too_long``,
``absolute_path``, ``traversal``, ``path_too_deep``, ``invalid_folder``,
``missing_parent``, ``parent_unavailable``, ``symlink_parent``,
``parent_not_directory``.  These are the codes the engines returned before
the consolidation; payload shape (``Path`` vs ``tuple`` vs ``None``) is left
to the callers because the engines disagree there on purpose.

Policy differences (ported, parameterised instead of unified)
------------------------------------------------------------
=======================  ===========  =============  =================  ==================
rule                     file_ops     file_transfer  archive_zip        directory_transfer
=======================  ===========  =============  =================  ==================
``a//b``, ``a/./b``      collapsed    collapsed      collapsed          rejected
(empty/``.`` component)                                                   (``invalid_…``)
leading ``./a``          collapsed    collapsed      collapsed          collapsed here;
                                                                          call sites still
                                                                          reject via the
                                                                          ``"."`` component
                                                                          check in ``""``
backslash ``a\\b``       ordinary     ordinary       ordinary in the    split as a
                         character    character      helper; ``create`` separator
                                                       rejects ``"\\"``
``a/``, ``a//``          rejected     rejected       rejected           rejected
``a/.``                  rejected     rejected       rejected           rejected
                         (closed hole: previously accepted, then silently
                         collapsed to directory ``a`` by the call sites)
``a/..``                 ``traversal`` in all four engines (a ``".."`` part)
NFC/NFKC normalisation   none         none           none               none
(decomposed ``e`` + combining acute stays decomposed; no engine re-normalises)
percent-encoded          literal      literal        literal            literal
``%2F`` / ``%5C``        (``..%2Fx`` is a normal name, not ``../x``)
reserved names           none         none           none               none
(``CON``, ``aux.txt`` keep their historic acceptance)
control characters       NUL only     NUL only       NUL only           NUL only
(other controls are legal file-name characters on POSIX)
path length (chars)      1024         1024           1024               1024
path length (bytes)      4096         4096           4096               4096
component bytes          255          255            255                255
depth                    32           32             32                 32
folder containment       ``base_for_  ``base_for_    ``base_for_        ``base_for_
                         key`` only   key`` + home   key`` + home       key`` + home,
                         (no home)    containment    containment        then
                                                                         ``resolve(strict)``
``parent_chain`` third   missing      ``None``       ``None``           ``None``
result                   parts
``parent_chain`` ENOENT  ``allow_     ``missing_     ``missing_          ``missing_
                         missing``    parent``       parent``           parent``
=======================  ===========  =============  =================  ==================

Design notes
------------
* ``file_ops`` never had ``_has_trailing_separator``; it tested
  ``str.endswith("/")`` inline and therefore accepted ``"a/."``, which then
  collapsed to directory ``"a"``.  ``relative_path_parts`` rejects every
  form whose ``Path`` form names a directory (final ``"/"`` or final ``".""``),
  so all four engines return ``invalid_relative_path``.  Error codes and
  engine messages are unchanged for every input that was already rejected.
* ``directory_transfer`` is the one engine whose ``_relative_path`` split on
  ``"\\"``; it is selected with ``POLICY_DIRECTORY_TRANSFER`` so the split
  never leaks into the other three.
* Payload differences are preserved: ``file_ops._relative_path`` and the
  others still return ``Path``; ``file_ops`` returns the *remaining* missing
  components for ``mkdir(parents=True)`` while the rest return ``None``.
* Nothing in ``fs_safe`` (transactional publish/remove) is involved.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

MAX_PATH_LENGTH = 1024
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_DEPTH = 32

INVALID_RELATIVE_PATH = "invalid_relative_path"
EMPTY_RELATIVE_PATH = "empty_relative_path"
PATH_TOO_LONG = "path_too_long"
ABSOLUTE_PATH = "absolute_path"
TRAVERSAL = "traversal"
PATH_TOO_DEEP = "path_too_deep"
INVALID_FOLDER = "invalid_folder"
MISSING_PARENT = "missing_parent"
PARENT_UNAVAILABLE = "parent_unavailable"
SYMLINK_PARENT = "symlink_parent"
PARENT_NOT_DIRECTORY = "parent_not_directory"

ERROR_MESSAGES = MappingProxyType(
    {
        INVALID_RELATIVE_PATH: "The relative path is not valid.",
        EMPTY_RELATIVE_PATH: "The relative path must not be empty.",
        PATH_TOO_LONG: "The path is too long.",
        ABSOLUTE_PATH: "Absolute paths are not allowed.",
        TRAVERSAL: "The path must not contain '..'.",
        PATH_TOO_DEEP: "The path is too deep.",
        INVALID_FOLDER: "The folder is not on the approved allowlist.",
        MISSING_PARENT: "The parent directory does not exist.",
        PARENT_UNAVAILABLE: "A parent path component is unavailable.",
        SYMLINK_PARENT: "A parent path component is a symbolic link.",
        PARENT_NOT_DIRECTORY: "A parent path component is not a directory.",
    }
)

# Errors raised while converting the value to bytes (``os.fsencode``) or to a
# ``Path``.  These are the exception tuples the engines already caught.
ENCODING_ERRORS = (OSError, RuntimeError, ValueError, TypeError)


class PathContractError(ValueError):
    """A path violated the contract; ``code`` is one of the stable codes."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, code)
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r})"


@dataclass(frozen=True)
class PathPolicy:
    """The two axes on which the engines genuinely disagree.

    ``reject_dot_components`` -- ``directory_transfer`` refuses ``a/./b``,
    a leading ``./``, and ``a//b`` (empty component) where the other three
    collapse them through ``Path``.  ``a/..`` is ``traversal`` in all four.

    ``backslash_is_separator`` -- ``directory_transfer`` splits ``a\\b`` into
    two components; the other three keep the backslash as an ordinary
    character in the helper and (archive_zip) reject it at the call site.
    """

    reject_dot_components: bool = False
    backslash_is_separator: bool = False

    @property
    def names(self) -> str:
        axes = []
        if self.reject_dot_components:
            axes.append("reject_dot_components")
        if self.backslash_is_separator:
            axes.append("backslash_is_separator")
        return "baseline" if not axes else "baseline+" + "+".join(axes)


POLICY_BASELINE = PathPolicy()
POLICY_FILE_OPS = POLICY_BASELINE
POLICY_FILE_TRANSFER = POLICY_BASELINE
POLICY_ARCHIVE_ZIP = POLICY_BASELINE
POLICY_DIRECTORY_TRANSFER = PathPolicy(
    reject_dot_components=True, backslash_is_separator=True
)


def _separators() -> frozenset[str]:
    found = {"/"}
    for separator in (os.sep, os.altsep):
        if separator:
            found.add(separator)
    return frozenset(found)


def _ends_with_separator_or_dot(value: str) -> bool:
    """``"a/"``, ``"a//"`` and ``"a/."`` describe a directory, not an item.

    A final ``"."`` component collapses through ``Path`` exactly like a
    trailing slash.  The engines *intended* to reject ``"a/"`` only
    (``str.endswith("/")``) but collapsed ``"a/."`` silently; one rule now
    covers both.  ``"a/.."`` is not included here -- it is ``traversal``.
    """
    if not value:
        return False
    if value.endswith(tuple(sorted(_separators()))):
        return True
    return (value + "/").split("/")[-2] == "."


def has_trailing_separator(value: Any) -> bool:
    """True when ``value`` ends with ``/`` or the platform separators."""
    if not isinstance(value, str) or not value:
        return False
    return value.endswith(tuple(sorted(_separators())))


def _dot_component_check(value: str) -> str | None:
    """directory_transfer-style component check (``""`` and ``"."``)."""
    parts = value.replace("\\", "/").split("/")
    if parts and parts[0] == ".":
        parts = parts[1:]
    if ".." in parts:
        return TRAVERSAL
    if any(part in {"", "."} for part in parts):
        return INVALID_RELATIVE_PATH
    return None


def relative_path_parts(value: Any, *, policy: PathPolicy = POLICY_BASELINE):
    """Validate ``value`` and return the lexical components as a tuple.

    Order of checks is the order the engines used, so the winning error code
    for multi-fault inputs does not change: type, empty, character length,
    NUL, byte length, ``Path()`` conversion, absolute, trailing separator or
    final ``"."`` (``invalid_relative_path``; it would silently strip to a
    directory name), ``.``/``""`` components (directory_transfer policy
    only), ``..``, depth, per-component byte length.
    """
    if not isinstance(value, str):
        raise PathContractError(INVALID_RELATIVE_PATH)
    if not value:
        raise PathContractError(EMPTY_RELATIVE_PATH)
    if len(value) > MAX_PATH_LENGTH:
        raise PathContractError(PATH_TOO_LONG)
    if "\x00" in value:
        raise PathContractError(INVALID_RELATIVE_PATH)
    try:
        if len(os.fsencode(value)) > MAX_PATH_BYTES:
            raise PathContractError(PATH_TOO_LONG)
        path = Path(value)
    except PathContractError:
        raise
    except ENCODING_ERRORS:
        raise PathContractError(INVALID_RELATIVE_PATH) from None

    if path.is_absolute():
        raise PathContractError(ABSOLUTE_PATH)
    # The engines reject a trailing separator after ``_relative_path`` had
    # already collapsed it ("a/" -> "\"a\").  The effect is the same; here it
    # is one rule instead of two call-site checks.
    if _ends_with_separator_or_dot(value):
        raise PathContractError(INVALID_RELATIVE_PATH)
    if policy.reject_dot_components:
        error = _dot_component_check(value)
        if error is not None:
            raise PathContractError(error)
    parts = path.parts
    if not parts:
        raise PathContractError(INVALID_RELATIVE_PATH)
    if ".." in parts:
        raise PathContractError(TRAVERSAL)
    if len(parts) > MAX_DEPTH:
        raise PathContractError(PATH_TOO_DEEP)
    try:
        if any(
            len(os.fsencode(part)) > MAX_COMPONENT_BYTES for part in parts
        ):
            raise PathContractError(PATH_TOO_LONG)
    except PathContractError:
        raise
    except ENCODING_ERRORS:
        raise PathContractError(INVALID_RELATIVE_PATH) from None
    return parts


def _home_containment(base: Path, resolver: Any) -> bool:
    home = getattr(resolver, "home", None)
    if home is None:
        return True
    try:
        home_path = Path(home).expanduser().resolve(strict=True)
        return base == home_path or base.is_relative_to(home_path)
    except ENCODING_ERRORS:
        return False


def approved_base(
    resolver: Any,
    folder: Any,
    *,
    enforce_home: bool = False,
    resolve_strict: bool = False,
) -> Path:
    """Map a folder key to an existing, non-symlink directory.

    ``enforce_home`` -- also require the base to sit under ``resolver.home``
    (file_transfer, archive_zip, directory_transfer).  ``file_ops`` relies on
    ``resolver.base_for_key`` alone and leaves it False.

    ``resolve_strict`` -- call ``resolve(strict=True)`` before the checks
    (directory_transfer only); the other three use ``base_for_key`` verbatim.
    """
    if not isinstance(folder, str) or not folder:
        raise PathContractError(INVALID_FOLDER)
    try:
        base = Path(resolver.base_for_key(folder))
        if resolve_strict:
            base = base.resolve(strict=True)
    except Exception as error:  # noqa: BLE001 - resolver errors carry the reason
        raise PathContractError(INVALID_FOLDER, str(error) or None) from None

    try:
        if not base.is_dir() or base.is_symlink():
            raise PathContractError(INVALID_FOLDER)
    except PathContractError:
        raise
    except ENCODING_ERRORS:
        raise PathContractError(INVALID_FOLDER) from None

    if enforce_home and not _home_containment(base, resolver):
        raise PathContractError(
            INVALID_FOLDER,
            "The approved folder resolves outside the home directory.",
        )
    return base


def parent_chain(
    base: Any,
    parts: Any,
    *,
    allow_missing: bool = False,
    collect_missing: bool = False,
    not_a_directory_code: str = PARENT_NOT_DIRECTORY,
):
    """Walk every parent of ``parts`` without following symlinks.

    Returns ``(parent, missing)``.  ``not_a_directory_code`` lets
    ``directory_transfer`` keep mapping ``NotADirectoryError`` from
    ``lstat`` to ``parent_not_directory`` (baseline engines surface it as
    ``parent_unavailable``).

    When ``allow_missing`` (file_ops ``mkdir(parents=True)``) a missing
    parent returns the first missing path and, with ``collect_missing``, the
    remaining components; otherwise the missing list is empty.
    """
    current = Path(base)
    tail = list(parts)
    parents = tail[:-1]
    for index, part in enumerate(parents):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                return current, (parents[index:] if collect_missing else [])
            raise PathContractError(MISSING_PARENT) from None
        except PathContractError:
            raise
        except OSError as exc:
            code = (
                not_a_directory_code
                if isinstance(exc, NotADirectoryError)
                else PARENT_UNAVAILABLE
            )
            raise PathContractError(code) from None
        if stat.S_ISLNK(info.st_mode):
            raise PathContractError(SYMLINK_PARENT)
        if not stat.S_ISDIR(info.st_mode):
            raise PathContractError(PARENT_NOT_DIRECTORY)
    return current, []
