# Native KWin visual pilot

## Scope

This is an **explicitly selected foreground pilot**, not a hidden fallback from Cua and not Cua's target-bound background input. The Cua adapter remains read-only. The default backend remains `legacy`.

The native pilot connects the existing KWin/context and trusted visible-region capture to the local [visual decision runtime](../app/brain/visual_decision.py). The deciding model receives the actual PNG plus current OCR target tokens and a strict schema. It does not receive an OCR caption in place of the image.

Supported first-stage action: one left click on a visible OCR word token. No arbitrary model coordinates, keyboard input, typing, scrolling, shell execution or form submission is exposed. Icons without OCR targets and ambiguous/overlapped windows require clarification rather than guessed positions.

Implementation: [provider](../app/desktop/providers/kwin.py), [controller](../app/brain/kwin_controller.py), [in-process ownership](../app/desktop/ownership.py).

## Using the pilot from the agent

With the `kwin` backend selected, requests the router classifies as desktop work ("Click Dragon Cave in Discord", "Open Discord and click the settings icon") are handed to the native controller.

- `MISTY_KWIN_INPUT` defaults to `1`; export `MISTY_KWIN_INPUT=0` for a strict preview-only run. Enabled never means unattended: every click still needs its own approval showing the OCR text and window.
- Window-only requests (focus, minimize, maximize, restore, close, move) never enter this path. They use their window tools deterministically, so the interpreter's approval policy and verified outcomes still apply.
- While the pilot owns input, direct pointer/typing tool dispatch is refused with `desktop_busy`; nothing is replayed or rerouted.
- Sensitive wording in the request or the clicked control stops before approval is even requested.

If the higher-priority interpreter denies a window action, that is intended: make an explicit request (for example "close Discord") and answer the approval prompt.

## Preview-only mode (strict)

Stop the existing Misty process before starting a new instance. Do not run competing heavyweight local models.

```bash
MISTY_DESKTOP_BACKEND=kwin MISTY_KWIN_INPUT=0 .venv/bin/python misty.py
```

This strict preview mode uses the same visual pipeline but never dispatches a click. Without `MISTY_KWIN_INPUT=0`, the pilot runs with per-click approval (see below). A clear visual interaction request resolves a single target window, requests permission to observe it, captures its visible region, performs OCR and sends the image/tokens to Qwen3VL. A valid proposed click is reported as a preview; no click is dispatched.

Target scope comes from what you actually named, using the same shared window resolver and catalog reasoning as the structured tools:

- an application named for content work ("Click Dragon Cave in Discord", "Open Discord and click …", "Search in Firefox for tickets"); a destination may additionally pin the screen;
- otherwise the window that was in front when you asked.

A named application that cannot be found, or an ambiguous match, stops with a clarification; the pilot never substitutes another window. Supported first-stage action is one approved left click on a visible OCR word token. Browsing by screen position without any named app is not yet a supported target.

Before each observation, the agent's resident Qwen server is stopped and actual liveness is checked — including in the standalone `misty.py` launcher, not only voice-controller runs — so two local models never intentionally share VRAM.

Existing structured tools, such as moving a window to another screen, do not use this visual loop. They keep their normal policy and direct/planner execution path, irrespective of the selected visual backend.

## Input with per-click approval

Run previews first. Then use a harmless controlled application whose visible control changes benign local state. Selecting `kwin` enables input admission; this is not proof that live acceptance passed:

```bash
MISTY_DESKTOP_BACKEND=kwin .venv/bin/python misty.py
```

Every click still requires a separate approval displaying its actual OCR text, window and image box. A model cannot supply an `approved` field or coordinates in the tool arguments. The provider accepts only the one-use token stored by the trusted host.

Keep the selected target in the foreground and unoccluded. The provider never focuses another application automatically. It conservatively refuses any intersecting visible foreign window, including Misty's popup; if necessary, use a target window/screen whose region does not overlap the popup. Without trustworthy stacking data it can refuse a window behind the target too. This favors refusal over clicking an unintended surface.

The popup now requests no keyboard focus and no activation. Mouse confirmation/cancel buttons still emit their normal signals. Actual compositor focus behavior still needs live validation; the provider does not infer focus from popup behavior.

## Click admission and freshness

Before dispatch the provider:

