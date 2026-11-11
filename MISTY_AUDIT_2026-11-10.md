# Misty source audit and integration plan

Date: 10 November 2026. Scope: the live source in `/home/forgottensin/misty`, not a supplied archive. No application code, settings, dependencies, models, or user databases changed during this audit. This report is the only deliverable file created.

**Status update (later on 10 November):** the first implementation batch, the approval/clipboard/activity safety foundations, and a live-test follow-up (launch-to-inspect filtering, read-only ambiguity auto-selection, popup masking, tolerant filename matching) have been implemented. See [the implementation record](</home/forgottensin/misty/MISTY_BATCH_2026-11-10.md>). Live desktop verification is still pending. The audit below records the pre-change state; its named-window, scoped-search, policy-wiring, approval-binding, clipboard-expiry, activity-redaction, and launch-to-inspect gaps are addressed in the current source.

## 1. Findings and evidence limits

Misty has a useful local foundation: KDE window control, an allowlisted registry, plan validation, persistent memory, a working hold-to-talk path, and injectable capture/OCR/VLM services. Extend those interfaces.

The largest mismatch with the handoff is execution: the current agent creates **one fixed JSON plan**, runs it, and summarizes. It does not feed intermediate observations back into a planning loop. The former system prompt and `MAX_TOOL_ROUNDS` remain defined but unused by `run()`. Dependencies control scheduling, not argument substitution.

I inspected entry points, configuration, execution and permission paths, speech, IPC, storage, desktop services, and relevant test code. I ran 97 tests with mocked backends or temporary fixtures. Those results do not certify actual KWin, Spectacle, AT-SPI, microphone, GPU, or model behavior. I did not read private database contents or capture the desktop.

Runtime package metadata reports Python 3.14.10, PySide6 6.12.0, NumPy 2.5.4, sounddevice 0.5.6, Pillow 13.0.0, PyGObject 3.58.1, Beautiful Soup 4.15.0 and psutil 7.2.2. Playwright, Docling and watchdog have no installed package metadata in this interpreter. Installed metadata does not prove feature integration.

