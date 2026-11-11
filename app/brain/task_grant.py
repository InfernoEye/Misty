"""One approval for a whole desktop task, instead of one per click.

Asking before every click makes a twenty-step task twenty prompts, so the
user stops reading them — which is worse than asking once, carefully. A
:class:`TaskGrant` records that the user approved *this task* and lets the
desktop tools run inside it, while keeping two things absolute:

* the grant is bound to one task and one thread, expires after a short
  window, and is closed when the task ends;
* a control that completes a purchase is refused even with a grant, because
  "click around and fill the form" was never consent to spend money.

Anything outside the grant behaves exactly as before: no approval, no action.
"""

import re
import threading
import time


#: How long a task approval stays usable before it must be renewed.
DEFAULT_GRANT_SECONDS = 600.0

#: Effects a task grant may cover. Anything else still asks per action.
GRANTABLE_EFFECTS = frozenset({"accessibility_action", "window_control"})


class TaskGrant:
    """A time-boxed, task-scoped approval for desktop automation."""

    def __init__(self, task, seconds=DEFAULT_GRANT_SECONDS, clock=time.monotonic):
        if not isinstance(task, str) or not task.strip():
            raise ValueError("a grant needs a task description")
        self.task = task.strip()
        self._clock = clock
        self._expires_at = clock() + max(1.0, float(seconds))
        self._owner = threading.get_ident()
        self._lock = threading.Lock()
        self._closed = False
        self._actions = 0

    # -- state ---------------------------------------------------------
    def active(self):
        """True while this grant may still be used by the calling thread."""
        with self._lock:
            if self._closed:
                return False
            if threading.get_ident() != self._owner:
                return False
            return self._clock() < self._expires_at

    def remaining(self):
        with self._lock:
            if self._closed:
                return 0.0
            return max(0.0, self._expires_at - self._clock())

    @property
    def actions(self):
        with self._lock:
            return self._actions

    def consume(self):
        """Count one action against the grant; True when it was usable."""
        with self._lock:
            if self._closed or threading.get_ident() != self._owner:
                return False
            if self._clock() >= self._expires_at:
                return False
            self._actions += 1
            return True

    def close(self):
        """End the grant. Idempotent; later actions ask again."""
        with self._lock:
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def __repr__(self):
        return f"TaskGrant(task={self.task!r}, actions={self.actions})"


