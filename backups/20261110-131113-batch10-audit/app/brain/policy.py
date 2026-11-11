"""Central tool permission and effect policy for Misty.

This module is an isolated, dependency-free description of *what a tool can
do* and *how much approval it needs before it may run*.  It deliberately does
not import GUI, model, desktop, memory or registry code, performs no I/O, keeps
no state outside its own objects, and never executes or inspects tool
arguments.  It exists so that ``tools.py``, ``agent.py`` and the confirmation
UI can share one policy source of truth later.

Design rules
------------
* Fail closed.  A tool that is not registered is treated as an unknown
  mutation: effect ``unknown_mutation``, risk ``high`` and confirmation
  required.
* Arguments are data, never instructions.  ``evaluate`` and
  ``describe_for_confirmation`` accept an ``arguments`` value only so the
  signature is stable for later callers; it is never read, formatted, hashed,
  logged or echoed.  Confirmation text comes from static per-tool
  descriptions, so private clipboard text, document paths, window titles and
  arbitrary argument strings cannot reach a prompt or a log line.
* Deterministic and side-effect free.  ``evaluate`` is a dictionary lookup
  plus constant text.
* Entries are immutable value objects, reads are lock-free, and
  ``register`` is serialised by a lock.

Risk levels
-----------
``none`` < ``low`` < ``medium`` < ``high`` < ``critical``.

A mutating effect always has risk ``low`` or higher.  Risk ``high`` and
``critical`` always require confirmation, and ``unknown_mutation`` always
requires confirmation; entries that break these invariants are rejected.

Baseline registry (current Misty tools)
---------------------------------------
===============================  ====================  ========  ======
tool                             effect                risk      confirm
===============================  ====================  ========  ======
open_folder                      launch                low       no
open_app                         launch                low       no
open_browser                     launch                medium    no
volume_up                        local_write           low       no
volume_down                      local_write           low       no
volume_mute                      local_write           low       no
get_time                         read_only             none      no
get_system_status                read_only             low       no
list_files                       read_only             low       no
get_file_info                    read_only             low       no
search_files                     read_only             low       no
list_installed_apps              read_only             low       no
launch_app                       launch                medium    no
list_windows                     read_only             low       no
get_active_window                read_only             low       no
focus_window                     window_control        low       no
minimize_window                  window_control        low       no
maximize_window                  window_control        low       no
restore_window                   window_control        low       no
close_window                     window_control        high      yes
list_screens                     read_only             low       no
move_window_to_screen            window_control        low       no
remember                         memory_write          low       no
recall                           read_only (private)   medium    no
list_memories                    read_only (private)   medium    no
forget_memory                    memory_delete         high      yes
read_clipboard                   clipboard_read        medium    no
write_clipboard                  clipboard_write       medium    yes
inspect_screen                   read_only (private)   medium    no
accessibility_list_applications  read_only             low       no
accessibility_inspect_application read_only (private)  medium    no
read_document                    read_only (private)   medium    no
===============================  ====================  ========  ======

Notes on the baseline
~~~~~~~~~~~~~~~~~~~~~
* ``close_window``, ``forget_memory`` and ``write_clipboard`` keep the
  current behaviour from ``tools.SENSITIVE_TOOLS``: explicit approval is
  always required.
* Private reads (``read_clipboard``, ``inspect_screen``, ``read_document``,
  ``accessibility_inspect_application``) are read-only and do not require
  approval by default, but they carry risk ``medium`` and are flagged
  ``private=True`` so callers never log their results.  ``recall`` and
  ``list_memories`` are flagged the same way because they surface stored
  private memory.
* ``open_app`` stays ``low`` because it is restricted to an approved enum;
  ``launch_app`` and ``open_browser`` are ``medium`` because they can start
  arbitrary installed programs or a network-capable browser.
* Volume changes and window moves are real mutations; they are modelled as
  ``local_write`` / ``window_control`` at ``low`` risk with no approval,
  matching today's behaviour.
* Effects that no current tool uses yet (``file_delete``, ``software_install``,
  ``send_message``, ``upload_private_data``, ``purchase``,
  ``privileged_setting``) are declared here so future tools can be described
  without changing the vocabulary.
* Tools added to ``tools.py`` after this baseline was frozen (for example the
  later ``fetch_web_page`` network reader) are intentionally not registered
  here: a policy entry is a reviewed decision, not an automatic echo of the
  tool list.  Until someone registers them they fail closed as unknown
  mutations, so nothing is silently granted a lower bar than this table.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
import re
import threading


__all__ = [
    "BASELINE_SPECIFICATIONS",
    "BASELINE_TOOL_NAMES",
    "CONFIRMATION_RISK_LEVELS",
    "DEFAULT_POLICY",
    "EFFECT_NAMES",
    "EFFECTS",
    "EffectDefinition",
    "EffectSpec",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NOTE_LENGTH",
    "MAX_TOOL_NAME_LENGTH",
    "PolicyValidationError",
    "RISK_LEVELS",
    "UNKNOWN_ACTION_SUMMARY",
    "UNKNOWN_TOOL_POLICY",
    "ToolPolicy",
    "describe_for_confirmation",
    "evaluate",
    "policy_for",
    "requires_confirmation",
]


RISK_LEVELS = ("none", "low", "medium", "high", "critical")

_RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}

CONFIRMATION_RISK_LEVELS = frozenset({"high", "critical"})

MAX_TOOL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 120
MAX_NOTE_LENGTH = 200

_UNKNOWN_TOOL_DISPLAY = "unknown"

_TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")

UNKNOWN_ACTION_SUMMARY = (
    "Unrecognized action (treated as potentially destructive)"
)


class PolicyValidationError(ValueError):
    """Raised when a policy entry contains an invalid or unsafe field."""


@dataclass(frozen=True, slots=True)
class EffectDefinition:
    """Static facts about one member of the effect vocabulary."""

    name: str
    label: str
    mutating: bool
    minimum_risk: str


EFFECTS = MappingProxyType({
    effect.name: effect
    for effect in (
        EffectDefinition("read_only", "Read-only observation", False, "none"),
        EffectDefinition("local_write", "Local state write", True, "low"),
        EffectDefinition("launch", "Application launch", True, "low"),
        EffectDefinition("window_control", "Window control", True, "low"),
        EffectDefinition(
            "accessibility_action", "Accessibility action", True, "medium"
        ),
        EffectDefinition("clipboard_read", "Clipboard read", False, "medium"),
        EffectDefinition("clipboard_write", "Clipboard write", True, "medium"),
        EffectDefinition("memory_write", "Memory write", True, "low"),
        EffectDefinition("memory_delete", "Memory delete", True, "high"),
        EffectDefinition("file_delete", "File delete", True, "high"),
        EffectDefinition("software_install", "Software install", True, "high"),
        EffectDefinition("send_message", "Message send", True, "medium"),
        EffectDefinition(
            "upload_private_data", "Private data upload", True, "high"
        ),
        EffectDefinition("purchase", "Purchase", True, "critical"),
        EffectDefinition(
            "privileged_setting", "Privileged setting change", True, "high"
        ),
        EffectDefinition("unknown_mutation", "Unknown mutation", True, "high"),
    )
})

EFFECT_NAMES = frozenset(EFFECTS)


def _require_text(field, value, max_length):
    if type(value) is not str:
        raise PolicyValidationError(f"{field} must be a plain string")
    if not value.strip():
        raise PolicyValidationError(f"{field} must not be empty")
    if value != value.strip():
        raise PolicyValidationError(
            f"{field} must not start or end with whitespace"
        )
    if len(value) > max_length:
        raise PolicyValidationError(
            f"{field} must be at most {max_length} characters"
        )
    for character in value:
        if character in "{}" or not character.isprintable():
            raise PolicyValidationError(
                f"{field} must be printable single-line text without braces"
            )


def _require_bool(field, value):
    if type(value) is not bool:
        raise PolicyValidationError(f"{field} must be a boolean")


def _is_valid_tool_name(value):
    return (
        type(value) is str
        and _TOOL_NAME_PATTERN.fullmatch(value) is not None
    )


def _validate_effect_spec(spec):
    effect = spec.effect
    risk = spec.risk

    if type(effect) is not str or effect not in EFFECTS:
        raise PolicyValidationError(
            "effect must be one of the known effect names"
        )
    if type(risk) is not str or risk not in _RISK_RANK:
        raise PolicyValidationError(
            "risk must be one of: " + ", ".join(RISK_LEVELS)
        )

    _require_bool("requires_confirmation", spec.requires_confirmation)
    _require_bool("arguments_affect_risk", spec.arguments_affect_risk)
    _require_bool("private", spec.private)
    _require_text("description", spec.description, MAX_DESCRIPTION_LENGTH)

    if type(spec.note) is not str:
        raise PolicyValidationError("note must be a plain string")
    if spec.note:
        _require_text("note", spec.note, MAX_NOTE_LENGTH)

    definition = EFFECTS[effect]

    if _RISK_RANK[risk] < _RISK_RANK[definition.minimum_risk]:
        raise PolicyValidationError(
            f"effect {effect} requires risk {definition.minimum_risk} "
            "or higher"
        )
    if definition.mutating and risk == "none":
        raise PolicyValidationError(
            f"mutating effect {effect} cannot have risk none"
        )
    if risk in CONFIRMATION_RISK_LEVELS and not spec.requires_confirmation:
        raise PolicyValidationError(
            f"risk {risk} always requires confirmation"
        )
    if effect == "unknown_mutation" and not spec.requires_confirmation:
        raise PolicyValidationError(
            "unknown_mutation always requires confirmation"
        )


@dataclass(frozen=True, slots=True)
class EffectSpec:
    """Immutable description of one tool's effect and approval requirement.

    ``description`` is static display text: it is shown to the user before a
    confirmed action and deliberately cannot contain argument placeholders.
    ``arguments_affect_risk`` records that different argument values can
    change the impact of a call (writing clipboard text versus reading it).
    It never lowers the recorded risk or the confirmation requirement;
    argument values are never inspected by this module.
    """

    effect: str
    risk: str
    requires_confirmation: bool
    description: str
    arguments_affect_risk: bool = False
    private: bool = False
    note: str = ""

    def __post_init__(self):
        _validate_effect_spec(self)

    @property
    def mutating(self):
        return EFFECTS[self.effect].mutating

    @classmethod
    def from_mapping(cls, mapping):
        """Build a validated spec from a mapping, rejecting extra fields."""
        if not isinstance(mapping, Mapping):
            raise PolicyValidationError("spec must be a mapping")
        keys = set(mapping.keys())
        allowed = {
            "effect",
            "risk",
            "requires_confirmation",
            "description",
            "arguments_affect_risk",
            "private",
            "note",
        }
        if keys - allowed:
            raise PolicyValidationError(
                "spec contains unsupported fields: "
                + ", ".join(sorted(str(key) for key in keys - allowed))
            )
        missing = {"effect", "risk", "requires_confirmation",
                   "description"} - keys
        if missing:
            raise PolicyValidationError(
                "spec is missing required fields: "
                + ", ".join(sorted(missing))
            )
        return cls(
            effect=mapping["effect"],
            risk=mapping["risk"],
            requires_confirmation=mapping["requires_confirmation"],
            description=mapping["description"],
            arguments_affect_risk=mapping.get("arguments_affect_risk", False),
            private=mapping.get("private", False),
            note=mapping.get("note", ""),
        )

    def to_dict(self):
        """Return a JSON-safe, argument-free representation of this spec."""
        return {
            "effect": self.effect,
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
            "description": self.description,
            "arguments_affect_risk": self.arguments_affect_risk,
            "private": self.private,
            "note": self.note,
            "mutating": self.mutating,
        }


UNKNOWN_TOOL_POLICY = EffectSpec(
    effect="unknown_mutation",
    risk="high",
    requires_confirmation=True,
    description=UNKNOWN_ACTION_SUMMARY,
    note="Fail-closed default applied to every tool without a policy entry.",
)


def _spec(
    effect,
    risk,
    description,
    *,
    confirm=False,
    arguments_affect_risk=False,
    private=False,
    note="",
):
    return EffectSpec(
        effect=effect,
        risk=risk,
        requires_confirmation=confirm,
        description=description,
        arguments_affect_risk=arguments_affect_risk,
        private=private,
        note=note,
    )


_BASELINE_SPECIFICATIONS = {
    # -- Launch and open -------------------------------------------------
    "open_folder": _spec(
        "launch", "low", "Open a known folder in the file manager",
        arguments_affect_risk=True,
        note="The chosen folder is restricted to the approved folder list.",
    ),
    "open_app": _spec(
        "launch", "low", "Launch an approved desktop application",
        arguments_affect_risk=True,
        note="The application is restricted to the approved application list.",
    ),
    "open_browser": _spec(
        "launch", "medium", "Open the web browser",
        note="A browser can reach the network; what it shows is untrusted.",
    ),
    "launch_app": _spec(
        "launch", "medium", "Launch an installed desktop application",
        arguments_affect_risk=True,
        note="Any installed desktop entry can be started in the background.",
    ),
    # -- System state ----------------------------------------------------
    "volume_up": _spec(
        "local_write", "low", "Increase system volume",
        note="Changes audio state only; no files are modified.",
    ),
    "volume_down": _spec(
        "local_write", "low", "Decrease system volume",
        note="Changes audio state only; no files are modified.",
    ),
    "volume_mute": _spec(
        "local_write", "low", "Mute system audio",
        note="Changes audio state only; no files are modified.",
    ),
    # -- Read-only inspection -------------------------------------------
    "get_time": _spec("read_only", "none", "Read the current local time"),
    "get_system_status": _spec(
        "read_only", "low", "Read system memory and CPU status",
    ),
    "list_files": _spec(
        "read_only", "low", "List entries in an approved folder",
        arguments_affect_risk=True,
        note="Reveals file and folder names inside the approved scope only.",
    ),
    "get_file_info": _spec(
        "read_only", "low", "Inspect file or folder metadata",
        arguments_affect_risk=True,
    ),
    "search_files": _spec(
        "read_only", "low", "Search for matching file and folder names",
        arguments_affect_risk=True,
        note="Searches names only and never reads file contents.",
    ),
    "list_installed_apps": _spec(
        "read_only", "low", "List installed desktop applications",
        arguments_affect_risk=True,
    ),
    # -- Window and display inspection -----------------------------------
    "list_windows": _spec(
        "read_only", "low", "List open application windows",
        note="Window titles and application IDs are returned.",
    ),
    "get_active_window": _spec(
        "read_only", "low", "Read the active window details",
    ),
    "list_screens": _spec("read_only", "low", "List connected displays"),
    # -- Window control ---------------------------------------------------
    "focus_window": _spec(
        "window_control", "low", "Bring a window to the foreground",
        arguments_affect_risk=True,
    ),
    "minimize_window": _spec(
        "window_control", "low", "Minimize a window",
        arguments_affect_risk=True,
    ),
    "maximize_window": _spec(
        "window_control", "low", "Maximize a window",
        arguments_affect_risk=True,
    ),
    "restore_window": _spec(
        "window_control", "low", "Restore a window to its normal size",
        arguments_affect_risk=True,
    ),
    "close_window": _spec(
        "window_control", "high", "Close window",
        confirm=True,
        arguments_affect_risk=True,
        note="Closing a window may discard unsaved work.",
    ),
    "move_window_to_screen": _spec(
        "window_control", "low", "Move a window to another display",
        arguments_affect_risk=True,
    ),
    # -- Memory -----------------------------------------------------------
    "remember": _spec(
        "memory_write", "low", "Save a memory entry",
        arguments_affect_risk=True,
        note="Only saved when the user explicitly asks to remember something.",
    ),
    "recall": _spec(
        "read_only", "medium", "Search saved memories",
        arguments_affect_risk=True,
        private=True,
        note="Reveals stored private memory entries for the given query.",
    ),
    "list_memories": _spec(
        "read_only", "medium", "List saved memories",
        private=True,
        note="Reveals stored private memory entries.",
    ),
    "forget_memory": _spec(
        "memory_delete", "high", "Delete a saved memory",
        confirm=True,
        arguments_affect_risk=True,
        note="Deleting a stored memory cannot be undone.",
    ),
    # -- Clipboard ---------------------------------------------------------
    "read_clipboard": _spec(
        "clipboard_read", "medium", "Read clipboard text",
        private=True,
        note=(
            "Returns private clipboard text; never monitor it automatically "
            "and never log the result."
        ),
    ),
    "write_clipboard": _spec(
        "clipboard_write", "medium", "Replace clipboard contents",
        confirm=True,
        arguments_affect_risk=True,
        private=True,
        note=(
            "Overwrites private clipboard text; the supplied text must never "
            "be logged or echoed."
        ),
    ),
    # -- Screen and accessibility inspection ------------------------------
    "inspect_screen": _spec(
        "read_only", "medium", "Inspect visible screen content",
        arguments_affect_risk=True,
        private=True,
        note=(
            "Captured pixels may contain private content; treat any result "
            "as an unverified observation."
        ),
    ),
    "accessibility_list_applications": _spec(
        "read_only", "low", "List accessibility-enabled applications",
    ),
    "accessibility_inspect_application": _spec(
        "read_only", "medium", "Inspect an application accessibility tree",
        arguments_affect_risk=True,
        private=True,
        note="Control names and labels may contain private data.",
    ),
    "read_document": _spec(
        "read_only", "medium", "Read text from a document",
        arguments_affect_risk=True,
        private=True,
        note=(
            "File contents may be private; never log or repeat them outside "
            "the user's request."
        ),
    ),
}

def _freeze_baseline(entries):
    for name in entries:
        if not _is_valid_tool_name(name):
            raise PolicyValidationError(
                f"invalid baseline tool name: {name!r}"
            )
    return MappingProxyType(dict(entries))


BASELINE_SPECIFICATIONS = _freeze_baseline(_BASELINE_SPECIFICATIONS)

BASELINE_TOOL_NAMES = frozenset(BASELINE_SPECIFICATIONS)


def _coerce_spec(spec):
    if isinstance(spec, EffectSpec):
        return spec
    if isinstance(spec, Mapping):
        return EffectSpec.from_mapping(spec)
    raise PolicyValidationError("spec must be an EffectSpec or a mapping")


def _entry_items(entries):
    if isinstance(entries, Mapping):
        return list(entries.items())
    if entries is None:
        return []
    try:
        return list(entries)
    except TypeError as error:
        raise PolicyValidationError(
            "entries must be a mapping or an iterable of pairs"
        ) from error


class ToolPolicy:
    """Thread-safe registry mapping tool names to validated effect specs.

    Reads never take the lock: the internal mapping is immutable and replaces
    the whole reference on every successful registration.  ``register`` is
    serialised with a lock and returns ``False`` (never raises) for invalid
    input, duplicates without ``replace=True``, or a locked registry.
    """

    def __init__(
        self,
        entries=None,
        *,
        include_baseline=True,
        allow_registration=True,
    ):
        specs = {}
        if include_baseline:
            specs.update(BASELINE_SPECIFICATIONS)

        seen = set()
        for item in _entry_items(entries):
            if not isinstance(item, tuple) or len(item) != 2:
                raise PolicyValidationError(
                    "entries must contain (name, spec) pairs"
                )
            name, spec = item
            if not _is_valid_tool_name(name):
                raise PolicyValidationError(
                    f"invalid tool name: {name!r}"
                )
            if name in seen:
                raise PolicyValidationError(
                    f"duplicate tool name in entries: {name!r}"
                )
            seen.add(name)
            specs[name] = _coerce_spec(spec)

        self._specs = MappingProxyType(specs)
        self._lock = threading.RLock()
        self._allow_registration = bool(allow_registration)

    # -- read access -----------------------------------------------------
    @property
    def registration_allowed(self):
        return self._allow_registration

    def __len__(self):
        return len(self._specs)

    def __contains__(self, tool_name):
        return self._lookup(tool_name) is not None

    def __repr__(self):
        state = "open" if self._allow_registration else "locked"
        return f"ToolPolicy(tools={len(self._specs)}, registration={state})"

    def _lookup(self, tool_name):
        # Exact built-in strings only: subclasses could override hashing.
        if type(tool_name) is not str:
            return None
        return self._specs.get(tool_name)

    def policy_for(self, tool_name):
        """Return the EffectSpec for a tool, or None when unregistered."""
        return self._lookup(tool_name)

    def requires_confirmation(self, tool_name):
        """True when the tool needs approval; unknown tools always do."""
        spec = self._lookup(tool_name)
        return spec is None or spec.requires_confirmation

    def names(self):
        """Return registered tool names in deterministic sorted order."""
        return tuple(sorted(self._specs))

    def snapshot(self):
        """Return a JSON-safe copy of every entry; later edits do not leak."""
        return {
            name: self._specs[name].to_dict()
            for name in sorted(self._specs)
        }

    # -- registration ----------------------------------------------------
    def register(self, name, spec, *, replace=False):
        """Validate and register one tool.

        Returns True only when the entry was added.  Any invalid field, an
        invalid name, a duplicate without ``replace=True``, or a locked
        registry yields False and leaves the registry unchanged.
        """
        try:
            if not _is_valid_tool_name(name):
                return False
            if type(replace) is not bool:
                return False
            candidate = _coerce_spec(spec)
        except Exception:
            # Malformed policy data is denied, never partially applied.
            return False

        if not self._allow_registration:
            return False

        with self._lock:
            current = self._specs
            if name in current and not replace:
                return False
            updated = dict(current)
            updated[name] = candidate
            self._specs = MappingProxyType(updated)
        return True

    # -- decisions -------------------------------------------------------
    def evaluate(self, tool_name, arguments=None):
        """Return a JSON-safe decision dict for one tool call.

        ``arguments`` is accepted for API stability and deliberately ignored.
        Keys: allowed, requires_confirmation, effect, risk, reason, message,
        plus reason_code, tool and private.
        """
        # Arguments are never inspected: no indexing, iteration or str().
        del arguments

        spec = self._lookup(tool_name)
        if spec is None:
            return {
                "allowed": False,
                "requires_confirmation": True,
                "effect": UNKNOWN_TOOL_POLICY.effect,
                "risk": UNKNOWN_TOOL_POLICY.risk,
                "reason": (
                    "No policy is registered for this tool, so it is treated "
                    "as an unknown mutation and always needs approval."
                ),
                "message": (
                    "This action is not recognized. Misty will ask for "
                    "explicit approval before doing anything."
                ),
                "reason_code": "unknown_tool",
                "tool": (
                    tool_name
                    if _is_valid_tool_name(tool_name)
                    else _UNKNOWN_TOOL_DISPLAY
                ),
                "private": False,
            }

        if spec.requires_confirmation:
            return {
                "allowed": False,
                "requires_confirmation": True,
                "effect": spec.effect,
                "risk": spec.risk,
                "reason": (
                    "This action can change things and always needs explicit "
                    "approval."
                ),
                "message": (
                    f"{spec.description}. Explicit user approval is required."
                ),
                "reason_code": "confirmation_required",
                "tool": tool_name,
                "private": spec.private,
            }

        if spec.mutating:
            reason_code = "allowed_mutation"
            reason = (
                "This action changes local state but is acceptable without "
                "approval."
            )
        elif spec.private:
            reason_code = "allowed_private_read"
            reason = (
                "This read-only action may reveal private data; approval is "
                "not required by default."
            )
        else:
            reason_code = "allowed_read_only"
            reason = "This read-only action does not change anything."

        return {
            "allowed": True,
            "requires_confirmation": False,
            "effect": spec.effect,
            "risk": spec.risk,
            "reason": reason,
            "message": f"{spec.description}. No approval is required.",
            "reason_code": reason_code,
            "tool": tool_name,
            "private": spec.private,
        }

    def describe_for_confirmation(self, tool_name, arguments=None):
        """Return a short static action summary for a confirmation prompt.

        Argument values are never interpolated, so clipboard text, document
        paths and window titles cannot appear here.  Unknown tools get a
        generic, content-free fallback.
        """
        del arguments

        spec = self._lookup(tool_name)
        if spec is None:
            return UNKNOWN_ACTION_SUMMARY
        return spec.description


DEFAULT_POLICY = ToolPolicy(allow_registration=False)


def evaluate(tool_name, arguments=None):
    """Evaluate one tool call against the locked default policy."""
    return DEFAULT_POLICY.evaluate(tool_name, arguments)


def describe_for_confirmation(tool_name, arguments=None):
    """Describe one tool call against the locked default policy."""
    return DEFAULT_POLICY.describe_for_confirmation(tool_name, arguments)


def requires_confirmation(tool_name):
    """True when the default policy requires approval; unknown tools do."""
    return DEFAULT_POLICY.requires_confirmation(tool_name)


def policy_for(tool_name):
    """Return the default policy entry for a tool, or None."""
    return DEFAULT_POLICY.policy_for(tool_name)
