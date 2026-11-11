"""Bounded, private conversational memory for follow-up requests.

Misty plans every request from scratch, so a follow-up such as "the one from
October" or "open it" had nothing to resolve against. :class:`SessionContext`
keeps just enough of the last few turns to resolve such references, and
nothing more:

* the last user request and a short, truncated answer;
* at most :data:`MAX_FACTS` ``tool -> one scalar fact`` pairs (a path, a
  window title, a URL), each truncated to :data:`MAX_FACT_LENGTH` characters;
* the active target (window title/id or file path);
* a rolling list of the last :attr:`SessionContext.max_turns` turns.

Privacy rules applied here, not at the call site:

* tool payloads are never read. Only a whitelisted scalar key from a
  successful, non-``sensitive`` tool result can become a fact;
* results from :data:`app.brain.agent.PRIVATE_RESULT_TOOLS` (clipboard,
  screen, memory, documents, browser text, knowledge, calendar, e-mail-style
  text searches, ...) can never contribute a fact, with or without the
  ``sensitive`` flag. Anything that looks like prose (spaces) is refused even
  for an allowlisted key;
* events marked ``sensitive`` are dropped wholesale, because the activity
  stream carries argument JSON there and arguments may be private payloads;
* oversized values are dropped, never stored truncated *and* usable as a
  path; the byte budget evicts the oldest turns first, then facts.

The module is stdlib only and never touches Qt, the network, or the desktop.
``SessionContext`` takes an injectable clock so TTL behaviour is testable
without sleeping.
"""

import copy
import threading
import time


#: Facts kept at most (one per tool, newest wins).
MAX_FACTS = 3
#: Hard cap on one stored fact value.
MAX_FACT_LENGTH = 200
#: Hard cap on the last user request.
MAX_REQUEST_LENGTH = 1000
#: Hard cap on the stored last answer.
MAX_ANSWER_LENGTH = 500
#: Per-turn truncation inside ``recent_turns``.
TURN_USER_LENGTH = 200
TURN_ASSISTANT_LENGTH = 300
#: Referenced by the widget only when "is this still the same task?" matters.
DEFAULT_MAX_TURNS = 6
DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_MAX_BYTES = 8192

#: Result keys that may become a fact, in priority order. Only the first
#: present key is used, and only when the result also reports success.
_CANDIDATE_KEYS = (
    "path",
    "file_path",
    "folder",
    "target_path",
    "window_title",
    "window_id",
    "title",
    "url",
    "application",
    "app_id",
)
#: Keys used to derive the active target, in priority order.
_TARGET_KEYS = (
    ("window_id", "window_title"),
    ("path",),
    ("file_path",),
    ("url",),
)
_PROSE_DETECTED = " "


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= limit else ""