1. Validates the exact window ID, PID, geometry, screen identity, current desktop and foreground state.
2. Rejects minimized, unavailable or overlapping targets.
3. Recaptures the same visible crop and requires its image digest to equal the approved image.
4. Resolves the stored OCR-word centre through the **fresh** trusted capture frame exactly once, including negative monitor origins.
5. Rechecks target/focus, cancellation and deadline, then consumes the entire observation before one canonical left click.

A later external observation invalidates all older tokens. Returned model metadata cannot change the private mapping. Any uncertain input result stops the task; it is not replayed. Repeated proposals for the same already-delivered text/position are refused rather than double-clicked blindly.

This is not an atomic OS transaction: human input or another application can change focus between the final check and input delivery. Do not use the pilot for payments, deletion, credential handling, messaging or security changes. Keyword guards are extra conservative refusal checks, not a complete semantic safety boundary; every actual click remains explicitly approved. Use a controlled foreground test application first.

Animated/dynamic target contents can fail the exact image-digest check. That is a refusal, not permission to relax freshness or silently reuse old coordinates. Mixed-DPI/ambiguous image mappings also refuse.

## Model and approval ownership

The request owner releases its text model through the existing controller hook before starting the visual runtime. It checks actual reasoning-process liveness, not the hook's boolean return. Every visual inference session is closed **before** requesting spoken/button approval, so confirmation Whisper never intentionally overlaps it. A later visual check starts a new owned runtime; this can be slower than a persistent visual server.

Unconfirmed model cleanup or input outcome blocks Misty's pointer admission for the rest of that process, including legacy dispatch. Inspect the desktop and stop any remaining owned model process before restarting Misty. There is no automatic retry or backend switch to evade that block.

Cancellation/deadlines are checked around synchronous capture/OCR and input admission. Those services retain their own timeouts; they are not advertised as instantly preemptible. Completed input is never rolled back.

## Honest outcomes

A delivered click is not independent proof of the requested application state. After each successful dispatch, the controller obtains a fresh observation and lets the visual model reassess. Model-reported completion is qualified as **not independently verified**, with the actual delivered count. A stale/missing observation ID cannot establish completion.

The legacy desktop loop similarly no longer treats generated `done` prose as verified success for an operational request. Read-only discovery alone cannot count as executing a move/click.

## Structured window-move regression

`Move Discord to the main screen.` now builds a narrow deterministic plan, runs the normal validator/resolver/policy/executor and formats the actual verified result without classification/planning/final-prose model calls. Negated, file-oriented, ambiguous and compound commands are not forced into this shortcut.

## Window names are not IDs

Every window-control tool now treats a planner/shortcut `window_id` as **possibly user language** until proven otherwise. A value is kept only when it is the exact ID of one live window; otherwise the user's phrase is resolved through the shared catalog path. Suffix naming for this rule must never be assumed: capitalization from a transcript does not disable identity matching, and reverse-DNS identities (`org.kde.dolphin`), `.desktop` files and non-branding window titles are matched through application identity rather than a hardcoded application list.

When direct identity/title matching finds nothing, the local model may read the bounded live catalog (identities, titles, descriptions; not focus state) and propose one observed window ID. Its proposal is rechecked for existence and identity (PID/app metadata) before execution. Ambiguity asks in the existing selection flow and never picks the first/active/sole window. An unavailable catalog is a service error, not "not found". This is what lets arbitrary applications — Dolphin, Firefox, or an unfamiliar app the user describes — work without special cases.

The KWin move service queries fresh window state after acknowledging the move. It reports a verified move, a verified already-correct window, or failure/uncertainty. It never claims that `sendClientToScreen` being called proves the requested output was reached. `main` means KDE's configured priority-1 output, **not necessarily the physically central monitor**. Missing/duplicate primary metadata remains a clarification/failure, not an arbitrary choice.

## Validation limits

Final full regression discovery ran **2,418 tests successfully (one skipped)**; resource-cleanup warnings remain in some tests. Routing coverage includes ordinary tool categories and compound structured-tool sequences, mixed GUI requests, negative paths and contradictory model summaries. A read-only KScreen metadata check reported DP-1 as priority 1, DP-3 as priority 2 and DP-2 as priority 3 at validation time; no window was moved by that check.

All new provider/controller/routing/popup and move-verification tests use fake inputs/windows/models or offscreen widgets. No real desktop click, window move, snapshot or heavyweight model was launched for this implementation. Live acceptance and latency/grounding remain outstanding. The scripts for [read-only capabilities](DESKTOP_V2_CAPABILITIES.md) and [visual-runtime readiness](VISUAL_RUNTIME_CHECK.md) remain available as separate checks.
