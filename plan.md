# Newsletter Digest → Obsidian Pipeline (plan.md)

## 1) Goal

Build a tool that:
1. Reads emails from Gmail filtered by one or more "Newsletter" labels.
2. Extracts article links from each newsletter email (deduped + cleaned).
3. Fetches the linked pages and produces:
   - summary
   - category
   - tags
   - optional "why it matters" / key takeaways
4. Writes Obsidian-friendly Markdown into a vault automatically.
5. Avoids re-processing the same emails/URLs on subsequent runs.

Primary success criteria:
- Running the tool daily produces clean, browseable notes in the vault.
- Minimal noise (tracking links, unsubscribe links, duplicates filtered out).
- Idempotent: re-running doesn’t explode into duplicate notes.

## 2) Non-goals (for MVP)

- Full realtime push notifications (Gmail watch / PubSub)
- Fancy UI
- Perfect summarization on every site (paywalls, JS-heavy pages, etc.)
- Multi-user support / hosted SaaS

## 3) High-level design

### Pipeline
Gmail (label filter) → fetch message → extract HTML → extract links → normalize/dedupe →
fetch article content → extract main text → summarize + classify → write Markdown →
persist state (SQLite) so it’s incremental next run

### Operating modes
- **Local runner** (default): run on your machine (cron/Task Scheduler).
- Optional later: server mode + push notifications.

## 4) Tech choices (recommended defaults)

- Language: Python (fast iteration, good libs), or Node if you prefer JS.
- Gmail access: Gmail API via OAuth.
- Storage: SQLite for cache + state.
- HTML parsing: BeautifulSoup / lxml.
- Article extraction: readability-lxml (or trafilatura).
- Summarization/classification: LLM call (OpenAI or local model) with deterministic schema.
- Obsidian output: Markdown files with YAML frontmatter.

## 5) Data model (SQLite)

Tables:

### `gmail_messages`
- message_id TEXT PRIMARY KEY
- internal_date INTEGER
- subject TEXT
- from_email TEXT
- label_ids TEXT (json)
- processed_at INTEGER
- content_hash TEXT (optional)

Purpose: mark newsletters as processed, store metadata for traceability.

### `links`
- url_canonical TEXT PRIMARY KEY
- first_seen_message_id TEXT
- domain TEXT
- title TEXT
- discovered_at INTEGER
- processed_at INTEGER
- fetch_status TEXT (ok, fail, paywall, timeout)
- content_hash TEXT (hash of extracted text)
- summary TEXT
- category TEXT
- tags TEXT (json array)
- note_path TEXT

Purpose: dedupe URLs; skip re-summary when unchanged; connect URL → note.

## 6) Link extraction + normalization rules

### Extraction
- Prefer `text/html` part of the email; fallback to `text/plain`.
- Parse anchors `<a href="...">`.
- Also scan plain text for URLs as fallback.

### Filtering (reduce noise)
Drop links matching:
- unsubscribe, manage preferences
- view-in-browser
- mailto:
- social share links
- tracking pixels / image links (gif, png, jpg)
- known redirect wrappers that are not the final target (handle by resolving)

### Normalization
- Remove common tracking query params:
  - utm_*, ref, fbclid, gclid, mc_cid, mc_eid, etc.
- Canonicalize scheme/host/path:
  - lower host, remove trailing slashes (careful), decode safe chars
- Optionally resolve redirects:
  - HEAD/GET with short timeout; store final URL

Deduping key: canonical URL.

## 7) Obsidian note strategy

### Folder layout (recommended)
Vault/
  Newsletters/
    Issues/
      2026/
        01/
          2026-01-30 - <NewsletterName>.md
    Articles/
      <Category>/
        2026/
          <slug>.md

### Note types
1) One note per **newsletter issue** (always created)
2) Optional one note per **article** (created for each processed link)

This gives you:
- A daily/issue log you can skim
- A durable article knowledge base you can link into other notes

### YAML frontmatter templates

#### Issue note
---
type: newsletter-issue
source: "<newsletter name>"
date: 2026-01-30
gmail_message_id: "<id>"
from: "<from email>"
tags: [newsletters]
---

