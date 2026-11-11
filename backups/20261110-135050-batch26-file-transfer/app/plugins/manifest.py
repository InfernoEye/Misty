"""Isolated, declarative plugin manifest validation for Misty.

Security model
--------------
A plugin manifest is inert data. This module parses JSON/TOML with the standard
library, validates the resulting tree and returns plain JSON-safe dictionaries.
It never imports plugin modules, resolves entrypoints, executes code, starts a
process, uses the network or evaluates a schema. The only I/O is read-only:
opening a manifest file, checking entrypoint containment and listing one
directory level during discovery.

Validation is fail-closed and bounded. Every string, list, mapping and nesting
depth has a hard limit; reference cycles and non-finite numbers are rejected;
only exact JSON container/value types are traversed, so a hostile custom object
cannot get its methods called by the validator. Errors are deterministic
(sorted by field) and never contain raw exception text or filesystem paths.

Entrypoints are never run. A declared entrypoint must be a relative POSIX path
inside the plugin directory; path traversal, absolute paths, backslashes, NUL
bytes and symbolic-link components are rejected. A missing entrypoint file is
allowed at validation time so a manifest can be inspected before installation.
"""

from __future__ import annotations

import errno
import json
import math
import os
import posixpath
import re
import stat
import tomllib
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any


__all__ = [
    "ALLOWED_EFFECTS",
    "ALLOWED_PERMISSIONS",
    "MAX_MANIFEST_BYTES",
    "PluginManifest",
    "PluginTool",
    "discover_manifests",
    "load_manifest",
    "validate_manifest",
]


# Public limits. Callers may tighten the file limit, never widen it.
MAX_MANIFEST_BYTES = 256_000
HARD_MAX_MANIFEST_BYTES = 4_000_000

MAX_ID_LENGTH = 64
MAX_NAME_LENGTH = 120
MAX_VERSION_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 500
MAX_LICENSE_LENGTH = 64
MAX_ENTRYPOINT_LENGTH = 300

MAX_TOOLS = 50
MAX_TOOL_NAME_LENGTH = 64
MAX_TOOL_DESCRIPTION_LENGTH = 300
MAX_TOOL_FIELDS = 4
MAX_TOOL_KEY_SCAN = 12
MAX_TOP_LEVEL_FIELDS = 16

MAX_PERMISSIONS = 20
MAX_PERMISSION_LENGTH = 64

MAX_REQUIRES = 20
MAX_REQUIRES_RANGE_LENGTH = 64

MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_STRING = 2_000
MAX_SCHEMA_KEY = 128
MAX_SCHEMA_KEYS = 64
MAX_SCHEMA_ITEMS = 64
MAX_SCHEMA_NODES = 4_000
MAX_JSON_INTEGER = 2**53 - 1

MAX_ERRORS = 50
MAX_CORE_TOOL_NAMES = 1_000

MAX_PLUGINS_HARD = 200
MAX_DISCOVERY_ENTRIES = 2_000
MAX_PATH_LENGTH = 4_096

MAX_STRUCTURAL_DEPTH = 32
MAX_STRUCTURAL_NODES = MAX_SCHEMA_NODES + 2_000

ALLOWED_PERMISSIONS = frozenset({
    "read_files",
    "write_files",
    "clipboard",
    "network",
    "desktop",
    "accessibility",
    "memory",
})

ALLOWED_EFFECTS = frozenset({
    "read_only",
    "local_write",
    "launch",
    "window_control",
    "accessibility_action",
    "clipboard_read",
    "clipboard_write",
    "memory_write",
    "memory_delete",
    "file_delete",
    "software_install",
    "send_message",
    "upload_private_data",
    "purchase",
    "privileged_setting",
    "unknown_mutation",
})

_TOP_LEVEL_FIELDS = frozenset({
    "id",
    "name",
    "version",
    "description",
    "license",
    "entrypoint",
    "permissions",
    "tools",
    "requires",
    "min_misty_version",
})

_TOOL_FIELDS = frozenset({"name", "description", "parameters", "effect"})

_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z", re.ASCII)
_TOOL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z", re.ASCII)
_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]{0,8})\."
    r"(?:0|[1-9][0-9]{0,8})\."
    r"(?:0|[1-9][0-9]{0,8})\Z",
    re.ASCII,
)
_VERSION_RANGE_RE = re.compile(r"[0-9A-Za-z.*+<>=~^| ,-]{1,64}\Z", re.ASCII)

_SCAN_FAILED = object()


class _DuplicateKeyError(ValueError):
    """Raised internally when JSON contains duplicate object keys."""


class _NonFiniteNumberError(ValueError):
    """Raised internally when JSON contains NaN or Infinity literals."""


