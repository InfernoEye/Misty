# Misty implementation record — first batch and safety foundations

Date: 10 November 2026. Status: implemented and covered by local tests. No live desktop, microphone, GPU, browser, or model test has been claimed here.

## 1. What changed

### Patch 1 — regression isolation and active policy

- `app/brain/agent.py`: split the unused prompt into `CORE_POLICY`, `TOOL_SEMANTICS`, `PLANNING_INSTRUCTIONS`, and `FINAL_RESPONSE_INSTRUCTIONS`. The actual planning and summary requests now include language rules, local-memory/privacy rules, no-invention rules, and the untrusted-tool-result rule.
- Registry tests now inject temporary `MemoryStore` instances instead of initializing `data/memory.sqlite3`.
- New assertion: planning and summary requests contain the shared policy and tool-routing guidance.

### Patch 2 — named-window inspection with provenance

- `inspect_screen` gains `mode="window"` with `window_id`. The registry resolves a name to one exact live window through the existing `resolve_window_target()`; ambiguous or missing targets fail before capture.
- `active_window` keeps its existing meaning: the window saved before listening began. Focusing another window does not silently redefine it.
- `CaptureService.capture()` accepts `window_id` and returns `CaptureResult` fields for `window_id`, `window_title`, `window_geometry`, and `capture_kind`.
- Window capture reuses the existing validated geometry crop and labels it `visible_region`. The tool explicitly reports `occlusion_possible`, because this is a desktop crop rather than an isolated surface.
- Planner, tool description, and system policy now route named visual questions to `list_windows` then `inspect_screen` with `mode="window"`.

### Patch 3 — constrained folder scope

- New `app/actions/file_scope.py`: `UserFolderResolver` resolves approved folder keys under the user's home, accepts an in-home XDG relocation, rejects external relocation, traversal, absolute paths, missing paths, and symlink escape.
- `search_files` accepts optional `folder` and `relative_path`. A scoped search seeds only that root; a missing or invalid scope does not fall back to all of home.
- File search now streams directory entries, checks its time budget per entry, caps the traversal queue, and reports `scoped`, `scope`, and `truncation_reason`.
- `ActionExecutor` uses the same resolver for listing, metadata, and opening, so localized XDG folders resolve consistently.

### Safety foundations

- New `app/engine/confirmations.py`: one pending confirmation, unique request ID, 60-second deadline. A mismatched, late, or expired response cannot approve anything.
- The popup echoes the request ID; the core rejects decisions for a different request.
- `ClipboardBridge` stamps each request with a deadline and refuses to execute a queued write after that deadline. A timeout now means nothing changed.
- `ActivityStore` redacts `sensitive` tool payloads (visual inspection, clipboard, memory) and caps stored detail length. New private tool events are marked sensitive by the agent.
- `voice_controller.py` delegates confirmation state to the broker and keeps spoken confirmation working for the single pending request.
- Audit logging is now live: the application creates `logs/audit/audit.jsonl`, and every executed or denied tool call records request ID, action, status, duration, and an error code. Arguments, results, and page/document/clipboard content are never written.

### Follow-up patch from live tests

The first live run showed the API changes worked but routing still caused the wrong behavior:

- “Look at Helium…” offered `open_browser`, so the planner opened a new browser instead of inspecting the existing one.
- Repeated requests then failed in `inspect_screen`; ambiguous Helium windows or a just-launched window were the likely causes.
- The captured image included Misty's own popup over the browser.
- The scoped search worked, but the spoken query “MISTI - Scope - Marker” did not match `misty-scope-marker.txt`.

Changes:

- Visual requests without an explicit open/launch verb no longer offer launcher tools to the planner. “Open Helium and…” still keeps them.
- `inspect_screen` auto-selects a unique focused window when several candidates match, reports `target_auto_selected` and the number of other matches, and never guesses for mutating actions.
- Window captures mask Misty's own popup by its `misty-popup` window identity before OCR/VLM, and report `masked_overlays`.
- Filename search normalizes spaces/punctuation and tolerates close spelling variants (`misti` → `misty`) with a bounded similarity threshold. Each result carries `match_score`.
- Console diagnostics now print the requested inspection mode/window ID, the captured window and mask count, and the bounded failure message.

Post-fix local suite after the first follow-up: **205 tests, OK**.

### Live-test follow-up 2 — virtual desktops and popup overlays

The second live run clarified the remaining causes:

- With two Helium windows, inspection succeeded but captured wallpaper. All outputs use scale 1, so DPI was not the cause; the likely cause is a target window on another virtual desktop or otherwise not visible.
- `masked_overlays=0` because Misty's KWin query deliberately excluded `misty-popup` from the window list, so capture could not see its geometry.
- Polish inspection failed because the planner wrote `Heleium`; exact/substring matching rejected it.

Changes:

- The KWin query now reports `on_current_desktop` for normal windows and a separate internal `overlays` list for Misty's popup. The popup remains excluded from model-visible windows.
- Capture rejects a target explicitly known to be on another virtual desktop. Visibility comparison returns unknown rather than false when KWin's desktop identities cannot be compared, so it cannot reject every normal window.
- Window capture masks the popup using the internal overlay geometry and reports `masked_overlays`.
- `resolve_window_target(..., allow_fuzzy=True)` tolerates close spelling for read-only inspection (`Heleium` → `Helium`). Mutating actions do not enable fuzzy matching.
- The generated KWin JavaScript was extracted and passed `node --check`.
- Console diagnostics now include target title, geometry, image size, and mask count.

Post-fix local suite: **209 tests, OK**, plus the JavaScript syntax check. A regression test now locks the generated KWin script contract.

### Intent confidence and step reasons

