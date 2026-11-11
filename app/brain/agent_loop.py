"""A bounded observe → act → check loop for desktop tasks.

The normal agent plans once and executes that plan. That is right for "move
Firefox to the left screen", and wrong for "research a good vacuum, open
Amazon, search for it": the next action depends on what the previous one
actually produced.

This module adds the second shape as a *separate* path, so the one-shot
planner is untouched:

    1. observe  — take the current screen state (a compact, bounded summary);
    2. decide   — ask the model for ONE next action, or to stop;
    3. act      — run that action through the normal registry and policy
                  (approval, egress, validation all still apply);
    4. check    — feed the action's result into the next observation.

Hard limits make it safe to leave running: a maximum number of rounds, a
wall-clock deadline, one action per round, and no action at all once a
terminal condition is reached.

Two rules are absolute and enforced here, not by prompt wording:

* a click is never sent to a control that completes a purchase (see
  :func:`app.brain.tools.is_submit_target`);
* the loop stops immediately when the model reports it is finished, when a
  round makes no progress, when an action needs approval the user declined,
  or when the deadline passes.

Nothing in this module touches Qt, the network or the desktop directly: the
observation function, the decision function and the action runner are all
injected, so the loop is testable without a screen, a model or a mouse.
"""

import time


#: Rounds allowed before the loop gives up (each round = one action).
DEFAULT_MAX_ROUNDS = 8
#: Wall-clock budget for the whole task, in seconds.
DEFAULT_BUDGET_SECONDS = 180.0
#: Consecutive rounds that may repeat the same observation before stopping.
DEFAULT_STALL_LIMIT = 2

#: Decision kinds the model may return.
DECIDE_ACT = "act"
DECIDE_DONE = "done"
DECIDE_ASK = "ask"

_TERMINAL = ("done", "ask", "stopped", "failed", "budget", "rounds")


class LoopStep:
    """One round of the loop, kept for the report."""

    __slots__ = ("round", "kind", "tool", "arguments", "result", "note")

    def __init__(self, round_number, kind, tool=None, arguments=None,
                 result=None, note=""):
        self.round = round_number
        self.kind = kind
        self.tool = tool
        self.arguments = dict(arguments or {})
        self.result = result
        self.note = note

    def to_dict(self, redact=True):
        """JSON-safe form. Arguments are dropped by default: they may carry
        typed text or a private query."""
        data = {
            "round": self.round,
            "kind": self.kind,
            "tool": self.tool,
            "note": self.note,
        }
        if not redact:
            data["arguments"] = self.arguments
        if isinstance(self.result, dict):
            data["success"] = self.result.get("success") is True
            data["code"] = self.result.get("code")
        return data


def _bounded_context(value, depth=0, budget=None):
    """Bound private, in-memory decision context; never used in public reports."""
    if budget is None:
        budget = [6000]
    if budget[0] <= 0 or depth > 5:
        return "[truncated]"
    budget[0] -= 16  # Bound empty containers and scalar nodes as well as text.
    if isinstance(value, str):
        text = value[:min(1500, budget[0])]
        budget[0] -= len(text)
        return text
    if value is None or isinstance(value, (bool, int, float)):
        budget[0] -= 16
        return value
    if isinstance(value, dict):
        return {str(key)[:80]: _bounded_context(item, depth + 1, budget)
                for key, item in list(value.items())[:30] if budget[0] > 0}
    if isinstance(value, (list, tuple)):
        result = []
        for item in value[:30]:
            if budget[0] <= 0:
                break
            result.append(_bounded_context(item, depth + 1, budget))
        return result
    return "[unsupported value]"


def _decision_history(steps):
    """Preserve observations/errors locally without leaking them to telemetry."""
    history = []
    for step in steps[-6:]:
        entry = step.to_dict()
        entry["arguments"] = _bounded_context(step.arguments)
        entry["result"] = _bounded_context(step.result)
        history.append(entry)
    return history


class LoopReport:
    """Outcome of one bounded loop run."""

    def __init__(self, status, steps, note=""):
        self.status = status
        self.steps = list(steps)
        self.note = note

    @property
    def actions(self):
        return [step for step in self.steps if step.kind == DECIDE_ACT]

    def to_dict(self):
        return {
            "status": self.status,
            "note": self.note,
            "rounds": len(self.steps),
            "actions": len(self.actions),
            "steps": [step.to_dict() for step in self.steps],
        }

    def summary(self):
        """One honest sentence for the user."""
        if self.status == "done":
            # Report what was achieved, not just how many clicks it took.
            note = (self.note or "").strip()
            if note:
                if not self.actions:
                    return note
                return f"{note} Done in {len(self.actions)} action(s)."
            return (
                f"Done in {len(self.actions)} action(s)."
                if self.actions else "Nothing needed doing."
            )
        if self.status == "ask":
            return self.note or "I need more information to continue."
        if self.status == "budget":
            return (
                f"I ran out of time after {len(self.actions)} action(s) and "
                "stopped. Nothing further was changed."
            )
        if self.status == "rounds":
            return (
                f"I stopped after {len(self.actions)} action(s) without "
                "finishing, rather than keep clicking."
            )
        if self.status == "failed":
            return self.note or "That task stopped after a failed action."
        return self.note or "The task stopped."