def _scalar(value):
    """Return a short printable scalar or ``""``.

    Only strings (and ints for ids) qualify. Anything containing whitespace
    is treated as prose and refused: a fact must look like a title, path or
    URL, never like clipboard, screen or knowledge text.
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return ""
    value = " ".join(value.splitlines()).strip()
    if not value or len(value) > MAX_FACT_LENGTH:
        return ""
    if _PROSE_DETECTED in value:
        # Titles and paths may contain single spaces; prose is longer. A
        # fact with more than 6 words is not a reference target.
        if len(value.split()) > 6:
            return ""
    return value


def _approx_bytes(value):
    """Cheap size estimate without pickling (never imports json lazily)."""
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace"))
    if isinstance(value, dict):
        return 2 + sum(
            _approx_bytes(str(key)) + _approx_bytes(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return 2 + sum(_approx_bytes(item) for item in value)
    if value is None:
        return 4
    return len(str(value))


class SessionContext:
    """Conversational context of the last few Misty turns.

    All methods are cheap, thread-safe and side-effect free apart from the
    context itself. Nothing here raises on hostile events: bad input is
    ignored.
    """

    def __init__(
        self,
        max_turns=DEFAULT_MAX_TURNS,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        max_bytes=DEFAULT_MAX_BYTES,
        is_private=None,
        accepts=None,
        clock=None,
    ):
        for name, value in (
            ("max_turns", max_turns),
            ("ttl_seconds", ttl_seconds),
            ("max_bytes", max_bytes),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive number.")
        if is_private is not None and not callable(is_private):
            raise TypeError("is_private must be callable.")
        if accepts is not None and not callable(accepts):
            raise TypeError("accepts must be callable.")
        self.max_turns = int(max_turns)
        self.ttl_seconds = float(ttl_seconds)
        self.max_bytes = int(max_bytes)
        self._is_private = is_private
        #: ``accepts(event) -> bool``: whether an event is wanted at all. The
        #: controller uses it to ignore events raised outside the request
        #: that is currently being recorded.
        self._accepts = accepts
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._clear_locked()

    # -- lifetime -------------------------------------------------------

    def _clear_locked(self):
        self._turns = []
        self._facts = {}
        self._active_target = None
        self._last_request = ""
        self._last_answer = ""
        self._last_activity = None
        self._open_turn = None

    def clear(self):
        """Forget everything (shutdown and the user-facing "forget" action)."""
        with self._lock:
            self._clear_locked()

    def __len__(self):
        with self._lock:
            self._expire_locked()
            return len(self._turns)

    @property
    def turns(self):
        with self._lock:
            self._expire_locked()
            return [dict(turn) for turn in self._turns]

    def _expire_locked(self):
        """Drop everything once the TTL since the last activity elapsed."""
        if self._last_activity is None:
            return False
        if self._clock() - self._last_activity <= self.ttl_seconds:
            return False
        self._clear_locked()
        return True

    def _touch_locked(self):
        self._last_activity = self._clock()

    # -- recording ------------------------------------------------------

    def begin_turn(self, user):
        """Start collecting the events of one request (before it runs)."""
        request = _text(user, MAX_REQUEST_LENGTH)
        with self._lock:
            self._expire_locked()
            self._open_turn = {
                "user": request,
                "events": [],
            }
            self._touch_locked()

    def record_turn(self, user, assistant, events=None, active_target=None):
        """Record one finished request/answer pair.

        ``events`` are the agent activity events observed during the request
        (``None`` keeps using the ones collected by :meth:`update_from_events`
        since :meth:`begin_turn`). ``active_target`` is the last tool whose
        result named a window/path/URL; when omitted, the newest such tool
        from the events is used.
        """
        request = _text(user, MAX_REQUEST_LENGTH)
        answer = _text(assistant, MAX_ANSWER_LENGTH)
        with self._lock:
            self._expire_locked()
            if events is None:
                events = (self._open_turn or {}).get("events") or []
            self._open_turn = None
            self._turns.append({
                "user": request[:TURN_USER_LENGTH],
                "assistant": answer[:TURN_ASSISTANT_LENGTH],
            })
            while len(self._turns) > self.max_turns:
                self._turns.pop(0)
            if request:
                self._last_request = request
            if answer:
                self._last_answer = answer
            if isinstance(active_target, str) and active_target.strip():
                self._active_target = _scalar(active_target) or None
            for event in list(events)[-200:]:
                self._absorb_event_locked(event)
            self._enforce_budget_locked()
            self._touch_locked()
            return self.next_context()

    def update_from_events(self, events):
        """Consume agent activity events; returns the facts that changed.

        Only ``kind == "tool"`` results with ``status`` success/failure are
        considered, ``sensitive`` events are ignored, and so is every tool
        in :data:`app.brain.session_context._private_tool_names`.
        """
        updated = {}
        with self._lock:
            if self._expire_locked():
                return updated
            for event in events or ():
                fact = self._absorb_event_locked(event)
                if fact is not None:
                    updated[fact[0]] = fact[1]
            if updated:
                self._enforce_budget_locked()
                self._touch_locked()
        return updated

    def _private(self, tool):
        """True when ``tool`` may never contribute a fact.

        The injected predicate is consulted first; when it is absent the
        ``PRIVATE_RESULT_TOOLS`` membership is imported from the agent.
        """
        if self._is_private is not None:
            try:
                if self._is_private(tool):
                    return True
            except Exception:
                return True
        from app.brain.agent import PRIVATE_RESULT_TOOLS

        return tool in PRIVATE_RESULT_TOOLS

    def _absorb_event_locked(self, event):
        """Apply one event; return ``(tool, value)`` or ``None``."""
        if not isinstance(event, dict):
            return None
        if self._accepts is not None:
            try:
                if not self._accepts(event):
                    return None
            except Exception:
                return None
        if event.get("sensitive") is True:
            return None
        if event.get("kind") != "tool":
            return None
        tool = event.get("tool")
        if not isinstance(tool, str) or not tool:
            return None
        status = event.get("status")
        if status not in ("success", "failure"):
            return None
        if self._open_turn is not None:
            self._open_turn["events"].append(event)
        if status != "success" or self._private(tool):
            return None
        payload = event.get("result")
        if not isinstance(payload, dict):
            # The agent's completion event carries only one pre-summarised
            # scalar (``fact``/``fact_key``); never the raw payload.
            key = event.get("fact_key")
            value = _scalar(event.get("fact"))
            if isinstance(key, str) and key in _CANDIDATE_KEYS and value:
                payload = {"success": True, key: value}
            else:
                return None
        if payload.get("success") is not True:
            return None
        if any(
            payload.get(key) is True
            for key in ("uncertain", "partial", "blocked", "sensitive")
        ):
            return None

        value = ""
        for key in _CANDIDATE_KEYS:
            if key not in payload:
                continue
            value = _scalar(payload.get(key))
            if value:
                break
        if not value:
            return None

        self._facts.pop(tool, None)
        self._facts[tool] = value
        while len(self._facts) > MAX_FACTS:
            self._facts.pop(next(iter(self._facts)))
        target = self._target_from(tool, payload)
        if target:
            self._active_target = target
        return tool, value

    def _target_from(self, tool, payload):
        """Return the active target named by one successful result."""
        for keys in _TARGET_KEYS:
            if not all(key in payload for key in keys):
                continue
            values = [_scalar(payload.get(key)) for key in keys]
            if all(values):
                return values[0]
        return ""

    # -- reading --------------------------------------------------------

    def next_context(self):
        """Compact planner context, or ``{}`` when there is nothing to say."""
        with self._lock:
            if self._expire_locked():
                return {}
            facts = dict(self._facts)
            active = self._active_target
            previous = dict(self._turns[-1]) if self._turns else {}
            last_request = self._last_request
            last_answer = self._last_answer
        if not previous and not facts and not active:
            return {}
        context = {}
        if previous:
            context["recent_turns"] = [previous]
        if facts:
            context["last_facts"] = facts
        if active:
            context["active_target"] = active
        if last_request:
            context["last_request"] = last_request[:TURN_USER_LENGTH]
        if last_answer:
            context["last_answer"] = last_answer[:TURN_ASSISTANT_LENGTH]
        return context

    def fact_for(self, tool):
        """The current scalar fact of ``tool`` (``""`` when none)."""
        with self._lock:
            if self._expire_locked():
                return ""
            return self._facts.get(tool, "")

    @property
    def active_target(self):
        with self._lock:
            if self._expire_locked():
                return None
            return self._active_target

    @property
    def last_request(self):
        with self._lock:
            if self._expire_locked():
                return ""
            return self._last_request

    def size_bytes(self):
        """Approximate stored size, used to enforce ``max_bytes``."""
        with self._lock:
            return self._size_locked()

    def _size_locked(self):
        return _approx_bytes({
            "turns": self._turns,
            "facts": self._facts,
            "active_target": self._active_target,
            "last_request": self._last_request,
            "last_answer": self._last_answer,
        })

    def _enforce_budget_locked(self):
        while self._size_locked() > self.max_bytes:
            if self._turns:
                self._turns.pop(0)
            elif self._last_answer:
                self._last_answer = ""
            elif self._facts:
                self._facts.pop(next(iter(self._facts)))
            elif self._active_target is not None:
                self._active_target = None
            else:
                self._last_request = ""
                break

    def snapshot(self):
        """Deep copy of the stored state (debugging/tests only)."""
        with self._lock:
            self._expire_locked()
            return copy.deepcopy({
                "turns": self._turns,
                "facts": self._facts,
                "active_target": self._active_target,
                "last_request": self._last_request,
                "last_answer": self._last_answer,
            })