- The planner now returns `intent_confidence` (0–1), `clarification`, and an optional short `reason` per step.
- Misty does not execute a plan when `clarification` is non-empty or when a non-empty plan reports confidence below **0.6**. It returns the clarification question, or a generic request to clarify, and stops.
- Step reasons appear in the local activity record alongside the existing plan details.
- The planner is instructed not to guess and execute a consequential action when confidence is low.
- Missing confidence defaults to 1.0 for compatibility with older plans; invalid values and overlong reasons/questions are rejected.

### Read-only accessibility tools

- New `app/desktop/accessibility/inspector.py` composes the existing AT-SPI service and returns bounded, JSON-safe, positional-path snapshots.
- Two registry tools are exposed: `accessibility_list_applications` and `accessibility_inspect_application`.
- Inspection is capped at 100 listed applications, depth 20, and 300 nodes by the tool; defaults are depth 6 and 80 nodes. Per-node action lookup is disabled for the planner to avoid repeated AT-SPI resolution.
- The adapter has no action-invocation path. Press/focus remains in the existing service/registry flow and is not reachable from these tools.

### Public web fetch tool

- New `app/web/fetch.py` and registry tool `fetch_web_page`.
- Only public HTTP(S), ports 80/443, no credentials, no cookies/auth, no localhost/private/link-local/multicast addresses, and every redirect is revalidated.
- Bounded bytes, text, links, redirects, and time. HTML scripts/styles/navigation are stripped; page text and links are explicitly untrusted data.
- The tool accepts one URL and fixed budgets; no model-supplied headers, proxy, upload, or raw HTML.
- Tests are fully mocked; no network was used.

### External document formats

- Added `app/documents/converters.py`: fixed local `pdftotext` and sandboxed `pandoc` invocations for `.pdf`, `.docx`, and `.odt`.
- No shell, URL, resource path, or model-supplied converter argument is used. Timeout, output-byte, and character limits apply; a converter that keeps writing is terminated.
- `DocumentReader` now routes those formats through the converter and reports `conversion_failed` safely when the tool is missing or the file is invalid.
- `read_document` advertises the new formats. `.doc`, `.xls`, `.ppt`, and other legacy Office formats remain unsupported.
- Verified with generated PDF, DOCX, and ODT fixtures; a filename containing shell metacharacters is passed as one argument and creates no side file.

### Bounded document reading

- New `app/documents/reader.py` provides read-only extraction for `.txt`, `.md`, `.html`, and `.htm` inside one approved root.
- New registry tool `read_document` requires an approved folder and a relative path, returns at most 20,000 characters, and never reads outside the folder.
- HTML scripts/styles are removed before text extraction. PDF and Office files return a clear unsupported-format result; no new dependency was added.
- The reader refuses traversal, symlink escape, directories, binary content, and inputs over 5 MB.

### Hardcoded-option audit

- Runtime paths and ports can now be overridden: `MISTY_MODEL_PATH`, `MISTY_LLAMA_SERVER`, `MISTY_LLAMA_HOST`, `MISTY_LLAMA_PORT`, `MISTY_LLAMA_GPU_LAYERS`, `MISTY_LLAMA_CONTEXT`, `MISTY_MIN_CONFIDENCE`, `MISTY_WHISPER_CLI`, `MISTY_WHISPER_MODEL`, `MISTY_WHISPER_THREADS`, `MISTY_WHISPER_DEVICE`, `MISTY_BROWSER`, and the existing `MISTY_SEARXNG_URL`.
- `config.toml` no longer claims F8 is the runtime shortcut, no longer names a nonexistent model, and now reflects auto public web research and the absence of TTS. Shortcut/microphone have their own JSON files.
- Settings → Integrations reads the configured SearXNG endpoint instead of always checking port 8080.
- Popup footer and selection text initialize from the saved shortcut rather than literal F8.
- KDE now reports the active binding as `Launch (5),Launch (5)`.

### Conversation and one-chord shortcut guards

- “Hello”, “Thank you”, and similar greetings/thanks now return a local conversational reply without starting Qwen or any tool. This stops the spurious browser launch after “Hello”.
- KDE global shortcuts support exactly one key/chord. The recorder now enforces one sequence; Save rejects comma-separated `9, 8` and explains why.
- The helper rejects multi-chord sequences and verifies the active key; KDE now reports `holdToTalk=Launch (5),Launch (5)`.
- Saving the already-active shortcut no longer forces a redundant helper restart.
- Expected SIGTERM during helper rebinding is no longer reported as an application error.

### Shortcut recorder repeat guard

- Held keys no longer append repeated chords (`F8, F8, F8…`).
- OS auto-repeat key press/release events are ignored while recording.
- Consecutive duplicate chords are collapsed even if Qt inserts them.
- Single press → one chord; multi-step still works with distinct chords.

### KGlobalAccel active shortcut fix

- The decisive bug: `KGlobalAccel::setShortcut` defaults to `Autoloading`, which ignores the supplied key and reloads the old active F8. The KDE config showed exactly `holdToTalk=F8,Launch (5)` — active F8, default the new key.
- The helper now passes `KGlobalAccel::NoAutoloading` for an explicitly configured shortcut, then verifies the active key and exits with an error rather than silently keeping F8.
- The helper logs the actually activated key.
- Remaining hardcoded F8 labels were removed from the popup footer/selection text, activity empty state, startup log, confirmation prompt, and no-audio message.

### Portable shortcut storage

- Root cause of “saved a new key but F8 still works”: `QKeySequenceEdit` saved localized NativeText (`Uruchom (5)`), which the C++ helper could not parse and silently replaced with F8.
- The UI now stores Qt PortableText (`Launch (5)`, `Ctrl+Alt+M`, …), normalizes old localized media-key names, and displays NativeText only in the UI.
- The helper rejects non-canonical/unparseable shortcuts instead of falling back to F8; it logs the configured key when ready.
- The old `Uruchom (5)` config was migrated to `Launch (5)`.

