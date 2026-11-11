# Misty integration ledger

Updated: 10 November 2026 (post-audit remediation and QOL batch). This tracks what is live, what is staged, and which safety contracts the execution path enforces. Each entry names the code that implements it, so a claim can be checked instead of trusted.

## Live in the application path

| Area | Status | Implementation |
|---|---|---|
| F8 audio → whisper.cpp → transcript | live | `app/engine/voice_controller.py`, `app/speech/transcriber.py` |
| Local Qwen plan → validate → execute → summarize | live; one fixed plan per request plus at most one read-only recovery replan | `app/brain/agent.py`, `app/brain/planner.py` |
| Result references between plan steps | live; `$steps.<id>.result.<field>` bound only to earlier `depends_on` steps | `planner.substitute_references` |
| Intent confidence, clarification, per-step reason | live; clarification answers carry the original request into the next plan | `agent.run(transcript, context=...)` |
| Negation and compound-request guards | live; direct routing declines negated/compound requests instead of executing a fragment | `app/brain/direct_actions.py` |
| KDE windows, screens, focus/state/close/move | live; targets resolved per step from the request clause | `tools.resolve_window_target`, `agent._hydrate_window_context` |
| Named-window visual inspection with overlay masking and desktop visibility | live | `app/desktop/vision/*` |
| Accessibility list/inspect/activate | live; activation binds the resolved canonical control label | `app/desktop/accessibility/activator.py` |
| Text/HTML/Markdown/PDF/DOCX/ODT document reading | live; converters bounded by one absolute deadline | `app/documents/*` |
| Public single-page web fetch | live; connections bound to validated addresses, redirects re-validated | `app/web/fetch.py` |
| Isolated browser automation | live; URL fence installed on the browser context | `app/browser/session.py` |
| Scoped filename search, file operations, transfers, ZIP | live; descriptor-anchored publish/remove | `app/actions/*`, `app/actions/fs_safe.py` |
| Duplicate finder, ripgrep text search | live; visited-entry/byte/time budgets | `app/actions/duplicates.py`, `app/actions/text_search.py` |
| Knowledge index and purge tools | live; hidden credential paths excluded; deleted sources labelled | `app/knowledge/index.py` |
| Memory, reminders, calendar, media | live | `app/brain/memory.py`, `app/automation/reminders.py`, `app/actions/calendar_ics.py`, `app/desktop/media.py` |
| Audit log and task journal | live; tool outcomes logged without content | `app/engine/audit_log.py` |

## Safety contracts enforced on the execution path

| Contract | Where | Guarantee |
|---|---|---|
| Approval bound to a request | `app/engine/confirmations.py`, `voice_controller.py`, `main.py` | A confirmation resolves only the exact request ID captured when listening began. Missing or mismatched IDs never approve anything. |
| Approval is argument-aware | `agent._requires_confirmation`, `tools.requires_confirmation(name, arguments)` | A search or fetch carrying private data prompts before it leaves the machine. |
| No automatic replay of mutations | `policy.is_retryable`, `policy.is_transient_failure`, `agent._execute_with_single_retry` | Only read-only tools are retried, only once, and only on an explicit transient marker. Uncertain or partial outcomes are never retried. |
| Outbound data policy | `app/brain/egress.py`, `policy.requires_confirmation_for` | URLs, queries and form text are classified; private payloads require approval. |
| Honest completion reporting | `agent` execution report | Original failures stay in the final report; recovery is labelled `phase: "recovery"` and never reported as “all actions succeeded”. |
| Activity privacy | `app/engine/activity_store.py` | Events carrying private tool results are redacted; retention and purge are available. |
| Bounded IPC | `app/engine/popup_ipc.py` | Message, line, buffer and per-read limits; leftover lines are drained exactly once. |
| Prompt hardening | `agent.run` | Mutations are never auto-retried; untrusted page text is data, not instructions. |

## Quality-of-life features (added after the audit)

| Feature | Status | Where |
|---|---|---|
| Follow-ups across turns ("open it", "the one from October") | live; bounded, private tool facts only, TTL 15 min | `app/brain/session_context.py`, `agent.run(..., context=...)` |
| Task card: live steps, Cancel, Retry failed step | live | `app/ui/task_card.py`, `VoiceController.cancel_active_request/retry_last_failed_step` |
| Read the selected text (accessibility, clipboard opt-in) | live tool `read_selection` | `app/desktop/accessibility/selection.py` |
| Preview and replace the selection | live tools `preview_selection_replacement` (read) and `replace_selection` (approval, AT-SPI then clipboard) | `app/desktop/accessibility/editable.py`, `app/desktop/ydotool_backend.py` |
| Reveal or open the file just found | live tools `reveal_in_file_manager`, `open_last_result` | `app/brain/tools.py`, `app/actions/executor.py` |
| Activity retention controls | live: delete older than 30 days, clear all | `app/ui/main_window.py` |
| Honest research relevance | live: a result must cover most of the topic | `app/brain/tools.py` |

Native typing/insertion stays opt-in: `ydotool` needs the `input` group and a running `ydotoold`, and fails closed otherwise.

## Staged, isolated, and intentionally not wired

| Module | Status | Why it stays out |
|---|---|---|
| `app/brain/model_lease.py` | live | Created at app startup; the controller registers `reasoning` and drives load/unload through `hold_model()`. Ownership rule preserved: it never unloads a model another live request owns. |
| `app/desktop/input.py` | live through `ydotool` | Wired behind approval and the verified-pointer gate. Native input stays opt-in and requires the `input` group, `/dev/uinput` and a running `ydotoold`. |
| `app/plugins/manifest.py` | validation only | No loader until trust, consent and signing are designed. |
| `app/brain/recovery.py` | live as a policy input | Only decides whether one read-only replan is offered; the per-call retry lives in the agent. |

## Open decisions and known limits

- **Browser fence is URL-based.** It blocks private literals and redirects at the router, but Chromium resolves DNS itself, so a rebinding server is only fully stopped by an egress proxy or firewall rule. Fetches bound at the socket layer are not affected.
- **Local TTS** has no engine selected; voice feedback is off.
- **Native text entry** is wired through the opt-in `ydotool` backend (`desktop_type_text`) and fails closed when `ydotool` or `/dev/uinput` is unavailable.
- **SearXNG** is optional; `web_search` is enabled when the local instance answers. Without it, only direct fetch and the isolated browser are available.
- **OCRmyPDF** is installed under `data/pipx`; scanned-PDF fallback is live.
- **Reproducibility** is documented in `README.md`, `requirements.txt` and `MANIFEST.lock`. The venv still inherits system site-packages; treat the pins as the authoritative set and the system packages as recorded prerequisites.

## Verification

Run the full suite in a read-only sandbox:

```
bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --unshare-net --die-with-parent \
  --unsetenv DBUS_SESSION_BUS_ADDRESS --unsetenv DISPLAY --unsetenv WAYLAND_DISPLAY \
  --unsetenv MISTY_MEDIA_LIVE_TEST --setenv QT_QPA_PLATFORM offscreen \
  --setenv PYTHONDONTWRITEBYTECODE 1 \
  .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

The suite is now 1900 tests (2 skipped). `tests/test_audit_regressions.py` pins the defects proven by the 10 November 2026 audit so they cannot quietly return, and mutation checks confirm several of them fail when the fix is reverted.