def _has_control_or_surrogate(value: str) -> bool:
    for character in value:
        code = ord(character)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return True
        if 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _has_nul_or_surrogate(value: str) -> bool:
    for character in value:
        code = ord(character)
        if code == 0 or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def _safe_text(value: str, limit: int) -> str:
    """Return bounded printable text for a result field or message."""
    text = value[:limit]
    cleaned = []
    for character in text:
        code = ord(character)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            cleaned.append("?")
        elif 0xD800 <= code <= 0xDFFF:
            cleaned.append("?")
        else:
            cleaned.append(character)
    return "".join(cleaned) or "?"


def _safe_label(value: str, limit: int = 48) -> str:
    """Bound and sanitise an untrusted manifest key or directory name."""
    if type(value) is not str:
        return "<?>"
    return _safe_text(value[:limit], limit)


class _Errors:
    """Bounded, deterministic validation error collection."""

    __slots__ = ("items",)

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, field: str, message: str) -> None:
        self.items.append({
            "field": _safe_text(field, 200),
            "message": _safe_text(message, 300),
        })

    def finalize(self) -> tuple[list[dict[str, str]], int]:
        unique: dict[tuple[str, str], dict[str, str]] = {}
        for item in self.items:
            key = (item["field"], item["message"])
            if key not in unique:
                unique[key] = item
        ordered = [unique[key] for key in sorted(unique)]
        return ordered[:MAX_ERRORS], len(ordered)


def _failure(
    field: str,
    message: str,
    *,
    errors: list[dict[str, str]] | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    if errors is None:
        errors = [{
            "field": _safe_text(field, 200),
            "message": _safe_text(message, 300),
        }]
        total = len(errors)
    display_message = message
    if total is not None and total > len(errors):
        display_message = (
            f"{message} Showing the first {len(errors)} of {total} error(s)."
        )
    return {
        "success": False,
        "manifest": None,
        "errors": errors,
        "message": _safe_text(display_message, 300),
    }


def _validation_failure(
    errors: _Errors,
    message: str = "Manifest validation failed",
) -> dict[str, Any]:
    ordered, total = errors.finalize()
    return _failure(
        "$",
        f"{message} with {total} error(s).",
        errors=ordered,
        total=total,
    )


def _clone_json(value: Any) -> Any:
    """Copy an already validated JSON value.

    Only exact JSON types are copied. The source is produced by this module's
    own scanner, so it cannot contain cycles or non-finite numbers.
    """
    if type(value) is dict:
        return {key: _clone_json(item) for key, item in value.items()}
    if type(value) is list:
        return [_clone_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PluginTool:
    """One declarative tool declaration inside a plugin manifest."""

    name: str
    description: str
    parameters: dict[str, Any]
    effect: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _clone_json(self.parameters),
            "effect": self.effect,
        }


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """A fully validated, inert plugin manifest value object.

    Instances returned by :func:`validate_manifest`/:func:`load_manifest` are
    valid. Direct construction is a trusted-code operation and does not
    re-validate its arguments; callers should prefer the validation API for
    untrusted data.
    """

    id: str
    name: str
    version: str
    description: str = ""
    license: str = ""
    entrypoint: str = ""
    permissions: tuple[str, ...] = ()
    tools: tuple[PluginTool, ...] = ()
    requires: dict[str, str] = dataclass_field(default_factory=dict)
    min_misty_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-safe dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "license": self.license,
            "entrypoint": self.entrypoint,
            "permissions": list(self.permissions),
            "tools": [tool.to_dict() for tool in self.tools],
            "requires": {
                key: self.requires[key] for key in sorted(self.requires)
            },
            "min_misty_version": self.min_misty_version,
        }


def _string_field(
    data: dict[str, Any],
    key: str,
    errors: _Errors,
    *,
    maximum: int,
    required: bool,
    allow_empty: bool = False,
) -> str | None:
    if key not in data:
        if required:
            errors.add(key, f"{key} is required.")
        return "" if not required else None
    value = data[key]
    if type(value) is not str:
        errors.add(key, f"{key} must be a string.")
        return None
    if len(value) > maximum:
        errors.add(key, f"{key} must be at most {maximum} characters.")
        return None
    if not allow_empty and not value.strip():
        errors.add(key, f"{key} must not be empty.")
        return None
    if _has_control_or_surrogate(value):
        errors.add(key, f"{key} must not contain control characters.")
        return None
    return value


def _validated_version(
    value: str,
    field_name: str,
    errors: _Errors,
) -> str | None:
    if type(value) is not str:
        errors.add(field_name, f"{field_name} must be a string.")
        return None
    if len(value) > MAX_VERSION_LENGTH:
        errors.add(
            field_name,
            f"{field_name} must be at most {MAX_VERSION_LENGTH} characters.",
        )
        return None
    if not _VERSION_RE.fullmatch(value):
        errors.add(
            field_name,
            f"{field_name} must be a MAJOR.MINOR.PATCH version "
            "(no leading zeros).",
        )
        return None
    return value