# The guard is intentionally a small recognizer, not a natural-language task
# executor. False means "ask the classifier", not "this is visual". Quoted
# contents and path/basename literals are masked only for boundary/action checks
# so a file called screenshot.png or "click then type.txt" is not GUI evidence.
_LITERAL = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?<!\w)(?:~?/|\./|\.\./)\S+|\b[\w.-]+\.[A-Za-z0-9]{1,10}\b''')
_SECOND_ACTION = re.compile(
    r"\b(?:then|afterwards)\b|;|\b(?:and|or)\s+(?:please\s+)?"
    r"(?:click|tap|type|press|scroll|drag|navigate|select|choose|fill|move|send|"
    r"focus|minimize|maximize|restore|close|activate|raise|resize|list|show|"
    r"open|search|find|read|write|save|create|copy|delete|rename|remind|remember|play|pause|stop)\b",
    re.I,
)
_GUI_ACTION = re.compile(
    r"\b(?:click(?:ing)?|double[- ]click|right[- ]click|tap|type|typing|press|scroll(?:ing)?|"
    r"drag(?:ging)?|navigate|select|choose|fill)\b|\b(?:log|sign)\s+in\b|"
    r"\bopen\s+(?:the\s+)?(?:channel|tab|menu|dialog|button|link|page)\b",
    re.I,
)
_VISUAL_OBSERVATION = re.compile(
    r"\b(?:take|capture)\s+(?:a\s+)?(?:screen\s*shot|screenshot|screen\s+capture)\b|"
    r"\b(?:describe|inspect|analy[sz]e|look\s+at|read)\s+(?:the\s+|my\s+|this\s+)?"
    r"(?:screen|desktop|window|screenshot|display)(?![\w.])|"
    r"\bwhat(?:'s|\s+is|\s+can\s+you\s+see)\s+(?:currently\s+)?(?:shown\s+|visible\s+)?"
    r"(?:on|in)\s+(?:the\s+|my\s+|this\s+)?(?:screen|window|display|desktop)\b",
    re.I,
)
_WINDOW_CONTENT = re.compile(
    r"\b(?:button|channel|tab|menu|dialog|slider|icon|cursor|pointer|picture|image|"
    r"file|folder|document|selection|selected|password|account|message|text|field|input|textbox|"
    r"checkbox|dropdown|pane|panel|popup|item|row|column|cell|option|link|page|form|control|"
    r"notification|widget|sidebar|element|taskbar|and|or)\b", re.I)


def prefer_structured_tools(request):
    """Recognize clear single tool-domain requests before any LLM routing.

    Window geometry/identity, files, clipboard, memory and other structured
    state do not need vision merely because their names mention screens.
    This returns no tool arguments and grants no permission. Ambiguous,
    compound, and GUI-content work stays with the existing classifier.
    """
    if not isinstance(request, str) or not request.strip() or len(request) > 8000:
        return False
    text = " ".join(request.strip().split()).rstrip(".!?")
    text = re.sub(r"^(?:please\s+|(?:can|could|would)\s+you\s+(?:please\s+)?|misty[, :]\s*)", "", text, flags=re.I)
    masked = _LITERAL.sub("LITERAL", text)
    if (_SECOND_ACTION.search(masked) or _VISUAL_OBSERVATION.search(masked)
            or re.search(r"\b(?:on|from|inside)\s+(?:the\s+|my\s+|this\s+|that\s+)?(?:screen|window|display|desktop)\b", masked, re.I)):
        return False

    def matches(pattern, value=masked):
        return re.fullmatch(pattern, value, re.I) is not None

    # Explicit stores/schedules carry content, not commands to operate that
    # content. Boundary checks above still reject "... then click ...".
    if matches(r"(?:remember|recall|forget)\s+.+|(?:list|show|search|clear|delete)\s+(?:my\s+|the\s+|all\s+)?(?:saved\s+)?memor(?:y|ies)(?:\s+.*)?"):
        return True
    if matches(r"remind\s+me\s+to\s+.+|(?:set|create|add|list|show|cancel|delete|remove)\s+(?:(?:a|my|the|all)\s+)?reminders?(?:\s+.*)?"):
        return True
    if matches(r"(?:list|show|add|create|schedule|delete|remove|cancel)\s+(?:(?:a|my|the|all|today's|tomorrow's)\s+)?calendar\s+events?(?:\s+.*)?|(?:list|show)\s+(?:my\s+)?(?:calendar|appointments)(?:\s+(?:today|tomorrow|this\s+week))?"):
        return True
    if matches(r"(?:read|show|clear|empty)\s+(?:the\s+|my\s+)?clipboard|what(?:'s|\s+is)\s+(?:in|on)\s+(?:the\s+|my\s+)?clipboard|(?:copy|write|set)\s+.+\s+to\s+(?:the\s+|my\s+)?clipboard|(?:set|replace|write)\s+(?:the\s+|my\s+)?clipboard\s+(?:to|with)\s+.+"):
        return True

    if matches(r"what\s+do\s+you\s+remember(?:\s+about\s+.+)?|what\s+reminders\s+(?:do\s+i\s+have|are\s+(?:set|pending))|what(?:'s|\s+is)\s+on\s+(?:my|the)\s+calendar(?:\s+(?:today|tomorrow|this\s+week))?|(?:add|create|schedule)\s+(?:a\s+|an\s+)?event\s+.+\s+(?:to|on)\s+(?:my|the)\s+calendar(?:\s+.*)?"):
        return True
    if _GUI_ACTION.search(masked):
        return False
    # Queries about content in an app are not filesystem/metadata operations.
    if re.search(r"\b(?:in\s+(?:my\s+|the\s+)?(?:browser|discord|slack|telegram|firefox|chrome)|on\s+(?:amazon|ebay|allegro))\b", masked, re.I):
        return False

    move = re.fullmatch(
        r"(?:move|send)\s+(?:the\s+|my\s+)?(?P<target>.+?)\s+(?:to|onto)\s+(?:the\s+|my\s+)?"
        r"(?:(?:[\w-]+\s+){0,3}(?:screen|monitor|display)(?:\s+(?:[\w-]+)){0,2}|(?:DP|HDMI|eDP)-\d+(?:-\d+)?)", text, re.I)
    if (move and not re.search(r"\b(?:and|or)\b", masked, re.I)
            and not _WINDOW_CONTENT.search(move["target"])
            and not re.fullmatch(r"this|that|it|something|thing", move["target"], re.I)
            and not re.search(r"[/\\]|\.[A-Za-z0-9]{1,10}\b", move["target"])):
        return True
    control = re.fullmatch(r"(?:focus|minimize|maximize|restore|close|activate|raise)\s+(?:the\s+|my\s+)?(?P<target>.+)", text, re.I)
    if (control and not _WINDOW_CONTENT.search(control["target"])
            and not re.fullmatch(r"this|that|it|something|thing", control["target"], re.I)
            and not re.search(r"[/\\]|\.[A-Za-z0-9]{1,10}\b|^on\s", control["target"], re.I)):
        return True
    if matches(r"(?:list|show|enumerate)\s+(?:(?:all|the|my|open|running|available|connected)\s+)*(?:windows|screens|monitors|displays)(?:\s+and\s+(?:windows|screens|monitors|displays))?"):
        return True
    if matches(r"(?:what|which)\s+(?:windows|screens|monitors|displays)\s+are\s+(?:open|running|available|connected)|(?:which|what)\s+window\s+is\s+(?:currently\s+)?(?:active|focused)|(?:what|which)(?:'s|\s+is)\s+(?:the\s+)?(?:current|active|focused)\s+(?:window|application|app)|(?:get|show)\s+(?:the\s+)?(?:current|active|focused)\s+window(?:\s+(?:title|id|info))?"):
        return True

    if matches(r"(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?(?:time|date)|what\s+time\s+is\s+it|(?:tell\s+me|show|get)\s+(?:the\s+)?(?:time|date)|(?:show|get|check)\s+(?:the\s+)?system\s+status|how\s+much\s+(?:disk\s+space|memory|ram)\s+is\s+(?:left|free|available))"):
        return True
    if matches(r"(?:play|pause|resume|stop)\s+(?:the\s+|my\s+|some\s+)?(?:music|media|playback)|(?:next|previous|skip)\s+(?:track|song)|(?:what(?:'s|\s+is)\s+(?:currently\s+)?playing)|(?:list|show)\s+(?:the\s+)?media\s+players"):
        return True
    if matches(r"(?:system\s+status|(?:show|get|check)\s+(?:the\s+)?(?:cpu|ram|memory|disk|battery)\s+(?:usage|status|temperature|level)|what(?:'s|\s+is)\s+(?:the\s+)?(?:cpu|ram|memory|disk|battery)\s+(?:usage|status|temperature|level))"):
        return True
    # Paths/basenames count only under explicit filesystem verbs; the same
    # literals under "click/open channel/inspect screen" never get here.
    file_noun = re.search(r"\b(?:files?|folders?|director(?:y|ies)|documents?)\b", masked, re.I)
    file_literal = re.search(r"(?<!\w)(?:~?/|\./|\.\./)\S+|\b[\w.-]+\.[A-Za-z0-9]{1,10}\b", text) is not None
    if (file_noun or file_literal) and matches(
        r"(?:find|search|list|read|write|create|make|copy|move|rename|delete|trash|open|reveal|grep|transcribe|index|save|show|get)\s+.+"):
        # "find files by using the on-screen search" describes GUI work,
        # unlike file names containing the same words.
        if not re.search(r"\b(?:on[- ]screen|using\s+(?:the\s+)?(?:gui|app|menu)|by\s+(?:using|opening)|(?:to|onto|in)\s+(?:the\s+)?(?:right\s+|left\s+|main\s+)?(?:screen|monitor|display))\b", masked, re.I):
            return True
    return False


#: Structured domains are handled deterministically first; remaining requests
#: use the model to distinguish genuine GUI content work from ordinary tools.
ROUTE_QUESTION = """
Does this request ask you to OPERATE a desktop application or a website on
the user's behalf (open something, look at it, click, type, search, fill in,
navigate), or does it ask for a fact, an answer, or something the existing
tools can do directly (time, files, memory, media control, a single launch)?

Answer with JSON only:
  {"route": "desktop", "reason": "<short>"}
  {"route": "direct", "reason": "<short>"}

Choose "desktop" when reaching the goal requires seeing or interacting with
a graphical application or a web page the user asked to open. Choose
"direct" when a single tool call or a short plan of tool calls answers it.

Examples of "desktop": "click the X on my screen", "in Discord open channel
Y", "find a product on <site> and show it", "go to my bank site and log in".
A screen/window mention alone does NOT require vision. Distinguish changing
window geometry/identity with existing tools from operating its visible contents.

Examples of "direct": "move Discord to the main screen", "minimize Firefox",
"focus Discord", "list windows", "which window is active", "list screens",
"what time is it", "find my files", "play music", "read my clipboard",
"set a reminder", "open youtube", "remember this", "how much disk space is left".
Window management has exact tools (move_window_to_screen, focus_window,
minimize_window, maximize_window, restore_window, close_window, list_windows,
list_screens); do not substitute screenshot inspection for these operations.
Compound content interaction such as "move Discord then click its channel"
still requires the desktop route.

When in doubt between the two, choose "desktop": the loop looks before it
acts and asks when it is unsure, while the planner cannot see the screen.
"""


#: Phrases that describe operating a GUI. After asking the model, these
#: rescue explicit visual requests classified as "direct" or left undecided
#: after bounded retries. They select a route, never authorize desktop input.
#: A screen or site to act on cannot be answered without seeing it.
_SCREEN_REFERENCES = (
    # Something only visible on a screen.
    "screen", "monitor", "display", "window", "click", "cursor",
    "right side", "left side", "taskbar", "tray", "desktop icon",
    # Operating a site in a browser: the result depends on what the page
    # shows, so the answer cannot come from a plan over facts.
    "on amazon", "on ebay", "on allegro", "in amazon", "in ebay",
    "go on ", "go to the site", "log in to", "sign in to", "add to cart",
    "show it", "show me the page", "open the page", "the listing",
    "in discord", "in slack", "in telegram", "in my browser",
)


def mentions_screen(text):
    """True when the request refers to something only visible on screen."""
    lowered = " ".join(str(text or "").casefold().split())
    return any(word in lowered for word in _SCREEN_REFERENCES)


def prefer_structured_sequence(request):
    """A sequence of known tool operations is still not visual interaction.

    Literal filenames/quoted payloads are masked only for route boundaries;
    the original request is preserved for planning and argument binding.
    """
    if not isinstance(request, str) or len(request) > 8000:
        return False
    masked = _LITERAL.sub("LITERAL", request)
    clauses = re.split(r"\s+(?:and\s+then|and|then)\s+|\s*;\s*", masked, flags=re.I)
    if len(clauses) < 2 or len(clauses) > 10 or not prefer_structured_tools(clauses[0]):
        return False
    first_is_window = re.match(r"\s*(?:(?:please|can you|could you)\s+)*(?:move|send|focus|minimize|maximize|restore|close|activate|raise)\b", clauses[0], re.I)
    return all(prefer_structured_tools(clause) or (
        first_is_window and re.fullmatch(r"\s*(?:focus|minimize|maximize|restore|close|activate|raise)\s+(?:it|this\s+window|that\s+window)[.!?]?\s*", clause, re.I)
    ) for clause in clauses[1:])


def looks_like_desktop_work(agent, request, attempts=2):
    """Decide whether ``request`` needs the desktop loop.

    Clear single structured-tool domains bypass model classification first.
    The model classifies remaining ambiguous/GUI work. If it answers "direct"
    but that remaining wording refers to on-screen content, the screen fallback
    can override it; window geometry/metadata requests never reach that fallback.

    If classification fails after the bounded retries, explicit screen
    references use the same fallback. Routing is not authorization: the
    desktop path still requires its normal task approval and capabilities.

    Returns True only when the desktop path should be tried.
    """
    if not isinstance(request, str) or not request.strip():
        return False
    if prefer_structured_tools(request) or prefer_structured_sequence(request):
        return False
    for _attempt in range(max(1, int(attempts))):
        route = route_from_model(agent, request)
        if route == "desktop":
            return True
        if route == "direct":
            # A "direct" answer is trusted unless the request names a screen.
            return mentions_screen(request)
    # An unavailable or malformed classifier must not send an explicitly
    # visual task into the one-shot planner. This selects a route only; it
    # never grants permission to click, type, or read the screen.
    return mentions_screen(request)


def route_from_model(agent, request):
    """Ask the model which route a request needs; None when it cannot say.

    This supplies the primary model classification. A failure or an
    unparseable answer returns None; ``looks_like_desktop_work`` retries and
    then uses explicit screen references as a route-only fallback. The same
    fallback can override a "direct" classification for visual requests.
    """
    import json

    if agent is None or not callable(getattr(agent, "_chat", None)):
        return None
    try:
        message = agent._chat(
            [
                {"role": "system", "content": ROUTE_QUESTION},
                {"role": "user", "content": json.dumps({"user_request": request})},
            ],
            tools=[],
            response_format={"type": "json_object"},
        )
    except Exception:  # noqa: BLE001 - routing must never break a request
        return None
    raw = message.get("content") if isinstance(message, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    route = data.get("route")
    if route not in ("desktop", "direct"):
        return None
    return route


def describes_desktop_task(text):
    """Deprecated keyword heuristic; kept only for the offline fallback.

    It cannot recognise "find a cheap flight" or "set my wallpaper", which is
    exactly why :func:`route_from_model` exists. Never use this as the
    primary decision.
    """
    if not isinstance(text, str):
        return False
    lowered = " ".join(text.casefold().split())
    if not lowered:
        return False
    task_verbs = (
        "open ", "click ", "type ", "fill ", "select ", "choose ",
        "search on ", "press ", "drag ", "scroll ", "go to ",
        "navigate ", "log in", "sign in", "upload ", "download ",
        "paste ", "copy ", "rename ", "move ", "delete ", "install ",
    )
    has_verb = any(verb in lowered or lowered.startswith(verb.strip() + " ")
                   for verb in task_verbs)
    has_sequence = (
        " and then " in lowered
        or " then " in lowered
        or lowered.count(" and ") >= 1
    )
    return has_verb and has_sequence