The project root has no `.git` entry or root dependency/build manifest, README, or license matching the checked names. The expected `research/computer-use-linux` directory is absent, so no local checkout revision or modifications can be verified. Separately, the [GitHub main-commit response](https://api.github.com/repos/agent-sh/computer-use-linux/commits/main) reports **`6b288a604283a500a2254dcbe1433491746a76f5`**, matching the handoff, with release v0.7.49 dated 9 November 2026. I inspected the upstream dependency manifest and selected KWin implementation at that revision; this does not establish the missing local checkout's state.

## 2. Architecture in the actual source

### Active request path

```text
misty.py → app.main.MistyApplication
  ├─ Qt main window + tray
  ├─ popup QProcess ↔ local JSON-line socket
  ├─ KWinWindowService → DesktopContext → CaptureService
  ├─ ClipboardBridge
  └─ VoiceController
       F8 helper PRESS → save active window → start recording/preload Qwen
       RELEASE → stop recording → whisper-cli → transcript
       → MistyAgent.run
           local llama-server / JSON plan
           → TaskPlanner validation and dependency scheduling
           → confirmation callback when required
           → ToolRegistry.execute → service/executor
           → deterministic partial-failure report OR local final summary
           → stop llama-server
       → popup response + local activity history + cleanup
```

- [Entry point](</home/forgottensin/misty/misty.py#L1-L9>) sets Qt Wayland/accessibility environment variables and calls [application composition](</home/forgottensin/misty/app/main.py#L30-L87>). The launch path does not call the older [launcher](</home/forgottensin/misty/launcher.py>) or standalone [recording session](</home/forgottensin/misty/session.py>).
- [KDE shortcut helper](</home/forgottensin/misty/kde-shortcut/main.cpp#L15-L48>) uses KGlobalAccel and emits PRESS/RELEASE. [Recorder](</home/forgottensin/misty/app/speech/recorder.py#L27-L100>) starts/stops the input stream around recording. [Audio selection](</home/forgottensin/misty/app/speech/audio_config.py>) reads [audio configuration](</home/forgottensin/misty/config/audio.json>), currently system default. Preserve this path.
- [Transcriber](</home/forgottensin/misty/app/speech/transcriber.py#L56-L159>) writes a temporary WAV, invokes local whisper-cli with automatic language detection and a 120-second timeout, and releases model resources when the subprocess exits. `load()` checks files; it does not load a persistent Whisper model.
- [Agent](</home/forgottensin/misty/app/brain/agent.py#L207-L277>) requests a JSON plan with tools disabled at the HTTP API layer. [Planner](</home/forgottensin/misty/app/brain/planner.py>) validates the tool schemas, a default 12-step maximum, IDs, dependencies and cycles. [Execution](</home/forgottensin/misty/app/brain/agent.py#L362-L518>) runs ready steps serially. Failed prerequisites block dependents; independent steps can continue.
- [Registry](</home/forgottensin/misty/app/brain/tools.py#L560-L587>) has 28 base tools and adds `inspect_screen` when a vision service exists. It provides the main extension point through schemas, injected services and `execute(name, arguments, confirmed=False)`.
- [Executor](</home/forgottensin/misty/app/actions/executor.py>) uses fixed command argument lists, approved folders and KWin delegation. [Application discovery](</home/forgottensin/misty/app/actions/applications.py#L174-L209>) validates desktop IDs against installed entries before using gtk-launch. There is no registered arbitrary shell tool.
- [MemoryStore](</home/forgottensin/misty/app/brain/memory.py>) uses SQLite, FTS5, parameterized SQL and update triggers. [ActivityStore](</home/forgottensin/misty/app/engine/activity_store.py>) is a separate persistent history, not the explicit-memory system.
- [Vision coordinator](</home/forgottensin/misty/app/desktop/vision/coordinator.py#L48-L71>) runs capture, Tesseract OCR, then optional local Qwen3-VL through llama.cpp. OCR failure currently stops the whole pipeline.
- [Popup IPC](</home/forgottensin/misty/app/engine/popup_ipc.py>) carries UI and approval messages. [Main application](</home/forgottensin/misty/app/main.py#L272-L289>) uses QLockFile for single-instance behavior; [process guard](</home/forgottensin/misty/app/engine/process_guard.py>) narrowly matches stale popup processes.

### Extension contracts worth retaining

| Contract | Use |
|---|---|
| `ToolRegistry.available_tools()` / `execute()` | One tool namespace and execution boundary for new subsystems |
| `requires_confirmation(name)` and confirmation callback | Existing approval path; strengthen rather than bypass |
| `TaskPlanner.prepare_structured_plan()` | Validate plans before executing any step |
| `resolve_window_target()` / `resolve_screen_target()` | Reuse existing live identity resolution and ambiguity handling |
| `ActionExecutor(window_service=...)` | Keep KDE operations behind structured methods |
| `DesktopVision(capture_service, ocr_service, vlm_service)` | Mockable perception pipeline |
| `CaptureService(context_provider, target_window_provider)` | Preserve saved pre-listening identity behavior |
| `MemoryStore(database=...)` | Testable local retrieval; avoid introducing a second memory framework |

## 3. Capability inventory

| Area | Present in source | Missing or limited |
|---|---|---|
| Voice | F8 hold-to-talk, sounddevice, local whisper.cpp, automatic language detection | No current TTS pipeline found; no need to change capture |
| Typed interaction | `MistyAgent.run(str)` accepts text | No wired user text-submission path found in the active UI/controller |
| Reasoning | Local Qwen3-4B, JSON plan, validation, dependencies, approval callbacks | No observation-driven re-plan, retry policy, checkpoint resume or result references |
| KDE desktop | App/folder launch; windows, focus/state/close; screens and monitor moves | General clicks, typing and application workflows not exposed |
| Accessibility | AT-SPI apps/trees/action discovery; restricted Press and Set Focus | Service exists but active registry/application composition does not expose it |
| Vision | Spectacle, desktop/screen/saved-active-window capture, English/Polish OCR, optional Qwen3-VL | No named-window input; crop is visible desktop region, not isolated window contents |
| Files | Bounded filename search, directory listing and metadata | No requested search scope, content reading, documents, indexing or file mutation tools |
| Memory | Explicit remember/recall/list/confirmed forget, SQLite FTS5 | No document corpus retrieval or retrieval-driven continuation |
| Clipboard | On-demand text read, confirmed write, Wayland utilities | Read authorization depends on model instruction; persistence through activity logs |
| Browser/internet | Opens Helium | No browser navigation/extraction/search tools or enforced outbound-data policy |
| System | Time, memory/load, volume | No structured install/admin/file-organization/workflow subsystem |
| Plugins | Injectable Python services | No approved plugin manifest/loader, capability metadata or MCP adapter |
| Audit/security | Local activity events, allowlists, validation, three sensitive tools | No central effect/data-flow policy, redaction/retention, approval request IDs or durable execution journal |

## 4. Confirmed defects and integration gaps

“Confirmed” here means demonstrated by source or mocked tests. I have not reproduced the user's live Helium request.

### High priority

1. **Named-window vision cannot express the target.** [Schema and dispatch](</home/forgottensin/misty/app/brain/tools.py#L518-L554>) accept mode, screen, question and visual toggle, but no window ID. [Capture](</home/forgottensin/misty/app/desktop/vision/capture.py#L82-L125>) resolves the saved pre-F8 window. Focus does not replace that saved identity. A question mentioning Helium reaches the VLM only after capture. Default mode is desktop. Fix argument routing and explicit target semantics before changing screenshot backends.

2. **Directory-scoped search is unsupported.** [Search schema](</home/forgottensin/misty/app/brain/tools.py#L156-L185>) and [dispatch](</home/forgottensin/misty/app/brain/tools.py#L1161-L1179>) accept only query/limit. [Service](</home/forgottensin/misty/app/actions/file_search.py#L38-L66>) seeds common folders plus all of home. The planner cannot honor “only Downloads” through this interface.

3. **No observation-driven continuation.** [Agent execution](</home/forgottensin/misty/app/brain/agent.py#L362-L551>) does not send results to the planner between steps. `depends_on` establishes execution order, not data binding. “Find a file, read the returned path, then summarize” cannot work as a general workflow. Ambiguous window selection skips the remainder and explicitly does not resume it ([lines 504–592](</home/forgottensin/misty/app/brain/agent.py#L504-L592>)).

4. **The active planning request omits the existing system policy.** [SYSTEM_PROMPT](</home/forgottensin/misty/app/brain/agent.py#L25-L91>) includes memory/privacy, language and untrusted-result rules, but [run()](</home/forgottensin/misty/app/brain/agent.py#L218-L250>) supplies a separate planning prompt. Reconnect a shared policy to actual planning and summarization; do not rely on prompt text as the permission boundary. The registry only filters a narrow English credential-assignment regex ([memory dispatch](</home/forgottensin/misty/app/brain/tools.py#L1040-L1062>)).

5. **Large-model process lifetimes overlap.** [Agent](</home/forgottensin/misty/app/brain/agent.py#L459-L461>) keeps its server alive during tool execution; [VLM](</home/forgottensin/misty/app/desktop/vision/vlm.py#L98-L125>) starts a second GPU process. Qwen preloading can also overlap Whisper transcription ([controller](</home/forgottensin/misty/app/engine/voice_controller.py#L718-L744>)). Cleanup exists, but there is no common resource arbiter. OOM or measured VRAM use remains unverified.

6. **Private content persists through activity history.** [Agent events](</home/forgottensin/misty/app/brain/agent.py#L485-L496>) include full serialized tool arguments/results; [ActivityStore](</home/forgottensin/misty/app/engine/activity_store.py#L38-L55>) stores details without redaction or retention. Thus clipboard contents, OCR text and memory data can persist beyond an on-demand read. “Forget memory” deletes the memory entry, not existing activity history. Add privacy-aware logging before browser/document ingestion.

7. **Approval messages are not bound to a request.** [Main handler](</home/forgottensin/misty/app/main.py#L182-L200>) forwards an approval boolean; [controller](</home/forgottensin/misty/app/engine/voice_controller.py#L210-L266>) applies it to the current pending request. There is no request ID, action digest or expiry in the response. A late response could approve a later action. IPC also accepts replacement clients and buffers unbounded lines ([server](</home/forgottensin/misty/app/engine/popup_ipc.py#L20-L35>), [connections](</home/forgottensin/misty/app/engine/popup_ipc.py#L72-L103>)). These are code-level gaps, not a demonstrated remote exploit. Add bounded messages, a private socket policy and session authentication; same-user compromise remains a broader limitation.

### Other confirmed issues and technical debt

- **Capture provenance is incomplete.** [CaptureResult](</home/forgottensin/misty/app/desktop/vision/capture.py#L19-L26>) lacks window identity and crop transform. [Fullscreen crop](</home/forgottensin/misty/app/desktop/vision/capture.py#L161-L175>) can contain occluding windows. Do not call those pixels isolated Helium contents.
- **Desktop snapshots are non-atomic.** [DesktopContext](</home/forgottensin/misty/app/desktop/context.py>) calls window and screen queries separately. Screen hotplug/window movement can invalidate their combination.
- **Search budgets are soft.** [Search](</home/forgottensin/misty/app/actions/file_search.py#L106-L118>) sorts an entire scandir iterator before checking the deadline again. Directory fanout can also grow the queue. Static symlink skipping is useful, but path-based checks do not provide a race-resistant filesystem sandbox.
- **Folder resolution differs by operation.** [Open-folder](</home/forgottensin/misty/app/actions/executor.py#L225-L255>) uses XDG resolution; [listing/metadata](</home/forgottensin/misty/app/actions/executor.py#L99-L122>) expand hardcoded roots. Localized or relocated XDG folders can produce inconsistent behavior.
- **A queued clipboard write can outlive its timeout.** [Submit](</home/forgottensin/misty/app/engine/clipboard_bridge.py#L29-L57>) returns “Nothing was changed” on timeout but does not cancel the queued request; [handler](</home/forgottensin/misty/app/engine/clipboard_bridge.py#L200-L255>) can execute it later. Add a deadline/cancellation check and report uncertain outcomes accurately.
- **Outcome verification is incomplete.** Launching a process or requesting a KWin action is not proof of the user's desired postcondition. AT-SPI already distinguishes unverified Press from observed focus; retain that distinction across tools.
- **AT-SPI needs integration and bounds.** [Service](</home/forgottensin/misty/app/desktop/accessibility/service.py>) has node/depth limits but no hard call timeout, positional target paths, and incomplete propagation of nested depth truncation. Start with read-only observation, then improve identity and worker timeouts before exposing Press.
- **Configuration/UI drift.** [Configuration](</home/forgottensin/misty/config.toml>) names basal-2.0-mini, enables voice feedback and declares explicit-request internet access; the runtime instead hardcodes Qwen3 and has no TTS or network policy layer. [Privacy panel](</home/forgottensin/misty/app/ui/main_window.py#L815-L835>) says an approval dialog does not exist despite the current approval path. Correct facts, not the layout.
- **Tests can touch production memory.** Some registry tests construct `ToolRegistry` without a temporary memory store, e.g. [tool tests](</home/forgottensin/misty/tests/test_tools.py#L16-L19>). Its default constructor initializes the real MemoryStore and rebuilds FTS. Isolate these fixtures before broad discovery.

### Not confirmed on this desktop

Mixed-DPI crop errors, actual GPU contention/OOM, portal/input compatibility, current Helium accessibility coverage, and successful post-action verification need controlled live tests. Existing crop tests largely verify dimensions with uniform images; they do not prove pixel selection under occlusion or mixed scaling.

## 5. Multi-project evaluation

I compared official sources and licenses. Upstream compatibility claims and Python classifiers do not certify this machine. Pin an exact release/commit and record transitive licenses before adoption. Effort estimates are relative to a narrow Misty adapter, not full feature delivery.

| Component | License and compatibility evidence | Reuse decision / effort / risks |
|---|---|---|
| **computer-use-linux** | [MIT](https://raw.githubusercontent.com/agent-sh/computer-use-linux/main/LICENSE). [Official README](https://raw.githubusercontent.com/agent-sh/computer-use-linux/main/README.md) documents Rust CLI/stdio MCP, KWin/AT-SPI, KDE Wayland coordinate handling, portal/uinput paths; claims no default network/telemetry. Shell is opt-in. | **Evaluate narrow actuator adapter; medium.** Read exact pinned Rust implementation/dependencies before adoption, run read-only diagnostics first, expose selected operations only. Do not install its whole stack, enable shell, grant uinput access or replace Misty's KWin service without evidence. Real three-monitor/mixed-scale/Qt6 tests remain mandatory. Local checkout missing. |
| **Playwright Python** | [Apache-2.0](https://raw.githubusercontent.com/microsoft/playwright-python/main/LICENSE). [Metadata](https://raw.githubusercontent.com/microsoft/playwright-python/main/pyproject.toml) includes Python 3.14; [official platforms](https://playwright.dev/python/docs/intro) list Debian/Ubuntu, not Arch. Maintained Microsoft project. | **Preferred browser library; medium.** Direct local browser contexts, locators and structured extraction, no second agent. Start with a separate profile; don't expose the normal authenticated Helium profile/debug port. Browser provisioning needs approval. Pages/subresources use the network; local execution does not imply private data stays local. |
| **Browser Use** | [MIT](https://raw.githubusercontent.com/browser-use/browser-use/main/LICENSE). [Current README](https://raw.githubusercontent.com/browser-use/browser-use/main/README.md) describes cloud browser/model defaults and a Python-REPL agent; warns of rapid API changes. Exact 3.14/KDE support unverified. | **Do not adopt whole agent; high adaptation risk.** Competing execution loop, cloud defaults and model-written Python conflict with Misty's boundaries. Study browser-state/observation ideas only when they beat direct Playwright. |
| **SearXNG** | [AGPL license](https://raw.githubusercontent.com/searxng/searxng/master/LICENSE); [package grant](https://raw.githubusercontent.com/searxng/searxng/master/setup.py) AGPL-3.0-or-later. Metadata classifiers reviewed through 3.13, so 3.14 unverified. [JSON search API](https://docs.searxng.org/dev/search_api.html) requires enabled format. Active engine integrations. | **Optional localhost service; low client/medium operations.** Search providers still receive query text. Public-topic searches may run automatically; block private-content queries without consent. Engine breakage/rate limits require fallback reporting. Review AGPL duties for modified deployment; service separation is not a license exemption. |
| **Beautiful Soup** | [Author's MIT statement](https://www.crummy.com/software/BeautifulSoup/) and [parser docs](https://www.crummy.com/software/BeautifulSoup/bs4/doc/). Upstream license-file fetch returned 403; capture installed/pinned distribution license before bundling. Installed locally; exact upstream 3.14 certification unverified. | **Use static HTML extraction; low.** Start with explicit stdlib `html.parser`; no model, cloud or JS engine. Controlled HTTP acquisition stays separate. Cap input/output and worker time. Long-lived project, but reviewed site/docs version labels differ. |
| **Docling** | [MIT code](https://raw.githubusercontent.com/docling-project/docling/main/LICENSE); [README](https://raw.githubusercontent.com/docling-project/docling/main/README.md) notes separate model licenses. [Modular metadata](https://raw.githubusercontent.com/docling-project/docling/main/pyproject.toml) includes Python 3.14 and separate Office/web/model extras. LF/IBM ecosystem. | **Evaluate non-model format adapters; medium.** Useful common document representation. Defer torch/ONNX/layout/VLM/ASR pipelines and automatic model downloads; no proven AMD/Arch GPU compatibility here. Parser isolation and dependency size matter. |
| **OCRmyPDF** | [MPL-2.0](https://raw.githubusercontent.com/ocrmypdf/OCRmyPDF/main/LICENSE). [Install docs](https://ocrmypdf.readthedocs.io/en/latest/installation.html) discuss Arch AUR and Tesseract. [Metadata](https://raw.githubusercontent.com/ocrmypdf/OCRmyPDF/main/pyproject.toml) has native PDF/image dependencies; 3.14 integration remains to test. | **Optional isolated CLI using existing Tesseract; low–medium.** Preserve original PDFs, bound CPU/time/pages/output, extract sidecars. Do not add RapidOCR/ONNX. Existing approved Tesseract remains valid. Current v17 docs make Ghostscript optional; avoid outdated dependency assumptions. |
| **Dogtail / direct AT-SPI** | [Dogtail GPLv2 text](https://gitlab.com/dogtail/dogtail/-/raw/master/COPYING); exact only/or-later grant needs further review. [README](https://gitlab.com/dogtail/dogtail/-/raw/master/README.md) emphasizes GNOME Wayland input and limited Qt support. No KDE6 input certification. | **Keep direct existing AT-SPI; whole Dogtail low value/high KDE adaptation effort.** Learn semantic traversal/testing techniques, avoid GNOME-specific input assumptions and unnecessary copied GPL code. Existing GI path already works in unit fixtures. |
| **sherpa-onnx** | [Apache-2.0](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/LICENSE), [offline capability docs](https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/master/README.md). Wide platform support; Python 3.14 wheels and AMD acceleration unverified. Model licenses separate. | **Defer.** Adds another inference runtime/model lifecycle without demonstrated need. Keep whisper.cpp and the microphone path. Offline TTS could justify a future user-approved evaluation, not an initial dependency. |
| **MCP Python SDK** | [MIT](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/LICENSE), [metadata](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/pyproject.toml) includes 3.14. Evolving protocol and SDK. | **Optional stdio transport; low–medium.** Approved executables and explicit tool mapping only. Deny sampling/model delegation, remote endpoints and arbitrary child commands by default. MCP does not enforce Misty's permissions. |

### computer-use-linux: pinned source findings

At the matching revision, the [dependency manifest](https://github.com/agent-sh/computer-use-linux/blob/6b288a604283a500a2254dcbe1433491746a76f5/Cargo.toml) requires Rust 1.89 and uses AT-SPI, zbus, Tokio, evdev, reis/libei-related support, Wayland protocols, image codecs, rmcp and an X11 backend. These are desktop/protocol dependencies, not an AI model stack. Use a locked build and audit the resolved dependency graph; the manifest's semver ranges are not a full pin. The release message mentions an rmcp security update, but I did not run cargo audit.

The inspected [KWin backend](https://github.com/agent-sh/computer-use-linux/blob/6b288a604283a500a2254dcbe1433491746a76f5/src/windowing/backends/kwin.rs) contains concrete Plasma-aware behavior:

- Temporary `/Scripting` transactions with a two-second timeout, script cleanup and callback-based completion, including a comment about Plasma 6 returning zero script IDs.
- Callback checks for the current KWin D-Bus owner, requested operation, nonce/plugin identity and duplicate responses. This is a useful pattern for future Misty IPC/D-Bus hardening.
- One script collects window frame/client geometry, output identity, device-pixel ratios and current-desktop visibility. Its element mapper checks PID/window ownership and refuses ambiguous same-PID window matches.
- Coordinate conversion rejects mirrored/overlapping outputs, unknown visibility and mixed/unknown output scales. **It is not a ready-made solution for arbitrary mixed-DPI three-monitor input.**
- Tests cover negative monitor origins, stale/ambiguous associations and transaction cleanup, but I did not execute them. The inspected backend uses numeric IDs derived from KWin UUIDs; an adapter must not assume these equal Misty's string IDs.

Candidate reuse: snapshot/coordinate validation patterns, stale-target checks and missing input facilities behind a restricted adapter. Do not copy its whole backend over Misty's working KWin code. The broader server/input/dependency graph still needs an adoption-stage security review.

Other candidates: the existing `/proc` system query does not need psutil replacement. Use installed psutil later for bounded process/resource telemetry if needed. Consider watchdog only after scoped indexing exists; no whole-home watcher. Consider ripgrep for explicit content-search roots after license/version/encoding/output-limit review. OpenCV has value for crop transforms or visual verification later; it should not replace working OCR or accessibility. These candidates received triage, not the full license/source evaluation above.

## 6. Dependency-ordered roadmap

### A. Reproducible baseline and target/scope correctness

Isolate tests from production databases. Record dependency/runtime versions and a source backup manifest. Repair named-window routing and scoped search using existing engines. Restore common model-policy instructions. Keep UI and microphone behavior intact.

**Exit gate:** mock regressions plus English/Polish desktop tests demonstrate correct identities/scopes, explicit ambiguity and no broad fallback.

### B. Shared execution, privacy and lifecycle foundations

Extend the existing registry with effect/risk metadata, data provenance, result/verification status and per-tool limits. Bind approvals to exact normalized arguments, task/request IDs and expiry. Redact activity payloads, add retention controls, fix expired clipboard writes. Add a shared model lease so reasoning and VLM unload/switch on demand; retain no idle model. Preserve current preload until measured tests justify changing its latency behavior.

Permission policy: allow ordinary local reads, focusing/navigation and public-topic web research without repeated confirmation. Require explicit approval for important-data deletion, software installation/removal, sending messages/email, private-data uploads, purchases/financial actions, privileged settings and other irreversible effects. Classify the intended effect, not just the low-level tool name: a click or Enter can submit a purchase. Unknown consequential effects fail closed. No cloud inference, remote model delegation, Ollama or model-generated shell commands.

Add a bounded observe→plan→validate→execute→verify loop around the existing planner: maximum steps/rounds/wall time, cancellation, stale-state checks, read-only retries, and no automatic replay of writes with unknown outcomes. Keep one local Qwen brain. Persist minimal task checkpoints without raw sensitive observations by default.

**Exit gate:** denied/stale approval tests, no private-data egress, cancellation/timeout tests, mocked model-order tests and measured idle-VRAM recovery.

### C. Parallel capability tracks after B

| Track | Increment | Verification |
|---|---|---|
| Desktop | Expose read-only existing AT-SPI; correlate KWin PID/window IDs; then guarded semantic actions; evaluate computer-use-linux only for missing input/verification | Fresh target identity, distinguish action accepted from postcondition observed; Qt/GTK/browser and three-monitor tests |
| Browser/research | Controlled public HTTP fetch + Beautiful Soup; optional SearXNG; direct Playwright for JS pages; mutations only after risk policy | Redirect/DNS/localhost/private-address/size limits; isolated profile; source URLs; block private query/body/cookie uploads; approval before sending/purchasing |
| Documents | Read bounded text/HTML first; Office and text-PDF parsing; OCRmyPDF with existing Tesseract for scans | Isolated workers, original preservation, page/text limits, citations to source/page, malformed-file tests |
| Knowledge | Separate document index using SQLite FTS5 and existing storage conventions; scoped incremental indexing | Explicit roots, exclusion rules, source freshness/deletion, no credential indexing; embeddings only if lexical retrieval proves insufficient |
| Files/system | Shared XDG scope resolver; resource/status tools; previewed file-operation plans with staging/trash and verification | Symlink/race checks, no shell, scoped destinations, confirmed destructive actions; watchers limited to opted-in roots |
| Speech/vision | Keep whisper.cpp/F8; fuse AT-SPI, OCR boxes and VLM with provenance; add typed submission through shared task entry point | English/Polish parity, DPI transforms, occlusion reporting, idle cleanup; no extra persistent model |

### D. Durable workflows and plugins

Build resumable workflows on the tested shared task engine. Add a small plugin manifest with schemas, effects, required permissions, dependency/version checks and lifecycle hooks. Treat local HTTP/D-Bus/MCP as adapters into the same registry. Do not let plugins create alternative permission systems or autonomous agent loops.

Do not start several services by default. Launch browser workers, parsers and optional search adapters only when configured and needed.

## 7. Proposed first implementation batch

**Scope: target/scope correctness plus regression isolation, in three reviewable patches.** No new dependency, browser stack, model, database migration, recorder change or UI redesign. This improves desktop visual understanding and file discovery while preparing safer integrations. Resource arbitration and approval redesign remain separate work because they need dedicated lifecycle/UI regression coverage.

### Patch 1: protect the baseline and reconnect policy

Use temporary injected memory stores in registry tests. Extract/reuse shared language, memory and untrusted-data instructions in the actual planning/final requests. Add tests asserting those instructions appear. This improves policy consistency; it does not claim to solve authorization with prompting.

### Patch 2: explicit visual window target

Extend `inspect_screen` with `mode="window"` requiring `window_id`; reject `window_id` in other modes and reject `screen_name` in window mode. Allow the existing resolver to convert a unique supplied name to a real ID at the registry boundary. Keep `active_window` meaning the saved pre-listening window. Preserve desktop/screen modes. Ambiguity returns candidates and requires clarification; do not invent resume behavior in this patch.

Forward the exact ID through `DesktopVision.inspect()` to `CaptureService.capture()`. Reuse saved-target validation and geometry-crop code through a common helper instead of replacing Spectacle capture. Add target identity, geometry and `capture_kind="visible_region"` provenance to results. Do not claim occlusion-free contents. Do not auto-focus inside a tool described as read-only; a user's explicit focus request stays a separate dependent action. Unknown scaling/visibility must fail or report a limitation rather than silently broaden capture.

### Patch 3: explicit file scope

Add optional `folder` and `relative_path` to `search_files`; preserve unscoped behavior for backward compatibility. Introduce one approved-folder/XDG resolver shared with listing/opening. Keep canonical scope under approved home roots for this patch; do not silently follow an external XDG root. Reject traversal, absolute paths and symlink escape; seed only the requested scope and return the effective scope. A missing or invalid scope must not fall back to all home. Stream enumeration with entry/queue budgets and return the truncation reason.

### Exact production files and interfaces proposed

| Existing file | Proposed change |
|---|---|
| [agent.py](</home/forgottensin/misty/app/brain/agent.py>) | Shared policy in actual planning/final prompts; explicit tool semantics for named windows and scoped search; preserve fixed-plan runner for this batch |
| [tools.py](</home/forgottensin/misty/app/brain/tools.py>) | Extend `INSPECT_SCREEN_DEFINITION`, search schema and dispatch; reuse `resolve_window_target()`; return target/scope provenance |
| [coordinator.py](</home/forgottensin/misty/app/desktop/vision/coordinator.py>) | Add optional `window_id` forwarding to `DesktopVision.inspect()` |
| [capture.py](</home/forgottensin/misty/app/desktop/vision/capture.py>) | Add explicit-window path and provenance; factor shared target validation/crop helper; preserve pre-listening provider |
| [file_search.py](</home/forgottensin/misty/app/actions/file_search.py>) | `search(query, limit=30, *, root=None)` with validated scoped traversal, entry/queue budgets and result scope |
| [executor.py](</home/forgottensin/misty/app/actions/executor.py>) | Delegate `_resolve_file_path()` and `_open_folder()` root resolution to common resolver without changing launch mechanism |

Proposed new production file: `app/actions/file_scope.py`, exposing a small injectable `UserFolderResolver` for approved folder names, XDG resolution and descendant validation. Do not put model-chosen shell commands in this resolver.

Existing tests to update: [agent planner](</home/forgottensin/misty/tests/test_agent_planner.py>), [tools](</home/forgottensin/misty/tests/test_tools.py>), [display resolution](</home/forgottensin/misty/tests/test_display_resolution.py>), [clipboard tools](</home/forgottensin/misty/tests/test_clipboard_tools.py>), [inspect-screen tool](</home/forgottensin/misty/tests/test_inspect_screen_tool.py>), [desktop vision](</home/forgottensin/misty/tests/test_desktop_vision.py>), [vision services](</home/forgottensin/misty/tests/test_vision_services.py>), [file search](</home/forgottensin/misty/tests/test_file_search.py>), and [executor](</home/forgottensin/misty/tests/test_executor.py>). New tests: `tests/test_file_scope.py`. Add a shared temporary-fixture helper only if needed; keep its usage explicit.

No first-batch edits proposed to [recorder](</home/forgottensin/misty/app/speech/recorder.py>), [transcriber](</home/forgottensin/misty/app/speech/transcriber.py>), [voice controller](</home/forgottensin/misty/app/engine/voice_controller.py>), [KDE helper](</home/forgottensin/misty/kde-shortcut/main.cpp>), UI layout, model files or user databases. If implementation reveals a need to widen that scope, stop and explain before doing so.

## 8. Testing and rollback

### Tests actually run during this audit

- 31 planner/agent tests: PASS, 0.004 seconds. Model responses and tool execution use fakes.
- 66 file-search, desktop-context, accessibility, capture/OCR/coordinator/VLM tests: PASS, 0.088 seconds. Desktop/model operations use mocks; file fixtures use temporary directories. Second command reported exit code 0.
- No full regression-suite claim. I avoided production-memory-default registry tests and live integration scripts. No real GPU, microphone, KWin, screenshot or browser smoke test occurred.

Reproduce the audited safe subset with one command:

```bash
cd ~/misty
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  tests.test_planner tests.test_agent_planner \
  tests.test_file_search tests.test_desktop_context \
  tests.test_accessibility_service tests.test_accessibility_actions \
  tests.test_vision_services tests.test_desktop_vision tests.test_vision_language -v
```

Expected on this audited source: `Ran 97 tests` and `OK`. Messages such as `Tool open_browser: success` come from the fake registry in agent tests; they do not launch a browser.

### First-batch acceptance tests

1. Mock named-window resolution, missing/stale/ambiguous IDs, explicit window versus saved pre-F8 target, and rejected contradictory arguments.
2. Use distinctive synthetic image regions to verify pixel selection, not just output dimensions. Test negative origins, unknown scaling and bounded failures. Do not claim full mixed-DPI support from synthetic tests alone.
3. Test scoped inclusion/exclusion, nested relative roots, XDG/Polish folder names, missing scopes, traversal/absolute/symlink rejection, result/time/entry/queue limits and no fallback outside scope.
4. Test active policy inclusion, original approval behavior and unchanged legacy tool signatures. Inject temporary memory stores throughout the test suite.
5. Run targeted tests after each patch; run reviewed broad unit discovery only after fixtures cannot touch production data. Keep live tests separate and opt-in.

For each desktop test request, use the exact normal launch command:

```bash
cd ~/misty
.venv/bin/python misty.py
```

Manual checks after implementation: start from a different active application, hold F8 and ask “Focus Helium and tell me what you see inside it”; repeat with multiple matching windows and in Polish. Verify the target ID/provenance and honest reporting of occlusion. Ask “Search only Downloads for …” and “Szukaj tylko w Pobranych …” with a second matching file outside that directory; the outside file must not appear. Confirm original voice capture, three-monitor moves, clipboard approval, memory recall and idle model unloading still behave as before.

Wait for the user's results before any risky follow-up change. This audit is not a request to run live tests now.

### Backup and rollback plan

Before each patch, create a timestamped backup of exactly the files to change and record hashes, modes and a list of new files. The current root lacks Git metadata, so do not assume `git reset` exists. Do not overwrite older backups. Explain the patch and provide one executable patch/backup script when implementation begins.

First batch needs no data migration. Leave memory/activity databases untouched. If later work needs a database backup, stop Misty or use SQLite's backup API; do not copy an actively written database as an ad hoc backup.

Rollback: quit Misty, restore the selected patch's backed-up files after verifying the absolute destination paths, and move newly introduced files to a rollback quarantine rather than deleting user files. Re-run the audited safe subset, then launch with the command above. Roll back on wrong-target/scope output, changed audio behavior, approval regression, a failed baseline test or model-cleanup regression. Successful earlier desktop actions are not undone by restoring source files.

## Recommended next decision

The first three patches plus approval binding, clipboard expiry, and activity redaction are implemented and covered by 199 local tests. Run the live checks in [the implementation record](</home/forgottensin/misty/MISTY_BATCH_2026-11-10.md>) before changing the agent execution loop. Keep broader browser, document and desktop-actuator adoption behind the shared verification and privacy foundations.

---

## Remediation record (11 November 2026, same session)

Everything in sections 4 to 6 was worked through after this audit. The suite now runs 1766+ tests in a read-only sandbox (previously 1264), and the audit's reproductions are pinned as regressions in [`tests/test_audit_regressions.py`](tests/test_audit_regressions.py).

| Audit finding | What changed | Evidence |
|---|---|---|
| Late spoken approval could approve another action | Confirmations are bound to the request ID captured when listening starts; a missing or mismatched ID never resolves anything | `app/engine/confirmations.py`, `app/engine/voice_controller.py`; mutation test: removing the ID check fails 3 tests |
| Approved mutations retried automatically | `READ_ONLY_RETRY_TOOLS` deleted; retry requires read-only policy, no confirmation and an explicit transient marker, once only; recovery and the plan loop share one helper | `app/brain/policy.py`, `app/brain/agent.py` |
| Every window step collapsed onto one target; file requests blocked by window lookup | Targets resolve per request clause, only when a window-control step exists; identity beats incidental title text; screen aliases fail closed | `app/brain/agent.py`, `app/brain/tools.py` |
| Negation and compound requests executed as a fragment | Direct routing declines negated/multi-intent text and keeps topic words, including Unicode | `app/brain/direct_actions.py` |
| Recovery erased the original failure | Execution report keeps original steps with `phase` and reports `outcome: recovered`; the answer never claims everything succeeded | `app/brain/agent.py` |
| Private data could leave without approval | Argument-aware egress classifier gates URLs, queries and form text; public research stays approval-free | `app/brain/egress.py`, `app/brain/policy.py` |
| DNS validation not bound to the connected address; browser followed redirects first | Connections pinned to validated addresses with Host/SNI preserved; browser context installs a fail-closed URL fence | `app/web/fetch.py`, `app/browser/session.py`, `app/actions/web_launch.py` |
| Deletion could follow a raced symlink; cross-device move deleted uncopied files; no-overwrite could clobber | Descriptor-anchored removal and publish; staged move with a verified manifest and in-place identity checks | `app/actions/fs_safe.py`, `app/actions/directory_transfer.py`, `app/actions/file_ops.py` |
| Final answers leaked private tool content into activity history | Final-answer events carry sensitivity; retention and purge controls exist and are proven to delete rows | `app/engine/activity_store.py`, `app/ui/main_window.py` |
| Media manifests could read outside the approved root; converter deadlines were soft | Demuxer/protocol restrictions and encoded-output limits; non-blocking reads against one deadline with process-group termination | `app/speech/file_transcriber.py`, `app/documents/converters.py` |
| Bound checks in duplicate/index scans were soft | Visited-entry, queue, byte and time budgets enforced per chunk; stale file identities skipped | `app/actions/duplicates.py`, `app/knowledge/index.py` |
| Reproducibility gap (no manifest, inherited site-packages) | Exact pins, system package list, observed inventory lock, `scripts/check_deps.py`, README | `requirements.txt`, `MANIFEST.lock`, `README.md` |
| Duplicated filesystem contract | One `path_contract` module used by all four engines; an AST test forbids reintroducing copies | `app/actions/path_contract.py` |

Also fixed while verifying: typed requests lost their request ID inside `_run_agent_session` (disabling every typed outcome signal), activity "delete older" passed no window and deleted nothing, and the SearXNG config port disagreed with the app default and the running service.

Added afterwards as quality-of-life work, on the same execution path: a bounded session context so follow-ups ("open it", "the one from October") resolve against the previous turn; a task card with live step status, Cancel and Retry; tools to read, preview and replace the focused selection (approval-bound, AT-SPI first then an opt-in clipboard route); tools to reveal or open the file just found; retention controls for the activity history; and stricter research relevance.

Found while writing the missing tests for the same work, and fixed:

- the task card could hand a receiver the raw agent event, including clipboard and screen text, on the same signal that carries the sanitised copy; only the sanitised copy is emitted now;
- every request emitted `task_started` twice (once from the card bookkeeping, once from the session runner);
- native clipboard writes and reads crashed the process when no widget-capable application existed, instead of failing closed;
- a retry that failed again was no longer retryable, and a retry of the same task was refused as "still working";
- a task that ended without an agent decision event never reported completion, so its failed step could not be retried;
- the task card ignored an injected clock while a task was running.

Remaining, deliberately not automated: the browser fence is URL-based, so a proxy or firewall is still the only complete answer for DNS rebinding inside Chromium; native text entry needs the `input` group and `ydotoold`; model overlap is measured but the arbiter is still unwired.
