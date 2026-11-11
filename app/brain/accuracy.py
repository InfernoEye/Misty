"""How reliably each tool does what its name promises.

The planner chooses tools from names and descriptions alone, so it has no way
to tell "search for a file whose exact name I do not know" (robust) from
"read this exact path" (brittle: one wrong character fails). When a request
names something imprecisely, that difference decides whether the plan works.

These ratings are *data*, not behaviour: nothing here routes a request or
rewrites a plan. They are handed to the planner as guidance, so the model can
reason about which tool fits what it actually knows, and they are exported as
one JSON object so a caller can inspect or extend them.

Rating axes
-----------
``precision``
    How much exact detail the call needs before it can succeed. ``fuzzy``
    tools work from a partial or human memory of a name; ``exact`` tools
    require a real path, URL or identifier.
``discovery``
    Whether the tool can find something the user only described. A tool with
    ``discovery: never`` cannot search: calling it with a guessed value is
    how invented filenames reach the file system.
``recovery``
    What a failure tells the planner to do next: ``retry_same`` (transient),
    ``narrow_scope`` (search wider first), or ``needs_user`` (only the user
    knows the missing value).
"""

from types import MappingProxyType


#: Per-tool ratings. Every tool that takes a path, URL or identifier and
#: cannot search is explicit here; unlisted tools take the conservative
#: default (exact, no discovery, needs_user).
_TOOL_ACCURACY = {
    # -- discovery tools: they can find a thing you only described ----------
    "search_files": {
        "precision": "fuzzy",
        "discovery": "name",
        "recovery": "narrow_scope",
    },
    "grep_files": {
        "precision": "fuzzy",
        "discovery": "content",
        "recovery": "narrow_scope",
    },
    "find_duplicates": {
        "precision": "exact",
        "discovery": "content",
        "recovery": "narrow_scope",
    },
    "list_files": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "needs_user",
    },
    "list_windows": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "list_screens": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "list_installed_apps": {
        "precision": "fuzzy",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "list_memories": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "list_reminders": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "list_calendar_events": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "media_list_players": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "accessibility_list_applications": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "knowledge_index_status": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "knowledge_stats": {
        "precision": "exact",
        "discovery": "browse",
        "recovery": "retry_same",
    },
    "recall": {
        "precision": "fuzzy",
        "discovery": "memory",
        "recovery": "narrow_scope",
    },
    "knowledge_search": {
        "precision": "fuzzy",
        "discovery": "content",
        "recovery": "narrow_scope",
    },
    "web_search": {
        "precision": "fuzzy",
        "discovery": "web",
        "recovery": "retry_same",
    },
    "web_research": {
        "precision": "fuzzy",
        "discovery": "web",
        "recovery": "retry_same",
    },

    # -- exact tools: they need a real value and cannot find one -------------
    "read_document": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "get_file_info": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "write_text_file": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "copy_path": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "move_path": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "rename_path": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "trash_path": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "make_directory": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "copy_folder": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "move_folder": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "zip_create": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "zip_extract": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "transcribe_file": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "fetch_web_page": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "open_url": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "open_folder": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
    "open_app": {
        "precision": "fuzzy",
        "discovery": "never",
        "recovery": "retry_same",
    },
    "launch_app": {
        "precision": "fuzzy",
        "discovery": "never",
        "recovery": "retry_same",
    },
    "focus_window": {
        "precision": "fuzzy",
        "discovery": "resolves",
        "recovery": "retry_same",
    },
    "move_window_to_screen": {
        "precision": "fuzzy",
        "discovery": "resolves",
        "recovery": "retry_same",
    },
    "minimize_window": {
        "precision": "fuzzy",
        "discovery": "resolves",
        "recovery": "retry_same",
    },
    "maximize_window": {
        "precision": "fuzzy",
        "discovery": "resolves",
        "recovery": "retry_same",
    },
    "restore_window": {
        "precision": "fuzzy",
        "discovery": "resolves",
        "recovery": "retry_same",
    },
    "close_window": {
        "precision": "fuzzy",
        "discovery": "resolves",
        "recovery": "retry_same",
    },
    "inspect_screen": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "retry_same",
    },
    "compare_selection": {
        "precision": "exact",
        "discovery": "never",
        "recovery": "needs_user",
    },
}

