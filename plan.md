# Newsletter Digest -> Obsidian Pipeline

## Status (as of 2026-02-04)

Implemented:
- Gmail ingest by label with OAuth and local token storage.
- Link extraction/filtering/canonicalization and dedupe into SQLite.
- Redirect resolution on ingest and post-hoc backfill.
- Issue note and article note generation for Obsidian.
- Article summarization with OpenAI (fallback stub when no key is set).
- Category/tag normalization with `needs-review` fallback tagging.
- Issue-link updates that replace matching external links with internal Obsidian links.
- `process-links` and `refresh` dry-run support.
- `refresh` filters by age/status/domain/category plus presets (`--failed-only`, `--ok-only`, `--stale-ok`).
- Structured JSONL event logging for `ingest`, `process-links`, and `refresh`.

Current source-of-truth usage is documented in `README.md`.

## Current command set

- `python run.py ingest ...`
- `python run.py process-links ...`
- `python run.py refresh ...`
- `python run.py backfill-redirects ...`

## Near-term roadmap

1. Add category-specific refresh shortcuts (for example `--security-only`).
2. Improve failure taxonomy and logging detail for fetch errors.
3. Add CLI integration tests (argument wiring and command dispatch).
4. Add lightweight observability views over JSONL logs.
