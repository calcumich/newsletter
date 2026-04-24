# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A Gmail newsletter ingestion pipeline that extracts links from newsletter emails, fetches/summarizes article content via OpenAI (with stub fallback), and writes Obsidian-flavored Markdown notes. State is kept in a local SQLite database (`newsletter.db`).

## Commands

```bash
# Install dependencies
python -m pip install -r requirements.txt

# Run all tests (quiet mode, tests/ directory)
python -m pytest

# Run a single test
python -m pytest tests/test_run.py::test_canonicalize_url_strips_tracking_and_normalizes

# Run the CLI (all commands go through run.py)
python run.py ingest --label "Tech Newsletters" --since-days 7 --max 20 --db newsletter.db --vault vault
python run.py process-links --db newsletter.db --vault vault --max-links 25
python run.py refresh --db newsletter.db --vault vault --older-than-days 30 --max-links 25
python run.py backfill-redirects --db newsletter.db --max-links 200

# Summarizer evaluation
python -m observability.eval_summarizer --mode stub --fixtures eval/fixtures.jsonl

# JSONL run-log report
python -m observability.log_report --logs "logs/*.jsonl" --since-days 7
```

## Architecture

**Entrypoint**: `run.py` is a thin wrapper that calls `newsletter.cli.main()`. All CLI argument parsing and command orchestration lives in `newsletter/cli.py`.

**Pipeline flow** (for `process-links` and `refresh`):
1. `db.get_unprocessed_links()` / `db.get_links_for_refresh()` selects candidate URLs from SQLite
2. `fetch.fetch_article_detailed()` downloads and extracts article text (trafilatura -> readability-lxml -> BeautifulSoup fallback chain)
3. `summarize.summarize_text()` summarizes via OpenAI Responses API if `OPENAI_API_KEY` is set, otherwise uses a stub sentence-splitter; output is normalized through `normalize_summary_output()` which enforces category constraints, tag deduplication, and confidence thresholds
4. `obsidian.write_article_note()` writes a Markdown note with YAML frontmatter; `obsidian.update_issue_note_with_article_link()` back-patches the issue note to replace external URLs with Obsidian `[[wikilinks]]`
5. `db.mark_link_processed()` records status, summary, category, tags, and note path

**Key modules**:
- `newsletter/links.py` - URL canonicalization (strips tracking params, normalizes scheme/host), link extraction from HTML/text, skip-rules for noisy URLs, redirect resolution
- `newsletter/gmail.py` - Gmail API auth and message retrieval; defines `GmailMessage` namedtuple
- `newsletter/db.py` - SQLite schema init with auto-migration (`ensure_link_columns`/`ensure_gmail_columns`), all DB read/write operations
- `newsletter/summarize.py` - OpenAI summarization with structured JSON output, stub fallback, normalization/validation layer with `CATEGORY_SET` and `PROMPT_VERSION` constants
- `newsletter/obsidian.py` - Obsidian vault note generation (issue notes grouped by domain, article notes with frontmatter)
- `observability/logs.py` - JSONL event loader + filter (glob support, time-window filtering)
- `observability/log_stats.py` - pure aggregation functions (success rate, error-class breakdown, top failing domains, LLM mode, latency percentiles, daily trend) over `url_processed` events
- `observability/log_report.py` - standalone text/JSON report CLI (`python -m observability.log_report`)

## Key design details

- **Idempotency**: messages dedupe on `gmail_messages.message_id`, links dedupe on `links.url_canonical`. Re-running commands with no new data produces no duplicates.
- **Dry-run**: `process-links` and `refresh` support `--dry-run` which lists candidates without mutating DB or filesystem.
- **JSONL observability**: all commands support `--log-jsonl` for structured event logging (run_start, url_processed, run_summary, etc.).
- **Category set**: summarizer categories are constrained to `CATEGORY_SET` in `summarize.py`. Unknown categories map to "Other" and get a `needs-review` tag. Low confidence (<0.5) also triggers `needs-review`.
- **Text extraction fallback chain**: trafilatura first, then readability-lxml, then raw BeautifulSoup. All three are optional imports with graceful degradation.

## Project conventions

- **Tests alongside code.** Any new module or non-trivial function ships with its tests in the same change. Never "tests to follow."
- **Docs stay current.** When behavior or surfaces change, update `README.md` and this file (and `plan.md` / `PROGRESS.md` if the milestone state shifts) in the same commit. Docs drift is a bug.
- **Prefer new module-level CLIs over adding flags to `run.py`.** `run.py` is the pipeline entrypoint; observability, evaluation, and future tooling expose their own entries as `python -m <module>` (see `observability.eval_summarizer` for the pattern). Only add a subcommand to `newsletter/cli.py` when it's a pipeline-stage operation (ingest / process-links / refresh / backfill-redirects). A unified tool can be built later if wanted — don't preemptively pile responsibilities into `run.py`.

## Testing notes

- Tests use `.test_tmp/` for filesystem fixtures (gitignored, cleaned up in `finally` blocks).
- Tests that touch CLI functions (`process_links`, `refresh_links`, `backfill_redirects`) monkeypatch `fetch_article_detailed` to avoid real HTTP calls.
- All tests are in a single file: `tests/test_run.py`.

## Environment variables

- `OPENAI_API_KEY` - enables OpenAI-backed summarization (optional; stub used if absent)
- `OPENAI_MODEL` - model for summarization (defaults to `gpt-4o-mini`)
