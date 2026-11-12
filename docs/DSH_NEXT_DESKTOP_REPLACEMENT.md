# DSH Next desktop integration: replacement design

## Scope and evidence

Inspected the installed DSH Next 2.0.22-next package and its shipped JavaScript through read-only filesystem tools. REA tools/CLI were unavailable; these are ordinary static-source observations, not REA Evidence or runtime verification. The ASAR is readable through the host read tool but not visible inside the shell's private temporary filesystem. No external code was executed and no plugin/profile/configuration was changed.

Installed identity: [Desktop manifest](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/package.json#L1-L29). Its computer-use dependency is [the native Cua provider](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/package.json#L78-L88), version 0.2.2-rc.1, using @trycua/cua-driver 0.28.0. The inspected manifests declare MIT licensing; retain relevant attribution if code is redistributed. Presence in the package does not prove that this provider is enabled in the current profile.

## Principles to absorb

1. **One coherent computer-use provider.** DSH reserves one provider registration and gives it lifecycle ownership. It does not separately improvise screenshot and pointer contracts: [registry](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@deepseek-ai/dsh-computer-use/lib/index.js#L7-L31).
2. **Driver-discovered schemas, not an invented tool API.** The provider creates Cua Driver, calls listToolsJson, validates its catalog, and registers the exact input/output schemas. Cancellation propagates to driver calls; unload waits for pending work and shuts the driver down: [provider implementation](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@deepseek-ai/dsh-experimental-computer-use-cua-driver-native/lib/index.js#L40-L128).
3. **Fresh, target-bound observations.** DSH guidance explicitly says discover the exact application/window, snapshot it, use an element_token or screenshot coordinates, and refresh after acting. Tokens become invalid when that window gets a new snapshot. Background-delivery refusal does not authorize a foreground retry. Input delivery is not proof of task completion: [provider guidance](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@deepseek-ai/dsh-experimental-computer-use-cua-driver-native/lib/index.js#L28-L32).
4. **Preserve actual images in the conversation.** MCP results are projected as ordered text/image content; admitted images are stored as attachments. Admission requires the current model route to explicitly declare image input. A text-only model receives an explicit image-unavailable diagnostic, not magical visual understanding: [image admission/projection](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js#L279-L350).
5. **Tool results drive the next model call.** The agent repeatedly assembles model/context/tools, executes model-returned tool calls, and continues with their results. It does not precompute a GUI DAG: [loop](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js#L739-L798), [tool execution](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js#L957-L973), [next request](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js#L1078-L1091).

## Mapping to Misty

| Contract | Current status | Evidence and gap |
|---|---|---|
| Iterative observe/act loop | Partial | [desktop task](../app/brain/desktop_task.py#L346) and [loop](../app/brain/agent_loop.py) already exist; enhance/replace this path, do not build a third planner. |
| Exact tool schemas | Implemented for current tools | [decision payload](../app/brain/desktop_task.py#L226-L249) now carries schemas, but not Cua's discovered schemas. |
| Image-bearing tool results to the deciding model | Missing in desktop path | [decision payload](../app/brain/desktop_task.py#L242) renders text; [vision coordinator](../app/desktop/vision/coordinator.py#L99-L104) sends an image to a separate captioning model. The reasoning model does not receive that image. |
| Trusted screenshot/input coordinates | Partial | [capture service](../app/desktop/vision/capture.py) and [frame binding](../app/brain/desktop_task.py#L260-L274) validate the custom coordinate path; they are not a substitute for Cua's exact-window/element-token contract. Avoid stacking transforms over Cua coordinates. |
| Provider lifecycle and cancellation | Partial | Misty owns local models and bounded tasks, but has no Cua transport/session shutdown boundary. |
| Approval | Existing, preserve | [task grant](../app/brain/task_grant.py) and [confirmation broker](../app/engine/confirmations.py) must remain outside driver input dispatch. Do not inherit promptless SDK defaults as authorization. |

Implementation-ready follow-up: [Desktop Agent v2 design](DESKTOP_AGENT_V2_DESIGN.md), covering component ownership, contracts, state transitions, approval, model handoff, rollout gates and acceptance criteria.

## Concrete replacement

Keep microphone transcription, tray/popup UI, approval broker and local non-GUI tools. Replace the visual desktop path with a provider adapter, not the whole DSH Electron application.

1. **Capability check first.** Establish an explicit Cua installation/runtime boundary and query the real tool catalog and permissions. Verify Linux/KDE Wayland capture, accessibility, targeting and input in the actual session. Linux native packages are listed in the [SDK manifest](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@trycua/cua-driver/package.json#L50-L56), but this does not establish feature parity or Wayland support. No silent fallback to guessed coordinate input.
2. **Choose transport.** Prefer Cua's supported Python SDK or a managed `cua-driver mcp` subprocess, depending on verified installed interfaces. The [SDK README](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@trycua/cua-driver/README.md#L20-L30) explicitly documents MCP for agents; its [SDK entrypoint](/tmp/.mount_DSH-NECgEGbb/resources/app.asar/node_modules/@trycua/cua-driver/dist/index.js#L7-L17) says Python and TypeScript use the same native library. Do not depend on DSH's temporary AppImage mount paths in production.
3. **Use an image-capable decision route.** Prefer the existing local Ministral 3 8B model as the GUI action-decider with schemas and fresh images, rather than asking it only for prose captions. Keep the text Qwen model for non-GUI planning if useful. Validate structured action output, image/coordinate conventions, latency and memory limits before replacing the old route. Installing Cua alone cannot make the text-only controller see.
4. **Exact driver contracts.** Forward discovered schemas and structured text/image results. Discover app/window, obtain a fresh snapshot, act using a token or the driver's coordinate representation, and snapshot again. Preserve driver refusals, cancellation, stale-token and delivery metadata. Never invent pid/window_id fields or convert large IDs through lossy number representations.
5. **Ownership and policy.** One active desktop task/provider session, bounded duration, user cancellation, cleanup on exception, approval before input, and additional approval for sensitive outcomes. No concurrent old ydotool and Cua controller ownership. No blind retries of uncertain side effects.
6. **Visible progress.** Expose capture/decision/action/verification stages and per-step durations. Log content-free decision/tool/error metadata so a slow VLM call is not presented as Listening or unexplained inactivity. Private screenshots/tool data stay out of persistent logs unless explicitly requested.

## Verification and rollout

- Unit tests: catalog parsing, structured/image result retention, exact-window identity, stale token handling, cancellation/cleanup, refusal propagation and approval enforcement.
- Real read-only smoke test: tool discovery, platform permissions, monitor/window enumeration and one snapshot, without any input.
- Controlled application test: discover a test window, snapshot, click one harmless control and verify its state changed. Test monitor offsets/scaling through driver contracts, not custom arithmetic assumptions.
- Only then test Discord under task approval. A delivered click is not acceptance; fresh snapshot evidence must show the requested server/channel selected.
- Roll out behind an explicit backend switch until live acceptance passes. Retain the previous route for rollback, never as an automatic input fallback after a Cua refusal.

## Remaining decisions and unknowns

No Cua binary/Python dependency has been installed, no driver has been launched, and no live screenshot/control probe was performed. The current DSH profile's provider activation and model image capability have not been queried. Native Rust implementation, exact current tool catalog and Linux compositor behavior remain unverified. These are prerequisites for a production replacement, not details to paper over with mocks.
