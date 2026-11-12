# Desktop Agent v2 — implementation design

Status: proposed; design only. No dependencies installed, runtime replaced, or desktop actions performed for this design.

## 1. Decision

Use the DSH Next computer-use pattern: one driver-owned desktop interface, real image-bearing observations, exact tool schemas, and an iterative model/tool loop. Keep Misty's voice and UI. Do not embed the DSH Electron application or copy its entire TypeScript plugin system.

Preferred controller: the existing local Ministral 3 8B model, acting as the GUI decision-maker rather than a caption generator. Keep text Qwen for ordinary non-GUI requests. Prefer a persistent task-scoped multimodal llama.cpp server if its image API and structured output are proven by an integration test. Do not assume the current vision CLI supports that contract.

Preferred provider boundary: a managed, version-pinned Cua `mcp` subprocess using its supported stdio protocol. DSH embeds the native SDK in process; choosing MCP for Python Misty reuses the driver's public contracts while isolating native crashes and avoiding a Node bridge. The exact executable/distribution and Linux support must be verified before selecting a dependency. If no supported Linux transport exists, stop at capability diagnosis rather than silently revive raw-coordinate automation.

Evidence and comparisons: [DSH replacement investigation](DSH_NEXT_DESKTOP_REPLACEMENT.md). Design decisions below are proposals, not observed DSH behavior.

## 2. User-visible behavior

For “Click Dragon Cave in Discord on the right screen”:

1. Transcribe; publish the recognized request.
2. Identify this as GUI work and check driver/model readiness.
3. Read monitor/window metadata; resolve rightmost monitor by geometry, not connector numbering. Filter Discord candidates by that monitor.
4. If multiple matching windows/targets remain, ask a specific question. Do not click a similarly named title and assume success.
5. Obtain task approval before any input. Capture the selected target's visible state within the approved observation scope.
6. Give Ministral 3 8B that actual image, accessible elements/tokens when available, the goal, and allowed tool schemas.
7. Validate one proposed action; execute via Cua; obtain a fresh observation.
8. Finish only when evidence supports the requested selection. Otherwise continue within limits, ask, or stop with an honest partial result.

UI shows `Checking desktop`, `Awaiting approval`, `Loading visual model`, `Observing Discord`, `Choosing next action`, `Acting`, and `Verifying`, with elapsed time. `Listening` means the microphone is recording, never that a desktop task is running. A persistent Stop action is available during every active phase.

## 3. Architecture and ownership

```text
Voice / typed request
  → Request router
      ├─ non-GUI → existing tools/text agent
      └─ GUI → DesktopTaskController (one task owner)
                 ├─ existing approval broker / TaskGrant
                 ├─ shared ModelLease → multimodal decision runtime
                 ├─ DesktopProvider → managed Cua MCP transport
                 ├─ private observation/conversation store
                 └─ progress events → existing Qt signal/IPC UI
```

| Component | Owns | Must not own |
|---|---|---|
| DesktopTaskController | Goal, task generation, state transitions, budgets, cancellation, target scope, progress | Driver-specific coordinate conversions or widget mutation |
| DesktopProvider | Discovery, catalog, exact driver sessions/results, shutdown | Model prompts or approval decisions |
| Cua transport | Protocol negotiation, framed stdio messages, bounded reads, call correlation, process lifecycle | Foreground fallback, tools beyond the allowed catalog |
| VisualDecisionRuntime | Image-capable model readiness, bounded conversation assembly, one structured decision | Calling the driver or treating page text as policy |
| PolicyGate | Effect classification, approval binding, scope checks, trusted audit decisions | Inferring safety from an optional model label |
| EvidenceVerifier | Fresh-observation checks and bounded outcome assessment | Claiming input delivery equals completion |
| UI adapter | Render progress, return exact-ID approvals/cancel requests | Blocking waits or process startup on the Qt thread |

One owner serializes all input for the task. All GUI executors, including the legacy route, share the same local desktop admission lock; enabling Cua must not leave the old ydotool path acting concurrently. This lock cannot prevent human input or other applications from changing the desktop, so freshness checks remain necessary.

## 4. Integration with current code

Enhance the existing desktop path rather than adding another independent router.