#### Article note
---
type: article
source: "<domain or newsletter>"
title: "<article title>"
date: 2026-01-30
url: "<canonical url>"
category: "<one of CATEGORY_SET>"
tags: ["tag1", "tag2"]
---

### Linking convention
Issue note should link to article notes:
- [[Newsletters/Articles/Category/2026/slug|Article Title]]

## 8) Categorization + tags

Start with a small, fixed category set (easy browsing):
- Backend
- Databases
- Distributed Systems
- Security
- ML Systems
- Programming Languages
- Dev Tools
- Product/Startups
- Other

Classification output schema (strict):
- title: string
- summary: 4–8 sentences
- bullets: 3–6 bullet takeaways
- category: one of CATEGORY_SET
- tags: 3–8 tags
- confidence: 0–1
- paywall_or_blocked: boolean

Low confidence items can be marked for review (e.g., tag `needs-review`).

## 9) MVP milestones

### Milestone 1 — Gmail ingestion + issue notes (no article fetch)
- OAuth + Gmail API integration
- Resolve label ID(s) for newsletters
- List recent messages under label
- Fetch each message content
- Extract links + clean + dedupe
- Create Issue note with:
  - metadata
  - list of canonical links
  - basic heuristics (domain, anchor text)
- Persist processed `message_id` so reruns are incremental

Deliverable:
- Running `python run.py` produces Issue notes for unprocessed newsletters.

### Milestone 2 — Article fetch + summarization + categorization
- For each new canonical URL:
  - fetch page
  - extract main text
  - summarize/classify with strict schema
  - write Article note
  - update Issue note with links to Article notes

Deliverable:
- Issue notes include linked summarized article notes.

### Milestone 3 — Robustness + quality
- Caching:
  - skip URLs already processed (unless refresh enabled)
- Error handling:
  - timeouts, 403, JS-heavy pages, paywalls
  - store failure state, don’t retry aggressively
- Better URL normalization + redirect resolution
- Add “daily digest index” note (optional)

Deliverable:
- Stable daily use.

## 10) CLI commands (proposed)

- `run.py ingest --label "Newsletters" --since 7d`
- `run.py process-links --max 25`
- `run.py full-run --since 24h --max-links-per-issue 20`
- `run.py backfill --since 90d --rate-limit 1rps`
- `run.py refresh --older-than 30d`

## 11) Scheduling

Windows Task Scheduler (daily at 7:30am) or cron on WSL/Linux.
Default: once/day; can bump to 2–4x/day later if desired.

## 12) Security + privacy

- Use least-privilege Gmail scope (readonly if possible).
- Store OAuth tokens locally (prefer OS keyring if easy).
- Never log full email bodies by default.
- Respect robots/timeouts; identify user-agent.
- Keep summaries local in the vault (unless you intentionally sync).

## 13) Testing plan

Unit tests:
- URL extraction on representative newsletter HTML samples
- URL normalization rules
- Dedup logic
- Markdown rendering templates

Integration tests:
- Gmail API: list + get message (can be mocked)
- Fetch + extract main text from a known static article page

Manual QA checklist:
- Issue note readability
- Does rerun produce duplicates?
- Are unsubscribe/tracking links filtered correctly?
- Are categories reasonable?

## 14) Risks + mitigations

- Paywalls / blocked content → store URL + mark as blocked, summarize from metadata/snippet if possible.
- Newsletter redirect links → implement redirect resolution and canonicalization.
- Too many links per issue → cap to top N (e.g., 20) or use heuristics (exclude same-domain spam).
- LLM variability → strict JSON schema + retry-on-parse-failure.

## 15) Next steps (do now)

1. Create repo skeleton:
   - src/
   - tests/
   - templates/
   - plan.md
2. Implement Milestone 1 end-to-end with ONE label and the latest 20 messages.
3. Add SQLite and idempotency.
4. Implement article fetch + extraction + summarization.
5. Iterate on link filtering rules using real newsletters.

---