### Shortcut recorder polish

- A Record button now arms the shortcut field; press a key/chord and release, then Save.
- Captures happen after release; single keys, modifier chords, and multi-step sequences (e.g. `Ctrl+K, Ctrl+C`) are supported.
- Cancel restores the previous shortcut.
- The field is 46 px tall, the action buttons moved below it, and the odd focus outline on settings navigation was removed.

### Typed chat containment

- Typed requests no longer send `show` to the voice overlay; they hide it and stay in the main chat.
- Typed answers no longer open the separate full-response window, even for list/search tools.
- Voice requests still use the overlay and full-response window.
- Approval and window/content-selection prompts still surface when genuinely required.

### Named window accuracy

- Window-control requests now extract the user’s named target (“Move EquiCord…”) and resolve it by title, app ID, resource name/class before execution.
- A named target overrides a model-chosen window: it will not silently move the console when the user named Equicord.
- Known client aliases resolve: `EquiCord` and `Vesktop` can match a Discord window.
- If several windows match, Misty lists them and asks; if nothing matches, it asks instead of moving an unrelated window.
- Window-control plans no longer open the full-response window, even though they call `list_windows`.

### Window target regression fix

- “Move this window/cord to the main screen” no longer fails plan validation with `missing required argument 'window_id'`.
- Before validation, window-control steps with an omitted or deictic target resolve `window_id` from the snapshot captured when listening began.
- Named windows are never replaced by the active window.
- If no snapshot exists, Misty asks which window instead of returning an internal validation error.
- The planner prompt now always requires both `window_id` and `screen_name` for moves.

### Main window control center

- Chat page now has a real conversation view and typed input; typed and voice requests share the same pipeline, and final answers append to the transcript.
- Tray “Ask Misty…” focuses the chat instead of opening a separate input dialog.
- Settings → Voice has a hold-to-talk shortcut editor (`QKeySequenceEdit`). Saving writes `config/shortcut.json`, restarts the KDE helper with `MISTY_SHORTCUT`, and updates the chat hint.
- The helper accepts a key sequence from `MISTY_SHORTCUT` or argv, falling back to F8; it was rebuilt.
- Audio settings keep the microphone picker, add a “Test input” check for 16 kHz mono support, and still apply on the next recording.
- Settings → Integrations reports live availability of the reasoning model, llama.cpp, Whisper binary/model, OCRmyPDF, Playwright Chromium, SearXNG, KDE/KWin, and local file/calendar/media tools.
- Privacy text now reflects the real local web research, confirmation, and activity-history behavior.
- Scroll pages and the shortcut field use the dark theme; settings labels render `AI & Models` and `Privacy & Permissions` correctly.

### Action-based full-response window

- The window no longer depends only on answer length or URLs.
- A successful result-heavy read tool opens it even for short answers: file listings, searches, duplicate reports, knowledge results, calendar lists, screen/page inspection, document reads, transcriptions, and media metadata.
- Navigation tools (`open_url`, `search_site`) do not open it.
- Tool names are carried in activity events; the window rule is centralized in `should_show_full_response`.

### News-aware research

- Freshness/security queries (`newest`, `latest`, `recent`, `CVE`, `vulnerability`, `breach`, `exploit`, `patch`) now set SearXNG’s `news` category automatically.
- “Research the newest Linux vulnerability and summarize it” strips the research/summarize wrapper and searches the news category.
- News results are filtered for relevance: dictionary/translation hosts are dropped, and titles/snippets/URLs must mention the topic.
- Pages with fewer than 200 readable characters are skipped so the summary is not built from empty MSN shells.

### Topic reasoning

- Video requests now extract the real subject: “Find me a cool Minecraft video” → `minecraft video`; “Find me a cool video about Katniss” → `katniss everdeen video`.
- Requests with no subject (“something cool to watch”, “a cool video”) use a clearly labeled `popular videos` query instead of searching the bare adjective.
- The planner is instructed never to search bare adjectives; `web_search`/`web_research` reject all-vague queries such as “cool videos” and ask for a topic.

### Video query and KWin handle fixes

- “Find me a cool Minecraft video” without a site now routes to YouTube choices.
- “Find me a cool video on YouTube about Minecraft” keeps the topic: query is “cool video about Minecraft”, not just “a cool video”.
- “Open Google with Katniss Everdeen” builds a Google search URL.
- KWin window UUIDs with braces are normalized, so the saved active-window handle can reach ScreenShot2. Invalid handles fall through to the Spectacle crop instead of failing immediately.

### Duplicate detection and file transcription

- `find_duplicates` scans one approved folder, groups by size, verifies with SHA-256, and reports relative paths, reclaimable bytes, and truncation. Read-only.
- `transcribe_file` converts local audio/video to 16 kHz mono WAV with ffmpeg, then runs whisper.cpp with the existing large-v3-turbo model. Supported formats: wav, mp3, flac, ogg, m4a, opus, mp4, mkv, webm, mov.
- Both are private, local, and bounded; transcripts and paths are not logged.

### Arbitrary site searches

- “Open Google with Katniss Everdeen”, “Open Reddit for Misty”, and “Google Katniss Everdeen” now build a search URL instead of opening the site home.
- Supported search sites include YouTube, Google, DuckDuckGo, Wikipedia, GitHub, Arch Wiki, Stack Overflow, Allegro, Bing, Reddit, Amazon, eBay, Twitch, and Spotify.
- “Search on example.com for Misty” falls back to a Google `site:example.com` search when the site has no dedicated template.
- “Google is a search engine” is not mistaken for a query.