def _validate_entrypoint(data: dict[str, Any], errors: _Errors) -> str:
    if "entrypoint" not in data:
        return ""
    value = data["entrypoint"]
    if type(value) is not str:
        errors.add("entrypoint", "entrypoint must be a string.")
        return ""
    if len(value) > MAX_ENTRYPOINT_LENGTH:
        errors.add(
            "entrypoint",
            f"entrypoint must be at most {MAX_ENTRYPOINT_LENGTH} characters.",
        )
        return ""
    if not value:
        errors.add("entrypoint", "entrypoint must not be empty.")
        return ""
    if _has_control_or_surrogate(value):
        errors.add(
            "entrypoint",
            "entrypoint must not contain control characters.",
        )
        return ""
    if "\\" in value:
        errors.add("entrypoint", "entrypoint must use '/' separators.")
        return ""
    if value.startswith("/"):
        errors.add("entrypoint", "entrypoint must be a relative path.")
        return ""
    if (
        len(value) >= 2
        and value[1] == ":"
        and value[0].isascii()
        and value[0].isalpha()
    ):
        errors.add("entrypoint", "entrypoint must not use a drive prefix.")
        return ""
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        errors.add(
            "entrypoint",
            "entrypoint must not contain empty, '.' or '..' components.",
        )
        return ""
    if parts[0].startswith("~"):
        errors.add(
            "entrypoint",
            "entrypoint must not start with '~'.",
        )
        return ""
    try:
        if posixpath.normpath(value) != value:
            errors.add(
                "entrypoint",
                "entrypoint must be a normalised relative path.",
            )
            return ""
    except (ValueError, OSError):
        errors.add("entrypoint", "entrypoint is not a valid path.")
        return ""
    return value


def _validate_permissions(data: dict[str, Any], errors: _Errors) -> tuple[str, ...]:
    if "permissions" not in data:
        return ()
    raw = data["permissions"]
    if type(raw) is not list:
        errors.add("permissions", "permissions must be a list of strings.")
        return ()
    if len(raw) > MAX_PERMISSIONS:
        errors.add(
            "permissions",
            f"permissions must contain at most {MAX_PERMISSIONS} entries.",
        )
    seen: set[str] = set()
    permissions: list[str] = []
    for index, value in enumerate(raw[:MAX_PERMISSIONS]):
        field_name = f"permissions[{index}]"
        if type(value) is not str:
            errors.add(field_name, "permission must be a string.")
            continue
        if len(value) > MAX_PERMISSION_LENGTH:
            errors.add(field_name, "permission is too long.")
            continue
        if value not in ALLOWED_PERMISSIONS:
            errors.add(field_name, "permission is not in the allowlist.")
            continue
        if value in seen:
            errors.add(field_name, "permission must not be repeated.")
            continue
        seen.add(value)
        permissions.append(value)
    return tuple(permissions)


class _ScanBudget:
    """Shared node budget for JSON-schema-like parameter trees."""

    __slots__ = ("remaining", "reported")

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        self.reported = False


def _report_scan_budget(
    budget: _ScanBudget,
    errors: _Errors,
    path: str,
) -> None:
    if not budget.reported:
        errors.add(path, "parameters exceed the overall size limit.")
        budget.reported = True