def _norm(observation):
    """Compare observations cheaply: a changed screen is a changed string."""
    if isinstance(observation, dict):
        # Fresh capture IDs/timestamps are provenance, not visual progress.
        observation = {key: value for key, value in observation.items()
                       if key not in {"frame", "captured_at"}}
    return str(observation)


def run_loop(observe, decide, act, *, max_rounds=DEFAULT_MAX_ROUNDS,
             budget_seconds=DEFAULT_BUDGET_SECONDS,
             stall_limit=DEFAULT_STALL_LIMIT, clock=time.monotonic,
             on_step=None):
    """Run the bounded loop and return a :class:`LoopReport`.

    ``observe()`` returns any JSON-safe description of the current state.
    ``decide(observation, history)`` returns a mapping with ``kind`` set to
    ``"act"`` (plus ``tool`` and ``arguments``), ``"done"`` or ``"ask"``
    (plus ``note``). ``act(tool, arguments)`` runs one action and returns
    the registry result.

    ``decide`` raising, returning a malformed decision, or ``act`` raising
    all stop the loop with ``status="failed"`` — never a retry storm.
    """
    if not callable(observe) or not callable(decide) or not callable(act):
        raise TypeError("observe, decide and act must be callable")
    rounds = max(1, int(max_rounds))
    deadline = clock() + max(1.0, float(budget_seconds))
    stalls = 0
    repairs = 0
    last_observation = None
    steps = []

    for round_number in range(1, rounds + 1):
        if clock() >= deadline:
            return LoopReport("budget", steps, "Time budget exhausted.")

        try:
            observation = observe()
        except Exception as error:  # noqa: BLE001 - stop, never spin
            return LoopReport(
                "failed", steps, f"Could not look at the screen: {error}"
            )

        if isinstance(observation, dict) and observation.get("available") is False:
            return LoopReport("failed", steps,
                              str(observation.get("note") or "Screen observation unavailable."))

        if last_observation is not None and _norm(observation) == last_observation:
            stalls += 1
            if stalls >= max(1, int(stall_limit)):
                return LoopReport(
                    "failed", steps,
                    "The screen stopped changing, so I stopped instead of "
                    "repeating the same action.",
                )
        else:
            stalls = 0
        last_observation = _norm(observation)

        try:
            decision = decide(observation, _decision_history(steps))
        except Exception as error:  # noqa: BLE001
            return LoopReport(
                "failed", steps, f"I could not decide what to do next: {error}"
            )
        if not isinstance(decision, dict):
            return LoopReport(
                "failed", steps, "The next action was not a decision."
            )

        kind = decision.get("kind")
        if kind == DECIDE_DONE:
            steps.append(LoopStep(round_number, "done"))
            return LoopReport("done", steps, str(decision.get("note") or ""))
        if kind == DECIDE_ASK:
            note = str(decision.get("note") or "").strip()
            steps.append(LoopStep(round_number, "ask", note=note))
            return LoopReport(
                "ask", steps,
                note or "I need more information before continuing.",
            )
        if kind != DECIDE_ACT:
            return LoopReport(
                "failed", steps,
                f"The decision kind must be one of {DECIDE_ACT!r}, "
                f"{DECIDE_DONE!r} or {DECIDE_ASK!r}.",
            )

        tool = decision.get("tool")
        arguments = decision.get("arguments") or {}
        if not isinstance(tool, str) or not tool:
            return LoopReport("failed", steps, "No tool was named.")
        if not isinstance(arguments, dict):
            return LoopReport("failed", steps, "The arguments were not an object.")

        if clock() >= deadline:
            return LoopReport("budget", steps, "Time budget exhausted.")

        try:
            result = act(tool, dict(arguments))
        except Exception as error:  # noqa: BLE001
            steps.append(LoopStep(
                round_number, DECIDE_ACT, tool, arguments,
                {"success": False, "code": "raised"},
            ))
            return LoopReport(
                "failed", steps, f"{tool} raised: {error}"
            )

        step = LoopStep(round_number, DECIDE_ACT, tool, arguments, result)
        steps.append(step)
        if on_step is not None:
            try:
                on_step(step)
            except Exception:  # noqa: BLE001 - a reporter must not stop work
                pass

        if isinstance(result, dict):
            if result.get("code") == "confirmation_declined":
                # The user said no: that is a question for them, not a fault.
                return LoopReport(
                    "ask", steps, "You declined that step, so I stopped."
                )
            if result.get("success") is not True:
                # Only explicitly unexecuted validation failures are safe to
                # repair automatically. Never replay an uncertain side effect.
                if (result.get("not_executed") is True
                        and result.get("code") in {"invalid_arguments", "stale_frame", "ungrounded_coordinates"}
                        and repairs < 2):
                    repairs += 1
                    continue
                # One failed action ends the loop: clicking again at the
                # same coordinates is how a mistake compounds.
                return LoopReport(
                    "failed", steps,
                    f"{tool} failed: {result.get('message') or 'no detail'}",
                )

    return LoopReport("rounds", steps, "Reached the round limit.")