### Page and screen summaries

- “Summarize the page I’m on”, “summarize this page”, and “what am I looking at” route directly to `inspect_screen` with `mode=active_window`.
- “Summarize what I’m seeing on the left/right/center/primary screen” routes to `inspect_screen` with a screen alias.
- Screen aliases resolve from live monitor geometry: left = leftmost, right = rightmost, center = middle, primary = KScreen priority 1.
- The local VLM summary is returned directly; long summaries open the full-response window. OCR text is the fallback when the visual model is unavailable.
- No confirmation: these are read-only captures.

### KWin direct window capture

- `KWinWindowCapture` uses Plasma 6 `org.kde.KWin.ScreenShot2.CaptureWindow(s handle, a{sv}, h pipe)` with the window UUID, reads the raw QImage stream, and converts it to PNG.
- `CaptureService` tries this first for `mode="window"`, so occluded/offscreen windows can be captured without focus or raising.
- Unsupported API / D-Bus unavailability falls back to the existing Spectacle crop path; invalid-window, timeout, oversized, and invalid-image results fail without silently cropping the desktop.
- Tests cover pipe/returned-fd variants, raw conversion, PNG validation, caps, and fake-bus errors. No real screen capture is performed by tests.

### Local ICS calendar

- `IcsCalendar` lists and appends events in one approved `.ics` file with UTC/date/all-day/TZID handling, 2 MiB/2000-event bounds, atomic writes, and no delete/overwrite API.
- Tools: `list_calendar_events` (read-only, private) and `add_calendar_event` (approval required). Adding appends one VEVENT and preserves the rest of the file.

### Recursive folder transfer and text search

- `DirectoryTransferEngine` copies/moves whole trees inside approved folders with 1000-file, 8 GiB, depth-32 bounds; no symlinks or special files.
- Tools: `copy_folder`, `move_folder`; both require approval. Same-filesystem moves preserve metadata; copies publish 0700/0600 trees; cross-device moves verify before deleting the source.
- `grep_files` uses local ripgrep with argv only, no shell, bounded results, excluded directories, no symlinks, and fixed failure codes. It supports an optional relative subfolder and per-file match limit.

### ZIP archives

- `ZipEngine` creates ZIPs from 1-100 approved files and extracts into a new directory.
- Tools: `zip_create`, `zip_extract`; both require approval and show entry counts.
- Extraction rejects traversal, absolute paths, links, encryption, oversized entries, compression bombs, and existing destinations; failed extraction removes the partial tree.

### YouTube choices in the popup

- YouTube site searches now inspect the live results page in the isolated browser and show up to three real video titles in the popup.
- The user chooses with “1”, “2”, “3”, an ordinal, or a title fragment; the chosen video opens in the default browser. “Cancel” abandons it.
- Watch/trending intents such as “Find me something cool to watch”, “What’s trending on YouTube?”, and “What is popular on YouTube?” map to YouTube choices instead of generic web research.
- The popup shows all three choices at once (the transcript card is hidden during selection) and its post-response timeout is now 7 seconds. Selection waits remain 60 seconds.

### Local media control

- `MediaController` discovers local MPRIS players and controls only play, pause, play_pause, next, previous, and stop.
- Tools: `media_list_players`, `media_now_playing`, `media_control`. Track metadata is private; control has no confirmation because the action allowlist is narrow and local.
- Ambiguous players are refused with bounded candidates; no guessing. Live discovery returned Helium and other local players.
- Volume, seek, shuffle, loop, and playback-rate changes are deliberately not exposed.

### Bounded read-only recovery

- When a plan fails and every failure is a retryable read-only tool, the agent asks the local model for one fresh read-only plan.
- Writes, denied actions, pending user choices, irreversible effects, and repeated failures are never retried. At most six recovery steps.
- The recovery plan still uses the registry and audit log; task journal status is now `failed` for failed tasks, not always `completed`.
- `app/brain/replan.py` builds the bounded, content-free recovery context; tool arguments, messages, paths, and page text never enter it.
- A live-model loop is bounded to one replan; if it fails, the deterministic partial-failure report returns.

### Semantic accessibility activation

- `ControlActivator` resolves an application by exact name or PID, searches the bounded AT-SPI tree, and requires one unique control match.
- Tool: `accessibility_activate(app_name or process_id, control_name, role?)`.
- It refuses missing/ambiguous controls and returns bounded candidates instead of guessing.
- Pressing requires approval and revalidates the expected control name before invoking the existing AT-SPI service. Verification is reported honestly as verified/unverified.
- Real read-only AT-SPI discovery was confirmed; no control was pressed during verification.

### Scoped file copy, move, and rename

- `FileTransferEngine` supports files only, scoped to approved folders, with no symlink following.
- Tools: `copy_path`, `move_path`, `rename_path`. All require explicit approval and show a content-free preview.
- Copy streams, fsyncs, and atomically publishes; same-filesystem moves are atomic, cross-device moves verify durability before unlinking the source.
- Limits: 8 GiB per file, bounded paths/depth, no directory recursion, no overwrite unless explicitly requested.

### Local reminders

- `ReminderStore` persists reminders in `data/reminders.sqlite3` with WAL, 0600 permissions, UTC normalization, and bounded text.
- Tools: `set_reminder(text, minutes_from_now)`, `list_reminders(status?)`, `cancel_reminder(id)`.
- A 20-second QTimer checks due reminders and shows a tray notification once; missed reminders fire on the next check.
- Reminder text is private and excluded from activity redaction rules only where the user must read it.

### Knowledge formats

- The local knowledge index now includes PDF, DOCX, and ODT in addition to text, Markdown, and HTML. It uses the existing bounded reader and OCRmyPDF fallback.
- Invalid external documents are counted as errors, not silently skipped.

