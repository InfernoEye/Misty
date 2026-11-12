# Cross-PC setup

How to provision another machine from the private
[InfernoEye/Misty](https://github.com/InfernoEye/Misty) repository.
Verified on Arch Linux (KDE Plasma, Wayland, CPython 3.14.10) on 2026-11-10.
This is a development snapshot, not an installer: models, virtual
environments, runtime databases and secrets are excluded and must be
recreated locally (see [repository contents and exclusions](REPOSITORY_CONTENTS.md)).

## 1. Access to the repository

The repository is **private**. You need one of:

- `gh` CLI signed in as `InfernoEye` (or an invited collaborator), or
- a GitHub personal access token with `repo` scope, used as the git user:
  `git clone https://<user>:<token>@github.com/InfernoEye/Misty.git`
  (prefer `git config credential.helper` or the `gh` keyring over putting a
  token in the remote URL; remove the token from `git remote -v` afterwards).

Clone including the pinned native source:

```bash
git clone --recurse-submodules https://github.com/InfernoEye/Misty.git ~/misty
cd ~/misty
```

`third_party/llama.cpp` and `third_party/whisper.cpp` are upstream Git
submodules pinned to exact commits (see `.gitmodules` and
[MANIFEST.lock](../MANIFEST.lock)). If you already cloned without
`--recurse-submodules`:

```bash
git submodule update --init --recursive
```

## 2. System packages (Arch)

[requirements-system.txt](../requirements-system.txt) documents each package
and why it is needed; the names are Arch package names. Install the
essentials:

```bash
sudo pacman -S --needed python3.14 cmake g++ make git \
  qt6-base kf6-kglobalaccel \
  ffmpeg poppler-utils pandoc tesseract tesseract-data-eng tesseract-data-pol \
  ripgrep wl-clipboard ydotool xdotool \
  vulkan-icd-loader vulkan-headers vulkan-tools vulkan-radeon
```

(AMD is assumed; use `nvidia-utils` on NVIDIA. Replace `vulkan-radeon` /
Qt/KF names with your distro's equivalents on other distros.)

Optional, only for desktop-automation input (`desktop_type_text` and friends):
add your user to the `input` group and start the ydotool daemon in your
session:

```bash
sudo usermod -aG input "$USER"   # then log out and back in
ydotoold &
ydotool type "ok"                # smoke test
```

Without this, injected typing fails closed with `uinput_unavailable`.

`desktop_type_text` returns `verified: true` only when the focused field's text
grew by exactly the typed string, `false` when it did not, and `null` when the
field cannot be read through AT-SPI. Check that value after a typing test.

## 3. Python environment

```bash
/usr/bin/python -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
```

Notes:

- `dbus-python` and `PyGObject` have no wheels; they must come from the
  distro (e.g. `sudo pacman -S python-dbus python-gobject`), or install with
  the distro build dependencies.
- `requirements.txt` is an observation of the recorded environment, not an
  isolation guarantee: the original `.venv` inherited system site packages.
- Optional gated Desktop-v2 stack:
  `.venv/bin/python -m pip install -r requirements-desktop-v2.txt`
  (pinned `cua-driver==0.28.0`; do not auto-upgrade — newer versions need
  acceptance again).
- OCRmyPDF is a pipx install, not a pip package:
  `pipx install ocrmypdf` and place/symlink the entry point at
  `data/bin/ocrmypdf` (the app prefers that path over `PATH`).

## 4. Build the native parts

```bash
cmake -S third_party/llama.cpp -B third_party/llama.cpp/build -DGGML_VULKAN=ON
cmake --build third_party/llama.cpp/build -j

cmake -S third_party/whisper.cpp -B third_party/whisper.cpp/build
cmake --build third_party/whisper.cpp/build -j

cmake -S kde-shortcut -B kde-shortcut/build
cmake --build kde-shortcut/build -j
```

Expected outputs used at runtime:

| Path | Used by |
| --- | --- |
| `third_party/llama.cpp/build/bin/llama-server` | planner model server |
| `third_party/llama.cpp/build/bin/llama-mtmd-cli` | VLM (vision) tool |
| `third_party/whisper.cpp/build/bin/whisper-cli` | speech transcription |
| `kde-shortcut/build/misty-shortcut` | hold-to-talk shortcut (launched by Misty itself) |

## 5. Models (not shipped, not downloaded by Misty)

The four model files are deliberately **not** in Git (8.3 GiB total; far
beyond ordinary Git and never uploaded via LFS or release assets). Obtain
them from a trusted source you control, place them at:

| Path | Size (bytes) |
| --- | --- |
| `models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf` | 5,027,784,800 |
| `models/ggml-large-v3-turbo-q5_0.bin` | 574,041,195 |
| `models/vision/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf` | 5,027,784,800 |
| `models/vision/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf` | 752,289,728 |

Then verify each file against the sha256 recorded in
[MANIFEST.lock](../MANIFEST.lock) before first use. A filename does not
establish provenance, revision or license — verify the hash or do not trust
the file. Do not add download URLs to this repository without established
provenance and expected hashes.

Browser: `.venv/bin/python -m playwright install chromium` (writes into
`data/playwright-browsers`; `app/browser/session.py` points Playwright there).

## 6. Local configuration and secrets

- SearXNG: copy the sanitized example and generate a fresh local secret:

  ```bash
  mkdir -p config/searxng
  cp config/searxng/settings.example.yml config/searxng/settings.yml
  # replace __GENERATE_LOCAL_SECRET__ with a new random value
  ```

  The real `settings.yml` is never committed. If your SearXNG service does
  not listen on `http://127.0.0.1:8080`, set `MISTY_SEARXNG_URL`.
- `config.toml` is descriptive only; live settings live in `config/shortcut.json`,
  `config/audio.json` and environment variables (see README
  "Configuration").
- Never copy this PC's `data/` (SQLite databases), `logs/`, recorded audio or
  any `.env`/key files to another machine. The new PC starts with empty
  runtime state, which is expected.

## 7. Verify the provisioning

```bash
.venv/bin/python -B scripts/check_deps.py            # PASS/FAIL report
.venv/bin/python -B scripts/check_deps.py --json     # machine-readable
make deps-check
```

Unit tests (isolated from desktop/network; fakes for model and desktop
behaviour):

```bash
make test
# or explicitly:
bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --unshare-net \
  env QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

The last verified run (2026-11-12, `main` at f50ea4c): 2,541 tests, OK (1 skipped). Passing tests does **not**
prove live microphone, GPU, KWin or browser behaviour; exercise those in the
real session. `scripts/check_desktop_v2.py` reports the optional Cua
capability gate separately.

## 8. Run

```bash
.venv/bin/python misty.py
```

`misty.py` is the supported entry point (sets the Qt/Wayland environment,
enforces a single instance). In a KDE Plasma/Wayland session, hold the
shortcut (default `Launch (5)`) and speak in English or Polish.

## 9. Access and collaboration notes

- Only `InfernoEye` (and explicitly added collaborators) can read or write
  the repository. To add another PC's account, grant it `read` or `write`
  under Settings → Collaborators; a fresh clone with a token from that
  account is the supported way for the other PC to work.
- A private repository does not by itself grant any external service (for
  example the ChatGPT GitHub connector) access, execution or write
  permission; that requires a separate, supported authorization. Review
  externally proposed changes before merging; the other PC pulls the merged
  branch and recreates its local runtime.
- Commit history: this snapshot is a reviewed, deliberately curated commit —
  it is not a byte-for-byte backup. Keep machine-level backups on a separate
  encrypted channel if you need one.
