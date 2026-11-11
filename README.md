# Misty

Misty is a local desktop assistant for KDE Plasma on Linux. Hold a key, speak
in English or Polish, and Misty transcribes the request, plans a short sequence
of tool calls with a local Qwen model, executes them, and shows a summary. No
account, cloud API key, or remote model is involved; everything runs on the
machine (see [Privacy model](#privacy-model)).

This is a personal tool, not a packaged product. The private development repository
is [InfernoEye/Misty](https://github.com/InfernoEye/Misty); there is no published
release. Start with the [cross-PC setup guide](docs/CROSS_PC_SETUP.md) when cloning
on another machine. Provisioning is recorded in [MANIFEST.lock](MANIFEST.lock).
Models, virtual environments, private runtime data and secrets are not shipped.
See [repository contents and exclusions](docs/REPOSITORY_CONTENTS.md) for the exact
publication boundary and [Desktop v2 capabilities](docs/DESKTOP_V2_CAPABILITIES.md)
for the experimental Cua backend's current read-only gate. A separately selected
[native KWin visual pilot](docs/NATIVE_KWIN_PILOT.md) now connects images/OCR tokens
to the local visual model; it is enabled by selecting `kwin` (`MISTY_KWIN_INPUT=0` gives strict
preview) and has no live acceptance claim yet. Ordinary structured tools, including window-focused
and window/display operations, bypass visual routing and retain their own validation and policy. Web answers summarise and cite the
sources the research tool actually retrieved; see [research quality](docs/RESEARCH_QUALITY.md).

## Architecture

1. **Hold-to-talk** — the KDE GlobalAccel helper (`kde-shortcut/build/misty-shortcut`)
   registers the shortcut (default `Launch (5)`, `config/shortcut.json`).
2. **Snapshot** — microphone audio arrives through `sounddevice` / PortAudio
   (`app/speech/recorder.py`), bounded in length.
3. **Whisper** — the WAV goes to `third_party/whisper.cpp/build/bin/whisper-cli`
   with `models/ggml-large-v3-turbo-q5_0.bin`.
4. **Planner** — `app/brain/agent.py` starts `llama-server` on demand
   (`models/Qwen3-4B-Q4_K_M.gguf`, Vulkan) and asks for one JSON plan
   (`response_format: json_object`), validated by `app/brain/planner.py`.
   At most one read-only recovery round follows a failure. The files under
   `config/grammars/` are reference grammars for the tool-call shape; no Python
   code reads them at runtime.
5. **Policy / approvals** — `app/brain/policy.py` decides which tools are
   read-only, which need confirmation, and which are refused; egress tools are
   fenced separately.
6. **Tools** — file operations, document conversion, screen capture/OCR/VLM,
   window control, clipboard, browser (Playwright), web search (SearXNG).
7. **Summary** — the transcript, the plan, per-step results and any approval
   requests are reported to the UI.
8. **GUI + popup IPC** — `app/main.py` runs the Qt main window; a separate
   `app/popup_process.py` process renders the floating response window.
9. **Transport** — the two processes talk over a local (Unix-socket) IPC
   channel in `app/engine/popup_ipc.py`, with confirmations in
   `app/engine/confirmations.py`.
10. **Logs** — `logs/audit/` keeps an audit trail; `logs/llama-server.log`
    captures the model server.

## Requirements and installation

Verified on Arch Linux on 2026-11-10 with CPython 3.14.10. Exact versions are
in [requirements.txt](requirements.txt) (Python), [requirements-system.txt](requirements-system.txt)
(system packages) and [MANIFEST.lock](MANIFEST.lock) (the observed inventory).

### 1. Virtual environment

Create it exactly as the recorded one was (`.venv/pyvenv.cfg`):

```bash
/usr/bin/python -m venv .venv
```

The existing `.venv` has `include-system-site-packages = true`, which means
most libraries (PySide6, Pillow, dbus-python, PyGObject, beautifulsoup4,
psutil) are inherited from `/usr/lib/python3.14/site-packages` and only
numpy, sounddevice and playwright are installed inside `.venv`. A fresh venv
created with the command above is isolated; then either install the pins:

```bash
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
```

or keep system site packages and make sure the distro packages match the pins
(`scripts/check_deps.py` verifies that). dbus-python and PyGObject have no
wheels and need the distro build dependencies.

### 2. System packages

See [requirements-system.txt](requirements-system.txt) for the list and why
each one is needed: Qt6 + KF6GlobalAccel and a C++ toolchain for the shortcut
helper, ffmpeg, poppler-utils, pandoc, ripgrep, tesseract, wl-clipboard,
ydotool (plus `input` group and `/dev/uinput`), xdotool, git, and Vulkan
loader/ICD packages. OCRmyPDF is not a system package here: it lives in a pipx
venv under `data/pipx` and is called as `data/bin/ocrmypdf`.

### 3. Vendored binaries

`third_party/llama.cpp` (commit `a4d4f2fd4ec85ec6a0a9ece4870416c1b6cc24f5`) and
`third_party/whisper.cpp` (`bd9e1a12352fa57e448bdea6a0649aa9c24c64aa`) are pinned
Git submodules. After cloning, run `git submodule update --init --recursive`.
The manual equivalent, if provisioning without submodules, is:

```bash
git clone https://github.com/ggml-org/llama.cpp third_party/llama.cpp
git -C third_party/llama.cpp checkout a4d4f2fd4ec85ec6a0a9ece4870416c1b6cc24f5
cmake -S third_party/llama.cpp -B third_party/llama.cpp/build -DGGML_VULKAN=ON
cmake --build third_party/llama.cpp/build -j

git clone https://github.com/ggml-org/whisper.cpp third_party/whisper.cpp
git -C third_party/whisper.cpp checkout bd9e1a12352fa57e448bdea6a0649aa9c24c64aa
cmake -S third_party/whisper.cpp -B third_party/whisper.cpp/build
cmake --build third_party/whisper.cpp/build -j

cmake -S kde-shortcut -B kde-shortcut/build
cmake --build kde-shortcut/build -j
```

The VLM is used through `llama-mtmd-cli` from the llama.cpp build.

### 4. Models (not shipped, not downloadable by Misty)

| Path | Purpose | Size (bytes) |
| --- | --- | --- |
| `models/Qwen3-4B-Q4_K_M.gguf` | planner | 2,497,280,256 |
| `models/ggml-large-v3-turbo-q5_0.bin` | whisper.cpp speech model | 574,041,195 |
| `models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf` | VLM | 5,027,784,800 |
| `models/vision/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf` | VLM projector | 752,289,728 |

The expected sha256 of each file is in `MANIFEST.lock`; compare after copying
them in. Playwright's Chromium bundle under `data/playwright-browsers` can be
fetched with `.venv/bin/python -m playwright install chromium` (network access
required; `app/browser/session.py` points Playwright at that directory).

## Running

```bash
.venv/bin/python misty.py            # normal desktop launch (sets Qt/Wayland env first)
.venv/bin/python -m app.main         # same application, without the Qt env preconditions
.venv/bin/python popup_preview.py    # UI preview of the popup (no voice, no engine)
```

`misty.py` is the supported entry point: it exports
`QT_WAYLAND_DISABLED_INTERFACES=xdg_session_manager_v1` and
`QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` before importing `app.main`, which then
enforces a single instance with `/tmp/misty-<uid>.lock`. `app/main.py` has an
`if __name__ == "__main__": main()` block, so `.venv/bin/python -m app.main`
starts the same application but without those two environment variables.

The voice helper is not a separate command: `app/engine/voice_controller.py`
defines `HELPER = ROOT / "kde-shortcut" / "build" / "misty-shortcut"` and starts
that binary itself, passing the shortcut sequence as `MISTY_SHORTCUT`. Build it
first (step 3), otherwise voice activation is unavailable. `launcher.py` is an
older standalone launcher that also spawns the same helper and
`popup_preview.py --service`; it is not the entry point in use.

## Tests

The suite is run isolated from the desktop, the network and the user's data:

```bash
cd ~/misty
bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --unshare-net \
  env QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

The tests use fakes and temporary directories; several live checks (microphone,
GPU, KWin, browser, real models) are opt-in and are not part of this command.
Provisioning itself can be verified with:

```bash
.venv/bin/python -B scripts/check_deps.py          # PASS/FAIL report, non-zero on failure
.venv/bin/python -B scripts/check_deps.py --json   # machine-readable
```

`make test` and `make deps-check` wrap these commands.

## Configuration

| File | Status | Contents |
| --- | --- | --- |
| `config/shortcut.json` | active | `{"sequence": "Launch (5)"}`; written by Settings → Voice |
| `config/audio.json` | active | `{"mode": "system"}` — chosen input device |
| `config/searxng/settings.yml` | active | SearXNG instance config (bind `127.0.0.1:8888` in this instance) |
| `config/grammars/*.gbnf` | reference | tool-call shape, not read at runtime |
| `config.toml` | **descriptive only** | documents intent and says where values really live; the only value read at runtime is `[decision] gpu_backend`, shown as a label in Settings |

Runtime settings are constants and environment variables:

| Variable | Default |
| --- | --- |
| `MISTY_LLAMA_SERVER` | `third_party/llama.cpp/build/bin/llama-server` |
| `MISTY_MODEL_PATH` | `models/Qwen3-4B-Q4_K_M.gguf` |
| `MISTY_LLAMA_HOST` / `MISTY_LLAMA_PORT` | `127.0.0.1` / `9931` |
| `MISTY_LLAMA_GPU_LAYERS` | `99` |
| `MISTY_LLAMA_CONTEXT` | `32768` |
| `MISTY_MIN_CONFIDENCE` | `0.6` |
| `MISTY_SEARXNG_URL` | `http://127.0.0.1:8080` |
| `MISTY_WHISPER_CLI` | `third_party/whisper.cpp/build/bin/whisper-cli` |
| `MISTY_WHISPER_MODEL` | `models/ggml-large-v3-turbo-q5_0.bin` |
| `MISTY_WHISPER_THREADS` / `MISTY_WHISPER_DEVICE` | `8` / `0` |
| `MISTY_BROWSER` | `helium` (allowlisted desktop browser id) |

## Privacy model

- **Inference is local.** Whisper, the planner and the VLM run as local
  processes; neither audio nor screenshots are uploaded.
- **Public research is automatic.** A request that needs the web goes to the
  local SearXNG instance and Playwright; a read-only web search does not ask
  for approval. `config.toml` records `internet_requires_explicit_request =
  false` — treat that as policy, not as a guarantee: any egress is still
  visible in the audit log and obeys the egress fence.
- **Consequential actions need approval.** Writing, copying, moving and renaming
  files and folders, closing a window, forgetting a memory, writing the
  clipboard and every desktop input action wait for a click. The approval
  rules are declared in the policy table in `app/brain/policy.py`; a tool
  missing from that table fails closed and always asks. Approval requests are
  handled by `app/engine/confirmations.py`. (Misty has no file-delete tool.)
  Any egress tool whose payload looks private, or that the classifier cannot judge, is held the same way:
  URLs and queries carrying credentials, home paths, userinfo, secret-shaped
  tokens, long free text or registered private markers require approval
  (`app/brain/egress.py`). Plain public navigation and public-topic search do
  not ask.
- **Bounded and logged.** Captures, transcripts, file reads and child-process
  output are size- and time-bounded; actions are written to `logs/audit/`.

## Known limitations

- English and Polish only, and Whisper quality depends on the microphone and
  on Vulkan support for the large-v3-turbo model.
- Needs a KDE Plasma/Wayland session. Several features degrade to a clear
  "unavailable" message instead of a crash (AT-SPI accessibility, KWin capture,
  SearXNG offline).
- Native text entry (typing or pasting into a non-browser app) needs the
  opt-in `ydotool` backend, and that needs system setup Misty does not do for
  you: `sudo usermod -aG input "$USER"` (log out and back in), the `ydotoold`
  daemon running for your session, and a successful `ydotool type` smoke test.
  Until then every call fails closed with `uinput_unavailable`, and the
  clipboard route is only used after you approve it.
- `.venv` inherits system site packages, so a distro package upgrade can change
  behaviour without touching this project; the pins in `requirements.txt` are
  an observation, not an isolation guarantee.
- The SearXNG instance is a system service outside this project. Its
  `settings.yml` and Misty's default both use port `8080`; if you move the
  service, set `MISTY_SEARXNG_URL` to match or web search reports itself
  unavailable.
- The first model load is slow (several seconds to tens of seconds); the
  server is preloaded at startup, but after it is unloaded (by request, or
  during shutdown) the next request pays that cost again. The single-owner
  model arbiter (`app/brain/model_lease.py`) is wired in at startup: the
  controller registers the reasoning model, and `hold_model()` acquires and
  releases it so the vision service and whisper never share memory with it.
  The arbiter refuses to unload a model another live request is using.
- **Desktop automation** (opt-in, when `ydotool` and `/dev/uinput` are
  usable): `desktop_move_pointer`, `desktop_click`, `desktop_drag`,
  `desktop_scroll`, `desktop_type_text` and `desktop_press_key` let Misty drive
  the real desktop. Every one requires approval, coordinates are validated
  against your actual screens, and a click on a control that completes a
  purchase or order is refused outright. Pointer moves are checked against the
  real cursor position; a click or drag is refused when that position cannot be
  verified. A multi-step task ("research a good vacuum, open Amazon, search
  for it") runs as a bounded loop: observe the screen → one action →
  observe again, with limits on rounds, time and repeated actions. It stops
  and asks rather than clicking blindly.
- The assistant is a planner over a fixed tool list, not a general agent: it
  cannot write arbitrary code, and unsupported requests end in a summary saying
  so.
- No packaging, no uninstall path, no migration tooling for `data/*.sqlite3`.
- The test suite is unit-level with fakes. It does not prove a working
  microphone, GPU, KWin session or browser.
