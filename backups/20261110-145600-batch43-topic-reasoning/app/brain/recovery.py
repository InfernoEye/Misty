"""Deterministic, side-effect-free failure-recovery policy for Misty.

This module decides *whether* a failed task plan may be replanned.  It does
not execute tools, create plans, call a model, read configuration, touch the
desktop or persist anything.  It is deliberately isolated so a future bounded
replanning loop in ``agent.py`` can ask one question:

    "Given this plan and these results, may the loop try one more time?"

Design rules
------------
* **Fail closed.**  Malformed input never raises: it returns a decision with
  ``replan=False`` and ``reason_code="invalid_input"``.  Unknown or
  unregistered tools are treated as irreversible/unknown mutations.
* **Bounded.**  Replans, total steps and consecutive failures all have explicit
  limits.  A decision is a small JSON-safe dictionary of booleans, integers,
  short strings and short lists of validated tool names.
* **No content, ever.**  Tool arguments, result messages, step reasons and
  descriptions are never read, formatted, hashed or echoed.  Only
  ``call_id``, tool name, step status and a handful of boolean result flags
  are inspected.  This keeps private clipboard text, document contents, paths
  and window titles out of recovery logs and prompts.
* **Denied stays denied.**  A user denial or a pending user choice always
  wins: no replan, no automatic retry.
* **Read-only only.**  Automatic retries are limited to policy effects
  ``read_only`` and ``clipboard_read`` that do not require confirmation.  A
  caller-provided ``retryable_tools`` set can narrow that allowlist, but it
  cannot promote a mutating, confirming or unknown tool into an automatic
  retry.  Irreversible effects always require verification.
* **No global mutable state.**  Limits live on a ``RecoveryPolicy`` instance;
  module-level allowlists are immutable frozensets built once from
  :mod:`app.brain.policy`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
import re

from app.brain.planner import TaskPlan
from app.brain.policy import BASELINE_SPECIFICATIONS, policy_for


__all__ = [
    "DEFAULT_RETRYABLE_TOOLS",
    "IRREVERSIBLE_EFFECTS",
    "MAX_LISTED_TOOLS",
    "RETRYABLE_EFFECTS",
    "RecoveryPolicy",
    "summarize",
]


# Effects that must never be retried automatically.  This mirrors the
# irreversible vocabulary frozen in app.brain.policy and adds the two
# "cannot be undone" effects that policy also marks high/confirmation.
IRREVERSIBLE_EFFECTS = frozenset({
    "file_delete",
    "software_install",
    "send_message",
    "upload_private_data",
    "purchase",
    "privileged_setting",
    "memory_delete",
    "unknown_mutation",
})

# Effects safe to repeat: an observation has no side effect to duplicate.
RETRYABLE_EFFECTS = frozenset({"read_only", "clipboard_read"})


# Explicit default allowlist derived from the reviewed policy baseline.  Only
# read-only tools without confirmation qualify; ``tools.py`` is never imported.
DEFAULT_RETRYABLE_TOOLS = frozenset(
    name
    for name, specification in BASELINE_SPECIFICATIONS.items()
    if specification.effect in RETRYABLE_EFFECTS
    and not specification.requires_confirmation
)


MAX_LISTED_TOOLS = 12
MAX_LISTED_STATUSES = 32
MAX_REASON_LENGTH = 240
MAX_MESSAGE_LENGTH = 240
MAX_TOOL_NAME_LENGTH = 64
MAX_CALL_ID_LENGTH = 128
MAX_PLAN_STATUS_LENGTH = 32
MAX_INPUT_STEPS = 256

_TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PLAN_STATUS_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")

_STATUSES = ("success", "failed", "blocked", "skipped", "pending", "running")
_STATUS_ALIASES = {
    "cancelled": "skipped",
    "canceled": "skipped",
    "ambiguous": "failed",
    "ambiguous_selection": "failed",
    "awaiting_user": "failed",
    "awaiting_user_choice": "failed",
    "denied": "failed",
}

_AWAITING_STATUSES = frozenset({
    "ambiguous",
    "ambiguous_selection",
    "awaiting_user",
    "awaiting_user_choice",
})

_EFFECT_RISKS_IRREVERSIBLE = frozenset({"high", "critical"})

_MISSING = object()


class _InvalidInput(Exception):
    """Internal marker: malformed input, converted to a safe decision."""


@dataclass(slots=True)
class _StepRecord:
    call_id: str
    tool: str
    status: str = "pending"
    denied: bool = False
    awaiting_user: bool = False


def _is_tool_name(value):
    return (
        type(value) is str
        and _TOOL_NAME_PATTERN.fullmatch(value) is not None
    )


def _validated_call_id(value):
    if type(value) is not str or not 1 <= len(value) <= MAX_CALL_ID_LENGTH:
        raise _InvalidInput
    for character in value:
        if not character.isprintable() or character.isspace():
            raise _InvalidInput
    return value


def _mapping_value(step, keys):
    if isinstance(step, Mapping):
        for key in keys:
            if key in step:
                return step[key]
        return _MISSING
    return _MISSING


def _step_value(step, keys, attribute):
    value = _mapping_value(step, keys)
    if value is not _MISSING:
        return value
    if not isinstance(step, Mapping):
        return getattr(step, attribute, _MISSING)
    return _MISSING


def _payload(step):
    if isinstance(step, Mapping):
        value = step.get("result")
    else:
        value = getattr(step, "result", None)
    return value if isinstance(value, Mapping) else None


def _payload_flag(payload, names):
    if payload is None:
        return False
    return any(payload.get(name) is True for name in names)


def _entry_flag(entry, names):
    if _payload_flag(entry, names):
        return True
    return any(entry.get(name) is True for name in names)


def _payload_status(payload):
    if payload is None:
        return None
    if payload.get("denied") is True:
        return "failed"
    if (
        payload.get("awaiting_user_choice") is True
        or payload.get("awaiting_user") is True
        or payload.get("ambiguous") is True
    ):
        return "failed"
    if payload.get("blocked") is True:
        return "blocked"
    if payload.get("skipped") is True:
        return "skipped"
    if payload.get("success") is True:
        return "success"
    if payload.get("success") is False:
        return "failed"
    return None


def _normalize_status(value):
    if type(value) is not str:
        raise _InvalidInput
    status = value.strip().lower()
    status = _STATUS_ALIASES.get(status, status)
    if status not in _STATUSES:
        raise _InvalidInput
    return status


def _combine_status(first, second):
    """Combine two status views, letting failure dominate success."""
    if "failed" in (first, second):
        return "failed"
    for status in ("success", "blocked", "skipped", "running", "pending"):
        if status in (first, second):
            return status
    return "pending"


def _record_from_plan_step(step):
    call_id = _step_value(step, ("call_id", "id"), "call_id")
    tool = _step_value(step, ("tool", "name"), "name")
    if call_id is _MISSING or tool is _MISSING:
        raise _InvalidInput
    call_id = _validated_call_id(call_id)
    if not _is_tool_name(tool):
        raise _InvalidInput

    payload = _payload(step)
    denied = _payload_flag(payload, ("denied",))
    awaiting = _payload_flag(
        payload,
        ("awaiting_user", "awaiting_user_choice", "ambiguous"),
    )

    raw_status = _step_value(step, ("status",), "status")
    if raw_status is _MISSING or raw_status is None:
        status = "pending"
    else:
        if (
            type(raw_status) is str
            and raw_status.strip().lower() == "denied"
        ):
            denied = True
        if (
            type(raw_status) is str
            and raw_status.strip().lower() in _AWAITING_STATUSES
        ):
            awaiting = True
        status = _normalize_status(raw_status)

    inferred = _payload_status(payload)
    if inferred is not None:
        status = _combine_status(status, inferred)

    return _StepRecord(
        call_id=call_id,
        tool=tool,
        status=status,
        denied=denied,
        awaiting_user=awaiting,
    )


def _record_from_result(entry, index, known):
    if not isinstance(entry, Mapping):
        raise _InvalidInput

    raw_id = entry.get("id", entry.get("call_id", _MISSING))
    if raw_id is _MISSING or raw_id is None:
        call_id = f"__result_{index}"
        while call_id in known:
            call_id += "_"
    else:
        call_id = _validated_call_id(raw_id)

    tool = entry.get("tool", entry.get("name", _MISSING))
    existing = known.get(call_id)
    if tool is _MISSING or tool is None:
        if existing is None:
            raise _InvalidInput
        tool = existing.tool
    if not _is_tool_name(tool):
        raise _InvalidInput
    if existing is not None and existing.tool != tool:
        raise _InvalidInput

    payload = entry.get("result")
    if not isinstance(payload, Mapping):
        payload = None

    denied = _entry_flag(entry, ("denied",))
    awaiting = _entry_flag(
        entry,
        ("awaiting_user", "awaiting_user_choice", "ambiguous"),
    )
    if type(entry.get("status")) is str:
        raw_status_text = entry["status"].strip().lower()
        if raw_status_text == "denied":
            denied = True
        if raw_status_text in _AWAITING_STATUSES:
            awaiting = True

    raw_status = entry.get("status", _MISSING)
    if raw_status is _MISSING or raw_status is None:
        inferred = _payload_status(payload)
        if inferred is None:
            if denied or awaiting:
                inferred = "failed"
            elif entry.get("blocked") is True:
                inferred = "blocked"
            elif entry.get("skipped") is True:
                inferred = "skipped"
            elif entry.get("success") is True:
                inferred = "success"
            elif entry.get("success") is False:
                inferred = "failed"
            else:
                inferred = "pending"
        status = inferred
    else:
        status = _normalize_status(raw_status)
        inferred = _payload_status(payload)
        if inferred is not None:
            status = _combine_status(status, inferred)

    return _StepRecord(
        call_id=call_id,
        tool=tool,
        status=status,
        denied=denied,
        awaiting_user=awaiting,
    )


def _extract_plan_steps(plan):
    if isinstance(plan, TaskPlan):
        steps = plan.steps
        if type(steps) not in (list, tuple):
            raise _InvalidInput
        return list(steps), plan.status
    if isinstance(plan, Mapping):
        steps = plan.get("steps", [])
        if type(steps) not in (list, tuple):
            raise _InvalidInput
        return list(steps), plan.get("status", "unknown")
    raise _InvalidInput


def _normalize(plan, execution_results):
    plan_steps, plan_status = _extract_plan_steps(plan)
    if type(execution_results) not in (list, tuple):
        raise _InvalidInput
    if len(plan_steps) > MAX_INPUT_STEPS:
        raise _InvalidInput
    if len(execution_results) > MAX_INPUT_STEPS:
        raise _InvalidInput

    records = []
    known = {}
    for step in plan_steps:
        record = _record_from_plan_step(step)
        if record.call_id in known:
            raise _InvalidInput
        known[record.call_id] = record
        records.append(record)

    for index, entry in enumerate(execution_results):
        incoming = _record_from_result(entry, index, known)
        existing = known.get(incoming.call_id)
        if existing is None:
            known[incoming.call_id] = incoming
            records.append(incoming)
            continue
        existing.denied = existing.denied or incoming.denied
        existing.awaiting_user = (
            existing.awaiting_user or incoming.awaiting_user
        )
        existing.status = _combine_status(
            existing.status, incoming.status
        )

    return records, plan_status


def _bounded(value, limit):
    if type(value) is not str:
        return ""
    text = " ".join(value.split())
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _unique_tools(records, status):
    return tuple(sorted({
        record.tool for record in records if record.status == status
    }))[:MAX_LISTED_TOOLS]


def _failed_tools(records):
    return _unique_tools(records, "failed")


def _blocked_tools(records):
    return _unique_tools(records, "blocked")


def _budget_counts(records):
    executed = sum(
        record.status in {"success", "failed", "running"}
        for record in records
    )
    remaining = sum(record.status == "pending" for record in records)
    return executed, remaining


def _consecutive_failures(records):
    count = 0
    for record in reversed(records):
        if record.status == "failed":
            count += 1
        elif record.status in {"blocked", "skipped", "pending"}:
            continue
        else:
            break
    return count


def _attempt_counts(attempted_tools, failed_records):
    """Return authoritative attempt counts per failed tool.

    ``attempted_tools`` may be a mapping of tool name to a non-negative count
    (counts include the failure recorded here) or an iterable of tool names.
    When a failed tool is absent from a supplied mapping, the failures visible
    in ``execution_results`` are used instead, so duplicate results still
    count as repetition.
    """
    failure_counts = Counter(record.tool for record in failed_records)
    if attempted_tools is None:
        return dict(failure_counts)
    if isinstance(attempted_tools, Mapping):
        counts = {}
        if len(attempted_tools) > MAX_INPUT_STEPS:
            raise _InvalidInput
        for tool, count in attempted_tools.items():
            if not _is_tool_name(tool):
                raise _InvalidInput
            if type(count) is not int or count < 0:
                raise _InvalidInput
            counts[tool] = count
        # A caller may report only the tools it tracked; fill the rest from
        # the failures visible in execution_results.
        for tool, count in failure_counts.items():
            counts.setdefault(tool, count)
        return counts
    if type(attempted_tools) not in (list, tuple, set, frozenset):
        raise _InvalidInput
    if len(attempted_tools) > MAX_INPUT_STEPS:
        raise _InvalidInput
    counts = Counter()
    for tool in attempted_tools:
        if not _is_tool_name(tool):
            raise _InvalidInput
        counts[tool] += 1
    return dict(counts)


def _coerce_tool_set(value):
    if value is None:
        return frozenset()
    if type(value) not in (list, tuple, set, frozenset):
        raise _InvalidInput
    if len(value) > MAX_INPUT_STEPS:
        raise _InvalidInput
    for tool in value:
        if not _is_tool_name(tool):
            raise _InvalidInput
    return frozenset(value)


def _budget_int(name, value, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}.")
    return value


def _empty_budget(
    max_replans,
    max_total_steps,
    max_consecutive_failures,
    replans_used=0,
    executed=0,
    remaining=0,
    consecutive=0,
):
    return {
        "max_replans": max_replans,
        "replans_used": replans_used,
        "replans_remaining": max(0, max_replans - replans_used),
        "max_total_steps": max_total_steps,
        "executed_steps": executed,
        "remaining_steps": remaining,
        "projected_total_steps": executed + remaining,
        "within_replan_budget": replans_used < max_replans,
        "within_step_budget": executed + remaining < max_total_steps,
        "max_consecutive_failures": max_consecutive_failures,
        "consecutive_failures": consecutive,
    }


def _decision(
    *,
    replan,
    reason_code,
    reason,
    failed_tools=(),
    retryable_tools_=(),
    blocked_tools=(),
    denied=False,
    remaining_steps=0,
    budget,
    requires_user=False,
    verification_required=False,
    message,
):
    return {
        "replan": bool(replan),
        "reason_code": reason_code,
        "reason": _bounded(reason, MAX_REASON_LENGTH),
        "failed_tools": list(failed_tools)[:MAX_LISTED_TOOLS],
        "retryable_tools": list(retryable_tools_)[:MAX_LISTED_TOOLS],
        "blocked_tools": list(blocked_tools)[:MAX_LISTED_TOOLS],
        "denied": bool(denied),
        "remaining_steps": int(remaining_steps),
        "budget": dict(budget),
        "requires_user": bool(requires_user),
        "verification_required": bool(verification_required),
        "message": _bounded(message, MAX_MESSAGE_LENGTH),
    }


class RecoveryPolicy:
    """Bounded, deterministic policy for deciding whether to replan.

    ``decide`` never raises for caller-provided data.  It returns a stable
    JSON-safe decision dictionary with these keys::

        replan, reason_code, reason, failed_tools, retryable_tools,
        blocked_tools, denied, remaining_steps, budget, requires_user,
        verification_required, message

    ``retryable_tools`` (when supplied to ``decide``) replaces the default
    read-only allowlist but cannot promote a mutating, confirming, unknown or
    irreversible tool.  ``irreversible_tools`` adds caller-known irreversible
    names.  ``attempted_tools`` counts include the current failure.
    """

    def __init__(
        self,
        max_replans=1,
        max_total_steps=24,
        max_consecutive_failures=3,
    ):
        self.max_replans = _budget_int("max_replans", max_replans, 0)
        self.max_total_steps = _budget_int(
            "max_total_steps", max_total_steps, 1
        )
        self.max_consecutive_failures = _budget_int(
            "max_consecutive_failures", max_consecutive_failures, 1
        )

    def __repr__(self):
        return (
            "RecoveryPolicy("
            f"max_replans={self.max_replans}, "
            f"max_total_steps={self.max_total_steps}, "
            "max_consecutive_failures="
            f"{self.max_consecutive_failures})"
        )

    # -- public API ------------------------------------------------------
    def decide(
        self,
        plan,
        execution_results,
        *,
        replans_used=0,
        denied=False,
        awaiting_user_choice=False,
        attempted_tools=None,
        retryable_tools=None,
        irreversible_tools=None,
    ):
        try:
            return self._decide(
                plan,
                execution_results,
                replans_used=replans_used,
                denied=denied,
                awaiting_user_choice=awaiting_user_choice,
                attempted_tools=attempted_tools,
                retryable_tools=retryable_tools,
                irreversible_tools=irreversible_tools,
            )
        except _InvalidInput:
            return self._invalid_decision(
                replans_used=0, executed=0, remaining=0
            )
        except Exception:
            # Defensive backstop: recovery must never take the agent down.
            return self._invalid_decision(
                replans_used=0, executed=0, remaining=0
            )

    def _invalid_decision(self, *, replans_used, executed, remaining):
        return _decision(
            replan=False,
            reason_code="invalid_input",
            reason=(
                "The recovery inputs were malformed or inconsistent, so no "
                "replan is allowed."
            ),
            failed_tools=(),
            retryable_tools_=(),
            blocked_tools=(),
            denied=False,
            remaining_steps=remaining,
            budget=_empty_budget(
                self.max_replans,
                self.max_total_steps,
                self.max_consecutive_failures,
                replans_used=replans_used,
                executed=executed,
                remaining=remaining,
            ),
            requires_user=False,
            verification_required=False,
            message=(
                "Recovery input was invalid. Misty stops without retrying."
            ),
        )

    def _decide(
        self,
        plan,
        execution_results,
        *,
        replans_used,
        denied,
        awaiting_user_choice,
        attempted_tools,
        retryable_tools,
        irreversible_tools,
    ):
        if type(replans_used) is not int or replans_used < 0:
            raise _InvalidInput
        if type(denied) is not bool:
            raise _InvalidInput
        if type(awaiting_user_choice) is not bool:
            raise _InvalidInput

        allowed = (
            DEFAULT_RETRYABLE_TOOLS
            if retryable_tools is None
            else _coerce_tool_set(retryable_tools)
        )
        caller_irreversible = _coerce_tool_set(irreversible_tools)

        records, _plan_status = _normalize(plan, execution_results)

        failed_records = [
            record for record in records if record.status == "failed"
        ]
        attempt_counts = _attempt_counts(attempted_tools, failed_records)

        failed_tools = _failed_tools(records)
        blocked_tools = _blocked_tools(records)
        executed, remaining = _budget_counts(records)
        consecutive = _consecutive_failures(records)

        effective_denied = denied or any(
            record.denied for record in records
        )
        effective_awaiting = awaiting_user_choice or any(
            record.awaiting_user for record in records
        )

        # Classify every failed tool before looking at budgets, so
        # verification_required reflects the real failure even when a budget
        # independently stops the loop.
        retryable = []
        irreversible = []
        non_retryable = []
        for record in failed_records:
            specification = policy_for(record.tool)
            if (
                record.tool in caller_irreversible
                or specification is None
                or specification.requires_confirmation
                or specification.effect in IRREVERSIBLE_EFFECTS
                or specification.risk in _EFFECT_RISKS_IRREVERSIBLE
            ):
                irreversible.append(record.tool)
            elif (
                specification.effect in RETRYABLE_EFFECTS
                and record.tool in allowed
            ):
                retryable.append(record.tool)
            else:
                non_retryable.append(record.tool)

        retryable_tools_failed = tuple(sorted(set(retryable)))
        needs_verification = bool(irreversible or non_retryable)
        needs_user = bool(irreversible or non_retryable)

        budget = _empty_budget(
            self.max_replans,
            self.max_total_steps,
            self.max_consecutive_failures,
            replans_used=replans_used,
            executed=executed,
            remaining=remaining,
            consecutive=consecutive,
        )

        base = {
            "failed_tools": failed_tools,
            "retryable_tools_": retryable_tools_failed,
            "blocked_tools": blocked_tools,
            "remaining_steps": remaining,
            "budget": budget,
        }

        # 1. Denial and pending user choice always win.
        if effective_denied:
            return _decision(
                replan=False,
                reason_code="denied",
                reason=(
                    "The user denied an action, and a denied action is never "
                    "retried automatically."
                ),
                denied=True,
                requires_user=True,
                verification_required=False,
                message=(
                    "The action was denied. Misty will not retry it."
                ),
                **base,
            )

        if effective_awaiting:
            return _decision(
                replan=False,
                reason_code="awaiting_user_choice",
                reason=(
                    "A user choice is still pending, so the plan will not "
                    "continue or replan automatically."
                ),
                denied=effective_denied,
                requires_user=True,
                verification_required=False,
                message=(
                    "Misty is waiting for your choice and will not continue "
                    "automatically."
                ),
                **base,
            )

        # 2. No independent failure.  Blocked-only plans land here too: a
        # dependency blocker by itself is not a reason to replan.
        if not failed_records:
            return _decision(
                replan=False,
                reason_code="no_failure",
                reason=(
                    "No step failed independently, so there is nothing to "
                    "recover and no replan is needed."
                ),
                requires_user=False,
                verification_required=False,
                message="No action failed. No replan is needed.",
                **base,
            )

        # 3. Budgets.  They are checked before tool classification so a
        # bounded loop cannot exceed its hard limits for any reason.
        if replans_used >= self.max_replans:
            return _decision(
                replan=False,
                reason_code="replan_budget",
                reason=(
                    "The replan budget is exhausted, so the failed plan will "
                    "not be replanned."
                ),
                requires_user=needs_user,
                verification_required=needs_verification,
                message=(
                    "The replan limit is used up. Misty will stop without "
                    "retrying."
                ),
                **base,
            )

        if executed + remaining >= self.max_total_steps:
            return _decision(
                replan=False,
                reason_code="step_budget",
                reason=(
                    "Executed plus remaining steps already reach the total "
                    "step budget, so no replan is allowed."
                ),
                requires_user=needs_user,
                verification_required=needs_verification,
                message=(
                    "The step limit would be exceeded. Misty will stop "
                    "without retrying."
                ),
                **base,
            )

        # 4. Irreversible or unsafe-to-retry effects.
        if irreversible:
            return _decision(
                replan=False,
                reason_code="irreversible_failure",
                reason=(
                    "A sensitive or irreversible action failed. It will not "
                    "be retried automatically; verify the real outcome first."
                ),
                requires_user=True,
                verification_required=True,
                message=(
                    "A sensitive action failed. Misty will not retry it "
                    "automatically; please check the result."
                ),
                **base,
            )

        if non_retryable:
            return _decision(
                replan=False,
                reason_code="not_retryable",
                reason=(
                    "The failed action is not an idempotent read-only action, "
                    "so it will not be retried automatically."
                ),
                requires_user=True,
                verification_required=True,
                message=(
                    "The failed action is not safe to retry automatically. "
                    "Misty will stop and verify instead."
                ),
                **base,
            )

        # 5. Repetition of the same tool means the retry has already been
        # tried; do not loop on it again.
        repeated = [
            tool
            for tool in failed_tools
            if attempt_counts.get(tool, 0) >= 2
        ]
        if repeated:
            return _decision(
                replan=False,
                reason_code="repeated_failure",
                reason=(
                    "A tool has already failed repeatedly, so it will not be "
                    "retried again."
                ),
                requires_user=False,
                verification_required=False,
                message=(
                    "The same action keeps failing. Misty will not retry it "
                    "again."
                ),
                **base,
            )

        # 6. Consecutive-failure ceiling.
        if consecutive >= self.max_consecutive_failures:
            return _decision(
                replan=False,
                reason_code="failure_limit",
                reason=(
                    "Too many consecutive failures occurred, so the bounded "
                    "loop stops instead of replanning."
                ),
                requires_user=False,
                verification_required=False,
                message=(
                    "Too many actions failed in a row. Misty will stop here."
                ),
                **base,
            )

        # 7. Every independent failure is a retryable read-only action.
        return _decision(
            replan=True,
            reason_code="retryable_failure",
            reason=(
                "Only retryable read-only actions failed, so one bounded "
                "replan is allowed."
            ),
            requires_user=False,
            verification_required=False,
            message=(
                "A bounded replan may retry the failed read-only action."
            ),
            **base,
        )


def summarize(plan, execution_results=None):
    """Return bounded, JSON-safe counts and statuses for diagnostics.

    Invalid input returns ``valid=False`` with an ``invalid_input`` reason
    instead of raising.  The helper reads no arguments or result messages.
    """
    empty_counts = {status: 0 for status in _STATUSES}
    try:
        if execution_results is None:
            execution_results = []
        records, plan_status = _normalize(plan, execution_results)
        counts = {status: 0 for status in _STATUSES}
        for record in records:
            counts[record.status] += 1
        executed, remaining = _budget_counts(records)
        statuses = [record.status for record in records][:MAX_LISTED_STATUSES]
        return {
            "valid": True,
            "reason_code": "ok",
            "plan_status": _safe_plan_status(plan_status),
            "total_steps": len(records),
            "status_counts": counts,
            "statuses": statuses,
            "executed_steps": executed,
            "remaining_steps": remaining,
            "failed_tools": list(_failed_tools(records)),
            "blocked_tools": list(_blocked_tools(records)),
            "denied": any(record.denied for record in records),
            "awaiting_user_choice": any(
                record.awaiting_user for record in records
            ),
        }
    except Exception:
        return {
            "valid": False,
            "reason_code": "invalid_input",
            "plan_status": "unknown",
            "total_steps": 0,
            "status_counts": empty_counts,
            "statuses": [],
            "executed_steps": 0,
            "remaining_steps": 0,
            "failed_tools": [],
            "blocked_tools": [],
            "denied": False,
            "awaiting_user_choice": False,
        }


def _safe_plan_status(value):
    if (
        type(value) is str
        and len(value) <= MAX_PLAN_STATUS_LENGTH
        and _PLAN_STATUS_PATTERN.fullmatch(value) is not None
    ):
        return value
    return "unknown"
