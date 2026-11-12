# Contributing to Misty

Misty is a personal desktop assistant for KDE Plasma on Linux. The
[repository](https://github.com/InfernoEye/Misty) is private and Misty has no
published release, so contributions are limited to collaborators with write
access and are made directly on this repo.

By participating, you agree to this repository's [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

Read `README.md` and `docs/CROSS_PC_SETUP.md` to understand the system. Then
read `AGENTS.md` — it lists hard boundaries you must not break, including never
committing real credentials or SearXNG `settings.yml`, and never adding a model
download URL unless its provenance, revision and expected hashes are
established.

## Environment

1. Create the environment and install pins:
   `/usr/bin/python -m venv .venv` then
   `.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt`
2. Build native parts: `git submodule update --init --recursive`, then the
   commands in `README.md` under [Vendored binaries](README.md), including
   `cmake --build kde-shortcut/build`.
3. Place the models listed in `README.md` and check them with
   `.venv/bin/python -B scripts/check_deps.py`.

## Testing

Run the suite with:

```
.venv/bin/python -m unittest discover -s tests
```

Tests run offscreen (`QT_QPA_PLATFORM=offscreen`) and use fakes for desktop and
model behavior. Passing them does not prove live input, GPU compatibility,
coordinate grounding, or task completion. Do not drive the user's live desktop
as a test without explicit approval and a controlled target.

## Conventions

- `misty.py` is the supported entry point. New code belongs in `app/`; tests
  belong in `tests/`.
- `launcher.py` and `popup_preview.py` are not production entry points — don't
  treat them as such.
- Preserve approval, cancellation, deadline, process-ownership, and privacy
  boundaries. Keep the Escape cancel key and the OCR/egress fences intact.
- When a change touches the shared model server, keep the output budget and
  launch flags sensible so a summary, plan, or research answer still fits inside
  one context window.
- Update the [README](README.md) or [docs/](docs/) if you change a user-facing
  behavior, a public setting, or an environment variable.

## Reporting issues and security

- General issues and feature requests: open an
  [issue](https://github.com/InfernoEye/Misty/issues) using the matching
  template.
- Security vulnerabilities: see [SECURITY.md](SECURITY.md).