### Isolated browser automation

- `app/browser/session.py` runs one persistent, lazily started headless Chromium in a dedicated thread with a fresh context: no user profile, cookies, extensions, downloads, uploads, or JavaScript evaluation.
- Tools: `browser_navigate`, `browser_read`, `browser_fill`, `browser_click`, `browser_press`.
- Navigate/read are available without approval; click and press require explicit approval because they can submit data; fill never submits.
- Page text, URLs, and previews are treated as private/untrusted and redacted from activity details.
- Live test: `https://example.com` navigated and read through the real registry.

### Deterministic navigation routing

- Clear commands are resolved before the model runs: direct public URLs and known sites (YouTube, Google, Wikipedia, GitHub, Arch Wiki, Stack Overflow, Allegro, DuckDuckGo).
- “Open youtube.com”, “Proszę otwórz w przeglądarce YouTube’a”, and the glued transcription “OpenYouTube.com” all map to `open_url` with the site home.
- “Search YouTube for lo-fi hip-hop”, “Szukaj na YouTube …”, “Find X on Wikipedia”, and “Find me X on YouTube” map to `search_site`; leading “me/mi/for me” is stripped from the query.
- Natural requests such as “Find me on YouTube a cool Minecraft video”, “What are some cool Minecraft videos on YouTube?”, and “Show me cool Minecraft videos on YouTube” also map to a site search, with leading articles (`a/an/some/any`) stripped.
- Ambiguous research and multi-action requests still go to the local model. Domain-like filenames such as `misty-node.txt` are not mistaken for URLs.
- Direct actions still use the registry, audit log, and task journal; the model is not invoked.

### Full-response window

- Long answers (280+ chars) or answers containing public source URLs automatically open a separate resizable **Misty — Full response** window.
- The window shows the transcript, the full selectable text, and a bounded Sources list; double-click opens a validated public URL.
- Long comma-separated filename lists are split into indented monospace bullets; paragraphs, model bullet lists, headings, bold, and inline code are formatted.
- HTML in model output is escaped. Copy writes the original raw response to the clipboard.
- It has Copy, Open link, and Close buttons and never replaces the compact popup.
- Direct link and site-search actions do **not** open this window; the browser is the destination. The compact popup still shows the URL.
- The tray now includes **Show last response**, so any response can be reopened.
- The popup remains unchanged; short replies do not open the window.

### Bounded web research

- New `web_research` tool chains the local SearXNG search and the SSRF-safe page fetcher in one call.
- It reads at most 5 top public pages, returns titles, source URLs, bounded excerpts, and search snippets, and reports fetch failures.
- No dynamic plan rewriting is required: one tool call produces citable research material for the final local summary.
- Live test: query `arch linux`, two pages read, zero fetch errors.

### Installed optional components

- **SearXNG** container `misty-searxng` runs with host networking on `127.0.0.1:8080`, `--restart unless-stopped`, JSON enabled, Bing/Wikipedia/Mojeek defaults. Misty auto-detects the port; `MISTY_SEARXNG_URL` can override. A live query returned real Bing results.
- **Playwright 1.65 + Chromium** installed in the venv; browsers at `data/playwright-browsers` (701 MB). A headless smoke test passed.
- **OCRmyPDF 17.14.1** installed in an isolated pipx environment under `data/pipx`; executable at `data/bin/ocrmypdf` (139 MB). Tesseract 5.5.4 with `eng+pol` is available.
- Scanned/image-only PDFs now fall back to local OCRmyPDF+Tesseract when `pdftotext` returns no text.

### URL opening and site search

- New tools `open_url` and `search_site`.
- `open_url` accepts one validated public HTTP(S) URL, rejects credentials/local/private literals/non-web schemes, and opens it with `xdg-open` in the default browser.
- `search_site` builds encoded search URLs for YouTube, Google, DuckDuckGo, Wikipedia, GitHub, Arch Wiki, Stack Overflow, and Allegro. It does not scrape results.
- The planner is instructed to use these for “open youtube.com” and “search YouTube for X”, and to let the model compose them with reading tools.

### Recovery policy and read-only retry

- `app/brain/recovery.py` is implemented and tested: it decides whether a failed plan may be replanned, bounded by replans, steps, and consecutive failures.
- It refuses automatic recovery for denied actions, pending user choices, irreversible effects, and repeated failures.
- The agent currently uses a narrower live behavior: one automatic retry of an allowlisted **read-only** tool. No write, launch, deletion, or confirmed action is retried.
- Full model replanning is not wired yet; it requires refactoring the single fixed-plan executor and a fresh live test.

### Confirmation previews and input abstraction

- File confirmations now state whether the target exists and how many bytes will be written, without exposing content.
- `app/desktop/input.py` exists with validated pointer/keyboard actions, a fake-backend test suite, and no real backend. It is not wired into the registry.

### Typed requests

- The tray now has **Ask Misty…**, which opens a one-line input dialog.
- Typed requests use the same agent, registry, confirmations, audit log, and task journal as voice. They do not change the main window layout.
- Requests are capped at 2000 characters; busy/recording states reject a second request instead of racing.

### Task journal

- Each model run now starts and finishes one `data/tasks.sqlite3` task entry using the same request ID as the audit events.
- Labels are static (`desktop request`), not the transcript. Checkpoints are not yet populated.
- Failed runs are marked `failed`; normal returns are marked `completed` even when some plan steps failed, because the run itself finished.

### System telemetry tool

- `get_system_telemetry` returns a bounded read-only snapshot: CPU, load, memory, swap, uptime, process count, and root disk usage.
- No command lines, environment variables, usernames, or arbitrary paths are read. The existing lightweight `get_system_status` remains unchanged.
- CPU is a non-blocking snapshot and may be 0.0 on the first sample.

