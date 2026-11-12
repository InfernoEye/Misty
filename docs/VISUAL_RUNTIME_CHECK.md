# Local visual-runtime smoke check

The [visual runtime check](../scripts/check_visual_runtime.py) separates model readiness from desktop access. It never launches Cua, captures a desktop, or executes an action.

## Default: metadata only

```bash
.venv/bin/python scripts/check_visual_runtime.py
```

This checks whether the configured llama.cpp server, Ministral 3 8B model and projector files exist. It starts **no model**. `artifacts_present` does not establish image input, valid inference or coordinate grounding. The implementation-session metadata check found all three files, but did not load them.

## Explicit synthetic image probe

Stop Misty and any other local heavyweight inference first. Then, from your normal terminal:

```bash
.venv/bin/python scripts/check_visual_runtime.py --run-model --confirm-models-stopped
```

The acknowledgement is required; this standalone script cannot arbitrate another process's GPU resources. It does not stop other applications for you. Do not run it concurrently with Misty's model preloads or a running desktop task.

The probe uses [VisualDecisionRuntime](../app/brain/visual_decision.py) to start one owned, authenticated loopback multimodal server. That runtime requires the server to declare vision input and answer a synthetic colored-image JSON question correctly. It closes the owned model process afterward. No user screenshot or private prompt is used. `--timeout N` bounds the probe admission deadline to 1–300 seconds; cleanup has its own bounded period.

Success reports `synthetic_image_only_desktop_unverified`. It does **not** mean the model is good at locating Discord targets or that Cua can capture/control this KDE session. `grounding_verified` and `input_ready` stay false, even if a fake/foreign readiness object claims otherwise.

## Exit statuses and privacy

- `0`: the requested artifact-only or synthetic probe passed.
- `2`: missing artifacts, failed/expired probe, invalid flags or unconfirmed cleanup.
- `130`: canceled probe, if cleanup succeeded.

Output contains booleans and fixed status categories, not arbitrary exception text, model responses, API keys or private filesystem paths. `model_probe_attempted` records an attempted real start; a failure can occur after process launch, so do not interpret a failed readiness result as proof that no process was started. `cleanup_confirmed` must also be true.

The [mocked tests](../tests/test_check_visual_runtime.py) cover default no-launch behavior, explicit acknowledgement, failure/timeout/cancellation, cleanup and CLI flags. They never launch a real model. A real synthetic probe result is still outstanding.