def _scan_json(
    value: Any,
    path: str,
    depth: int,
    active: set[int],
    budget: _ScanBudget,
    errors: _Errors,
) -> Any:
    if budget.remaining <= 0:
        _report_scan_budget(budget, errors, path)
        return _SCAN_FAILED
    budget.remaining -= 1

    value_type = type(value)
    if value_type is dict or value_type is list:
        if depth > MAX_SCHEMA_DEPTH:
            errors.add(
                path,
                f"parameters must not be nested deeper than "
                f"{MAX_SCHEMA_DEPTH} levels.",
            )
            return _SCAN_FAILED
        identity = id(value)
        if identity in active:
            errors.add(path, "parameters must not contain reference cycles.")
            return _SCAN_FAILED
        active.add(identity)
        try:
            if value_type is dict:
                if len(value) > MAX_SCHEMA_KEYS:
                    errors.add(
                        path,
                        f"objects must contain at most {MAX_SCHEMA_KEYS} keys.",
                    )
                    return _SCAN_FAILED
                result: dict[str, Any] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        errors.add(path, "object keys must be strings.")
                        continue
                    if len(key) > MAX_SCHEMA_KEY:
                        errors.add(
                            path,
                            f"object keys must be at most "
                            f"{MAX_SCHEMA_KEY} characters.",
                        )
                        continue
                    if _has_nul_or_surrogate(key):
                        errors.add(
                            path,
                            "object keys must not contain NUL or surrogates.",
                        )
                        continue
                    child = _scan_json(
                        item,
                        f"{path}.{_safe_label(key, 40)}",
                        depth + 1,
                        active,
                        budget,
                        errors,
                    )
                    if budget.remaining <= 0:
                        _report_scan_budget(budget, errors, path)
                        return _SCAN_FAILED
                    if child is not _SCAN_FAILED:
                        result[key] = child
                return result
            if len(value) > MAX_SCHEMA_ITEMS:
                errors.add(
                    path,
                    f"arrays must contain at most {MAX_SCHEMA_ITEMS} items.",
                )
                return _SCAN_FAILED
            items: list[Any] = []
            for index, item in enumerate(value):
                child = _scan_json(
                    item,
                    f"{path}[{index}]",
                    depth + 1,
                    active,
                    budget,
                    errors,
                )
                if budget.remaining <= 0:
                    _report_scan_budget(budget, errors, path)
                    return _SCAN_FAILED
                if child is not _SCAN_FAILED:
                    items.append(child)
            return items
        finally:
            active.remove(identity)

    if value_type is str:
        if len(value) > MAX_SCHEMA_STRING:
            errors.add(
                path,
                f"strings must be at most {MAX_SCHEMA_STRING} characters.",
            )
            return _SCAN_FAILED
        if _has_nul_or_surrogate(value):
            errors.add(
                path,
                "strings must not contain NUL or surrogates.",
            )
            return _SCAN_FAILED
        return value
    if value_type is bool:
        return value
    if value_type is int:
        if abs(value) > MAX_JSON_INTEGER:
            errors.add(path, "integers must be within the JSON safe range.")
            return _SCAN_FAILED
        return value
    if value_type is float:
        if not math.isfinite(value):
            errors.add(path, "numbers must not be NaN or Infinity.")
            return _SCAN_FAILED
        return value
    if value is None:
        return None
    errors.add(
        path,
        "value must be JSON-safe (object, array, string, number, boolean "
        "or null).",
    )
    return _SCAN_FAILED


def _validate_parameters(
    data: dict[str, Any],
    field_path: str,
    errors: _Errors,
    budget: _ScanBudget,
) -> dict[str, Any] | None:
    if "parameters" not in data:
        errors.add(field_path, "parameters is required.")
        return None
    raw = data["parameters"]
    if type(raw) is not dict:
        errors.add(field_path, "parameters must be a JSON object.")
        return None
    errors_before = len(errors.items)
    scanned = _scan_json(raw, field_path, 1, set(), budget, errors)
    if scanned is _SCAN_FAILED or type(scanned) is not dict:
        if len(errors.items) == errors_before:
            # A budget/abort path must never fail open without an error.
            errors.add(field_path, "parameters could not be validated safely.")
        return None
    return scanned


