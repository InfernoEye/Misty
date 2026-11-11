# Misty Reconsidered: Primitives, a Loop, and One Honest Gate

**Status:** design proposal, not implemented
**Date:** 10 November 2026
**Scope:** how Misty should let the local model operate the desktop

---

## 1. The problem in one paragraph

Misty has **50 advertised tools and 82 policy rows**, and a model whose only
job is to pick one. When the model needs something the tool list did not
anticipate, the request fails — and the fix is always a new tool, a new
policy row, and a new test. In one session this produced, in order: a missing
window ID killed a click; "Use helium" could not be matched to a window;
`discord` could not be matched to Equibop; an unresolved `$steps` template was
searched for as a window name. Each was patched. None was a model error. All
were seams between components that the model was not allowed to cross.

Meanwhile the harness this is written in exposes roughly nine primitives —
`bash`, read, write, edit, glob, grep, fetch, search, subagent — and does
everything with them. It has no `check_ydotool` tool; it runs three shell
commands. It has no `run_tests` tool; it runs the suite. Its safety lives in
the **environment** (a write-bounded sandbox) and **one human approval**, not
in curated tool schemas.

This document proposes applying that shape to Misty.

---

## 2. What is wrong today (measured, not asserted)

| Symptom | Root cause | Where it bit |
|---|---|---|
| "click X" plans `list_windows` → `inspect_screen` → fails | the planner cannot SEE; it plans over facts | every click request |
| `inspect_screen` demanded a window ID | a read-only tool required a handle it did not need | the Dragoncave request |
| `discord` → no window | an alias table lacked a row for the installed client | "in Discord…" |
| "Use helium" → no match | filler words were compared against window names | the clarification loop |
| a retry asked three times | the loop bypassed the task grant | desktop tasks |
| no desktop path at all | routing is a separate model call that answered "direct" | Amazon |

Every row is a **permission or plumbing** problem. Not one is "the model could
not reason about the task."

### The evidence that matters

The model, when finally given the loop, performed correctly: it observed the
screen, picked one action, acted, and observed again. The failures happened
before the loop was reached, or at the boundaries of the curated tools.

Additional evidence, from `tools/planner_lab.py` (real model, 19 requests,
three sweeps):

- 10/19, 10/19, 12/19 — and three cases changed between identical runs.
- Two false-positive guards I wrote (window IDs, CSS selectors) had to be
  retrofitted because the "safety" heuristic blocked legitimate work.
- Hiding three selection tools raised accuracy from 5/8 to 6/8: the tool
  **list** was the problem, not the wording. No prompt fixed it.

**Conclusion: the difficulty is combinatorial surface, not model capability.**

---

## 3. The reframe

> Stop asking "which tool?" and start asking "what do I know, and what would
> tell me more?"

A capable agent needs four things. Misty has them badly or not at all.

### 3.1 Perception the model controls

Today `inspect_screen` is one rigid call, and its arguments were policed hard
enough to kill requests. It should be: *look at the screen, whenever, at
whatever, and get back text plus geometry the model can reason over.*

### 3.2 Primitive actions, not curated compositions

`click(x, y)`, `type(text)`, `key(combo)`, `launch(name)`. The model
composes. Compare:

| | Curated | Primitive |
|---|---|---|
| `open_last_result(index, depends_on)` | 1 tool, 1 policy row, several tests | `click(x,y)` after looking |
| `reveal_in_file_manager(folder, relative_path, mode)` | as above | `key("ctrl+l")` + `type(path)` |
| `search_site(site, query)` | allowlist of 14 sites | `open_url`, `type`, `key("Return")` |

Every composition encodes an assumption. Each assumption is a seam that will
break. Primitives have no assumptions to break.

### 3.3 A scratchpad

Somewhere the model writes *"Dragoncave should be a channel in Discord's left
sidebar; if I don't see it, scroll."* Today each turn forgets — `session_context`
holds one turn, 900 s, and deliberately stores no tool payloads.

### 3.4 Honest feedback

Not `{"success": true}` but *what changed*. "The screen still shows the same
channel list." That is the signal that lets a model self-correct. A validator
saying "invalid argument" teaches nothing.

---

## 4. Proposed architecture

```
                    ┌──────────────────────────────┐
   user request ───►│  ONE approval gate (per task)│
                    └──────────────┬───────────────┘
                                   ▼
        ┌──────────────────────────────────────────────────┐
        │              the loop (bounded)                  │
        │                                                  │
        │   observe ──► scratchpad ──► decide one step     │
        │        ▲                          │              │
        │        │                          ▼              │
        │        └────────── act ◄──────────┘              │
        │                   │                              │
        │                   ▼                              │
        │            honest result                         │
        └──────────────────────────────────────────────────┘
                                   │
                                   ▼
                       ┌───────────────────────┐
                       │  environment floor    │
                       │  (not tool schemas)   │
                       └───────────────────────┘
```

### 4.1 The primitive surface (target: ~10 tools)

| Primitive | What it does | Why |
|---|---|---|
| `see(region?)` | screen text + geometry | perception, model-driven |
| `click(x, y, button)` | pointer | no window, no app |
| `type(text)` | keyboard input | no target validation |
| `key(combo)` | shortcuts | Enter, Ctrl+S, Escape, Super+T |
| `scroll(dx, dy)` | wheel | bring things into view |
| `launch(name)` | any installed app | discovery, not an allowlist |
| `run(command)` | **the `bash` of the desktop** | the escape hatch that makes the rest unnecessary |
| `note(text)` / `recall()` | scratchpad | continuity |
| `ask(question)` | stop and ask the human | the honest failure |