### Knowledge index and conditional web search

- `index_documents`, `knowledge_search`, and `knowledge_stats` are live when the local index opens at `data/knowledge.sqlite3`.
- Indexing is limited to one approved folder per call, uses `DocumentReader`, skips symlinks/excluded directories, and never modifies source files. Search returns bounded snippets and paths only.
- `web_search` is advertised only when `MISTY_SEARXNG_URL` points at a local SearXNG instance. Without it, no dead tool is offered.
- Both return values are treated as private/untrusted activity data.

### Central policy and file tools

- `ToolRegistry.requires_confirmation` now delegates to `app/brain/policy.py`; unknown tools fail closed. `close_window`, `forget_memory`, `write_clipboard`, and the new file tools require approval.
- New tools: `write_text_file`, `make_directory`, `trash_path`. All are confirmation-gated, bounded to approved folders, and use atomic writes plus read-back verification.
- `trash_path` moves items to `data/quarantine`; it never permanently deletes and refuses non-empty directories unless `recursive` is set.
- File content is excluded from activity details and confirmation arguments; the popup shows character count, folder, relative path, and overwrite/recursive flags instead.
- The planner prompt now routes file creation to the new tools and still refuses unsupported move/copy/rename operations.
- `fetch_web_page` was added to the reviewed policy baseline as a public, read-only, medium-risk fetch without confirmation.

### Live-test follow-up 3 — intent guards and launch timing

The third live run showed:

- `open_app` then `inspect_screen` raced the new Dolphin window; the lookup ran before the window existed.
- A request to create a file made the planner misuse `remember`, `list_files`, and `write_clipboard` because no file-write tool exists.
- “Do that thing” was treated as a concrete request and launched the wrong application.
- A controls question used screenshot inspection rather than accessibility.

Changes:

- Whole-request vague phrases (“do that thing”, “do it”, “zrób to”) now return a clarification question immediately, without starting Qwen or executing tools.
- Requests to create/write/move/delete files are refused explicitly while no file-write tool exists; the planner cannot substitute memory or clipboard tools.
- `remember` is offered only when the user explicitly asks to remember/save/note something.
- Clipboard tools are offered only when the transcript mentions the clipboard.
- Window inspection retries a failed lookup for about two seconds, covering a just-launched window.
- Accessibility inspection retries “no matching application” briefly for the same reason.
- Planner guidance now prefers accessibility tools for controls/buttons/fields and reserves `inspect_screen` for visual appearance.
- Planner guidance forbids using memory or clipboard as file-writing substitutes.

Post-fix local suite: **347 tests, OK**.

**Live verification passed:** “Do that thing” returned a clarification with 0.00 s agent time; “Open Dolphin and tell me what controls it shows” opened Dolphin and inspected the correct window with `masked_overlays=1`. After file tools were added, the user verified that the approval flow hides the file text and that the file changes only on approval; a denied repeat changed nothing.

**Live-test incident:** during the earlier complex file request, the agent called `remember` and stored one memory without user authorization. Memory metadata shows ID 3, category `instruction`, created 2026-11-10T12:58:27+01:00, 60 characters. Deleted with explicit user approval on 2026-11-10. A SQLite backup was written to `backups/20261110-130532-memory3-delete/memory.sqlite3` with a SHA-256 manifest. Verification after deletion: 0 rows with ID 3, 1 memory remaining, FTS row count matches, `PRAGMA integrity_check` returned `ok`.

## 2. Files

Changed:

- `app/brain/agent.py`
- `app/brain/tools.py`
- `app/actions/executor.py`
- `app/actions/file_search.py`
- `app/desktop/vision/capture.py`
- `app/desktop/vision/coordinator.py`
- `app/engine/clipboard_bridge.py`
- `app/engine/activity_store.py`
- `app/engine/voice_controller.py`
- `app/engine/kwin_windows.py`
- `app/desktop/context.py`
- `app/main.py`
- `app/popup_process.py`
- Tests listed under section 3.

New:

- `app/actions/file_scope.py`
- `app/engine/confirmations.py`
- `app/desktop/accessibility/inspector.py`
- `app/documents/__init__.py`
- `app/documents/reader.py`
- `app/documents/converters.py`
- `app/brain/policy.py`
- `app/actions/file_ops.py`
- `app/knowledge/index.py`
- `app/web/search.py`
- `app/web/__init__.py`
- `app/web/fetch.py`
- `tests/test_file_scope.py`
- `tests/test_confirmation_broker.py`
- `tests/test_confirmation_binding.py`
- `tests/test_clipboard_expiry.py`
- `tests/test_activity_store.py`
- `tests/test_accessibility_inspector.py`
- `tests/test_accessibility_tools.py`
- `tests/test_document_reader.py`
- `tests/test_document_tool.py`
- `tests/test_kwin_script.py`
- `tests/test_document_converters.py`
- `tests/test_policy.py`
- `tests/test_file_ops.py`
- `tests/test_file_tools.py`
- `tests/test_knowledge_index.py`
- `tests/test_knowledge_tools.py`
- `tests/test_web_search.py`
- `tests/test_web_search_tool.py`
- `tests/test_web_fetch.py`
- `tests/test_web_tool.py`

Not changed: UI layout, F8 helper, recorder, transcriber, model files, llama.cpp/whisper.cpp builds, configuration, or existing user databases.

## 3. Test evidence

Run from `~/misty`:

