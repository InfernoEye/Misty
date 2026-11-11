# Working on Misty

## Start here

- Current application code is in `app/`; `misty.py` is the supported entry point.
- Tests are in `tests/`. `launcher.py`, `popup_preview.py`, and historical snapshots are not the production entry point.
- Read `README.md` and `docs/CROSS_PC_SETUP.md` before provisioning another machine.
- `backups/`, `.edit_snapshots/`, and `.audit_archive/evidence/` preserve historical source and audit evidence. Do not confuse these copies with current code or apply a fix to all historical copies.

## Runtime and validation

- Target platform is Linux/KDE Plasma; voice, KWin, accessibility, input and local GPU models need real session integration.
- Default automated check: `.venv/bin/python -m unittest discover -s tests`.
- Tests use fakes for desktop/model behavior. Passing them does not prove live input, GPU compatibility, coordinate grounding or task completion.
- Do not drive the user's live desktop as a test without explicit approval and a controlled target.
- Preserve approval, cancellation, deadline, process-ownership and privacy boundaries. Never automatically replay uncertain input or fall back to another backend after a refusal.

## Desktop Agent v2 status

- `docs/DESKTOP_AGENT_V2_DESIGN.md` is the design, not a completion report.
- Cua 0.28.0 is an optional pinned dependency. The current adapter and explicit `cua` backend are read-only capability gates.
- Native host discovery/image/input acceptance remains unresolved; zero-window discovery was observed in both the agent sandbox and a user-provided normal-terminal result.
- The local visual decision runtime proposes strict image-grounded decisions but is not connected to an enabled production Cua input loop.
- An explicit native `kwin` foreground pilot now connects real visible-region images/OCR tokens to the visual runtime; input admission is enabled by selecting it (`MISTY_KWIN_INPUT=0` gives strict preview), and every click still requires separate approval. See `docs/NATIVE_KWIN_PILOT.md`; no live acceptance or atomic focus guarantee is claimed.
- Supported structured operations (especially window/display moves) must bypass visual routing. The narrow window-move shortcut retains normal validation/resolution/policy and requires observed destination state before claiming a verified move.
- Do not describe the staged integration as working autonomous desktop control or enable native-Wayland/input escalation silently.

## Repository and private assets

- Large model weights, installed environments/builds, caches, runtime databases, logs, recorded audio and secrets are deliberately excluded; see `docs/REPOSITORY_CONTENTS.md`.
- Native engines are pinned upstream Git submodules. Keep local modifications explicit and reproducible if you change them.
- Never commit real credentials or local SearXNG `settings.yml`. Use the sanitized example and generate a local secret.
- Do not add a model download URL unless its provenance, revision and expected hashes are established. `MANIFEST.lock` is historical inventory, not proof that every dependency is currently installed on another PC.
