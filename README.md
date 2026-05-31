# Newsletter Pipeline

Gmail ingestion pipeline that extracts newsletter links and (optionally) fetches article pages.

## Background Problem

I found that I subscribed to a number of tech newsletters (e.g. TLDR, MarksTechPost).  Individually they were great, but as a whole they are just overwhelming.  Oftentimes links get re-used between newsletters, and packaged slightly differently depending on the audience.  If you actually want to read the articles you can spend hours each day just trying to figure out what other people are doing.

## Goal

Create a pipeline that pulls from Gmail, where I have a "Tech Newsletters" label.  De-duplicate links between newsletters, and add minimal persistence so that if you re-run the pipeline you don't see links added twice. 

Feed the cleaned article to a cheap LLM, get a summary and tags categorizing it.  Store simple entries in an Obsidian vault in an organized fashion.  

## Future Ideas

Down the road, I'd like to add some rebalancing to re-consider the broad themes, but I haven't dug into that, and nowadays it seems like throwing an Agent at the task might offer the same benefits. I'd also like to add in some rating for personal preferences (e.g. "this topic is of particular interest for me"), but I haven't thought through that thoroughly.

## Quickstart

Create a local vault in this repo (gitignored):
```bash
mkdir -p vault
```

Ingest recent newsletters (creates `newsletter.db`):
```bash
python run.py ingest --label "Tech Newsletters" --since-days 7 --max 20 --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault"
```
Write structured ingest logs (JSONL):
```bash
python run.py ingest --label "Tech Newsletters" --since-days 7 --max 20 --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --log-jsonl logs\\ingest.jsonl
```

Fetch article pages and write article notes:
```bash
python run.py process-links --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --max-links 25
```
Preview `process-links` candidates without changing anything:
```bash
python run.py process-links --db newsletter.db --max-links 25 --dry-run
```
Write structured process logs (JSONL):
```bash
python run.py process-links --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --max-links 25 --log-jsonl logs\\process-links.jsonl
```

Backfill redirect URLs (optional):
```bash
python run.py backfill-redirects --db newsletter.db --max-links 200 --redirect-rate-limit 0.2
```

Refresh old processed links (optional):
```bash
python run.py refresh --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --older-than-days 30 --max-links 25
```
Refresh only previously failed links:
```bash
python run.py refresh --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --older-than-days 30 --failed-only
```
Refresh only previously successful links:
```bash
python run.py refresh --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --older-than-days 30 --ok-only
```
Refresh stale successful links (preset):
```bash
python run.py refresh --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --older-than-days 30 --stale-ok
```
Preview refresh candidates without changing anything:
```bash
python run.py refresh --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --older-than-days 30 --statuses "ok,fail" --dry-run
```
Refresh only specific sources/categories:
```bash
python run.py refresh --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --older-than-days 30 --domains "example.com,news.ycombinator.com" --categories "Dev Tools,Security"
```
Write structured refresh logs (JSONL):
```bash
python run.py refresh --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --older-than-days 30 --log-jsonl logs\\refresh.jsonl
```

## Project structure (high level)

- `run.py`: entrypoint CLI (will remain as thin wrapper).
- `newsletter/`: package for core pipeline modules (refactor in progress).
- `newsletter/cli.py`: CLI orchestration and command dispatch.
- `observability/`: notebooks and scripts for inspecting outputs.
- `tests/`: unit tests.

## CLI reference

### `ingest`
- Purpose: Pull labeled Gmail messages, extract/store links, and optionally fetch articles.
- Common flags: `--label`, `--since-days`, `--max`, `--db`, `--vault`, `--resolve-redirects`.
- Optional outputs: `--fetch-summary-json <path>`, `--log-jsonl <path>`.

### `process-links`
- Purpose: Process unprocessed links into article notes.
- Common flags: `--db`, `--vault`, `--articles-dir`, `--max-links`.
- Safe preview: `--dry-run`.
- Optional outputs: `--fetch-summary-json <path>`, `--log-jsonl <path>`.

### `refresh`
- Purpose: Reprocess already processed links using age/status/domain/category filters.
- Common flags: `--db`, `--vault`, `--older-than-days`, `--max-links`.
- Filters: `--statuses`, `--domains`, `--categories`.
- Presets: `--failed-only`, `--ok-only`, `--stale-ok`.
- Safe preview: `--dry-run`.
- Optional outputs: `--fetch-summary-json <path>`, `--log-jsonl <path>`.

### `backfill-redirects`
- Purpose: Resolve and update canonical URLs for already-stored links.
- Common flags: `--db`, `--max-links`, `--redirect-timeout`, `--redirect-retries`, `--redirect-rate-limit`.

## JSONL events

When `--log-jsonl` is set, commands append one JSON object per line.

- Common event types:
  - `run_start`
  - `candidate` (dry-run candidate URL)
  - `message_processed` (ingest only)
  - `url_processed` (fetch/process result per URL)
  - `ingest_summary` (ingest only)
  - `run_summary`
- Common fields:
  - `timestamp`, `event`, `command`
  - URL-level events include fields like `url`, `domain`, `status`, `title`, `note_path`.
  - Failure diagnostics on URL-level events: `error_class`, `http_status`, `retry_count`.
  - Summarization events include `llm_mode`, `fallback_used`, `model`, `prompt_version`, `llm_latency_ms` when applicable.
- Example:
```json
{"timestamp":1738675200,"event":"url_processed","command":"process-links","url":"https://example.com/a","domain":"example.com","status":"ok","title":"Example","note_path":"C:\\vault\\Newsletters\\Articles\\Other\\2026\\Example.md"}
```

## Log report

Summarize one or more JSONL run logs — success rate, top failing domains, LLM mode mix, latency percentiles, and daily trend.

```bash
python -m observability.log_report --logs "logs/*.jsonl" --since-days 7
python -m observability.log_report --logs logs/process.jsonl --command process-links --top-domains 20
python -m observability.log_report --logs "logs/*.jsonl" --json > report.json
```

Answers "what broke this week?" without launching a notebook. The same
aggregations (`observability.log_stats`) are reused in the companion notebook
`observability/observability_examples.ipynb` for exploratory analysis and
charts.

## Tests

Install deps:
```bash
python -m pip install -r requirements.txt
```

Run tests:
```bash
python -m pytest
```

Notes:
- Tests create a temporary `.test_tmp` directory in the repo (gitignored).

## Summarizer Evaluation

Fixture-based summarizer evaluation is available via:
```bash
python -m observability.eval_summarizer --mode stub --fixtures eval\\fixtures.jsonl
```

To evaluate the OpenAI path directly:
```bash
python -m observability.eval_summarizer --mode openai --fixtures eval\\fixtures.jsonl --model gpt-4o-mini
```

## Optional LLM Summarizer

Set `OPENAI_API_KEY` to enable OpenAI-backed summaries. Optionally set `OPENAI_MODEL`
(defaults to `gpt-4o-mini`). If no key is set, a local stub summarizer is used.