```bash
cd ~/misty
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

Expected: `Ran 1264 tests` and `OK` (one opt-in live media test skipped).

Targeted batch command:

```bash
cd ~/misty
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest \
  tests.test_file_scope tests.test_file_search tests.test_tools \
  tests.test_executor tests.test_inspect_screen_tool \
  tests.test_desktop_vision tests.test_vision_services \
  tests.test_confirmation_broker tests.test_confirmation_binding \
  tests.test_clipboard_expiry tests.test_activity_store \
  tests.test_agent_planner -v
```

Observed: 1264 tests, `OK`, about 7.4 s. These tests use mocks and temporary directories. They do not prove live KWin, Spectacle, AT-SPI, microphone, GPU, or UI behavior.

## 4. Live checks to run before the next architectural change

Launch normally:

```bash
cd ~/misty
.venv/bin/python misty.py
```

1. **Named window.** Open Helium, focus another application, hold F8, and say: “Look at Helium and tell me what is visible.” Expected: Misty does not open a new browser. It lists windows, selects one Helium window, and describes that window. If several Helium windows match, it chooses a focused one and reports the auto-selection. The terminal prints the captured `window_id` and `masked_overlays` count. If the Helium window is covered, Misty may report that occlusion is possible. If the selected window is on another virtual desktop, Misty must report that it is not visible instead of describing wallpaper. If the planner misspells Helium, read-only inspection should still match it.
2. **Ambiguous window.** Open two Helium windows and repeat. Expected: Misty asks which window; it does not capture either one.
3. **Polish routing.** Say: “Spójrz na Helium i powiedz, co widzisz.” Expected: the same named-window behavior.
4. **Scoped search.** Create a marker outside Downloads and one inside:

```bash
mkdir -p ~/Pobrane ~/Documents
: > ~/Pobrane/misty-scope-marker.txt
: > ~/Documents/misty-scope-marker.txt
```

Ask: “Search only Downloads for misty-scope-marker.” Expected: exactly the `Pobrane` result, with `scoped: true` and scope under home. A spoken form such as “MISTI - Scope - Marker” should still match; the result has `match_score` below 1.0. Repeat in Polish: “Szukaj tylko w Pobranych pliku misty-scope-marker.”
5. **Approval binding.** Ask Misty to copy a harmless word to the clipboard. Expected: one confirmation popup; approve it; the clipboard changes. Repeat and deny; nothing changes.
6. **Activity redaction.** After a clipboard or screen inspection, inspect the newest rows without sharing their content:

```bash
cd ~/misty
sqlite3 data/activity.sqlite3 \
  "SELECT id, title, status, substr(details,1,60) FROM activity ORDER BY id DESC LIMIT 8;"
