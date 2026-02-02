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

Fetch article pages and write article notes:
```bash
python run.py process-links --db newsletter.db --vault "C:\\Path\\To\\Repo\\vault" --max-links 25
```

Backfill redirect URLs (optional):
```bash
python run.py backfill-redirects --db newsletter.db --max-links 200 --redirect-rate-limit 0.2
```

## Project structure (high level)

- `run.py`: entrypoint CLI (will remain as thin wrapper).
- `newsletter/`: package for core pipeline modules (refactor in progress).
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
