# Newsletter Pipeline Plan

## Current State (2026-02-04)

Complete:
- End-to-end ingest -> link extraction -> DB persistence -> issue notes.
- Link processing and refresh workflows with summarization and article note generation.
- Redirect handling (`--resolve-redirects` + `backfill-redirects`).
- Issue-note internal linking replacement logic.
- Safe preview modes (`process-links --dry-run`, `refresh --dry-run`).
- Refresh filters and presets (`--statuses`, `--domains`, `--categories`, `--failed-only`, `--ok-only`, `--stale-ok`).
- Structured JSONL logging across key commands.

Reference:
- Behavior/spec contract lives in `spec.md`.
- Usage and examples live in `README.md`.

## Milestone Plan

### Milestone A: Refresh UX and Profiles

Scope:
- Add category shortcut flags (example: `--security-only`, `--ml-only`).
- Add saved filter profiles (optional config file support).

Acceptance criteria:
- Common refresh intents can be run with short commands.
- Profiles can be loaded repeatably in local automation scripts.

### Milestone B: Failure Taxonomy and Diagnostics

Scope:
- Normalize fetch failure classes (network, timeout, non-html, blocked, parse failure).
- Emit richer structured fields in JSONL (`error_class`, `http_status`, `retry_count` where available).

Acceptance criteria:
- Failed URLs are easier to triage from logs without rerunning manually.
- Summary outputs distinguish dominant failure causes.

### Milestone C: CLI Integration Safety Net

Scope:
- Add integration-style tests for command parsing and dispatch.
- Cover mutually exclusive and invalid flag combinations.

Acceptance criteria:
- CLI regressions are caught before release.
- New flags can be added safely with confidence.

### Milestone D: Observability Utilities

Scope:
- Add script/notebook examples that aggregate JSONL runs (success rate, top failing domains, trend over time).

Acceptance criteria:
- Operator can answer "what broke this week?" quickly from logs.

### Milestone E: LLM Summarization Productionization

Scope:
- Version prompts and model choices for repeatable behavior.
- Add LLM quality checks (schema validity, category sanity, summary length guardrails).
- Improve fallback/retry behavior for API failures and rate limits.
- Add cost/latency controls (input truncation/token budgeting, timeout policy).
- Add evaluation fixtures for summary/category quality checks over a representative sample.

Acceptance criteria:
- LLM path is stable enough for routine use with predictable failure behavior.
- Quality regressions are detectable through tests/fixtures.
- Run logs expose enough metadata to debug LLM outcomes quickly.

## Immediate Next Tasks

1. Add first pass failure taxonomy fields to `url_processed` events.
2. Add one integration test module for CLI dispatch and validation paths.
3. Expand summarizer fixture set and add prompt/model version markers to evaluation outputs.
