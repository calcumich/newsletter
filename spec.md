# Newsletter Pipeline Specification

## 1. Purpose

The project ingests newsletter emails from Gmail, extracts and normalizes article links, processes article content, and writes Obsidian-friendly notes while maintaining idempotent state in SQLite.

## 2. Goals

- Daily/periodic ingest from one or more Gmail labels.
- Minimize noisy links (tracking/unsubscribe/social/image URLs).
- Avoid duplicate message and URL processing across runs.
- Produce readable issue notes and article notes in an Obsidian vault.
- Provide safe preview modes and operational observability (JSONL logs + summary JSON).

## 3. Non-goals

- Realtime Gmail push/watch pipeline.
- Full UI or hosted multi-user service.
- Perfect extraction/summarization for every site/paywall.

## 4. System Boundaries

Inputs:
- Gmail API message metadata/content.
- Article web pages.
- Optional OpenAI API summarization.

Outputs:
- SQLite DB (`gmail_messages`, `links`).
- Markdown notes in vault.
- Optional summary JSON and JSONL event logs.

## 5. Core Commands

### `ingest`
- Reads labeled Gmail messages (`--label`, `--since-days`, `--max`).
- Stores message metadata.
- Extracts/stores canonical links.
- Writes issue notes.
- Optional inline fetch stage with `--fetch-articles`.

### `process-links`
- Selects unprocessed links.
- Optional `--dry-run` candidate listing.
- Fetches/extracts/summarizes links.
- Writes article notes and updates issue notes with internal links.

### `refresh`
- Reprocesses already processed links by age (`--older-than-days`).
- Filters: `--statuses`, `--domains`, `--categories`.
- Presets: `--failed-only`, `--ok-only`, `--stale-ok`.
- Optional `--dry-run`.

### `backfill-redirects`
- Resolves and updates canonical URLs for existing links.

## 6. Data Model Contract

### `gmail_messages`
- `message_id` (PK)
- `internal_date`, `subject`, `from_email`, `label_ids`
- `processed_at`, `content_hash` (optional), `issue_note_path`

### `links`
- `url_canonical` (PK)
- `first_seen_message_id`, `domain`, `title`
- `discovered_at`, `processed_at`, `fetch_status`
- `content_hash`, `summary`, `category`, `tags`, `note_path`
- `original_url`

## 7. Note Contracts

Issue note:
- One note per newsletter message.
- Includes summary counts and grouped links by domain.
- Article links are integrated by replacing matching external links where possible; otherwise appended under `## Articles`.

Article note:
- YAML frontmatter: type/source/title/date/url/category/tags.
- Sections: Summary, Key takeaways, optional Why it matters.

## 8. Summarization Contract

- OpenAI-backed when `OPENAI_API_KEY` is present; stub fallback otherwise.
- Output is normalized and validated:
  - category constrained to known set or `Other`
  - tag cleanup + dedupe
  - confidence clamped to `[0,1]`
  - adds `needs-review` on low confidence or invalid category mapping

Productionization requirements (next phase):
- Prompt/model versioning should be explicit and trackable.
- LLM failure handling should include clear retry/fallback policy.
- Cost/latency bounds should be enforced by timeout/input-size guardrails.
- Quality should be measured on a small regression fixture set (summary usefulness + category accuracy).
- Observability should capture LLM path metadata (for example model, fallback-used, timing where available).

## 9. Observability Contract

Optional `--log-jsonl` events:
- `run_start`
- `candidate` (dry-run)
- `message_processed` (ingest)
- `url_processed` (process/refresh/fetch stage)
- `ingest_summary`
- `run_summary`

Each event includes `timestamp`, `event`, `command`, plus command-specific fields.

## 10. Invariants

- URL dedupe key: canonical URL.
- Message dedupe key: Gmail `message_id`.
- Re-running without new data should not create duplicate DB rows.
- Dry-run modes do not mutate DB or notes.
- Command behavior should remain scriptable and non-interactive.
