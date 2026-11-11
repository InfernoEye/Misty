# Desktop Agent v2 — read-only capability gate

## What is implemented

The [diagnostic](../scripts/check_desktop_v2.py) uses Misty's [managed Cua MCP provider](../app/desktop/providers/cua_mcp.py). It starts an owned `cua-driver mcp --direct` process, negotiates the pinned catalog, makes only reviewed observation calls, and closes the owned process. It does not launch a language model, send input, change desktop permissions, opt into native Wayland, configure a daemon, or retry through the legacy backend.

Default mode lists the reviewed catalog and asks for visible windows. Output contains counts/status only: no application titles, accessibility text, screenshots, base64 data, file paths or native exception prose. The optional exact-target mode requests one bounded window snapshot; it still prints only readiness metadata. Images stay in process memory and are not saved by the diagnostic.

`input_ready` and `platform_verified` are **always false**. A successful read-only observation is not authorization or proof of supported autonomous input.

## Explicit Misty backend selection

The default remains `legacy`; no user configuration is changed automatically. The [settings loader](../app/settings.py) accepts `MISTY_DESKTOP_BACKEND=legacy|cua` or a small explicit configuration at `config/desktop.json` containing `{"backend":"cua"}`. That configuration is optional and has not been created by this implementation. Invalid configuration refuses GUI input rather than silently selecting legacy.

```bash
MISTY_DESKTOP_BACKEND=cua .venv/bin/python misty.py
```

At this stage `cua` means the [read-only readiness controller](../app/brain/desktop_controller.py): GUI requests discover visible-window metadata and return the outstanding capability gate. They do not ask permission to click, capture arbitrary windows, load the visual model or send input. [Legacy pointer dispatch](../app/brain/tools.py) also rejects direct pointer/keyboard tool calls while this backend is selected, including confirmed calls, so the planner cannot bypass the gate. Ordinary non-GUI tools remain on their existing route. Backend selection is not a claim of full v2 rollout.

The separate [visual decision runtime](../app/brain/visual_decision.py) provides a task-scoped authenticated loopback multimodal server, real image blocks, exact schemas, strict one-decision validation, and bounded cleanup. A [guarded visual-runtime smoke check](VISUAL_RUNTIME_CHECK.md) now provides metadata-only verification by default and an explicitly acknowledged synthetic-model probe. It must pass an actual model/image probe before readiness and never executes tools itself. It has not been connected to the production input loop or validated on the installed GPU/model; fake-server tests do not establish that acceptance. Policy/target execution, model/voice handoff and controlled-app acceptance remain pending behind the host gate.

Optional dependency pins: [requirements-desktop-v2.txt](../requirements-desktop-v2.txt). They do not auto-install or upgrade on application startup.

## Observed installation and runtime evidence

Earlier implementation probes reported:

- `cua-driver 0.28.0` installed in Misty's local virtual environment from the Linux wheel. The [pinned PyPI metadata](https://pypi.org/pypi/cua-driver/0.28.0/json) describes a bundled Rust library and executable. Production must not depend on DSH's temporary AppImage mount.
- A real managed MCP handshake/catalog probe succeeded and its owned process cleanup was confirmed.
- A real `list_windows` call returned an empty `windows` array **inside the DSH sandbox**. This does not establish that the normal logged-in desktop has no windows or is unsupported.
- Session environment reported KDE and Wayland. Native target capture/input and a real image-bearing snapshot have not passed acceptance in the user's normal desktop session.

These are prior recorded observations, not new native probes performed by the diagnostic's mocked tests. The [design](DESKTOP_AGENT_V2_DESIGN.md#12-phased-delivery-and-gates) keeps desktop writes gated pending live capability, policy and controlled-application acceptance.

