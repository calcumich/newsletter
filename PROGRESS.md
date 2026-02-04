# Progress Report

## Current capabilities
- Gmail ingestion by label with OAuth tokens stored locally.
- Link extraction + canonicalization + filtering (tracking, unsubscribe, images).
- SQLite state to avoid reprocessing.
- Issue notes written to Obsidian vault with summary counts + links grouped by domain.
- `process-links` command to fetch articles, extract main text, summarize, and write article notes.
- Article notes are linked back into issue notes under an "Articles" section.
- OpenAI-backed summarizer via `OPENAI_API_KEY`, with a stub fallback.
- Observability tooling: `observability/inspect_examples.py` and `observability/observability_examples.ipynb`.
- Local `vault/` folder created in-repo and gitignored (optional usage).

## Refactor status
- Package modules split into `newsletter/`:
  - `db.py`, `links.py`, `obsidian.py`, `fetch.py`, `summarize.py`, `gmail.py`, `cli.py`
- `run.py` is a thin wrapper that calls `newsletter.cli.main(...)`.

## Resume here (quick commands)
- Run tests:
  - `python -m pytest`
- Ingest newsletters:
  - `python run.py ingest --label "<Label>" --since-days 7 --max 20 --db newsletter.db --vault "C:\Path\To\Repo\vault"`
- Process links:
  - `python run.py process-links --db newsletter.db --vault "C:\Path\To\Repo\vault" --max-links 25`
  - Dry run: `python run.py process-links --db newsletter.db --max-links 25 --dry-run`
  - JSONL log: `python run.py process-links --db newsletter.db --vault "C:\Path\To\Repo\vault" --max-links 25 --log-jsonl logs\process-links.jsonl`
- Backfill redirect URLs:
  - `python run.py backfill-redirects --db newsletter.db --max-links 200 --redirect-rate-limit 0.2`
- Refresh old links:
  - `python run.py refresh --db newsletter.db --vault "C:\Path\To\Repo\vault" --older-than-days 30 --max-links 25`
  - Dry run: `python run.py refresh --db newsletter.db --vault "C:\Path\To\Repo\vault" --older-than-days 30 --statuses "ok,fail" --dry-run`
  - Filtered: `python run.py refresh --db newsletter.db --vault "C:\Path\To\Repo\vault" --older-than-days 30 --domains "example.com" --categories "Dev Tools"`
  - JSONL log: `python run.py refresh --db newsletter.db --vault "C:\Path\To\Repo\vault" --older-than-days 30 --log-jsonl logs\refresh.jsonl`

## What's done vs pending

Done:
- Milestone 1 end-to-end flow with issue notes.
- Milestone 2 core pipeline (fetch/extract/summarize/write article notes).
- Issue note linking to article notes.
- Summary output normalization with category validation and `needs-review` fallback tagging.
- Issue note replacement of matching external links with internal Obsidian links.
- `refresh` command to reprocess previously processed links by age/status.
- `refresh` supports domain/category filters and dry-run mode.
- `process-links` supports dry-run mode.
- Structured JSONL event logging for `ingest`, `process-links`, and `refresh`.
- Default CLI behavior runs `ingest` when no subcommand is provided.
- Redirect resolution for canonical URLs (optional via `--resolve-redirects`).

Pending / improvements:
- Add retry/backoff and better failure logging for fetches (already partially done).
- Add rate limiting for link processing (already partially done).
- Add preset filter aliases for `refresh` (e.g., `--failed-only`, `--security-only`).

## Next steps (recommended)
1. Add preset filters for `refresh` and optionally save/load filter profiles.
2. Strengthen fetch failure taxonomy/logging for easier troubleshooting.
3. Add integration tests around CLI subcommands and note mutation behavior.