def _validate_tools(
    data: dict[str, Any],
    errors: _Errors,
    core_tool_names: frozenset[str],
) -> tuple[PluginTool, ...]:
    if "tools" not in data:
        return ()
    raw = data["tools"]
    if type(raw) is not list:
        errors.add("tools", "tools must be a list.")
        return ()
    if len(raw) > MAX_TOOLS:
        errors.add(
            "tools",
            f"tools must contain at most {MAX_TOOLS} entries.",
        )
    seen: set[str] = set()
    tools: list[PluginTool] = []
    budget = _ScanBudget(MAX_SCHEMA_NODES)
    for index, item in enumerate(raw[:MAX_TOOLS]):
        prefix = f"tools[{index}]"
        if type(item) is not dict:
            errors.add(prefix, "each tool must be a JSON object.")
            continue
        if len(item) > MAX_TOOL_FIELDS:
            errors.add(
                prefix,
                f"a tool must contain at most {MAX_TOOL_FIELDS} fields.",
            )
        # Build a sanitised, exact-string-key view. This both bounds the key
        # scan and guarantees later fixed-key lookups cannot invoke methods on
        # a hostile key object stored in the incoming mapping.
        safe_item: dict[str, Any] = {}
        for key_index, (key, value) in enumerate(item.items()):
            if key_index >= MAX_TOOL_KEY_SCAN:
                break
            if type(key) is not str:
                errors.add(prefix, "tool field names must be strings.")
                continue
            if len(key) > 64:
                errors.add(prefix, "tool field name is too long.")
                continue
            if key not in _TOOL_FIELDS:
                errors.add(
                    f"{prefix}.{_safe_label(key, 40)}",
                    f"unknown tool field: {_safe_label(key, 40)}.",
                )
                continue
            safe_item[key] = value

        name = _string_field(
            safe_item,
            "name",
            _ToolErrors(errors, prefix),
            maximum=MAX_TOOL_NAME_LENGTH,
            required=True,
        )
        if name is not None:
            if len(name) > MAX_TOOL_NAME_LENGTH:
                name = None
            elif not _TOOL_NAME_RE.fullmatch(name):
                errors.add(
                    f"{prefix}.name",
                    "tool name must be 1-64 lowercase letters, digits, "
                    "'_', '.', or '-' and start with a letter or digit.",
                )
                name = None
        if name is not None:
            if name in seen:
                errors.add(
                    f"{prefix}.name",
                    "tool names must be unique within a manifest.",
                )
            else:
                seen.add(name)
                if name in core_tool_names:
                    errors.add(
                        f"{prefix}.name",
                        "tool name collides with a reserved core tool.",
                    )

        description = _string_field(
            safe_item,
            "description",
            _ToolErrors(errors, prefix),
            maximum=MAX_TOOL_DESCRIPTION_LENGTH,
            required=True,
        )
        parameters = _validate_parameters(
            safe_item,
            f"{prefix}.parameters",
            errors,
            budget,
        )
        effect: str | None = None
        if "effect" not in safe_item:
            errors.add(f"{prefix}.effect", "effect is required.")
        elif type(safe_item["effect"]) is not str:
            errors.add(f"{prefix}.effect", "effect must be a string.")
        elif safe_item["effect"] not in ALLOWED_EFFECTS:
            errors.add(
                f"{prefix}.effect",
                "effect is not in the allowlist.",
            )
        else:
            effect = safe_item["effect"]

        if (
            name is not None
            and description is not None
            and parameters is not None
            and effect is not None
        ):
            tools.append(
                PluginTool(
                    name=name,
                    description=description,
                    parameters=parameters,
                    effect=effect,
                )
            )
    return tuple(tools)


class _ToolErrors:
    """Route tool sub-field errors under a bounded ``tools[i]`` prefix.

    This tiny adapter keeps ``_string_field`` reusable without changing the
    stable field names produced for top-level fields.
    """

    __slots__ = ("_errors", "_prefix")

    def __init__(self, errors: _Errors, prefix: str) -> None:
        self._errors = errors
        self._prefix = prefix

    def add(self, key: str, message: str) -> None:
        self._errors.add(f"{self._prefix}.{key}", message)


def _validate_requires(data: dict[str, Any], errors: _Errors) -> dict[str, str]:
    if "requires" not in data:
        return {}
    raw = data["requires"]
    if type(raw) is not dict:
        errors.add("requires", "requires must be a JSON object.")
        return {}
    if len(raw) > MAX_REQUIRES:
        errors.add(
            "requires",
            f"requires must contain at most {MAX_REQUIRES} entries.",
        )
    requires: dict[str, str] = {}
    for index, (key, value) in enumerate(raw.items()):
        if index >= MAX_REQUIRES:
            break
        if type(key) is not str:
            errors.add("requires", "dependency ids must be strings.")
            continue
        if len(key) > MAX_ID_LENGTH or not _ID_RE.fullmatch(key):
            errors.add(
                f"requires.{_safe_label(key, 40)}",
                "dependency id must be a safe plugin id.",
            )
            continue
        field_name = f"requires.{_safe_label(key, 40)}"
        if type(value) is not str:
            errors.add(field_name, "dependency range must be a string.")
            continue
        if not value.strip():
            errors.add(field_name, "dependency range must not be empty.")
            continue
        if len(value) > MAX_REQUIRES_RANGE_LENGTH:
            errors.add(
                field_name,
                "dependency range is too long.",
            )
            continue
        if _has_control_or_surrogate(value):
            errors.add(
                field_name,
                "dependency range must not contain control characters.",
            )
            continue
        if not _VERSION_RANGE_RE.fullmatch(value):
            errors.add(
                field_name,
                "dependency range contains unsupported characters.",
            )
            continue
        requires[key] = value
    return requires


def _normalize_core_tool_names(core_tool_names: Any) -> frozenset[str]:
    if core_tool_names is None or type(core_tool_names) is str:
        return frozenset()
    names: set[str] = set()
    try:
        iterator = iter(core_tool_names)
    except TypeError:
        return frozenset()
    for _ in range(MAX_CORE_TOOL_NAMES):
        try:
            item = next(iterator)
        except StopIteration:
            break
        except Exception:
            break
        if type(item) is str and 0 < len(item) <= 128:
            names.add(item)
    return frozenset(names)


