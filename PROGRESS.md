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

## How to run

Install dependencies:
```bash
python -m pip install -r requirements.txt
```

Ingest newsletters:
```bash
python run.py ingest --label "Newsletters" --vault "C:\Path\To\Vault"
```

Process article links:
```bash
python run.py process-links --vault "C:\Path\To\Vault" --max-links 25
```

Enable OpenAI summarization (optional):
```bash
set OPENAI_API_KEY=your_key
set OPENAI_MODEL=gpt-4o-mini
```

## What's done vs pending

Done:
- Milestone 1 end-to-end flow with issue notes.
- Milestone 2 core pipeline (fetch/extract/summarize/write article notes).
- Issue note linking to article notes.
- Default CLI behavior runs `ingest` when no subcommand is provided.

Pending / improvements:
- Enforce category set in LLM schema and validate outputs.
- Replace external links in issue notes with internal links (instead of appending).
- Add retry/backoff and better failure logging for fetches.
- Add rate limiting for link processing.

## Next steps (recommended)
1. Add category enforcement + validation + fallback tag (`needs-review`).
2. Improve issue note updates to replace external links with internal links.
3. Add a `refresh` mode to reprocess old links on demand.