```

Expected: new private-tool rows show `[details redacted from local activity history]`. Older rows are unchanged.
7. **Read-only accessibility.** Open Dolphin and ask: “What controls does Dolphin show?” Expected: `accessibility_inspect_application` returns roles/names/paths and does not click or focus anything. If AT-SPI is unavailable, Misty should report a bounded failure, not invent a tree.
8. **Document read.** Create a harmless note and ask Misty to read it:

```bash
printf 'Misty document test\n' > ~/Documents/misty-note.txt
```

Ask: “Read Documents misty-note.txt.” Expected: `read_document` returns the text, format `text`, and no path outside `Documents`.
9. **Web research and full window.** Ask: “Research Arch Linux and summarize the top results with sources.” Expected: `web_research` returns several pages; the answer cites them; a separate full-response window opens with selectable text and a Sources list. The tray **Show last response** reopens it.
10. **Public web fetch.** Ask: “Fetch https://example.com and tell me what it says.” Expected: `fetch_web_page` returns readable text. Then ask: “Fetch http://127.0.0.1:8080” and expect a blocked-address failure, not a request to the local service.
11. **Vague follow-up.** Say “Do that thing” or “Zrób to.” Expected: an immediate clarification question, with no Qwen start and no tool execution.
12. **File write with confirmation.** Say “Create a file called misty-test.txt with the text hello.” Expected: one approval popup showing the folder, relative path, character count, and overwrite flag, but not the text. Approve and verify `~/Documents` (or the chosen approved folder) contains the file. Repeat and deny; nothing changes.
13. **Unsupported move.** Say “Move the file misty-test.txt into Documents.” Expected: a clear refusal for move/copy/rename; no write or trash call.
14. **Typed request.** Use the tray **Ask Misty…** action, type “What time is it?”, and confirm the popup/response path works without F8.
15. **Launch then inspect.** Say “Open Dolphin and tell me what controls it shows.” Expected: Dolphin opens, then either accessibility inspection or window inspection succeeds after the brief retry. No false “no matching window” failure.
16. **Regression.** Confirm F8 hold-to-talk, English/Polish transcription, window focus/minimize, memory recall, and idle model unload still behave as before.

If a check fails, capture the terminal output and the popup text. Do not paste private clipboard or screen contents.

## 5. Rollback

Batch 1 snapshot (pre-change files and checksums):

```bash
cd ~/misty
ls backups/20261110-122512-batch1
```

Restore selected batch-1 files only after quitting Misty, verifying the absolute target, and preserving current copies. The backup manifest covers the exact pre-change files.

Batch 2 has a reconstructed baseline and restore script:

```bash
cd ~/misty
bat backups/20261110-batch2-reconstructed-pre/README.md
bash backups/20261110-batch2-reconstructed-pre/RESTORE.sh
```

The script quarantines current versions before copying and refuses an unexpected project root. It does not delete user data.

Snapshots:

- `backups/20261110-123023-batch2-complete` is the verified state before the live-test follow-up patch.
- `backups/20261110-124102-batch3-complete` is the verified state after the first live-test follow-up.
- `backups/20261110-125020-batch4-complete` is the verified state after the virtual-desktop/overlay follow-up.
- `backups/20261110-125614-batch5-complete` is the verified state after accessibility, document, and intent integration.
- `backups/20261110-130052-batch6-complete` is the verified state after the intent-guard and launch-timing follow-up.
- `backups/20261110-130814-batch8-complete` is the verified state after PDF/DOCX/ODT extraction and the isolated knowledge index module.
- `backups/20261110-130903-batch9-complete` is the verified state after integrating the public web fetch tool.
- `backups/20261110-131113-batch10-audit` is the verified state after integrating content-free audit logging.
- `backups/20261110-131304-batch11-policy-fileops` is the verified state after central policy delegation and confirmation-gated file tools.
- `backups/20261110-131425-batch12-knowledge-search` is the verified state after local knowledge tools and conditional SearXNG search.
- `backups/20261110-131842-batch13-telemetry-journal` is the verified state after telemetry, task journal, policy, file, knowledge, search, audit, accessibility, document, and web-fetch integrations.
- `backups/20261110-131941-batch14-typed-input` is the verified state after adding the tray typed-request path.
- `backups/20261110-132011-batch15-control-routing` is the verified state after accessibility-first control routing.
- `backups/20261110-132208-batch16-input-preview` is the verified state after content-free confirmation previews and the isolated input abstraction.
- `backups/20261110-132302-batch17-recovery-input` is the verified state after completing both modules and adding one-time read-only retries.
- `backups/20261110-132701-batch18-url-search` is the verified state after adding URL opening and site search.
- `backups/20261110-133221-batch19-search-ocr-installed` is the verified state after installing/configured SearXNG, Playwright, and OCRmyPDF.
- `backups/20261110-133343-batch20-web-research` is the verified state after adding bounded search-and-read research.
- `backups/20261110-133828-batch21-response-window` is the verified state after adding the full-response window.
- `backups/20261110-134304-batch22-direct-navigation` is the verified state after deterministic URL/site routing.
- `backups/20261110-134535-batch23-browser-tools` is the verified state after isolated browser automation.
- `backups/20261110-134703-batch24-response-format` is the verified state after response formatting.
- `backups/20261110-134951-batch25-reminders` is the verified state after reminder integration.
- `backups/20261110-135050-batch26-file-transfer` is the verified state after file transfer integration.
- `backups/20261110-135235-batch27-accessibility-activation` is the verified state after accessibility activation.
- `backups/20261110-135558-batch28-media-recovery` is the verified state after media control and read-only recovery.
- `backups/20261110-135728-batch29-replan-media` is the verified state after replan-context integration.
- `backups/20261110-135934-batch30-navigation-window` is the verified state after navigation-window focus and search-query fixes.
- `backups/20261110-140149-batch31-no-nav-window` is the verified state after removing response-window auto-open for navigation.
- `backups/20261110-140428-batch32-arbitrary-search` is the verified state after natural site-search phrasing.
- `backups/20261110-141317-batch33-youtube-choices` is the verified state after the popup YouTube choice flow.
- `backups/20261110-141734-batch34-zip-choices` is the verified state after ZIP integration and popup layout fixes.
- `backups/20261110-142315-batch35-folder-grep` is the verified state after recursive folder transfer and ripgrep search.
- `backups/20261110-143010-batch36-calendar` is the verified state after calendar integration.
- `backups/20261110-143218-batch37-calendar-kwin` is the verified state after KWin direct capture.
- `backups/20261110-143315-batch38-kwin-calendar-final` is the verified state after KWin fallback tests and the final calendar/choice integration.
- `backups/20261110-143559-batch39-summary-routing` is the verified state after page/screen summary routing.
- `backups/20261110-143718-batch40-site-search` is the verified state after arbitrary site searches.
- `backups/20261110-144146-batch41-duplicates-transcribe` is the verified state after duplicate detection and file transcription.
- `backups/20261110-145326-batch42-video-kwin-fixes` is the verified state after video-query and KWin handle fixes.
- `backups/20261110-145600-batch43-topic-reasoning` is the verified state after topic extraction and vague-query rejection.
- `backups/20261110-145701-batch44-list-window` is the verified state after action-based response-window opening.
- `backups/20261110-150224-batch45-news-research` is the verified state after news-aware research.
- `backups/20261110-151743-batch46-main-window-powerhouse` is the verified state after the main-window expansion.
- `backups/20261110-151839-batch47-main-window-final` is the final verified state after UI polish.
- `backups/20261110-152152-batch48-window-context` is the verified state after the window-target regression fix.
- `backups/20261110-152740-batch49-window-accuracy` is the verified state after named-window accuracy and response-window fixes.
- `backups/20261110-153147-batch50-chat-only` is the verified state after typed-chat containment.
- `backups/20261110-153418-batch51-shortcut-recorder` is the verified state after shortcut recorder polish.
- `backups/20261110-154115-batch52-shortcut-portable` is the verified state after portable shortcut storage.
- `backups/20261110-154446-batch53-kglobalaccel-noautoload` is the verified state after the KGlobalAccel active-binding fix.
- `backups/20261110-154546-batch54-shortcut-repeat` is the verified state after the recorder auto-repeat guard.
- `backups/20261110-155115-batch55-conversation-singlechord` is the verified state after conversation short-circuiting and the one-chord shortcut guard.
- `backups/20261110-155451-batch56-hardcode-audit` is the verified state after the hardcoded-option audit.

No database migration is required. Restoring source does not undo actions already performed on the desktop.

## 6. Deliberately deferred

- Model lease/arbitration between Qwen and Qwen3-VL. Both still unload after a request; overlap was not measured.
- Desktop input backend (`app/desktop/input.py`) and plugin manifest loader remain isolated.
- Live KWin, GPU, microphone, and three-monitor occlusion verification.
- Chat transcript persistence across restarts.

These remain in the audit roadmap behind live verification of this batch.
