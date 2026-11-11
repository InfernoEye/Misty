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


#: The question put to the model instead of a keyword list. The model already
#: reads every request, so it — not a hardcoded vocabulary — decides whether
#: the user is asking for work performed on the desktop.
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
A request that names a screen, a window or a website to OPERATE is desktop,
even if it sounds like one step — acting on it needs the screen.

Examples of "direct": "what time is it", "find my files", "play music",
"open youtube", "remember this", "how much disk space is left".

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


def looks_like_desktop_work(agent, request, attempts=2):
    """Decide whether ``request`` needs the desktop loop.

    The model is asked first (no keyword list decides the route). If it
    answers "direct" but the wording unambiguously refers to something on
    screen — "click on X", "on the right screen", "in window Y" — the answer
    is overridden, because the one-shot planner cannot look at a screen and
    will otherwise plan an inspection that goes nowhere.

    If classification fails after the bounded retries, explicit screen
    references use the same fallback. Routing is not authorization: the
    desktop path still requires its normal task approval and capabilities.

    Returns True only when the desktop path should be tried.
    """
    if not isinstance(request, str) or not request.strip():
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
