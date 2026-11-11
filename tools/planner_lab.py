"""Empirical planner harness: run many requests against the real local model.

Read-only. It builds the real registry over a throwaway home directory, sends
each request through the real planning prompt and tool list, and records the
plan that comes back. Nothing is executed: the script inspects the plan the
model would run, which is the accuracy question.

Usage:  .venv/bin/python tools/planner_lab.py [--case NAME] [--json out.json]
"""

import argparse
import json
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.agent import (  # noqa: E402
    ACCURACY_GUIDANCE,
    CORE_POLICY,
    PLANNING_INSTRUCTIONS,
    TOOL_SEMANTICS,
    _tool_accuracy_for,
)
from app.brain.memory import MemoryStore  # noqa: E402
from app.brain.planner import TaskPlanner  # noqa: E402
from app.brain.tools import ToolRegistry  # noqa: E402


#: (name, request, expected_good, expected_bad)
#: expected_good: a tool that SHOULD appear; expected_bad: a tool that should
#: NOT be the whole answer (the tool the model wrongly picks).
CASES = [
    # -- reading a file found by description (the reported failure) --------
    ("read-transcript", "read the transcript on my desktop", "read_document",
     "open_last_result"),
    ("read-transcript-plain", "on the desktop there is transcript, please read it",
     "read_document", "open_last_result"),
    ("read-transcript-again", "read the transcript again", "read_document",
     "open_last_result"),
    ("read-notes", "what does my notes file in documents say?", "read_document",
     "open_last_result"),
    ("summarise-invoice", "summarise the invoice from october", "read_document",
     "open_last_result"),
    ("show-not-read", "show me the transcript in the file manager",
     "open_last_result", None),
    ("open-folder", "open my Downloads folder", "open_folder", None),

    # -- searching ---------------------------------------------------------
    ("find-invoice", "find my invoice files", "search_files", None),
    ("find-big", "what is my biggest file in downloads?", "list_files", None),
    ("grep-text", "which file mentions the dentist?", "grep_files", None),

    # -- windows -----------------------------------------------------------
    ("focus-window", "focus the konsole window", "focus_window", None),
    ("list-windows", "what windows are open?", "list_windows", None),

    # -- web ---------------------------------------------------------------
    ("web-search", "search the web for the arch wiki pacman page", "web_search",
     None),
    ("open-url", "open youtube", "open_url", None),

    # -- system / memory ---------------------------------------------------
    ("time", "what time is it?", "get_time", None),
    ("remember", "remember that my dentist is called Anna", "remember", None),
    ("recall", "what is my dentist called?", "recall", None),

    # -- deliberately vague: should ask, not act ---------------------------
    ("vague-file", "read that thing from yesterday", None, "read_document"),
    ("vague-open", "open it", None, None),
]


def build_registry(home, tmp):
    """A real registry scoped to the throwaway home."""
    from app.actions.executor import ActionExecutor
    from app.actions.file_scope import UserFolderResolver

    resolver = UserFolderResolver(home=home, xdg_lookup=lambda _name: None)
    return ToolRegistry(
        executor=ActionExecutor(folder_resolver=resolver),
        folder_resolver=resolver,
        memory_store=MemoryStore(tmp / "memory.sqlite3"),
    )


def plan_once(agent, request):
    """Ask the real model for a plan; return (plan_or_none, raw, error)."""
    # The real entry path runs deterministic shortcuts before the model.
    # Skipping them would "fail" cases the shipped app handles correctly.
    from app.brain.direct_actions import resolve_direct_action

    direct = resolve_direct_action(request)
    if direct is not None:
        tool, arguments = direct
        from app.brain.planner import PlanStep, TaskPlan

        plan = TaskPlan(steps=[PlanStep(
            call_id="direct", name=tool, arguments=dict(arguments),
            description="direct action", position=1,
        )], intent_confidence=1.0)
        return plan, {"response": f"direct: {tool}"}, None

    definitions = agent.registry.available_tools()
    prompt = CORE_POLICY + TOOL_SEMANTICS + PLANNING_INSTRUCTIONS + ACCURACY_GUIDANCE
    data = {
        "user_request": request,
        "current_time": "2026-11-10T12:00:00+01:00",
        "available_tools": definitions,
        "tool_accuracy": _tool_accuracy_for(definitions),
    }
    try:
        raw = agent._chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            # Matches the agent exactly: the tools are described in the
            # payload, and the model answers with JSON rather than calling
            # them. Passing them as callable tools returns tool-call format.
            tools=[],
            response_format={"type": "json_object"},
        )
    except Exception as error:  # noqa: BLE001 - report, never crash the sweep
        return None, None, f"{type(error).__name__}: {error}"
    text = raw.get("content") if isinstance(raw, dict) else None
    if not isinstance(text, str):
        return None, raw, "no content"
    try:
        parsed = json.loads(text)
    except ValueError as error:
        return None, text, f"invalid JSON: {error}"
    try:
        plan = TaskPlanner().prepare_structured_plan(parsed, definitions)
    except Exception as error:  # noqa: BLE001
        return None, parsed, f"invalid plan: {error}"
    return plan, parsed, None


def summarise(plan, parsed):
    if plan is None:
        return {"tools": [], "confidence": None, "clarification": None}
    return {
        "tools": [step.name for step in plan.steps],
        "confidence": plan.intent_confidence,
        "clarification": (plan.clarification or "").strip() or None,
        "response": (parsed or {}).get("response", "")[:120],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    from app.brain.agent import MistyAgent

    tmp = pathlib.Path(tempfile.mkdtemp())
    home = ROOT / ".lab" / "home"
    registry = build_registry(home, tmp)
    agent = MistyAgent(registry=registry)

    cases = [c for c in CASES if not args.case or c[0] in args.case]
    results = []
    for name, request, good, bad in cases:
        plan, parsed, error = plan_once(agent, request)
        info = summarise(plan, parsed)
        tools = info["tools"]
        ok = True
        if error:
            ok = False
        elif good is not None:
            ok = good in tools
        if bad is not None and bad in tools:
            ok = False
        asked = bool(info["clarification"])
        if good is None and bad is not None:
            # A vague request should ask rather than act.
            ok = asked and bad not in tools
        info.update({
            "case": name,
            "request": request,
            "expected_good": good,
            "expected_bad": bad,
            "ok": ok,
            "asked": asked,
            "error": error,
        })
        results.append(info)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name:22} {str(tools):46} conf={info['confidence']}"
              f"{' ASKED' if asked else ''}")
        if error:
            print(f"        error: {error}")
        if info["clarification"]:
            print(f"        asks: {info['clarification'][:100]}")

    passed = sum(1 for r in results if r["ok"])
    print(f"\n{passed}/{len(results)} passed")
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(results, indent=2, ensure_ascii=False)
        )
        print(f"written: {args.json}")


if __name__ == "__main__":
    main()
