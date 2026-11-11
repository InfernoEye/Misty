# Misty data model

Misty keeps all state in plain SQLite files under `data/`. Nothing here leaves the machine, and no schema is shared with a server.

| File | Owner | Purpose |
|---|---|---|
| `data/memory.sqlite3` | `app/brain/memory.py` | Explicit memories the user asked Misty to keep |
| `data/activity.sqlite3` | `app/engine/activity_store.py` | Local activity history and task summaries |
| `data/tasks.sqlite3` | `app/engine/audit_log.py` (`TaskJournal`) | Task start/finish journal |
| `data/knowledge.sqlite3` | `app/knowledge/index.py` | Indexed document text and metadata |
| `data/reminders.sqlite3` | `app/automation/reminders.py` | Reminders and their due times |

Every store accepts a `database=` path in its constructor and tests always pass a temporary path. `data/audit/` holds the append-only tool audit text files.

## memory.sqlite3

| Table | Columns | Notes |
|---|---|---|
| `memories` | `id`, `category`, `content`, `created_at`, `updated_at` | `category` is constrained to `preference`, `fact`, `project` or `instruction` |
| `memories_fts` | FTS5 over `content` (`category` unindexed) | External-content index kept in sync by triggers |

Credential-shaped values are refused by the registry before they reach this store.

## activity.sqlite3

| Table | Columns | Notes |
|---|---|---|
| `activity` | `id`, `timestamp`, `kind`, `title`, `details`, `status` | `timestamp` is a sortable UTC ISO-8601 string |

Events flagged `sensitive` (or carrying `answer_sensitive`/`details_sensitive`) store `REDACTED_DETAILS` instead of their content. `retention_days` prunes old rows opportunistically; `purge()` and `clear()` exist for explicit cleanup. The UI reads history only through `recent_events(limit)`.

## tasks.sqlite3

| Table | Columns | Notes |
|---|---|---|
| `task_journal` | `task_id`, `request_label`, `status`, `created_at`, `updated_at`, `checkpoint_json` | One row per request; `status` reflects the honest outcome (`completed`, `failed`) |

## knowledge.sqlite3

| Table | Columns | Notes |
|---|---|---|
| `roots` | `root`, `indexed_at` | One row per explicitly indexed folder |
| `documents` | `id`, `root`, `rel_path`, `title`, `content`, `size`, `mtime`, `mtime_ns`, `content_hash`, `indexed_at` | Stored extracted text; unique per `(root, rel_path)` |
| `documents_fts` | FTS5 over `title` and `content` (unicode61, diacritics removed) | Kept in sync by insert/update/delete triggers |
| `documents_root_idx` | index on `root` | Pruning and status queries |

Credential-looking paths (`.ssh`, `.gnupg`, `.config`, key files, `.env`, browser profiles, ...) are excluded by default and can be re-admitted only through explicit `allowed_patterns`. Search reports each hit's source status so content from deleted or changed files is labelled instead of silently trusted. `knowledge_forget` and `knowledge_clear` remove stored text only; the user's files are never touched.

## reminders.sqlite3

| Table | Columns | Notes |
|---|---|---|
| `reminders` | `id`, `text`, `due_at`, `created_at`, `status`, `fired_at` | `status` is one of `pending`, `fired`, `cancelled`; `idx_reminders_due` supports the 20-second polling timer |

## Gotchas

- All connections are opened per operation and closed explicitly; a lingering connection would leave `-wal`/`-shm` files and trigger `ResourceWarning` under tests.
- SQL is always parameterised. FTS5 queries are tokenised and quoted before use.
- Timestamps are stored as zero-padded UTC ISO strings so lexical ordering equals chronological ordering.