The adapter pins 0.28.0. Current [Cua platform documentation](https://cua.ai/docs/cua-driver/concepts/platform-support) describes KDE/KWin as experimental and its helper as read-only for important native input routes. It distinguishes XWayland windows from native Wayland windows and requires an explicit native-Wayland opt-in. Current documentation is not itself a guarantee of every pinned-version contract. Do not silently set an opt-in, install a compositor helper, request portal grants, escalate privileges or switch delivery/backend automatically to make a refusal disappear. A separately authorized, process-local native-Wayland discovery probe does not authorize input.

Cua has product telemetry and startup update checks by default. The provider sets the child-local environment `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` and `CUA_DRIVER_RS_UPDATE_CHECK=0`; it does not rewrite persisted preferences or enable history/recording.

## Run from the normal desktop terminal

Run as the logged-in desktop user, from the Misty workspace—not through the DSH sandbox, `sudo`, a headless SSH session or an unrelated display session:

```bash
.venv/bin/python scripts/check_desktop_v2.py
```

This performs catalog discovery plus `list_windows` only. A successful exit means discovery returned at least one visible window; it does **not** mean a screenshot or input was tested.

For one deliberate, read-only snapshot, supply an exact Cua-native integer process/window pair selected from Cua discovery:

```bash
.venv/bin/python scripts/check_desktop_v2.py --pid 12345 --window-id 67890
```

The numbers above are placeholders. Do not use a title, list position, invented step reference or KWin UUID as the native numeric window ID. Both flags are required together. The diagnostic first confirms exactly one matching visible discovery target; only then does it request a snapshot using `max_elements=100` and `max_depth=8`. It never uses `screenshot_out_file`.

If native identifiers are not already known, this optional read-only discovery snippet prints **only** process/window identifiers, without titles or accessibility contents:

```bash
.venv/bin/python - <<'PY'
import json
import time
from app.desktop.providers.cua_mcp import CuaMcpProvider
p = CuaMcpProvider()
try:
    p.start(deadline=time.monotonic() + 15)
    result = p.call('list_windows', {'on_screen_only': True},
                    deadline=time.monotonic() + 10)
    state = result.structured
    windows = state.get('windows', []) if isinstance(state, dict) else []
    print(json.dumps({'targets': [
        {'pid': w['pid'], 'window_id': w['window_id']}
        for w in windows if isinstance(w, dict)
        and isinstance(w.get('pid'), int) and not isinstance(w['pid'], bool)
        and isinstance(w.get('window_id'), int) and not isinstance(w['window_id'], bool)
    ]}))
finally:
    print('cleanup:', p.close().stopped)
PY
```

Use local process inspection to identify the intended application's PID. Do not choose an arbitrary window merely because its ID is available.

Optional `--timeout N` bounds the total observation admission deadline to 1–120 seconds (default 30). Provider cleanup has its own bounded termination period. Interrupting the command requests cancellation and closes the owned provider; no input has been admitted in either mode.

## Optional non-capture health

```bash
.venv/bin/python scripts/check_desktop_v2.py --health
```

This adds only the explicitly selected `binary_version`, `platform_supported`, `session_active` and `ax_capability` checks. The adapter refuses an empty/omitted include list, `skip`, capture checks and arbitrary check names before contacting the driver. It also reports sanitized session-family/native-opt-in/PID-namespace hints and queries D-Bus `NameHasOwner` for KWin and Cua's helper. These queries do not load a helper, activate a window or request permissions. Raw health messages/hints are not printed. The optional health tool does not become required for older catalogs.

The user's earlier normal-terminal result was zero visible windows with default Cua discovery. A subsequently authorized native-Wayland probe in the agent's sandbox also returned zero windows. The enhanced sandbox report showed basic health/accessibility checks passing, KWin present, `org.cua.KWinTarget` absent, and a `bwrap` PID-namespace hint. This is not proof that the normal host's native opt-in has been tested successfully.

The **pinned** [KWin adapter source](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.0/libs/cua-driver/rust/crates/platform-linux/src/wayland/kwin_helper.rs) establishes a separate limitation: its raw-input capability function returns false and its focused-window input wrapper refuses rather than invoking the action. Installing the read-only discovery helper alone would therefore not establish supported target-addressed raw input. Misty's window UUIDs must not be substituted for driver-native tokens to evade this boundary. A different explicitly selected provider or a proven supported driver route is required before input can be integrated for this host.

## Reading the result

| Field / gate | Meaning |
|---|---|
| `available` | The pinned provider negotiated successfully, not a declaration of input support. |
| `catalog_tool_count` | Number of reviewed observation tools exposed by this adapter, not every installed native tool. |
| `visible_window_count` | Visible discovery records returned in this process/display context. |
| `discovery_only_snapshot_not_checked` | Discovery passed; no snapshot requested. |
| `no_visible_windows` | Discovery returned zero windows here; investigate session/sandbox/XWayland exposure. |
| `exact_visible_target_not_found` | Requested numeric pair was absent, ambiguous, hidden or malformed; no snapshot requested. |
| `snapshot_target_mismatch` | Snapshot metadata did not exactly match the selected integer PID and window ID. |
| `image_block_present` | At least one MCP image block exists; bytes may still be invalid. |
| `image_decoded` | Every admitted image passed strict base64, bounded raster decoding and reported MIME/dimension consistency checks. |
| `snapshot_not_grounded` | Driver reported a screenshot error or non-valid frame; no readiness claim. |
| `surface_identity_unproven` | Preserved known driver refusal category; do not substitute another screen capture silently. |
| `snapshot_image_missing` / `snapshot_image_invalid` | Tree/text-only result or unusable image cannot satisfy visual observation readiness. |
| `observation_ready` | One matching, non-refused, decoded observation passed this diagnostic. Does not prove causal freshness, coordinate calibration, semantic correctness, model readiness or input support. |
| `observation_only_input_disabled` | Read-only snapshot passed; writes remain disabled. |
| `cleanup_unconfirmed` | Owned process shutdown could not be established; result is non-ready even if observation had passed. |
| `input_ready: false` | Unconditional safety gate. No diagnostic result enables input. |

Exit statuses: `0` = requested read-only discovery/snapshot passed; `2` = invalid CLI usage or an unverified/failed gate; `130` = keyboard cancellation when cleanup succeeds. Unconfirmed cleanup returns `2`. Neither success status implies the desktop task was completed.

## Validation and remaining acceptance

The health/smoke-check extension passed 70 focused tests and full discovery ran 2,220 tests successfully (one skipped). Live read-only native discovery still returned zero windows, even while the selected non-capture health checks passed. The default visual smoke check found all artifacts but launched no model. No desktop input, helper installation, permission request or real multimodal inference was performed in this follow-up.

[Mocked diagnostic tests](../tests/test_check_desktop_v2.py) cover default discovery-only behavior, exact target binding, bounded snapshot parameters, correct PNG decoding, malformed base64/image bytes, MIME/dimension mismatches, image limits, missing images, Wayland refusal, no private prose in summaries, unconditional input gating, cancellation and cleanup failure. They do not call a native driver or start a model.

Before enabling desktop writes, obtain a real exact-target image in the normal session, validate the local multimodal decision runtime, enforce task/target policy and cancellation, and run the controlled-app acceptance matrix. Keep unsupported native input routes refused; a blank sandbox discovery result is neither a reason to declare the host unsupported nor permission to bypass this gate.
