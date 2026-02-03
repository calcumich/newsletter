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
- Backfill redirect URLs:
  - `python run.py backfill-redirects --db newsletter.db --max-links 200 --redirect-rate-limit 0.2`

## What's done vs pending

Done:
- Milestone 1 end-to-end flow with issue notes.
- Milestone 2 core pipeline (fetch/extract/summarize/write article notes).
- Issue note linking to article notes.
- Default CLI behavior runs `ingest` when no subcommand is provided.
- Redirect resolution for canonical URLs (optional via `--resolve-redirects`).

Pending / improvements:
- Enforce category set in LLM schema and validate outputs.
- Replace external links in issue notes with internal links (instead of appending).
- Add retry/backoff and better failure logging for fetches (already partially done).
- Add rate limiting for link processing (already partially done).
- Add a `refresh` mode to reprocess old links on demand.

## Next steps (recommended)
1. Enforce category validation + fallback tag (`needs-review`).
2. Improve issue note updates to replace external links with internal links.
3. Add a `refresh` mode to reprocess old links on demand.
