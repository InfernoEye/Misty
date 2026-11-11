# Desktop autonomy audit

## Scope

Source audit of desktop request routing, observation, model decisions, execution and feedback. No live clicks or model-driven desktop test was performed. The findings table records the original audit; implementation progress follows below. This is not a claim of verified unattended operation.

## Findings

| Priority | Finding | Evidence | Status |
|---|---|---|---|
| P1 | Visual requests can fall into the static planner when routing fails. Two unsuccessful classifier attempts return False rather than using the existing screen-reference fallback. | [task_grant.py](../app/brain/task_grant.py#L141-L161), [agent.py](../app/brain/agent.py#L2328-L2334) | Open |
| P1 | Desktop routing happens before the run method starts Qwen. `_chat` itself does not ensure startup. A prewarmed model can hide this dependency. | [agent.py](../app/brain/agent.py#L2224-L2248), [agent.py](../app/brain/agent.py#L2328-L2367) | Open |
| P1 | Vision explicitly releases the reasoning model; the desktop decider subsequently calls `_chat` without ensuring it has restarted. | [coordinator.py](../app/desktop/vision/coordinator.py#L94-L104), [main.py](../app/main.py#L211-L225), [desktop_task.py](../app/brain/desktop_task.py#L227-L234) | Open |
| P1 | Decider receives tool names, not argument schemas, and uses `tools=[]`. It must guess argument names and contracts. | [desktop_task.py](../app/brain/desktop_task.py#L207-L234) | Open |
| P1 | OCR image coordinates are offered as click positions without preserving capture origin/scale. Click dispatch uses x/y directly, ignoring a screen argument. | [desktop_task.py](../app/brain/desktop_task.py#L136-L170), [tools.py](../app/brain/tools.py#L3005-L3032) | Open |
| P1 | Loop history removes action arguments and most tool-result content, so discovery results and actionable errors do not reach the next decision. | [agent_loop.py](../app/brain/agent_loop.py#L66-L80), [agent_loop.py](../app/brain/agent_loop.py#L185-L186) | Open |
| P2 | Observation is generic rather than goal-directed, truncated to 60 OCR rows and 4000 rendered characters. Same-row grouping can mix unrelated controls. | [desktop_task.py](../app/brain/desktop_task.py#L26-L29), [desktop_task.py](../app/brain/desktop_task.py#L103-L132), [desktop_task.py](../app/brain/desktop_task.py#L312-L324) | Open |
| P2 | One failed action terminates the loop, even a read-only lookup or recoverable argument error. Unchanged observations stop it before it can reconsider. | [agent_loop.py](../app/brain/agent_loop.py#L173-L180), [agent_loop.py](../app/brain/agent_loop.py#L243-L255) | Open |
| P1 | Completion is accepted from the model without independent evidence. Prompt also permits `done` when nothing more can be done safely, conflating completion and safe stopping. | [desktop_task.py](../app/brain/desktop_task.py#L55-L57), [agent_loop.py](../app/brain/agent_loop.py#L196-L199) | Open |

The static planner explicitly instructs named-application inspection to use a window capture: [agent.py](../app/brain/agent.py#L690-L693). This explains the window-inspection choice; it is not evidence that vision has continuous desktop access.

The currently inspected handler already broadens an absent window ID to a screen/desktop capture: [tools.py](../app/brain/tools.py#L4374-L4398). Therefore the reported missing-ID error needs fresh runtime tracing before assigning its exact current origin.

## Implementation progress

- **Implemented:** explicit-screen routing fallback after bounded classifier failure; reasoning readiness at every real chat transport call, including after vision releases Qwen.
- **Implemented:** full tool schemas in desktop decision context, tool-name availability enforcement, bounded private action/result history (public reports remain redacted), and goal-directed visual prompts.
- **Implemented:** trusted capture frame IDs, image-local coordinate contract, signed desktop translation once at execution, bounds/layout/age checks, crop-window geometry checks, immutable stored transforms, monitor geometry metadata, and explicit rejection of unsupported pointer `screen` arguments.
- **Implemented:** desktop decider binds coordinate actions to its actual grounded observation; unavailable/ungrounded observations cannot silently generate raw global clicks. Distant OCR columns are split rather than assigned one empty-space click centre.
- **Implemented:** up to two repairs only for explicitly unexecuted argument/frame failures. Uncertain side effects still stop immediately. Capture IDs/timestamps do not masquerade as visual progress.
- **Partially addressed:** success prompt now requires visible evidence and distinguishes asking from completion. Independent semantic completion verification is still future work.
- **Limits:** coordinate grounding currently supports validated 1:1 pixel/logical mappings. Mixed-DPI/ambiguous mappings and native-window decoration transforms are refused, not guessed. Frames expire after 120 seconds to accommodate local VLM and reasoning reload latency; this and layout checks do not establish that UI contents stayed unchanged. Legacy manually approved no-frame global-coordinate calls remain available outside the grounded desktop decider.
- **Limits:** task approval remains mandatory. Label-based sensitive-control checks are not a complete semantic security boundary. Do not treat this release as safe unattended operation on purchases, deletions, credentials or submissions.
- **Remaining:** automatic monitor-specific observation selection, element-level VLM grounding, explicit UI-settle detection, stronger semantic verification, and a real controlled-application/Discord acceptance test. Whole-desktop observation remains the loop default, though callers can supply screen/window observation targets.

## Follow-up: stale Listening state during desktop approval

The newly exercised desktop approval path exposed a voice-confirmation state bug: releasing confirmation audio stopped the microphone but did not leave `listening`, and confirmation recording did not acquire the normal watchdog. Clearing the confirmation card also did not restore the popup state. Saved activity metadata showed approval and a later successful input event while the popup could still claim Listening; input success does not establish target correctness.

The controller now transitions confirmation recording through transcription and back to approval/processing, restores the approval state on unclear/error input, bounds held confirmation recordings, and closes recording ownership when the prompt resolves. Regression tests cover these paths without real microphone/model/input use. This diagnosis does not rule out a separate shortcut/helper or microphone issue if normal recording itself never releases. After this repair, full discovery ran 2,138 tests successfully (one skipped); all seven focused confirmation-state regressions also passed, including the subsequently added held-recording approval/denial/timeout case. Live popup behavior still requires restarting the running application to load the changes.

## Validation

Final compilation of the seven modified runtime modules succeeded. Full unittest discovery ran 2,132 tests: 2,131 passed, one skipped, exit code 0. Resource-cleanup warnings remain in tests. A partial voice-controller fixture was corrected to stub its recording handoff rather than accidentally launching the real request worker. All new capture/input tests use fake images and fake input backends; no desktop clicks, real model cycle or live Discord acceptance test was performed.

## Proposed implementation order

1. **Reliable entry and model lifecycle.** Start/ensure reasoning before routing and every decision after vision releases it. Route explicit visual interaction into the desktop loop even after classifier errors. If desktop control is unavailable, explain the missing capability instead of generating an inspection-only action plan. Log selected route, reason and capabilities without screenshot contents.
2. **Observation contract and coordinates.** Give each capture an observation ID, timestamp, monitor identity, image dimensions, desktop origin and explicit image-to-input transform. Preserve these through OCR and VLM. Accept clicks against observation IDs and grounded element IDs or image coordinates; validate freshness/bounds and transform server-side. Test right/left monitors, negative origins, mixed DPI and cropped windows before enabling unattended clicks.
3. **Useful model context.** Supply actual allowed tool schemas, structured bounded prior results and errors. Keep the goal in the observation prompt. Prefer accessibility element bounds; use OCR for text and VLM grounding for icons/non-text targets. Do not assume that a prose visual description supplies accurate coordinates.
4. **Adaptive loop.** Observe, choose one grounded action, execute, wait for UI settling, then observe and verify. Permit bounded repair of read-only/validation failures. Never blindly replay a possibly completed side effect. Track attempted action plus observed outcome, not just whether pixels changed.
5. **Task-scoped permissions.** Keep one approval per bounded desktop task and explicit sensitive-action gates. Screen/page text is untrusted data. Label-based purchase blocking is not a complete security boundary: a coordinate click with an omitted/misleading label can evade semantic intent checks. Require fresh approval for sensitive submissions and enforce policy at execution.
6. **Acceptance tests and capability reporting.** First validate a real capture and input backend without clicking. Then use a controlled test application for clicks, focus, scrolling and typing. Finally test the requested Discord target on a selected monitor, requiring post-action evidence before reporting success. Expose whether observation used OCR, VLM or both; do not silently call OCR-only mode full visual understanding.

## Example desired behavior

“Click Dragon Cave on the right screen” → enumerate monitor geometry → capture the selected right-hand monitor → locate the requested element (not merely a window with a similar title) → resolve ambiguity if needed → transform grounded target to desktop input coordinates → click once under task approval → capture again → verify the expected navigation happened.

Desktop-wide capture should be a deliberate observation scope, not permission to read every hidden window or click arbitrary targets. Window capture remains useful for clarity and privacy, but must not be an unnecessary prerequisite for a monitor-targeted request.
