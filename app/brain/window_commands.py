"""Narrow deterministic plans for unambiguous, single window operations.

Only builds a plan. Existing schema validation, window resolution, policy,
approval and execution still run in MistyAgent. Compound/negated/file requests
are deliberately left to the normal structured planner.
"""
import re

_FILLER = re.compile(r"^(?:(?:please|can you|could you|would you|will you|kindly|prosz[eę])\s+)+", re.I)
_MOVE = re.compile(
    r"^(?:move|send|put|place|przenie[sś]|przesu[nń])\s+"
    r"(?P<target>.+?)\s+(?:to|onto|na)\s+(?:the\s+)?"
    r"(?P<screen>main|primary|left|right|center|centre|middle|g[lł][oó]wny|g[lł][oó]wnym|lewy|prawy)\s+"
    r"(?:screen|monitor|display|ekran|ekranie|monitorze)\s*[.!?]?$", re.I)
_CONNECTOR = re.compile(
    r"^(?:move|send|put|place|przenie[sś]|przesu[nń])\s+(?P<target>.+?)\s+"
    r"(?:to|onto|na)\s+(?:the\s+)?(?P<screen>(?:DP|HDMI|eDP)-\d+(?:-\d+)?)"
    r"(?:\s+(?:screen|monitor|display))?\s*[.!?]?$", re.I)
_CONTROL = re.compile(
    r"^(?P<verb>focus|switch to|bring up|minimize|minimise|maximize|maximise|restore|close|"
    r"przenie[sś] do|zamknij|przywr[oó][cć])\s+(?:the\s+|my\s+|a\s+)?"
    r"(?P<target>.+?)(?:\s+(?:window|app|application|okno|aplikacj[ęe]))?\s*[.!?]?$", re.I)
#: Same display-leave phrasing, kept for compatibility with earlier callers.
_COMPOUND = re.compile(r"[;\n]|\b(?:and|then|after|before|or|i potem|nast[eę]pnie|potem)\b", re.I)
_FILE = re.compile(r"[/\\]|\b(?:file|folder|directory|files|folders|plik|pliki|katalog)\b|"
                   r"\.(?:txt|pdf|docx?|xlsx?|png|jpe?g|zip|json|py|csv)\b", re.I)
#: Words that would make the "target" a description of screen content, not an
#: application: those need visual reasoning, not a window tool.
_CONTENT = re.compile(
    r"\b(?:button|channel|server|message|sidebar|tab|menu|dialog|icon|cursor|pointer|"
    r"picture|image|slider|dropdown|checkbox|link|form|field|input|entry|page|search)\b", re.I)
_LOOKUP = re.compile(r"^(?:what|which|who|list|show|tell me|name)\b", re.I)
_TOOLS = {"focus": "focus_window", "switch to": "focus_window", "bring up": "focus_window",
          "minimize": "minimize_window", "minimise": "minimize_window",
          "maximize": "maximize_window", "maximise": "maximize_window",
          "restore": "restore_window", "close": "close_window",
          "przenieś do": "focus_window", "zamknij": "close_window", "przywróć": "restore_window"}
_ALIASES = {"centre":"center", "middle":"center", "główny":"main", "glowny":"main",
            "głównym":"main", "glownym":"main", "lewy":"left", "prawy":"right"}


_DEICTIC = re.compile(r"^(?:this|that|it|current|active|last|previous|ten|ta|to)\b", re.I)


def _target_name(raw, *, content_is_visual=True):
    """A bounded application/window name, or None when it is not one."""
    target = (raw or "").strip().strip('"\'')
    target = re.sub(r"^(?:the|a|an)\s+", "", target, flags=re.I).strip()
    # "my" belongs to possessive app names ("my app"); deictic words below are
    # rejected outright so they can never become a window lookup.
    if not target or len(target) > 80 or _FILE.search(target):
        return None
    if content_is_visual and (_DEICTIC.match(target) or _CONTENT.search(target)):
        return None
    return target