def _structural_problem(value: Any) -> str | None:
    """Detect cycles, excessive depth/size before field-level validation."""
    active: set[int] = set()
    budget = [MAX_STRUCTURAL_NODES]

    def walk(item: Any, depth: int) -> str | None:
        item_type = type(item)
        if item_type is not dict and item_type is not list:
            return None
        if depth > MAX_STRUCTURAL_DEPTH:
            return "Manifest data is nested too deeply."
        identity = id(item)
        if identity in active:
            return "Manifest data must not contain reference cycles."
        if budget[0] <= 0:
            return "Manifest data is too large to validate."
        budget[0] -= 1
        active.add(identity)
        try:
            children = item.values() if item_type is dict else item
            for child in children:
                problem = walk(child, depth + 1)
                if problem is not None:
                    return problem
        finally:
            active.remove(identity)
        return None

    return walk(value, 1)


def validate_manifest(
    data: Any,
    *,
    core_tool_names: Any = None,
) -> dict[str, Any]:
    """Validate an in-memory manifest mapping.

    Returns ``{"success", "manifest", "errors", "message"}``. The input is
    never mutated. Only exact JSON types are inspected; a mapping subclass or
    other custom object is rejected rather than having its methods invoked.
    """
    errors = _Errors()

    if type(data) is not dict:
        errors.add("$", "manifest must be a JSON object.")
        return _validation_failure(errors, "Manifest validation failed")

    problem = _structural_problem(data)
    if problem is not None:
        errors.add("$", problem)
        return _validation_failure(errors)

    if len(data) > MAX_TOP_LEVEL_FIELDS:
        errors.add(
            "$",
            f"manifest must contain at most {MAX_TOP_LEVEL_FIELDS} fields.",
        )
        return _validation_failure(errors)

    # Build a sanitised view with exact-string keys only. The original mapping
    # is never queried by field name, so a hostile key object cannot have its
    # __hash__/__eq__ invoked by a fixed-key lookup.
    safe_data: dict[str, Any] = {}
    for key, value in data.items():
        if type(key) is not str:
            errors.add("$", "manifest field names must be strings.")
            continue
        if len(key) > 64:
            errors.add("$", "manifest field name is too long.")
            continue
        if key not in _TOP_LEVEL_FIELDS:
            errors.add(
                _safe_label(key, 40),
                f"unknown manifest field: {_safe_label(key, 40)}.",
            )
            continue
        safe_data[key] = value

    manifest_id = _string_field(
        safe_data,
        "id",
        errors,
        maximum=MAX_ID_LENGTH,
        required=True,
    )
    if manifest_id is not None and not _ID_RE.fullmatch(manifest_id):
        errors.add(
            "id",
            "id must be 1-64 lowercase letters, digits, '_', '.', or '-' "
            "and start with a letter or digit.",
        )
        manifest_id = None

    name = _string_field(
        safe_data,
        "name",
        errors,
        maximum=MAX_NAME_LENGTH,
        required=True,
    )

    version: str | None = None
    if "version" not in safe_data:
        errors.add("version", "version is required.")
    else:
        version = _validated_version(
            safe_data["version"],
            "version",
            errors,
        )

    description = _string_field(
        safe_data,
        "description",
        errors,
        maximum=MAX_DESCRIPTION_LENGTH,
        required=False,
        allow_empty=True,
    )
    license_text = _string_field(
        safe_data,
        "license",
        errors,
        maximum=MAX_LICENSE_LENGTH,
        required=False,
        allow_empty=True,
    )
    entrypoint = _validate_entrypoint(safe_data, errors)
    permissions = _validate_permissions(safe_data, errors)
    core_tools = _normalize_core_tool_names(core_tool_names)
    tools = _validate_tools(safe_data, errors, core_tools)
    requires = _validate_requires(safe_data, errors)

    min_misty_version = ""
    if "min_misty_version" in safe_data:
        parsed = _validated_version(
            safe_data["min_misty_version"],
            "min_misty_version",
            errors,
        )
        if parsed is not None:
            min_misty_version = parsed

    ordered, total = errors.finalize()
    if total:
        return _failure(
            "$",
            f"Manifest validation failed with {total} error(s).",
            errors=ordered,
            total=total,
        )

    if (
        manifest_id is None
        or name is None
        or version is None
        or description is None
        or license_text is None
    ):
        # Defensive: a field failed without registering an error. Fail closed.
        errors.add("$", "manifest could not be fully validated.")
        ordered, total = errors.finalize()
        return _failure(
            "$",
            f"Manifest validation failed with {total} error(s).",
            errors=ordered,
            total=total,
        )

    manifest = PluginManifest(
        id=manifest_id,
        name=name,
        version=version,
        description=description,
        license=license_text,
        entrypoint=entrypoint,
        permissions=permissions,
        tools=tools,
        requires=requires,
        min_misty_version=min_misty_version,
    )
    return {
        "success": True,
        "manifest": manifest.to_dict(),
        "errors": [],
        "message": "Manifest is valid.",
    }