| Existing location | Proposed change |
|---|---|
| [desktop task adapter](../app/brain/desktop_task.py#L346) | Dispatch the selected backend; v2 orchestrates image/tool conversations, not OCR captions. Legacy is an explicit rollback option only. |
| [agent loop](../app/brain/agent_loop.py) | Reuse bounds/report concepts; add explicit task states, cancellation and evidence handling where needed. Keep ordinary planner behavior unchanged. |
| [tool registry](../app/brain/tools.py) | Register only validated/allowed Cua schemas under a collision-safe namespace; route execution through the same policy gate. No shell escape or generic tool-name bypass. |
| [vision CLI adapter](../app/desktop/vision/vlm.py#L89-L144) | Keep read-only inspect functionality. Add a distinct image-capable decision-runtime interface; current 4,000-character prompt/256-token caption defaults are not action-agent defaults. |
| [model lease](../app/brain/model_lease.py#L29-L60) | Retain exclusive ownership. Add actual visual-runtime/Whisper registrations and task-owner handoff, not competing ad-hoc unload hooks. |
| [controller resource registration](../app/engine/voice_controller.py#L1272-L1278) | Currently registers reasoning; wire all real model owners for this route. Never let a confirmation thread unload another owner's process. |
| [confirmation broker](../app/engine/confirmations.py) | Keep exact-ID, expiring approvals. Add scope/risk metadata in trusted controller state, not model-supplied permission claims. |
| [task grant](../app/brain/task_grant.py) | Continue ordinary task-level input approval; narrow effect/target scope for v2 and require separate sensitive-action approval. |

Proposed new modules, not yet created:

```text
app/desktop/providers/base.py          # provider-neutral interfaces and records
app/desktop/providers/cua_mcp.py       # supported MCP transport and Cua adapter
app/brain/desktop_controller.py        # task states, ownership, deadlines
app/brain/visual_decision.py           # multimodal runtime and decision validation
app/brain/desktop_policy.py            # scoped effects and sensitive-action gate
app/brain/desktop_evidence.py          # observation freshness/outcome checks
```

Do not create parallel implementations of model leasing, confirmations, or input policy. Names/module splits can be reduced during implementation if existing modules provide the same boundary.

## 5. Provider and tool contracts

The following are Misty adapter contracts, not assertions about Cua's current JSON field names.

```text
DesktopProvider.start(cancel, deadline) → CapabilityReport
DesktopProvider.catalog() → validated original tool schemas
DesktopProvider.call(tool_name, original_arguments, cancel, deadline) → ToolOutcome
DesktopProvider.close(deadline) → CleanupOutcome
```

`CapabilityReport` records provider/version, OS/compositor, supported observation scopes, actual permission status, supported input/delivery modes and diagnostic refusals. Unknown is not supported. Read-only capability discovery must not prompt for permissions or send input implicitly.

`ToolOutcome` preserves the canonical raw/structured result and ordered text/image blocks, plus trustworthy transport metadata. Maintain separate `delivered`, `refused`, `failed_before_execution`, `unknown_outcome`, and `verified` concepts where the driver exposes them. Do not fabricate a verification flag when the driver lacks one.

Every advertised schema is preserved, including required fields, enums and output schemas. Before exposing a tool, classify its effects using a reviewed adapter map for the pinned catalog. Unknown/new tools are withheld pending review; descriptions are not a permission system. Detect schema drift and fail readiness rather than guessing argument shapes.

Keep window IDs opaque and lossless. Use driver-native element tokens/coordinates without applying Misty's legacy frame transforms. Preserve the driver's targeting and delivery mode. Unsupported background delivery never triggers automatic foreground delivery. A driver refusal never authorizes a legacy-backend retry.

## 6. Observations and visual decisions

Private observation record:

```text
Observation {
  id, task_generation, provider_generation,
  captured_at_monotonic, target_identity, scope,
  ordered_content: [text | image_ref],
  driver_snapshot_identity, available_element_tokens,
  image_geometry_metadata, provenance
}
```

Keep the original image and driver geometry. Any model-input resize requires a proven reversible mapping to driver coordinates; otherwise do not resize silently. Explicitly test the visual model's grounding convention (pixels versus normalized coordinates) against known targets; do not assume it matches Cua or infer a conversion from a few live clicks. If that convention cannot be validated, allow only trusted element-token actions or ask the user. Element-token actions are preferred when the requested target has a unique accessible match, but tokens are not treated as authorization or immunity from stale UI.

The next model request includes:

- System policy and the user's goal, kept separate from untrusted observation text.
- Current real screenshot(s), not only OCR/prose; structured accessibility state where available.
- Only reviewed, currently usable tool schemas.
- Bounded previous actions and canonical result/error summaries.
- The current target scope and explicit remaining budget.

Old images can be dropped from the prompt after retaining a bounded action/evidence summary. Old tokens must never remain usable merely because they survive in conversation history. Snapshot identities are checked outside the model.

Normalized decision envelope:

```json
{"kind":"act","tool":"<discovered allowed name>","arguments":{},"observation_id":"..."}
{"kind":"finish","evidence_ids":["..."],"summary":"..."}
{"kind":"ask","question":"..."}
```

This envelope does not replace the driver's tool schema. The controller validates both. Native model function calls may be normalized into the same internal envelope after transport conformance passes. Initially permit only one action per decision; do not execute model-generated batches of dependent GUI writes.

Malformed output: one bounded repair request using the same observation if still fresh; then stop/ask. Never extract executable JSON with permissive string slicing or guess a tool from prose. An image-unavailable model route cannot proceed as if it saw the screen.

## 7. State machine and cancellation

```text
ADMITTED → CHECKING_CAPABILITIES → RESOLVING_SCOPE → AWAITING_APPROVAL
  → ACQUIRING_MODEL → OBSERVING → DECIDING → VALIDATING → ACTING
  → VERIFYING → [OBSERVING | AWAITING_APPROVAL | ASKING | COMPLETED]

Any active state → CANCELING → CANCELED
Any active state → FAILED | TIMED_OUT
```

Observation requiring broader screen access must first extend the observation scope. Metadata discovery is not permission to inspect all hidden windows. Terminal transitions are emitted once. Every callback carries task and provider generation; late results are discarded before state updates or input admission.

Check cancellation before inference, tool admission, input dispatch and result publication. Cancel subprocess operations according to their supported protocol; if blocked, close admission, terminate the owned subprocess after a bounded grace period, and reap it. Reconnection creates a new provider generation; old tokens/results are invalid. Never claim cancellation rolled back input already delivered.

The task duration is an admission deadline, not proof an OS/native call is preemptible. A timed-out input has unknown outcome until inspected; do not retry it. If cleanup cannot establish that the old input path has stopped, keep input admission blocked rather than start a competing provider.

## 8. Models and spoken approval

Avoid reloading text Qwen between every visual step. After routing, release text reasoning ownership and acquire the visual runtime for the task. Release/unload it on task exit. Task residency reduces load overhead; this requires verified persistent multimodal-server support and sufficient memory, not a promise about current performance.

Before **every** approval or clarification wait, including button-only prompts:

1. Pause at a safe checkpoint; finish or cancel any in-flight inference/input operation first.
2. The task owner releases the visual lease and waits for bounded unload acknowledgement. Only after success may it publish the prompt and block in the existing confirmation broker. This avoids requiring a blocked owner to process a later microphone handoff request.
3. A bounded speech job may now acquire Whisper, transcribe, release it, and return the exact confirmation request ID. Resolve the answer only after speech cleanup has completed; denial/cancellation can close admission immediately while cleanup finishes.
4. The task owner handles the answer; approved tasks reacquire visual resources only after the speech lease has been released, then refresh observation before acting.

No confirmation worker directly stops another thread's model. No lease is held while waiting on a different lease. If unload fails or inference cannot be canceled safely, do not enter a prompt that starts Whisper; show the resource error and stop the task safely. A button answer avoids speech inference but follows the same ownership protocol. The prompt's 60-second approval expiry starts after successful resource release, not during model cleanup.

## 9. Approval and risk policy

Default task grant: ordinary navigation/focus/scroll and clearly scoped benign interaction with the selected app. Approving “open Dragon Cave” does not approve posting messages, buying, deleting, running shell commands, changing security settings, or exposing credentials.

Sensitive actions require a separate approval bound to exact task, target, intended effect and current observation. A changed target or expired/stale observation invalidates it. Never trust model-provided `label`, `safe`, `risk`, or page text as the sole evidence of effect classification.

Coordinates alone may not reveal semantics. If trusted driver/accessibility information plus context cannot establish that the proposed click is within benign scope, ask for action-specific approval or leave the final action to the user. Do not advertise that arbitrary coordinate automation can perfectly detect purchases or deletions.

Unknown-effect tools, arbitrary command execution and broader app/screen access are unavailable by default. Runtime permissions granted to Cua are distinct from the user's task authorization.

## 10. Verification and recovery

Two gates are required for completion:

1. **Mechanical:** a fresh observation of the same intended target was causally captured after the last action settled; no unresolved unknown-outcome input or transport error remains. A later response/adapter timestamp is insufficient: use the provider's validated uncached-capture contract, generation or capture timestamp to reject cached/out-of-order snapshots. Action delivery is not application settlement; use bounded state-change/stability observations without repeating input. If causal freshness cannot be established, completion remains unverified.
2. **Semantic:** the fresh observation supports the user's actual goal. Prefer deterministic accessible-state checks; otherwise use a separate visual verification decision with cited observation/element evidence and a conservative uncertain result.

A model's confidence/evidence string is not independent proof. If semantic verification is unavailable, report “input delivered; outcome unverified,” not “completed.” For Discord, a relevant title string alone is weaker than evidence that the intended server/channel is selected.

| Failure | Response |
|---|---|
| Read-only transient error | One retry within deadline; retain the original error. |
| Stale element token | New snapshot of the same target, then reconsider; do not substitute a coordinate blindly. |
| Pre-execution schema error | One model repair; no input replay. |
| Input delivery uncertain / tool timed out | Fresh observation; no automatic replay. |
| Target moved, disappeared or scope changed | Re-resolve within scope or ask; invalidate old evidence. |
| Permission refusal / unsupported delivery | Stop with capability diagnosis; no privilege/backend escalation. |
| Unchanged relevant state | At most two observation/replanning attempts, then stop/ask; this is not permission to repeat input. |
| Provider/model crash | Mark partial/unknown result, close admission, clean up; no automatic continuation of writes. |

Unchanged pixels alone are not a failure: focusing a field can be valid without OCR changing. Conversely, animations elsewhere are not meaningful progress. Track action signatures, relevant target state, and delivered/verified outcomes separately. Even a delivered-but-unverified click must not be automatically repeated: delayed navigation or double-activation can compound effects. Repetition requires a fresh observation and a reviewed operation-specific safe-repeat/idempotence rule; otherwise ask or stop.

## 11. Initial limits and observability

Pilot configuration, to tune after measurement: at most 12 input actions per task; one action per decision; a 180-second execution admission budget; explicit capped model startup/inference and driver-call deadlines derived from remaining budget; one repair per malformed decision; two no-progress attempts. Approval waiting has its own 60-second expiry; capture/token freshness is rechecked afterward. Report cleanup time separately from the admission budget.

Emit content-free progress events with task ID, generation, phase, round, tool name, duration, outcome category and error code. Log no screenshot pixels, typed content, full accessibility trees, credentials or private prompts by default. Do not log a success solely because a pointer backend returned successfully.

A status view should distinguish waiting for user permission, loading a model, processing a screenshot, and a stalled provider. Stop must remain usable even when the worker is awaiting I/O. Do not busy-poll the UI or block Qt on model/process work.

## 12. Phased delivery and gates

| Phase | Deliverable | Gate before proceeding |
|---|---|---|
| A — capability spike | Pinned supported Cua distribution; read-only MCP handshake/catalog/permissions and one targeted snapshot | Works on this KDE/Wayland session; no input tools invoked; supported features recorded |
| B — provider adapter | Managed transport, exact schemas/results/images, cancellation, generation/cleanup tests | No orphan process, no token reuse across reconnect, no unknown tool exposure |
| C — visual decision runtime | Local Ministral 3 8B receives actual screenshot plus schemas and emits a validated action proposal | Controlled screenshot tests, image-capability check, bounded malformed-output handling, measured latency/memory |
| D — controller/policy | Existing desktop route dispatches v2; approval/scope/model handoff wired end-to-end | Fake-only tests prove no unauthorized input and every terminal path cleans up |
| E — controlled live acceptance | One harmless test application with visible state changes | Targeted action and fresh-state verification succeed, including cancellation and stale-token cases |
| F — Discord pilot | Task-scoped real request on right monitor | Correct target selected and verified; diagnostic progress visible; no false completion |

Use explicit settings `desktop_backend = legacy | cua` and `desktop_visual_model = <validated local route>`; these are proposed configuration keys, not existing options. Retain legacy for deliberate rollback while v2 is gated. Never automatically switch backend after a failed/refused action. Default changes only after phase F passes.

## 13. Acceptance matrix

- Exact app/window disambiguation; rightmost monitor selected by geometry; duplicated labels require clarification.
- Negative monitor origins and mixed DPI: Cua contracts work or clearly refuse; no double scaling/translation.
- Snapshot image reaches visual model; image failure/text-only route refuses input.
- Schema drift, unknown tool, invalid arguments and malicious observation instructions do not bypass policy.
- Stale token, moved window, changed app focus and human interference trigger refresh/reassessment.
- Input delivery success without observed goal change never reports completion; unchanged/delayed state does not cause a duplicate click.
- Cached or out-of-order snapshots cannot satisfy post-action verification merely because their response arrived later.
- Cancel during approval, model loading, inference, tool call and verification; no late input/state updates after canceled generation.
- Approval by voice/button, denial, timeout and unclear speech; no stale Listening state or overlapping heavyweight models.
- Sensitive or semantically uncertain actions require specific approval; optional labels cannot waive it.
- Process crash, broken stdio, oversized/malformed image/result, hung call and failed cleanup remain bounded and do not start another input owner.
- Logs contain useful durations/error categories without private observation content.

## 14. Scope exclusions and unresolved facts

This design does not promise unrestricted autonomous desktop control, perfect semantic verification, hidden-window visibility or platform feature parity. It does not migrate Misty's non-GUI tools or change its default LLM provider to a cloud service.

Before implementation lock-in: establish the real supported Cua executable/Python packaging and exact catalog, KDE Wayland capabilities, multimodal llama.cpp server flags/API/structured-output behavior for the installed Qwen3VL artifacts, and available GPU memory/latency. These require targeted capability tests, not further mock-only assurance. If phase A or C fails, publish the concrete limitation and reconsider the backend/model choice before integrating writes.
