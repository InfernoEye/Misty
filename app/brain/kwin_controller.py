"""Explicit foreground KWin pilot: real images, OCR tokens, per-click approval.

Default selection is preview-only. Input additionally requires an explicit
MISTY_KWIN_INPUT=1 opt-in, target observation approval and a separate approval for
every single left click. This is not atomic/background target-bound input and
is not an automatic fallback from Cua. No typing, keys, shell or arbitrary
coordinates are exposed to the visual model.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
import threading
import time
import unicodedata

from app.brain.visual_decision import (
    VisualDecisionRuntime, VisualImage, VisualDecisionCancelled, VisualDecisionTimeout,
)
from app.desktop.ownership import native_task_lease, native_task_blocked, block_native_admission
from app.desktop.providers.base import DesktopProviderError

CLICK_SCHEMA = {
    "name": "kwin_click",
    "description": "Propose one left click on an OCR element_token from the current observation. "
                   "Benign navigation only. No purchases, deletions, messages, submissions or security changes.",
    "inputSchema": {"type": "object", "properties": {
        "element_token": {"type": "string", "minLength": 1, "maxLength": 256}},
        "required": ["element_token"], "additionalProperties": False},
}
_SENSITIVE = re.compile(r"\b(buy|purchase|pay|checkout|delete|remove|send|submit|order|install|confirm|"
                        r"password|credential|unsubscribe|zap[lł]a[cć]|usu[nń]|wy[sś]lij|kup|zam[oó]w)\b", re.I)


def _fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold()) if c.isalnum())


def _input_enabled(environment):
    """Input admission for the native pilot.

    Leaving ``MISTY_DESKTOP_BACKEND=kwin`` set means the user chose the pilot;
    its input path is enabled by default. Preview-only remains an explicit,
    separate choice (``MISTY_KWIN_INPUT=0``). Every click is still approved
    individually, so "enabled" never means "unattended".
    """
    value = environment.get("MISTY_KWIN_INPUT", "1")
    if value not in {"0", "1"}:
        raise ValueError("MISTY_KWIN_INPUT must be exactly 0 or 1")
    return value == "1"


@dataclass(frozen=True)
class KWinTaskReport:
    status: str
    actions: int = 0
    question: str = ""

    def summary(self):
        if self.status == "preview":
            return ("KWin preview produced a text-target click proposal from the real image. "
                    "No input was sent. The foreground pilot requires explicit input opt-in and per-click approval.")
        if self.status == "model_observed":
            if self.actions:
                return (f"{self.actions} single click(s) were delivered. A fresh visual-model check reports "
                        "the requested state is visible; the outcome is not independently verified.")
            return "No input was sent. The visual model reports the requested state is already visible; this is not independently verified."
        if self.status == "ask":
            return self.question or "Please clarify the target window before continuing."
        if self.status == "canceled":
            return f"Native desktop task canceled after {self.actions} delivered click(s). Completed input was not rolled back."
        if self.status == "denied":
            return f"Approval was not granted. No further input was sent ({self.actions} earlier click(s) delivered)."
        if self.status == "sensitive_action":
            return "This native pilot supports benign navigation only. Sensitive controls and submissions are left for you."
        if self.status == "repeat_refused":
            return "I stopped rather than repeat a delivered but unverified click. Please inspect the target."
        if self.status in {"cleanup_failed", "admission_blocked", "input_outcome_unknown"}:
            return ("Native input admission is blocked after unconfirmed cleanup or input outcome. "
                    "Inspect the desktop and stop any remaining model process before restarting Misty.")
        if self.status == "target_not_active":
            return "Please bring the selected app to the foreground and try again. The native pilot never focuses another app automatically."
        if self.status == "target_occluded":
            return "The selected window overlaps another window or Misty's popup. Move the popup/other window away and try again; nothing further was clicked."
        if self.status == "timeout":
            return f"Native desktop time budget expired after {self.actions} delivered click(s). No further input was admitted."
        return (f"Native desktop task stopped ({self.status}) after {self.actions} delivered click(s). "
                "No automatic replay or backend fallback was performed.")


class _StopTask(Exception):
    def __init__(self, status):
        self.status = status


class KWinTaskController:
    def __init__(self, agent, *, provider_factory=None, runtime_factory=VisualDecisionRuntime,
                 environment=None, budget_seconds=180.0, max_actions=3):
        if isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float)) or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 600:
            raise ValueError("Invalid native task budget")
        if isinstance(max_actions, bool) or not isinstance(max_actions, int) or not 1 <= max_actions <= 5:
            raise ValueError("Invalid native action budget")
        if provider_factory is None:
            from app.desktop.providers.kwin import KWinDesktopProvider
            provider_factory = KWinDesktopProvider
        self.agent = agent
        self.provider = provider_factory(agent.registry)
        self.runtime_factory = runtime_factory
        self.input_enabled = _input_enabled(os.environ if environment is None else environment)
        self.budget_seconds = budget_seconds
        self.max_actions = max_actions
        self.cancel = getattr(agent, "cancel_event", None) or threading.Event()
        self.deadline = 0.0
        self.actions = 0

    def _check(self):
        if self.cancel.is_set():
            raise _StopTask("canceled")
        if time.monotonic() >= self.deadline:
            raise _StopTask("timeout")

    def _emit(self, title, status="planned"):
        self.agent._emit_activity({"kind": "decision", "title": title,
            "details": "Native foreground pilot; current phase has bounded admission and explicit scope.",
            "status": status})

    def _release_reasoning(self):
        hook = getattr(self.agent, "before_visual_callback", None)
        if callable(hook):
            hook()
        else:
            stop = getattr(self.agent, "stop", None)
            if callable(stop):
                stop()
        process = getattr(self.agent, "process", None)
        if process is not None and process.poll() is None:
            raise _StopTask("reasoning_busy")

    def _approve(self, name, description):
        self._check()
        self._release_reasoning()
        callback = getattr(self.agent, "confirmation_callback", None)
        if not callable(callback):
            return False
        self._emit("Awaiting native desktop approval")
        began = time.monotonic()
        try:
            approved = callback(name, {}, description) is True
        finally:
            waited = time.monotonic() - began
            # Existing broker expires in 60s. Approval wait does not consume
            # inference admission budget, but arbitrary host callbacks are not
            # advertised as preemptible.
            self.deadline += min(waited, 65.0)
        if waited > 65.0:
            raise _StopTask("timeout")
        self._check()
        return approved

    def _target(self, goal):
        context = self.provider.discover()
        candidates = [w for w in context["windows"] if not w.get("minimized")
                      and w.get("on_current_desktop") is not False]
        screen_match = re.search(r"\b(right|left|primary|main|center|centre)(?:[- ]hand)?\s+(?:screen|monitor|display)\b", goal, re.I)
        connector = re.search(r"\b((?:DP|HDMI|eDP)-\d+(?:-\d+)?)\b", goal, re.I)
        if screen_match or connector:
            resolver = getattr(self.agent.registry, "resolve_screen_target", None)
            if not callable(resolver):
                raise _StopTask("screen_unavailable")
            resolution = resolver((screen_match or connector).group(1))
            if not isinstance(resolution, dict) or resolution.get("success") is not True or not resolution.get("screen_name"):
                raise _StopTask("screen_ambiguous")
            candidates = [w for w in candidates if w.get("screen") == resolution["screen_name"]]
        named_app = self._named_app(goal)
        if named_app:
            resolver = getattr(self.agent.registry, "resolve_window_target", None)
            if not callable(resolver):
                raise _StopTask("target_unavailable")
            # Reuse the shared catalog path (identity, typo and description
            # reasoning). It never guesses among equal matches.
            resolution = resolver(named_app, allow_fuzzy=True)
            if not isinstance(resolution, dict):
                raise _StopTask("target_unavailable")
            if resolution.get("ambiguous"):
                raise _StopTask("target_ambiguous")
            if resolution.get("success") is not True or not resolution.get("window_id"):
                raise _StopTask("target_not_found")
            candidates = [w for w in candidates if w.get("id") == resolution["window_id"]]
        elif len(candidates) > 1 and not (screen_match or connector):
            # No named app and no destination: use the window that was in
            # front when you asked, never an arbitrary window on the desktop.
            active = context.get("active_window")
            candidates = [w for w in candidates if isinstance(active, dict) and w.get("id") == active.get("id")]
        if not candidates:
            raise _StopTask("target_not_found")
        if len(candidates) != 1:
            raise _StopTask("target_ambiguous")
        return candidates[0]

    @staticmethod
    def _named_app(goal):
        """The application the user actually named, or None.

        Requires content work on that app ("in X", "X and click", "on X's
        channel"), so a bare window-control phrasing stays with its tool.
        """
        # Most specific first: a scoped preposition followed by the action.
        match = (re.match(r"\s*(?:please\s+)?(?:in|inside|within)\s+([^.!?,;]+?)\s+"
                          r"(?:open|click|select|choose|type|press|scroll|search|find|look|go|navigate)\b",
                          goal, re.I)
                 or re.search(r"\b(?:in|inside|within)\s+(.+?)\s+(?:for|with|about|by)\b", goal, re.I)
                 or re.search(r"([^.!?]+?)\s+and\s+(?:then\s+)?(?:click|open|select|choose|type|press|scroll)\b", goal, re.I)
                 # "Click the channel in Discord on the right screen."
                 or re.search(r"\b(?:in|inside|within)\s+(.+?)"
                              r"(?=\s+on\s+(?:the\s+)?(?:right|left|primary|main|center|centre|DP-|HDMI-|eDP-)|[.!?,;]|$)",
                              goal, re.I))
        if not match:
            return None
        phrase = match.group(1).strip().strip("\"'")
        phrase = re.sub(r"^(?:the|a|an|my)\s+", "", phrase, flags=re.I).strip()
        # Drop a leading verb such as "open" or "click" from a two-clause form.
        phrase = re.sub(r"^(?:click|open|select|choose|type|press|scroll|go to|navigate to)\s+", "", phrase, flags=re.I).strip()
        if len(_fold(phrase)) < 2 or len(phrase) > 120:
            return None
        return phrase

    def _decide(self, goal, observation, history, tools):
        self._check()
        self._release_reasoning()
        runtime = None
        self._emit("Loading local visual decision model")
        try:
            runtime = self.runtime_factory()
            runtime.start(cancel=self.cancel, deadline=self.deadline)
            self._emit("Choosing next native desktop step")
            metadata = {"kind": "current_observation", "observation_id": observation.observation_id,
                        "elements": [{key: element[key] for key in ("element_token", "text", "x", "y", "width", "height")
                                      if key in element} for element in observation.elements[:150]]}
            result = runtime.decide(goal=goal, observation_id=observation.observation_id,
                images=[VisualImage(observation.image)], tool_schemas=tools,
                history=[*history[-5:], metadata], cancel=self.cancel, deadline=self.deadline)
        finally:
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:
                    block_native_admission()
                    raise _StopTask("cleanup_failed") from None
        self._check()
        return result

    def run(self, goal):
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            return KWinTaskReport("invalid_goal")
        if _SENSITIVE.search(goal):
            return KWinTaskReport("sensitive_action")
        if native_task_blocked():
            return KWinTaskReport("admission_blocked")
        if self.input_enabled and getattr(self.agent.registry, "input_backend", None) is None:
            return KWinTaskReport("input_unavailable")
        self.deadline = time.monotonic() + self.budget_seconds
        history, attempted = [], set()
        try:
            with native_task_lease():
                self._check()
                self._emit("Resolving native KWin target")
                target = self._target(goal)
                title = str(target.get("title") or "selected window")[:180]
                mode = "Per-click approved foreground pilot" if self.input_enabled else "Preview only: no input"
                if not self._approve("kwin_observe", f"{mode}. Allow a local screenshot and visual analysis of '{title}'? "
                                     "This pilot does not type or submit forms. Do not use it for purchases or destructive actions."):
                    return KWinTaskReport("denied")
                while True:
                    self._check()
                    self._emit("Observing native target" if not self.actions else "Checking post-action target state")
                    observation = self.provider.observe(target["id"], cancel=self.cancel, deadline=self.deadline)
                    tools = [CLICK_SCHEMA] if self.actions < self.max_actions else []
                    decision = self._decide(goal, observation, history, tools)
                    if not isinstance(decision, dict):
                        raise _StopTask("invalid_decision")
                    if decision.get("kind") == "ask":
                        return KWinTaskReport("ask", self.actions, str(decision.get("question", ""))[:500])
                    if decision.get("kind") == "finish":
                        evidence = decision.get("evidence_ids")
                        if (not isinstance(evidence, list)
                                or any(not isinstance(item, str) for item in evidence)
                                or observation.observation_id not in evidence):
                            raise _StopTask("unverified_finish")
                        return KWinTaskReport("model_observed", self.actions)
                    arguments = decision.get("arguments")
                    if (decision.get("kind") != "act" or decision.get("tool") != "kwin_click"
                            or decision.get("observation_id") != observation.observation_id
                            or not isinstance(arguments, dict) or set(arguments) != {"element_token"}
                            or self.actions >= self.max_actions):
                        raise _StopTask("invalid_decision")
                    token = arguments["element_token"]
                    elements = [e for e in observation.elements if e.get("element_token") == token]
                    if len(elements) != 1:
                        raise _StopTask("invalid_token")
                    element = elements[0]
                    if _SENSITIVE.search(str(element.get("text", ""))):
                        return KWinTaskReport("sensitive_action", self.actions)
                    if not self.input_enabled:
                        return KWinTaskReport("preview", self.actions)
                    signature = (target["id"], element.get("text"), element.get("x"), element.get("y"))
                    if signature in attempted:
                        return KWinTaskReport("repeat_refused", self.actions)
                    label = str(element.get("text", ""))[:160]
                    if not self._approve("kwin_click", f"Approve ONE foreground left click on OCR text '{label}' "
                        f"in '{title}' (image box {element.get('x')}, {element.get('y')}, "
                        f"{element.get('width')} x {element.get('height')})? "
                        "Review the app. This is not atomic background input; keep other input still. "
                        "An unchanged target image and foreground identity will be rechecked before dispatch."):
                        return KWinTaskReport("denied", self.actions)
                    self._check()
                    attempted.add(signature)
                    self._emit("Applying approved native click")
                    try:
                        outcome = self.provider.click(observation.observation_id, token, approved=True,
                                                      cancel=self.cancel, deadline=self.deadline)
                    except Exception:
                        block_native_admission()
                        raise _StopTask("input_outcome_unknown") from None
                    if not isinstance(outcome, dict) or outcome.get("success") is not True:
                        if not isinstance(outcome, dict) or outcome.get("not_executed") is not True:
                            block_native_admission()
                            return KWinTaskReport("input_outcome_unknown", self.actions)
                        code = outcome.get("code")
                        safe = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,60}", code) else "input_unknown"
                        return KWinTaskReport(safe, self.actions)
                    self.actions += 1
                    history.append({"round": self.actions, "kind": "action_result", "tool": "kwin_click",
                                    "delivered": True, "verified": False})
        except _StopTask as error:
            return KWinTaskReport(error.status, self.actions)
        except VisualDecisionCancelled:
            return KWinTaskReport("canceled", self.actions)
        except VisualDecisionTimeout:
            return KWinTaskReport("timeout", self.actions)
        except DesktopProviderError as error:
            safe = error.code if re.fullmatch(r"[a-z_]{1,60}", error.code) else "provider_error"
            return KWinTaskReport(safe, self.actions)
        except Exception:
            return KWinTaskReport("native_task_failed", self.actions)


def run_kwin_task(agent, goal):
    try:
        report = KWinTaskController(agent).run(goal)
    except Exception:
        return KWinTaskReport("configuration_error").summary()
    agent._emit_activity({"kind": "decision", "title": "Native KWin pilot stopped",
        "details": f"Status: {report.status}; delivered clicks: {report.actions}. No automatic rollback.",
        "status": "info"})
    return report.summary()