def _coerce_max_bytes(max_bytes: Any) -> int | None:
    if type(max_bytes) is not int or max_bytes <= 0:
        return None
    return min(max_bytes, HARD_MAX_MANIFEST_BYTES)


def _coerce_path(path: Any) -> str | None:
    try:
        value = os.fspath(path)
    except (TypeError, ValueError):
        return None
    if type(value) is not str or not value or len(value) > MAX_PATH_LENGTH:
        return None
    if _has_nul_or_surrogate(value):
        return None
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError()
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise _NonFiniteNumberError()


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _load_file_bytes(path: str, limit: int) -> tuple[bytes | None, str]:
    """Read at most ``limit + 1`` bytes from a regular, non-symlink file.

    Returns ``(data, error_message)``. The error message is static and never
    contains the path or raw exception text.
    """
    if os.path.islink(path):
        return None, "Manifest path must not be a symbolic link."

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return None, "Manifest file does not exist."
        if error.errno == errno.ELOOP:
            return None, "Manifest path must not be a symbolic link."
        if error.errno in (errno.EACCES, errno.EPERM):
            return None, "Manifest file is not readable."
        return None, "Manifest file could not be opened read-only."

    raw = b""
    try:
        try:
            metadata = os.fstat(fd)
        except OSError:
            return None, "Manifest file metadata could not be read."
        if not stat.S_ISREG(metadata.st_mode):
            return None, "Manifest path must reference a regular file."
        if metadata.st_size > limit:
            return None, "Manifest file exceeds the size limit."
        try:
            raw = _read_bounded(fd, limit)
        except OSError:
            return None, "Manifest file could not be read."
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    if len(raw) > limit:
        return None, "Manifest file exceeds the size limit."
    return raw, ""


def _entrypoint_error(plugin_dir: str, entrypoint: str) -> str | None:
    """Check entrypoint containment without reading or executing it."""
    try:
        base = os.path.realpath(plugin_dir)
    except (OSError, ValueError):
        return "Plugin directory could not be resolved safely."

    parts = entrypoint.split("/")
    current = base
    for part in parts:
        current = os.path.join(current, part)
        try:
            if os.path.islink(current):
                return "entrypoint must not traverse symbolic links."
        except OSError:
            return "entrypoint could not be checked safely."

    try:
        target = os.path.realpath(os.path.join(base, *parts))
        if os.path.commonpath([base, target]) != base:
            return "entrypoint must stay inside the plugin directory."
    except (OSError, ValueError):
        return "entrypoint must stay inside the plugin directory."

    try:
        if os.path.exists(target) and not os.path.isfile(target):
            return "entrypoint must reference a regular file."
    except OSError:
        return "entrypoint could not be checked safely."
    return None


def load_manifest(
    path: Any,
    *,
    max_bytes: Any = MAX_MANIFEST_BYTES,
    core_tool_names: Any = None,
) -> dict[str, Any]:
    """Load and validate one ``.json`` or ``.toml`` manifest file.

    The file is opened read-only with ``O_NOFOLLOW``; non-regular files,
    symlinks and files above ``max_bytes`` are rejected. Parsing is delegated
    to :mod:`json`/:mod:`tomllib` and the result is validated in memory.
    Entrypoint containment is checked, but the entrypoint is never opened,
    imported or executed.
    """
    limit = _coerce_max_bytes(max_bytes)
    if limit is None:
        return _failure(
            "max_bytes",
            "max_bytes must be a positive integer.",
        )

    file_path = _coerce_path(path)
    if file_path is None:
        return _failure("path", "Manifest path must be a valid text path.")

    suffix = os.path.splitext(file_path)[1].lower()
    if suffix not in (".json", ".toml"):
        return _failure(
            "path",
            "Manifest file must use a .json or .toml extension.",
        )

    raw, read_error = _load_file_bytes(file_path, limit)
    if raw is None:
        return _failure("path", read_error)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _failure("$", "Manifest file must be valid UTF-8 text.")

    if suffix == ".json":
        try:
            data = json.loads(
                text,
                object_pairs_hook=_json_object,
                parse_constant=_reject_constant,
            )
        except _DuplicateKeyError:
            return _failure(
                "$",
                "Manifest JSON must not contain duplicate object keys.",
            )
        except _NonFiniteNumberError:
            return _failure(
                "$",
                "Manifest JSON must not contain NaN or Infinity.",
            )
        except RecursionError:
            return _failure("$", "Manifest JSON is nested too deeply.")
        except MemoryError:
            return _failure("$", "Manifest JSON is too large to parse.")
        except ValueError:
            return _failure("$", "Manifest JSON is malformed.")
    else:
        try:
            data = tomllib.loads(text)
        except RecursionError:
            return _failure("$", "Manifest TOML is nested too deeply.")
        except MemoryError:
            return _failure("$", "Manifest TOML is too large to parse.")
        except ValueError:
            return _failure("$", "Manifest TOML is malformed.")

    result = validate_manifest(data, core_tool_names=core_tool_names)
    if not result["success"]:
        return result

    entrypoint = result["manifest"]["entrypoint"]
    if entrypoint:
        plugin_dir = os.path.dirname(os.path.abspath(file_path))
        problem = _entrypoint_error(plugin_dir, entrypoint)
        if problem is not None:
            return _failure("entrypoint", problem)
    return result


