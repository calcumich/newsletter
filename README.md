# Newsletter Pipeline

Minimal Gmail ingestion pipeline that extracts newsletter links and (optionally) fetches article pages.

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

## Optional LLM Summarizer

Set `OPENAI_API_KEY` to enable OpenAI-backed summaries. Optionally set `OPENAI_MODEL`
(defaults to `gpt-4o-mini`). If no key is set, a local stub summarizer is used.