`run()` deserves emphasis. The reason DSH can do anything is not that it has
many tools — it is that `bash` makes every tool optional. The desktop
equivalent is a **raw input channel**: run an arbitrary `ydotool`/DBus
invocation. Everything Misty calls a "desktop tool" becomes a command the
model could have written itself.

The existing 50 tools do not need deleting on day one. They become
**conveniences the model may use**, not the only doors.

### 4.2 The loop

Already exists (`app/brain/agent_loop.py`, 257 lines, 21 tests). What changes:

| Today | Proposed |
|---|---|
| 8 rounds, 180 s | rounds bounded by *progress*, not a count |
| first failure ends the loop | failure means **look differently**, then retry |
| stall limit = same observation | stall means *say what you expected*, then ask |
| report: `{"success": bool}` | report: **what is on screen now** |
| one action per round | one action per round (keep) |

The critical change is **failure is not terminal**. "I clicked and it didn't
work" should lead to "let me scroll and look again," not an error message.

### 4.3 Memory

A bounded scratchpad, written by the model, read by the model:

```
goal    : find a good vacuum on Amazon
learned : vacuum filters: HEPA, bagless, ~250 EUR
tried   : typed query (ok), clicked first result (page didn't load)
plan    : go back, try the second result
```

Bounded (a few hundred tokens), private-tool-proof, cleared at task end.
The existing `session_context.py` is the wrong shape for this — it is
conversational, not task-scoped.

---

## 5. Where safety actually lives

The instinct behind the 82 policy rows is legitimate: a model with `click(x,y)`
can do anything. But the control we needed is not a curated tool list.

### 5.1 Three real layers

| Layer | Mechanism | Already exists? |
|---|---|---|
| **Environment** | bounded filesystem, no network by default, kill switch | partially (`fs_safe`, egress fence) |
| **One gate** | task-level approval, explicit text, once | yes (`task_grant.py`) |
| **Honest reporting** | every action and its outcome is visible | yes (activity stream) |

### 5.2 What to delete

Everything that is friction pretending to be safety:

- window-ID resolution policies (`_hydrate_window_context` and friends)
- app and site allowlists (`APPLICATIONS`, the 14-site `search_site` list)
- per-action confirmation for desktop primitives (the grant covers them)
- argument validators that encode a *strategy* rather than a *bound*
  (the submit-guard heuristic, filler stripping, the accuracy ratings)

The accuracy ratings (`app/brain/accuracy.py`) deserve a note: they gave the
model useful information, but the enforcement built on them produced two
false positives in one afternoon. Keep the table as **advice**; drop the veto.

### 5.3 What to keep — the floor

Non-negotiable, enforced in code, not prompt text:

1. **No purchase, ever, without a separate explicit confirmation.** Whole-phrase
   matching, false-negatives preferred over false-positives.
2. **No destructive file operation** without its own confirmation. Deleting is
   different from clicking; it does not go in the batch.
3. **No credential entry.** The model never types a password or card number it
   read from the screen.
4. **The kill switch.** A shortcut that stops the loop *mid-round*, and a
   visible indication of what it is about to do.
5. **Everything is logged** — the action, the coordinate, the screen text it
   reasoned from.

That is five things, not eighty-two.

---

## 6. Migration plan

Ordered so each step is independently useful and independently testable.

### Phase 0 — verify the current loop (blocking)
Restart Misty. Confirm the loop is reached at all. The failures reported so far
predate the wiring. **Do not design further until the loop is observed running
against the real model.** Everything below assumes it works.

### Phase 1 — make failure recoverable
`agent_loop.py`: a failed action returns to the loop with the observation,
instead of ending it. Bound retries by *distinct* approaches, not by count.
Test: a scripted model that fails twice and succeeds on the third round.

### Phase 2 — `run()` primitive
Expose raw input execution behind the task grant. Measure whether the curated
desktop tools are then ever chosen. Hypothesis: they will not be.

### Phase 3 — scratchpad
Task-scoped, bounded, model-written. Measured by: does a 20-step task still
know its goal?

### Phase 4 — prune
Remove the compositions and validators that the primitives made unnecessary.
Keep what the measurements say is used.

### Phase 5 — re-measure
Re-run `tools/planner_lab.py` against the primitive surface. Target: fewer
tools, equal or better accuracy, stable across runs.

**Stop rule:** if Phase 1 does not measurably improve task completion, the
architecture is not the bottleneck and the remaining phases should be dropped.

---

## 7. Risks and what would falsify this

| Risk | Detection |
|---|---|
| A 4B model cannot plan with primitives either | Phase 5 accuracy drops |
| Removing allowlists enables a genuinely harmful action | the five-item floor fails |
| The loop burns tokens/frames faster than the local model can afford | latency per task |
| Vision is too weak to localise a target from a screenshot | wrong pixels, measured |

**This proposal is falsified if**, after Phase 5, a primitive surface with a
recovering loop scores no better than today's 50-tool surface on the same
19-request benchmark. In that case the limiting factor is model capability, and
the correct next step is a larger model, not more architecture.

---

## 8. The uncomfortable trade

DSH-style means the model can, in principle, click anything on your screen.
There is no GUI sandbox worth the name — you cannot containerise a mouse.

Which is precisely why the honest design is: **one loud approval, then get out
of the way.** Not twenty quiet ones that train you to click yes.

The measure of success is not "how much did we prevent" but **"did the model
finish the task, and would you have caught it if it hadn't"** — and the answer
to the second question is the logging, not the allowlists.