def _manifest_kind(path: str) -> str | None:
    """Classify a candidate manifest path without following symlinks."""
    try:
        metadata = os.lstat(path)
    except (OSError, ValueError):
        return None
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return None


def _discovery_failure(message: str) -> dict[str, Any]:
    return {
        "success": False,
        "manifests": [],
        "errors": [],
        "scanned": 0,
        "skipped_symlinks": 0,
        "truncated": False,
        "message": _safe_text(message, 300),
    }


def discover_manifests(
    directory: Any,
    *,
    max_plugins: Any = 50,
    core_tool_names: Any = None,
) -> dict[str, Any]:
    """Scan immediate child directories for ``plugin.json``/``plugin.toml``.

    Symlinked directories and symlinked manifest files are skipped. The number
    of examined directory entries, the number of plugins, and each manifest's
    size are bounded. Nothing is imported or executed. Invalid manifests are
    reported per plugin with sanitised labels; no filesystem paths are leaked.
    """
    if type(max_plugins) is not int or max_plugins <= 0:
        return _discovery_failure("max_plugins must be a positive integer.")
    plugin_limit = min(max_plugins, MAX_PLUGINS_HARD)

    base_dir = _coerce_path(directory)
    if base_dir is None:
        return _discovery_failure("Plugin directory must be a valid text path.")
    if os.path.islink(base_dir):
        return _discovery_failure("Plugin directory must not be a symbolic link.")

    candidates: list[str] = []
    skipped_symlinks = 0
    entries_seen = 0
    truncated = False
    try:
        if not os.path.isdir(base_dir):
            return _discovery_failure("Plugin directory is not a directory.")
        with os.scandir(base_dir) as iterator:
            for entry in iterator:
                entries_seen += 1
                if entries_seen > MAX_DISCOVERY_ENTRIES:
                    truncated = True
                    break
                try:
                    if entry.is_symlink():
                        skipped_symlinks += 1
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                candidates.append(entry.name)
    except OSError:
        return _discovery_failure("Plugin directory could not be read.")

    candidates.sort()
    manifests: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    scanned = 0

    for plugin_name in candidates:
        plugin_dir = os.path.join(base_dir, plugin_name)
        json_path = os.path.join(plugin_dir, "plugin.json")
        toml_path = os.path.join(plugin_dir, "plugin.toml")
        json_kind = _manifest_kind(json_path)
        toml_kind = _manifest_kind(toml_path)

        if json_kind == "symlink":
            skipped_symlinks += 1
            json_kind = None
        if toml_kind == "symlink":
            skipped_symlinks += 1
            toml_kind = None

        if json_kind is None and toml_kind is None:
            continue
        if scanned >= plugin_limit:
            truncated = True
            break
        scanned += 1

        if json_kind == "file" and toml_kind == "file":
            message = (
                "Plugin directory contains both plugin.json and plugin.toml."
            )
            errors.append({
                "plugin": _safe_label(plugin_name, 120),
                "errors": [{"field": "path", "message": message}],
                "message": message,
            })
            continue

        manifest_path = json_path if json_kind == "file" else toml_path
        result = load_manifest(
            manifest_path,
            core_tool_names=core_tool_names,
        )
        if result["success"]:
            manifests.append(result["manifest"])
        else:
            errors.append({
                "plugin": _safe_label(plugin_name, 120),
                "errors": result["errors"],
                "message": result["message"],
            })

    message = (
        f"Discovered {len(manifests)} valid manifest(s); "
        f"{len(errors)} plugin(s) rejected."
    )
    if truncated:
        message += " The scan was bounded by configured limits."

    return {
        "success": True,
        "manifests": manifests,
        "errors": errors,
        "scanned": scanned,
        "skipped_symlinks": skipped_symlinks,
        "truncated": truncated,
        "message": _safe_text(message, 300),
    }