def window_control_plan(request):
    """A single plan over window-specific tools, or None to use the planner.

    Window placement/visibility is metadata: it never needs vision, and a
    deterministic plan cannot invent clicks or coordinates. Anything that
    mentions screen *content* is left to the visual path.
    """

    if not isinstance(request, str) or not request.strip() or len(request) > 400:
        return None
    text = _FILLER.sub("", request.strip())
    if _COMPOUND.search(text) or _LOOKUP.match(text) or _CONTENT.search(text):
        return None
    if not (re.search(r"\b(?:window|app|application|okno)\b", text, re.I)
            or re.search(r"\b(?:focus|switch to|minimize|minimise|maximize|maximise|restore|"
                         r"move|send|put|place|bring up|close|zamknij|przenie[sś]|przywr[oó][cć])\b",
                         text, re.I)):
        return None
    move = window_move_plan(text)
    if move is not None:
        if _DEICTIC.match(move["steps"][0]["arguments"]["window_id"]):
            return None
        return move
    match = _CONTROL.fullmatch(text)
    if match is None:
        return None
    target = _target_name(match["target"])
    if target is None:
        return None
    tool = _TOOLS.get(" ".join(match["verb"].casefold().split()))
    if tool is None:
        return None
    return {"steps": [{"id":"window-action", "tool":tool,
                       "arguments":{"window_id":target}, "depends_on":[]}],
            "intent_confidence":1.0, "response":""}


def window_control_summary(tool, result):
    """Deterministic, non-overclaiming wording for a single window tool."""
    if tool == "move_window_to_screen":
        return window_move_summary(result)
    if not isinstance(result, dict) or result.get("success") is not True:
        detail = result.get("message") if isinstance(result, dict) else None
        return f"The window action failed: {detail[:500] if isinstance(detail, str) and detail else 'no result was returned.'}"
    if "verified" in result and result.get("verified") is not True:
        return "The window command was delivered, but its effect could not be verified."
    labels = {"focus_window":"Focused the window", "minimize_window":"Minimized the window",
              "maximize_window":"Maximized the window", "restore_window":"Restored the window",
              "close_window":"Asked the window to close"}
    return labels.get(tool, "Window action completed") + "."


def window_move_plan(request):
    if not isinstance(request, str) or not request.strip() or len(request) > 400:
        return None
    text = _FILLER.sub("", request.strip())
    if _COMPOUND.search(text):
        return None
    match = _MOVE.fullmatch(text) or _CONNECTOR.fullmatch(text)
    if match is None:
        return None
    target = match["target"].strip().strip('"\'')
    if not target or _FILE.search(target):
        return None
    screen = match["screen"]
    screen = _ALIASES.get(screen.casefold(), screen.casefold() if "-" not in screen else screen)
    return {"steps": [{"id":"window-move", "tool":"move_window_to_screen",
                       "arguments":{"window_id":target, "screen_name":screen}, "depends_on":[]}],
            "intent_confidence":1.0, "response":""}


def window_move_summary(result):
    """Never turn an attempted/unverified/no-op move into a success claim."""
    if not isinstance(result, dict) or result.get("success") is not True:
        detail = result.get("message") if isinstance(result, dict) else None
        return "Window move failed: " + (detail[:500] if isinstance(detail, str) and detail else "no verified result was returned.")
    if result.get("verified") is not True:
        return "The window command was delivered, but its destination could not be verified."
    screen = result.get("actual_screen") or result.get("screen_name")
    screen = screen[:128] if isinstance(screen, str) and screen else "the requested screen"
    if result.get("changed") is False or result.get("already_on_screen") is True:
        return f"The window is already on {screen}; no move was needed (verified)."
    if result.get("changed") is True:
        return f"Moved the window to {screen} (verified)."
    return f"The window is on {screen} (verified); its previous screen was not established."