#: Applied to any tool without an explicit entry: assume it needs an exact
#: value and cannot find one, which makes the planner ask instead of guess.
DEFAULT_ACCURACY = MappingProxyType({
    "precision": "exact",
    "discovery": "never",
    "recovery": "needs_user",
})


def accuracy_for(tool_name):
    """Return the rating mapping for ``tool_name`` (never raises)."""
    if not isinstance(tool_name, str):
        return dict(DEFAULT_ACCURACY)
    entry = _TOOL_ACCURACY.get(tool_name)
    if entry is None:
        return dict(DEFAULT_ACCURACY)
    return dict(entry)


def can_discover(tool_name):
    """True when the tool can find something the user only described."""
    return accuracy_for(tool_name)["discovery"] != "never"


def needs_exact_value(tool_name):
    """True when a guessed argument makes the call fail, not search."""
    return accuracy_for(tool_name)["precision"] == "exact"


def guidance():
    """The JSON-safe table handed to the planner.

    Only tools with an explicit entry are listed; anything else follows
    :data:`DEFAULT_ACCURACY`, which the prompt states once instead of
    repeating per tool.
    """
    return {name: dict(rating) for name, rating in _TOOL_ACCURACY.items()}


def rated_tools():
    """Names that carry an explicit rating, sorted."""
    return tuple(sorted(_TOOL_ACCURACY))


#: Argument names that carry a path, URL or identifier the tool will use
#: verbatim. Only these are checked: a free-text query is allowed to be fuzzy.
#: Only values the tool consumes verbatim and cannot resolve on its own.
#: A window id or a CSS selector is resolved against the live desktop/page,
#: and a query is deliberately fuzzy, so none of them belongs here.
_VALUE_ARGUMENTS = frozenset({
    "relative_path",
    "path",
    "url",
})


def invented_value(tool_name, argument, value, user_request, plan_texts):
    """Return a short reason when ``value`` looks invented, else ``None``.

    "Invented" means the tool needs an exact value, cannot discover one, and
    the value appears neither in what the user said nor in anything an
    earlier step produced. This is the check that stops a plausible-sounding
    file name from reaching the file system.
    """
    if not isinstance(argument, str) or argument not in _VALUE_ARGUMENTS:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    rating = accuracy_for(tool_name)
    if rating["precision"] != "exact" or rating["discovery"] != "never":
        return None
    # A result reference is filled later from a real result; never "invented".
    if "$steps." in value:
        return None
    haystack = (str(user_request or "")).casefold()
    for text in plan_texts or ():
        haystack += "\n" + str(text or "").casefold()
    needle = value.strip().casefold()
    if needle in haystack:
        return None
    # The user named the stem ("transcript"); a default extension on it is a
    # reasonable completion, because the tool reports the real file if it is
    # wrong. Adding WORDS to the stem creates a name they never mentioned:
    # "transcript.NIST-D" is not "transcript". So the stem must match and the
    # suffix must be a plain extension (letters/digits only, <= 5 chars).
    leaf = needle.replace("\\", "/").rsplit("/", 1)[-1]
    stem, _dot, suffix = leaf.partition(".")
    if not stem:
        return None
    if stem not in haystack:
        return _invented_message(tool_name, argument, value)
    if suffix and not (suffix.isalnum() and len(suffix) <= 5):
        return _invented_message(tool_name, argument, value)
    return None

def _invented_message(tool_name, argument, value):
    return (
        f"{tool_name}.{argument} is '{value}', which the user did not say and "
        "no earlier step produced; this tool cannot search for it. Find the "
        "real value with a discovery tool first, or ask the user."
    )


def suspicious_steps(steps, user_request):
    """Return ``[(position, tool, argument, value, reason)]`` for a plan.

    Used as *advice*: a plan that looks like it guesses a value is reported
    so the caller can decide (ask the user, add a discovery step, or refuse).
    It never rewrites a plan by itself.
    """
    found = []
    texts = []
    for step in steps or ():
        name = getattr(step, "name", None)
        arguments = getattr(step, "arguments", None)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            continue
        for argument, value in arguments.items():
            reason = invented_value(name, argument, value, user_request, texts)
            if reason is not None:
                found.append((
                    getattr(step, "position", 0), name, argument, value, reason
                ))
        found_values = []
        for value in arguments.values():
            if isinstance(value, str):
                found_values.append(value)
        texts.extend(found_values)
    return found
