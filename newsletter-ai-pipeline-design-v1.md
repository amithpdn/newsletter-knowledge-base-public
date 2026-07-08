# Newsletter AI Processing Pipeline — Design Document v1.0

> **Document scope:** Authoritative technical and architectural reference for the Newsletter AI Processing Pipeline. Covers intention, requirements, agent design, data model, technology choices, cost analysis, risks, and operational guidance.

---

## Table of Contents

1. [Project Intention](#1-project-intention)
2. [Scope & Goals](#2-scope--goals)
3. [Functional Requirements](#3-functional-requirements)
4. [Technical Architecture](#4-technical-architecture)
5. [Agent Design](#5-agent-design)
6. [Data Design](#6-data-design)
7. [Technology Stack](#7-technology-stack)
8. [Cost Analysis](#8-cost-analysis)
9. [Risks & Constraints](#9-risks--constraints)
10. [Proposed Future Enhancements](#10-proposed-future-enhancements)
11. [Appendix A — CLI Flag Reference](#appendix-a--cli-flag-reference)
12. [Appendix B — Folder & Repository Structure](#appendix-b--folder--repository-structure)
13. [Appendix C — Gmail Multi-Account Setup Guide](#appendix-c--gmail-multi-account-setup-guide)
14. [Appendix D — Obsidian Setup Guide (Windows + Linux)](#appendix-d--obsidian-setup-guide-windows--linux)
15. [Appendix E — Topic Linking & Wikilink Design](#appendix-e--topic-linking--wikilink-design)
16. [Appendix F — Link Pipeline Design](#appendix-f--link-pipeline-design)
17. [Appendix G — Link Fetch Reliability: Six-Way Classification, RSS & Playwright Fallbacks](#appendix-g--link-fetch-reliability-six-way-classification-rss--playwright-fallbacks)
18. [Appendix H — Ingestion Performance & Index Maintenance](#appendix-h--ingestion-performance--index-maintenance)
19. [Appendix I — Link Review & Manual Research Queue](#appendix-i--link-review--manual-research-queue)
20. [Appendix J — Live Progress UI Design](#appendix-j--live-progress-ui-design)
21. [Appendix K — Dashboard Design](#appendix-k--dashboard-design)
22. [Appendix L — Pre-Production Cleanup & Go-Live Checklist](#appendix-l--pre-production-cleanup--go-live-checklist)
23. [Appendix M — Obsidian Knowledge Extraction Workflows](#appendix-m--obsidian-knowledge-extraction-workflows)
24. [Appendix N — Storage Optimisation & Data Quality Maintenance](#appendix-n--storage-optimisation--data-quality-maintenance)
    - N.1 Duplicate image storage
    - N.2 Unbounded Related Notes sections
    - N.3 Choosing threshold and cap values
    - N.4 Invalid Obsidian tags and fragmented wikilinks

---

## 1. Project Intention

Most newsletter readers face a common problem: valuable editorial content arrives in email but is never revisited. It lives in the inbox, gets scanned once, and is effectively lost. There is no persistent, searchable, cross-linked knowledge layer that accumulates over time.

This pipeline exists to solve that problem at a personal scale, without manual effort after initial setup.

The core intention is to transform incoming newsletters and saved article links into durable, structured knowledge notes — automatically, consistently, and at near-zero cost per item. Each note becomes part of a growing, interlinked knowledge base in Obsidian, where topics connect across sources and time, and where new material is contextualised against what has already been read and processed.

The pipeline is intentionally personal: it runs locally, stores data in OneDrive, respects OAuth2 boundaries, and does not require any external infrastructure. It is designed to be left running on a schedule and largely forgotten, with a local dashboard available for monitoring and manual intervention when needed.

---

## 2. Scope & Goals

### In scope

- Multi-account Gmail ingestion via OAuth2
- Content classification to filter marketing email from editorial newsletters
- AI-generated structured notes: summary, key takeaways, entity mentions, tags
- Obsidian-compatible Markdown output with `[[wikilinks]]` and YAML frontmatter
- Semantic topic linking across notes using local sentence embeddings
- Image extraction from newsletter HTML with quality filtering
- Article link processing from a CSV file and browser bookmarklet
- Multi-layer fetch reliability (direct HTTP → Playwright headless browser → RSS)
- On-demand topic research using Claude + web search, appended to notes
- Local HTML dashboard for run statistics, cost tracking, and queue management
- SQLite-backed deduplication, run logging, and image metadata
- Git-based code and index backup to GitHub
- Windows Task Scheduler integration for scheduled runs

### Out of scope

- Cloud deployment or server hosting
- Real-time email processing (webhook-based)
- Multi-user support
- Email sending or reply generation
- Integration with note-taking tools other than Obsidian
- Mobile-native applications

### Goals

| Goal | Measure |
|---|---|
| Eliminate manual note-taking for newsletters | 100% of processed newsletters produce a structured note |
| Filter marketing content before AI processing | ≥80% of emails resolved at Stage 0/1 (no API cost) |
| Keep per-newsletter cost negligible | ≤$0.003 per processed email |
| Build a self-linking knowledge base | Every note links to related notes by shared topic |
| Support offline-first operation | All storage is local; cloud sync via OneDrive and GitHub |
| Provide operational visibility | Dashboard shows cost, volume, fallback rates, and queue state |

---

## 3. Functional Requirements

### FR-01 Gmail Ingestion
The pipeline must fetch unprocessed emails from one or more configured Gmail accounts using OAuth2. Each account must be independently configurable with its own credentials, label filter, sender allowlist, and sender blocklist.

### FR-02 Deduplication
Emails already recorded in the processing log must not be re-processed, regardless of their status (success, failed, skipped). Deduplication is based on Gmail Message-ID, not subject line or sender.

### FR-03 Content Classification
Before AI summarisation, each email must be classified as editorial, marketing, or mixed using a tiered pipeline:
- Stage 0a: Sender allowlist bypass (no classification required)
- Stage 0b: Sender blocklist skip (no classification required)
- Stage 1: Heuristic scoring (regex-based, zero API cost)
- Stage 2: LLM classification (Haiku 4.5, conditional on ambiguous heuristic score)
- Stage 3: Editorial extraction (Haiku 4.5, mixed emails only — strips marketing sections before summarisation)

### FR-04 AI Summarisation
Each editorial email must be summarised by Claude into a structured JSON output containing: a prose summary, bullet-point key takeaways, and entity mentions (tools, papers, people named). Tags are extracted as topic labels for the knowledge index.

### FR-05 Topic Linking
After summarisation, the pipeline must identify notes related to the current email's tags using cosine similarity over local sentence embeddings. Related notes are linked in the output Markdown. New tags not yet in the topic index must be flagged for research.

### FR-06 Image Extraction
Where enabled, the pipeline must extract images from newsletter HTML, filter out tracking pixels and decorative spacers, download qualifying images, convert WEBP to GIF for Obsidian compatibility, and save them to a per-note asset directory.

### FR-07 Topic Research
For new topics (tags not previously seen in the topic index), the pipeline must optionally call Claude with web search to generate a brief contextual summary. This summary is appended as a `## Context` section within the relevant note.

### FR-08 Note Writing
All agent outputs must be assembled into a single Obsidian-compatible Markdown file with YAML frontmatter, written to the notes directory. The filename format is `YYYY-MM-DD-{account}-{slug}.md`.

### FR-09 Gmail Labelling
Successfully processed emails must receive an "AI Processed" Gmail label. Emails skipped as marketing or flagged for review must receive an "AI Review" label. Labels are created on first use if not already present.

### FR-10 Index Maintenance
Each processed note must append a row to `INDEX.md` (an append-only Markdown table) and update `topics_index.json` (a JSON mirror of the topic index, Git-committable).

### FR-11 Link Ingestion
The pipeline must fetch and summarise article URLs from `links.csv` and any JSON files placed in `pipeline/manual_content/`. Fetched articles pass through the same agent chain as emails (classification optional, summarisation through note writing).

### FR-12 Manual Ingest
The pipeline must expose a POST `/api/ingest` endpoint (via the dashboard server) that accepts article content pasted from a browser bookmarklet, queues it as a JSON file in `pipeline/manual_content/`, and optionally triggers a link pipeline run via POST `/api/run_links`.

### FR-13 Dashboard
A local HTML dashboard must be generated after each run, displaying: run statistics, cost by day and account, classification breakdown, tag cloud, recent email table, link fetch overview with fallback rates, link fetch log, and research queue summary.

### FR-14 Run Logging
All token usage, costs, durations, classification metadata, and image counts must be persisted to SQLite for dashboard queries and cost analysis.

### FR-15 Git Backup
Code files and index files (excluding `registry.db`, `.env`, and `secrets/`) must be committable to a GitHub repository via a scheduled Git agent.

---

## 4. Technical Architecture

### Overview

The pipeline is a sequential multi-agent system with two entry paths:

```
┌─────────────────────────────────────────────────────────────────┐
│  Entry Path A: Email Pipeline                                   │
│                                                                 │
│  Gmail API → Agent 1 (Ingestion)                                │
│            → Agent 1.5 (Classification)                         │
│            → Agent 2 (Summarisation)                            │
│            → Agent 3 (Topic Linking)                            │
│            → Agent 4 (Image Extraction)                         │
│            → Agent 5 (Research)                                 │
│            → Agent 7 (Local Writer)                             │
│            → Agent 8 (Gmail Label)                              │
│            → Agent 6 (Logging)                                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Entry Path B: Link Pipeline                                    │
│                                                                 │
│  links.csv / manual_content/ → Link Ingestion Agent             │
│                              → Agent 2 (Summarisation)          │
│                              → Agent 3 (Topic Linking)          │
│                              → Agent 4 (Image Extraction)       │
│                              → Agent 5 (Research)               │
│                              → Agent 7 (Local Writer)           │
│                              → Agent 6 (Logging)                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Shared Infrastructure                                          │
│                                                                 │
│  registry.db (SQLite)   — all state and logs                    │
│  topics_index.json      — Git-visible topic mirror              │
│  INDEX.md               — append-only note index                │
│  dashboard/*.html       — generated UI (no server required)     │
│  serve_dashboard.py     — optional local HTTP server + API      │
│  Agent 9 (Git Backup)   — scheduled, independent of pipeline    │
└─────────────────────────────────────────────────────────────────┘
```

### Design principles

**Sequential over parallel.** Each agent's output is the next agent's input. This keeps the execution model simple, debuggable, and restartable per-email. Parallelism is not needed at personal-scale email volumes.

**Shared email dict schema.** All agents operate on a common Python dict schema. The link ingestion agent produces dicts in the same schema as email dicts, meaning agents 2–7 are completely reusable across both pipelines with zero modification.

**Fail-safe classification.** The classifier defaults to marketing on any parse failure, directing ambiguous emails to "AI Review" rather than silently dropping or incorrectly processing them.

**Local-first storage.** SQLite is the primary runtime store. All outputs are files on disk. OneDrive handles cloud sync transparently — no pipeline code is needed for cloud propagation.

**Cost-awareness by design.** The heuristic classifier exists specifically to prevent LLM calls for clearly-marketing content. Models are chosen per agent by cost/quality trade-off (Haiku for classification and summarisation; Sonnet for research). Prompt caching is used where supported.

**Dashboard is generated, not served.** `dashboard/index.html` is a self-contained static file generated after each run. The dashboard server (`serve_dashboard.py`) is optional and only needed for interactive features (research queue, manual ingest).

### Storage layout

| Location | Purpose | Sync method |
|---|---|---|
| `notes/` | Markdown notes + image assets | OneDrive (auto) |
| `registry.db` | SQLite DB — all state | OneDrive (auto), gitignored |
| `topics_index.json` | JSON topic mirror | OneDrive + GitHub |
| `INDEX.md` | Append-only note index | OneDrive + GitHub |
| `dashboard/*.html` | Generated dashboard pages | OneDrive + GitHub |
| `secrets/` | OAuth2 tokens | OneDrive only, gitignored |
| `.env` | API key | Local only, gitignored |

---

## 5. Agent Design

### Agent 1 — Gmail Ingestion (`agents/ingestion.py`)

**Purpose:** Authenticate with each configured Gmail account and fetch unprocessed newsletter emails.

**Inputs:** Account configuration from `config.GMAIL_ACCOUNTS`; set of already-processed Message-IDs from `db.get_processed_ids()`

**Process:**
1. Authenticate via OAuth2 using stored token file (browser flow on first run)
2. Build a Gmail search query based on `newsletter_label` or `sender_allowlist`
3. Fetch matching messages using the Gmail API
4. Skip any message whose Message-ID is already in the processing log
5. Parse each message: subject, sender, received date, body (plain text preferred, HTML fallback), headers, Gmail ID

**Outputs:** List of email dicts ready for classification

**API cost:** None (Gmail API, no LLM)

---

### Agent 1.5 — Content Classification (`agents/classification.py`)

**Purpose:** Gate emails before summarisation. Prevent marketing content from consuming API budget or producing noise in the knowledge base.

**Four-stage pipeline:**

| Stage | Method | API cost | Trigger |
|---|---|---|---|
| 0a | Sender allowlist | None | Sender in allowlist → action: process |
| 0b | Sender blocklist | None | Sender in blocklist → action: skip |
| 1 | Heuristic scoring | None | Score ≥ 8 → skip; score < 3 → pass; 3–7 → escalate |
| 2 | LLM classify | Haiku 4.5 | Score 3–7; returns editorial/marketing/mixed + confidence |
| 3 | Editorial extraction | Haiku 4.5 | Mixed classification only; strips marketing sections |

**Heuristic signals (Stage 1):** Subject-line sales language, urgency phrases, unsubscribe footer patterns, CTA button text, promotional language, price patterns, bulk mail headers, high link density, image-heavy / low-text body.

**LLM classification (Stage 2):** Returns `{"classification": "editorial|marketing|mixed", "confidence": 0.0–1.0}`. If confidence < `CLASSIFIER_CONFIDENCE_THRESHOLD` (default 0.75), email is escalated to "AI Review" regardless of classification.

**Editorial extraction (Stage 3):** For mixed emails, a second LLM call strips promotional sections and returns clean editorial text. This clean text replaces `body_text` before Agent 2 runs, ensuring summarisation only sees editorial content.

**Outputs:** Enriched email dict with `classification`, `classification_stage`, `confidence_score`, `heuristic_score`, `heuristic_signals`, `action` (process | skip)

---

### Agent 2 — Summarisation (`agents/summarisation.py`)

**Purpose:** Generate a structured knowledge note from the editorial email body.

**Model:** `claude-haiku-4-5` (default; overridable via `--model` flag)

**Prompt output schema (JSON):**
```json
{
  "summary":       "Plain prose summary — no [[wikilinks]] or bracket syntax",
  "key_takeaways": ["Plain prose bullet — no [[wikilinks]]", "..."],
  "tags":          ["hyphenated-tag", "another-tag", "..."],
  "mentions": {
    "tools":   ["Tool A", "Tool B"],
    "papers":  ["Paper Title"],
    "people":  ["Person Name"]
  }
}
```

**Tag format:** Tags must be lowercase with words separated by hyphens — no spaces (e.g. `rag-pipelines`, `agent-architecture`). Obsidian tags are invalid if they contain spaces, breaking tag search, Graph View, and Dataview queries. The prompt explicitly instructs the LLM to use hyphens; `_sanitise_tag()` in `_normalise()` runs as a safety net on every tag regardless.

**Prose constraint:** The `summary` and `key_takeaways` fields must be plain prose. The prompt explicitly forbids `[[wikilinks]]` or bracket syntax in these fields. Wikilinks are added by Agent 7's scoped injection pass afterward; if the LLM adds them itself, multi-word tag phrases fragment into invalid links (e.g. `agent [[architecture]]` instead of `[[agent-architecture]]`).

**Body truncation:** `SUMMARISATION_BODY_LIMIT` (default 8,000 characters) to stay within token budget for very long newsletters.

**Outputs:** `structured` dict passed to agents 3, 4, 5, and 7

---

### Agent 3 — Topic Linking (`agents/topic_linking.py`)

**Purpose:** Find existing notes that share topics with the current note, and identify new topics that have never been seen before.

**Embedding model:** `all-MiniLM-L6-v2` via `sentence-transformers` — runs entirely locally, zero API cost. ~80MB model, fast inference.

**Related note detection:**
1. Embed the current note's tags
2. Load all embeddings from `topic_index` in `registry.db`
3. Compute cosine similarity between new tags and existing tags
4. Include a note if similarity ≥ `SIMILARITY_THRESHOLD` (default 0.75) on at least one tag pair, excluding the current note
5. Filter to notes accumulating at least `RELATED_NOTES_MIN_SHARED_TAGS` (default 2) distinct shared tags
6. Sort by shared-tag count descending and truncate to `RELATED_NOTES_MAX_RESULTS` (default 10)

Steps 5 and 6 exist specifically to prevent the Related Notes section from growing unbounded as the topic index accumulates notes around frequently-recurring tags. See Appendix N.2 for the full rationale and a one-off script to retroactively re-trim already-written notes.

**New topic detection:**
- Simple exact match (case-insensitive) against `topic_index.tag`
- Intentionally not semantic: a new tag that is similar-but-distinct from an existing one should still trigger research

**Topic index update:**
- After note write (Agent 7), update `topic_index` with new tags and note references
- Existing tags: append new note reference to `note_files` JSON array
- New tags: insert row with embedding vector serialised as binary blob

**Outputs:** `related_notes` list (for note assembly); `new_topics` list (for Agent 5)

---

### Agent 4 — Image Extraction (`agents/image_extraction.py`)

**Purpose:** Extract meaningful images from newsletter HTML and save them for embedding in Obsidian notes.

**Filter cascade (in order):**

| Rule | Threshold | Reason |
|---|---|---|
| File too small | < 10KB | Tracking pixels |
| File too large | > 5MB | Storage cap |
| Dimension 1×1 | width or height ≤ 1px | Tracking pixel |
| Dimension too small | width or height < 50px | Decorative spacer |
| Tracking URL | URL contains known tracking substrings | Analytics beacon |
| Unsupported type | Not PNG/JPEG/GIF/WEBP/SVG | Format not usable |

**WEBP conversion:** WEBP images are converted to GIF using Pillow for compatibility with Obsidian's image renderer and iOS Obsidian.

**Asset storage:** `notes/assets/{message_slug}/` where `message_slug` is a hash-derived short ID from the Gmail Message-ID.

**Deduplication:** Before saving an image that passed the filter cascade, its content (SHA-256 of the final bytes, post-conversion) is checked against `image_dedup_index`. If an identical-content image has already been saved — common for recurring newsletter logos, banners, and sponsor graphics — the note references the existing canonical file instead of writing a new copy, and `reuse_count` is incremented. Controlled by `IMAGE_DEDUP_ENABLED` (default True). See Appendix N.1 for the rationale and a one-off cleanup script for images saved before this was introduced.

**Manifest:** A `manifest.json` is written to each asset folder recording all images (accepted and rejected) with full metadata for auditability.

**API cost:** None (local processing only)

---

### Agent 5 — Research (`agents/research.py`)

**Purpose:** For topics new to the knowledge base, generate a brief contextual summary using Claude with web search, and append it to the note as a `## Context: {topic}` section.

**Model:** `claude-sonnet-4-6` (higher quality for research synthesis)

**Trigger:** Tags in the current email that are not present in `topic_index` (exact match, case-insensitive)

**Research per topic:** One Claude API call with web search tool enabled. The system prompt requests a concise (200–300 word) briefing covering: what the topic is, why it matters, and key players or recent developments.

**Cost control:** `MAX_RESEARCH_TOPICS_PER_RUN` limits automatic research to N topics per pipeline run (default 0 = disabled; use `--no-research` flag and research manually via the Research Queue UI).

**Manual research queue:** Topics flagged as new are written to the `research_queue` table in `registry.db` with status `pending`. The Research Queue UI (`dashboard/research_queue.html`) allows manual selection and research via the dashboard server's `POST /api/research` endpoint.

**Outputs:** `research` dict mapping `{topic: {summary, cost_usd, usage}}` — passed to Agent 7 for note assembly

---

### Agent 6 — Logging Agent (`agents/logging_agent.py`)

**Purpose:** Persist all metrics, token usage, and run outcomes to SQLite for dashboard queries and cost analysis.

**Per-email logging (`processing_log`):**
- Message metadata: ID, account, sender, subject, received date
- Classification result: stage, confidence, heuristic score, signals, marketing sections flag
- Token usage: input, output, cache creation, cache read (summed across classify + summarise + research calls)
- Calculated cost in USD
- Image counts: found, saved, filtered, disabled
- Note path, status, duration

**Per-run logging (`run_summary`):**
- Run start and end timestamps
- Account list
- Aggregate email counts by status
- Aggregate token totals and cost
- Cache hit rate
- Image totals
- Git backup status

**Cost calculation:** `config.calculate_cost()` applies model-specific per-token pricing with Batch API multiplier (50% discount on input/output; cache reads not discounted).

---

### Agent 7 — Local Writer (`agents/local_writer.py`)

**Purpose:** Assemble all agent outputs into a structured Obsidian Markdown note and write it to disk.

**Note structure:**
```markdown
---
source_account: personal
sender: author@newsletter.example.com
received: 2026-06-18
processed: 2026-06-18T14:32:00+00:00
tags: ["rag-pipelines", "agent-architecture"]
---

# Newsletter Title — 2026-06-18
**Account:** personal | **Source:** author@newsletter.example.com

## Summary
[AI-generated plain prose summary — no [[wikilinks]]]

## Key Takeaways
- Plain prose takeaway
- Plain prose takeaway

## Mentions
- **Tools:** Tool A, Tool B
- **Papers:** —
- **People:** Person Name

## Tags
[[rag-pipelines]] [[agent-architecture]]

## Images
![alt text](assets/{slug}/filename.png)

## Related Notes
- [[2026-05-15-personal-ai-weekly]] — shared tags: rag-pipelines, agent-architecture

## Context: rag-pipelines
[Research summary from Agent 5]
```

**Wikilink injection:** Two-pass mechanism:
1. **Reliable pass:** Every tag is listed as an explicit `[[wikilink]]` in the Tags section — guaranteed regardless of prose content. Tags render as `[[rag-pipelines]] [[agent-architecture]]` using hyphenated format.
2. **Best-effort pass (scoped):** Tag phrases that appear verbatim in the `## Summary` and `## Key Takeaways` sections only are replaced with inline `[[wikilinks]]`. The scope is intentionally restricted to these two sections — running the pass over the full note caused multi-word tag phrases in `## Related Notes` to fragment (e.g. `agent architecture` → `agent [[architecture]]`) and tool names in `## Mentions` to acquire unwanted links. Implemented via `_inject_wikilinks_in_scope()` in `local_writer.py`.

**INDEX.md update:** Appends a row: `| date | [subject](relative/path/to/note.md) | account | tag1, tag2 |`

**topics_index.json update:** Appends the note reference to each tag's entry in the JSON mirror.

**Filename format:** `YYYY-MM-DD-{account}-{slug}.md` where slug is a URL-safe truncation of the subject line.

---

### Agent 8 — Gmail Label (`agents/gmail_label.py`)

**Purpose:** Apply Gmail labels to processed emails to prevent re-ingestion and enable inbox triage.

**Label rules:**

| Outcome | Label applied |
|---|---|
| Note written successfully | "AI Processed" |
| Skipped — marketing or low confidence | "AI Review" |
| Skipped — cross-account duplicate | No label |
| Failed — exception during processing | No label (retried next run) |

**Label creation:** Labels are created in Gmail on first use if not already present. Label IDs are cached in-memory for the run duration (one `labels.list()` call per account per run).

**Failure handling:** Label application failure is logged as a warning and does not invalidate the note that was already written.

---

### Agent 9 — Git Backup (`agents/git_backup.py`)

**Purpose:** Commit and push code, notes, and index files to GitHub as a secondary backup.

**Sequence:** `git add .` → `git commit -m "Backup: newsletter notes — {timestamp}"` → `git push origin main`

**What is pushed:** All files not excluded by `.gitignore` — code, Markdown notes, `INDEX.md`, `topics_index.json`, `dashboard/*.html`. Excludes `registry.db`, `.env`, `secrets/`.

**Scheduling:** Runs as an independent Windows Task Scheduler task, typically 30 minutes after the main pipeline. A push failure is a warning — primary OneDrive storage is unaffected.

**"Nothing to commit"** is treated as a successful no-op (exit code 0).

---

### Link Ingestion Agent (`agents/link_ingestion.py`)

**Purpose:** Fetch article content from saved URLs and manually-pasted content, producing article dicts compatible with the email pipeline's agent chain.

**Two ingestion modes:**

**`ingest_links()`** — fetches URLs from `links.csv`:
- Reads URL, label, and added_date from each row
- Checks `link_log` for URLs already processed (deduplication)
- Fetches each URL via the multi-layer fetch chain (see Appendix G)
- Classifies fetch result into one of six statuses
- Returns article dicts for agents 2–7

**`ingest_manual()`** — reads pre-pasted content from `pipeline/manual_content/`:
- Reads JSON files written by `POST /api/ingest`
- Builds article dicts with `fetch_status="manual"` and `via_manual_paste=True`
- Moves processed files to `pipeline/manual_content/processed/`
- Zero network calls — content is already in the JSON payload

**Session logging:** Each `ingest_links()` call writes a human-readable log file to `logs/link_fetch_{timestamp}.log` and a companion `.json` summary. Durable per-URL rows are persisted to `link_log` in `registry.db`.

---

## 6. Data Design

### SQLite Schema (`registry.db`)

#### `processing_log`
Primary record for every email seen by the pipeline.

| Column | Type | Description |
|---|---|---|
| `message_id` | TEXT PK | Gmail Message-ID |
| `account_alias` | TEXT | Account alias (personal, work) |
| `sender` | TEXT | From address |
| `subject` | TEXT | Email subject |
| `received_date` | TEXT | RFC 2822 received date |
| `processed_at` | TEXT | ISO 8601 UTC timestamp |
| `duration_seconds` | REAL | Wall-clock processing time |
| `model_used` | TEXT | Summarisation model identifier |
| `input_tokens` | INTEGER | Total input tokens (all agents) |
| `output_tokens` | INTEGER | Total output tokens |
| `cache_creation_tokens` | INTEGER | Prompt cache write tokens |
| `cache_read_tokens` | INTEGER | Prompt cache read tokens |
| `cost_usd` | REAL | Calculated total cost |
| `classification` | TEXT | editorial / marketing / mixed / blocked |
| `classification_stage` | TEXT | allowlist / blocklist / heuristic / llm |
| `confidence_score` | REAL | LLM confidence (null for non-LLM stages) |
| `heuristic_score` | INTEGER | Raw heuristic score |
| `heuristic_signals` | TEXT | JSON list of fired signal names |
| `marketing_sections` | TEXT | "extracted" if Stage 3 ran |
| `images_found` | INTEGER | Images found in HTML |
| `images_saved` | INTEGER | Images that passed filter and were saved |
| `images_filtered` | INTEGER | Images rejected by filter |
| `images_disabled` | INTEGER | 1 if image extraction was disabled |
| `note_path` | TEXT | Relative path of written note |
| `status` | TEXT | success / failed / skipped_marketing / skipped_duplicate / skipped_blocklist |

#### `run_summary`
One row per pipeline run.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `run_started_at` | TEXT | ISO 8601 start timestamp |
| `run_completed_at` | TEXT | ISO 8601 end timestamp |
| `accounts_processed` | TEXT | JSON array of account aliases |
| `emails_processed` | INTEGER | Successfully processed count |
| `emails_failed` | INTEGER | Failed count |
| `emails_skipped_marketing` | INTEGER | Skipped as marketing |
| `emails_skipped_duplicate` | INTEGER | Skipped as duplicate |
| `total_input_tokens` | INTEGER | Run total input tokens |
| `total_output_tokens` | INTEGER | Run total output tokens |
| `total_cache_reads` | INTEGER | Run total cache read tokens |
| `total_cost_usd` | REAL | Run total cost |
| `cache_hit_rate` | REAL | cache_reads / input_tokens |
| `total_images_saved` | INTEGER | Images saved this run |
| `total_images_filtered` | INTEGER | Images rejected this run |
| `images_enabled` | INTEGER | 1 if image extraction was enabled |
| `git_backup_status` | TEXT | success / failed / pending / skipped |

#### `topic_index`
One row per unique topic tag seen across all processed notes.

| Column | Type | Description |
|---|---|---|
| `tag` | TEXT PK | Lowercase tag phrase |
| `first_seen` | TEXT | ISO 8601 timestamp of first occurrence |
| `first_seen_account` | TEXT | Account alias where tag first appeared |
| `note_files` | TEXT | JSON array of `{file, account_alias}` objects |
| `embedding_vector` | BLOB | Serialised numpy float32 array (all-MiniLM-L6-v2) |

#### `image_log`
One row per image encountered (accepted or rejected) by Agent 4.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `message_id` | TEXT FK | References `processing_log.message_id` |
| `account_alias` | TEXT | Account alias |
| `filename` | TEXT | Saved filename (null if rejected) |
| `source_url` | TEXT | Original image URL |
| `source_type` | TEXT | external |
| `original_format` | TEXT | PNG / JPEG / GIF / WEBP / SVG |
| `saved_format` | TEXT | Format after conversion (WEBP → GIF) |
| `size_bytes` | INTEGER | Downloaded file size |
| `width_px` | INTEGER | Image width |
| `height_px` | INTEGER | Image height |
| `alt_text` | TEXT | Alt attribute from img tag |
| `filter_result` | TEXT | accepted / accepted_deduplicated / too_small / 1x1_tracker / tracking_url / etc. |
| `local_path` | TEXT | Absolute path if saved |
| `processed_at` | TEXT | ISO 8601 timestamp |

#### `image_dedup_index` *(new — see Appendix N.1)*
Maps a SHA-256 content hash to the first saved copy of that image, enabling cross-message deduplication. Populated by Agent 4 when `IMAGE_DEDUP_ENABLED` is True.

| Column | Type | Description |
|---|---|---|
| `content_hash` | TEXT PK | SHA-256 of the saved image bytes (post any WEBP→GIF conversion) |
| `canonical_path` | TEXT | Relative path to the first saved copy |
| `first_seen_at` | TEXT | ISO 8601 UTC timestamp |
| `first_message_id` | TEXT | Message-ID that first produced this image |
| `reuse_count` | INTEGER | Number of times this hash was matched again and a duplicate save was skipped |

#### `link_log`
One row per URL processed by the link pipeline.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `url` | TEXT UNIQUE | Source URL |
| `url_hash` | TEXT | SHA-256 of URL for lookup |
| `label` | TEXT | User-supplied label from links.csv |
| `added_date` | TEXT | Date added to links.csv |
| `fetch_status` | TEXT | fetched / partial / js_required / blocked / paywalled / failed / manual |
| `http_status` | INTEGER | HTTP response code |
| `word_count` | INTEGER | Extracted word count |
| `via_rss_fallback` | INTEGER | 1 if content came from Substack RSS feed |
| `via_playwright_fallback` | INTEGER | 1 if content came from Playwright |
| `via_manual_paste` | INTEGER | 1 if content was manually pasted |
| `fetch_error` | TEXT | Error message on failure |
| `fetched_at` | TEXT | ISO 8601 fetch timestamp |
| `note_path` | TEXT | Written note path |

#### `research_queue`
Tracks topics flagged for manual research.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `tag` | TEXT UNIQUE | Topic tag |
| `status` | TEXT | pending / done / skipped |
| `source_note` | TEXT | Note that triggered the research flag |
| `queued_at` | TEXT | ISO 8601 timestamp |
| `researched_at` | TEXT | Completion timestamp (null until done) |
| `cost_usd` | REAL | Research API cost |
| `summary` | TEXT | Research output (null until done) |

#### `git_backup_log`
Records every Git backup attempt.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `run_at` | TEXT | ISO 8601 timestamp |
| `files_staged` | INTEGER | Count of staged files |
| `commit_hash` | TEXT | Short commit hash (null if nothing to commit) |
| `push_status` | TEXT | success / failed / nothing_to_commit / skipped |
| `error_message` | TEXT | Error text on failure |

### File-based indexes

**`INDEX.md`** — Human-readable append-only table of all processed notes:
```markdown
┌────────────┬──────────────────────────────────────────────┬──────────┬───────────────────────────────────┐
│    Date    |                    Note                      |  Account |               Tags                |
├────────────┼──────────────────────────────────────────────┼──────────┼───────────────────────────────────┤
| 2026-06-18 | [Subject](notes/2026-06-18-personal-slug.md) | personal | rag-pipelines, agent-architecture |
└────────────┴──────────────────────────────────────────────┴──────────┴───────────────────────────────────┘
```

**`topics_index.json`** — JSON mirror of `topic_index` for Git visibility and Obsidian Dataview queries:
```json
{
  "rag-pipelines": [{"file": "2026-06-07-personal-ai-weekly.md", "account": "personal"}],
  "llm-fine-tuning": [...]
}
```

---

## 7. Technology Stack

| Layer | Technology | Version / Notes |
|---|---|---|
| Language | Python | 3.11+ |
| AI / LLM | Anthropic Claude API | Haiku 4.5 (classify, summarise), Sonnet 4.6 (research) |
| Local embeddings | sentence-transformers | `all-MiniLM-L6-v2`, ~80MB, CPU inference |
| Gmail access | Google Gmail API v1 | OAuth2 via `google-auth-oauthlib` |
| HTML parsing | BeautifulSoup4 | Content extraction, image tag parsing |
| Web fetch | requests | Primary HTTP fetch |
| JS rendering | Playwright | Chromium headless; optional fallback |
| Image processing | Pillow | Dimension inspection, WEBP→GIF conversion |
| Database | SQLite | `registry.db`; WAL mode; foreign keys enabled |
| Primary storage | Windows OneDrive | Auto-sync to cloud; no pipeline code needed |
| Code backup | GitHub + Git for Windows | Scheduled via Agent 9 |
| Dashboard | Vanilla HTML/JS + Chart.js 4.4 | Self-contained; no build step |
| Note format | Obsidian Markdown | YAML frontmatter, `[[wikilinks]]` |
| Scheduling | Windows Task Scheduler | Two tasks: pipeline run, git backup |
| CLI | argparse | `main.py` unified entry point |

---

## 8. Cost Analysis

### Model pricing (June 2026)

| Model | Input ($/1M) | Output ($/1M) | Cache read ($/1M) | Batch multiplier |
|---|---|---|---|---|
| claude-haiku-4-5 | $1.00 | $5.00 | $0.10 | 0.50× |
| claude-sonnet-4-6 | $3.00 | $15.00 | $0.30 | 0.50× |

### Per-email cost estimates (Batch API, Haiku 4.5)

| Stage | Tokens (approx.) | Cost (approx.) |
|---|---|---|
| Classification (ambiguous only) | 500 in / 20 out | $0.00025 |
| Editorial extraction (mixed only) | 1,500 in / 800 out | $0.00275 |
| Summarisation | 1,000 in / 300 out | $0.00125 |
| **Total (editorial email, no research)** | ~1,300 in / 320 out | **~$0.00143** |
| **Total (mixed email, full pipeline)** | ~2,800 in / 1,100 out | **~$0.00418** |

### Classification efficiency

~80% of emails are expected to resolve at Stage 0 (allowlist/blocklist) or Stage 1 (heuristic), incurring zero API cost. Only the ambiguous 20% escalate to Stage 2 (LLM classification), and only mixed emails trigger Stage 3 (editorial extraction).

### Research cost (Sonnet 4.6, per new topic)

| Tokens (approx.) | Cost (approx.) |
|---|---|
| 800 in / 250 out | ~$0.0051 |

Research is on-demand and controlled by `MAX_RESEARCH_TOPICS_PER_RUN` (default disabled). At typical newsletter volumes introducing 2–3 new topics per week, research costs remain under $0.02/week.

### Monthly estimate (typical usage)

| Scenario | Emails/month | Cost/month |
|---|---|---|
| Light (5 newsletters/week) | ~20 | ~$0.03 |
| Moderate (15 newsletters/week) | ~60 | ~$0.09 |
| Heavy (30 newsletters/week + research) | ~120 + 10 topics | ~$0.23 |

---

## 9. Risks & Constraints

### R1 — Gmail OAuth token expiry
**Risk:** OAuth2 refresh tokens can expire if not used for extended periods or if Google revokes them (e.g., on password change).
**Mitigation:** Run the pipeline at least weekly to keep tokens active. Monitor for `401 Unauthorized` errors in logs and re-authenticate with `--dry-run` when needed.

### R2 — Playwright dependency brittleness
**Risk:** Playwright Chromium updates may break headless rendering on specific sites. The dependency is heavy (~300MB).
**Mitigation:** Playwright is optional. If unavailable, the pipeline falls back to RSS-only (for Substack) or marks the URL as `js_required`. Sites that genuinely require JS rendering and are not Substack will require manual ingest via the bookmarklet.

### R3 — Substack RSS feed coverage
**Risk:** The Substack RSS fallback only covers recent posts (typically 20–50 items). Older articles in `links.csv` may not be retrievable via this path.
**Mitigation:** Manual ingest via bookmarklet is the intended fallback for articles behind this limit.

### R4 — LLM output schema instability
**Risk:** If Claude returns malformed JSON from the summarisation prompt, the note cannot be assembled.
**Mitigation:** Agent 2 wraps JSON parsing in try/except with a graceful fallback (empty structured dict). The email is logged as `failed` and can be retried on the next run.

### R5 — SQLite concurrent access
**Risk:** If the dashboard server reads `registry.db` while the pipeline is writing, there is potential for lock contention.
**Mitigation:** WAL (Write-Ahead Log) journal mode is enabled, which allows concurrent reads and one writer. Practical risk is low at personal-scale write rates.

### R6 — OneDrive sync conflicts
**Risk:** If the pipeline runs on two machines simultaneously (or if OneDrive syncs while the pipeline is writing), file conflicts may arise for `registry.db` or note files.
**Mitigation:** OneDrive handles SQLite files with difficulty due to their binary format. This is a known limitation. The pipeline is designed for single-machine use. Do not run concurrent instances.

### R7 — Cost runaway from research agent
**Risk:** If many new topics are introduced in a single newsletter (e.g., a "year in review" edition with 20+ new terms), auto-research could generate unexpected API costs.
**Mitigation:** `MAX_RESEARCH_TOPICS_PER_RUN` caps auto-research (default 0 = fully disabled). The Research Queue UI provides manual control. Sonnet pricing is visible on the dashboard.

### R8 — Git credential management on Windows
**Risk:** Git push via Personal Access Token may fail if the token expires or is revoked.
**Mitigation:** Agent 9 treats push failures as warnings, not errors. The pipeline continues normally. Credential renewal is a manual step (Windows Credential Manager).

### R9 — Unbounded growth of derived storage and link density
**Risk:** Two specific outputs scale with the *square* of accumulated content rather than linearly: duplicate image storage (the same recurring newsletter image gets re-saved per message rather than once) and Related Notes link density (a popular tag accumulates associations with every note that ever used it). Both were unbounded prior to this revision and degrade the knowledge base's usefulness — and, for images, disk usage — as the vault grows past the first few dozen notes.
**Mitigation:** Content-hash image deduplication (`IMAGE_DEDUP_ENABLED`) and a configurable minimum-shared-tags threshold plus hard cap (`RELATED_NOTES_MIN_SHARED_TAGS`, `RELATED_NOTES_MAX_RESULTS`) address both going forward. One-off scripts (`cleanup_duplicate_images.py`, `retrofit_related_notes.py`) clean up data that accumulated before the fix. Full detail in Appendix N.

### R10 — Invalid Obsidian tags and fragmented wikilinks
**Risk:** Tags containing spaces (e.g. `rag pipelines`) are invalid Obsidian tag syntax, silently breaking tag-based search, Graph View filtering, and Dataview queries. Separately, if the wikilink injection pass runs over sections other than Summary and Key Takeaways, multi-word tag phrases in Related Notes and elsewhere fragment into partial links (e.g. `agent [[architecture]]`).
**Mitigation:** The summarisation prompt now explicitly instructs the LLM to use hyphens between words in tags, and `_sanitise_tag()` in `_normalise()` runs as a code-level safety net regardless of LLM compliance. The injection pass in Agent 7 is scoped to `## Summary` and `## Key Takeaways` only via `_inject_wikilinks_in_scope()`. For already-written notes with space-containing tags, `retag_existing_notes.py` provides a retroactive fix. Full detail in Appendix N.4.

### C1 — Windows-only primary path
The pipeline is designed and tested on Windows with OneDrive. It can run on Linux/macOS (with path adjustments and a different sync mechanism) but this is not the primary supported configuration.

### C2 — Single-user design
The pipeline does not support concurrent users, shared knowledge bases, or multi-tenant operation. It is intentionally personal-scale.

---

## 10. Proposed Future Enhancements

### Near-term

- **Scheduled run automation:** PowerShell wrapper for Windows Task Scheduler with configurable schedule (daily, weekday-only)
- **Podcast transcript ingestion:** RSS-based transcript extraction for podcast newsletters (similar to Substack RSS path)
- **Incremental note updates:** Re-process an existing note when research is added, rather than appending — requires tracking note version
- **Tag normalisation:** Auto-merge near-duplicate tags (e.g., "llm" and "large language models") using embedding similarity on the full topic index

### Medium-term

- **Cross-account duplicate detection improvement:** Currently based on exact Message-ID match. Could be extended to detect semantically duplicate content from newsletters that cross-post to multiple accounts
- **Newsletter quality scoring:** Track which senders produce consistently high-quality summaries and surface them in the dashboard
- **Obsidian plugin integration:** A simple plugin to trigger pipeline runs and show queue status from within Obsidian
- **Configurable note templates:** Allow per-sender or per-tag note templates rather than the single fixed structure

### Longer-term

- **Vector search across notes:** Replace cosine similarity over tag embeddings with a proper vector index (e.g., FAISS or ChromaDB) over full note embeddings for richer related-note discovery
- **Q&A over the knowledge base:** A local RAG interface for querying accumulated notes by question rather than tag
- **Batch API integration:** Switch summarisation to the Anthropic Batch API for a further 50% cost reduction on high-volume runs
- **Multi-format ingestion:** PDF newsletters, YouTube video transcripts, Twitter/X threads via bookmarklet

---

## Appendix A — CLI Flag Reference

All commands are invoked via `python main.py [command] [options]`. Running with no command defaults to `emails`.

### Commands

| Command | Description |
|---|---|
| `emails` | Process emails from all configured Gmail accounts (default) |
| `links` | Process URLs from `links.csv` and `manual_content/` |
| `all` | Run `emails` → `links` → regenerate all dashboard HTML files; auto-opens dashboard on success |
| `dashboard` | Regenerate all three dashboard files: `index.html`, `link_review.html`, `research_queue.html` |
| `queue` | Regenerate `dashboard/research_queue.html` only |
| `serve` | Start the local dashboard HTTP server (blocks until Ctrl+C) |

### Shared flags — `emails`, `links`, `all`

| Flag | Description |
|---|---|
| `--dry-run` | Fetch and log only — no LLM calls, no notes written |
| `--no-images` | Skip image extraction (Agent 4) |
| `--no-classify` | Skip classification — all items go directly to summarisation |
| `--no-research` | Skip topic research (Agent 5); new topics are queued for manual review |
| `--limit N` | Process at most N emails/links per run |

### Email flags — `emails`, `all`

| Flag | Description |
|---|---|
| `--account ALIAS` | Process one account only (e.g. `personal`, `work`) |
| `--no-backup` | Skip Agent 9 Git backup after run |
| `--bootstrap` | Bootstrap mode: reprocess all historical emails (ignores processing log) |

### Link flags — `links`, `all`

| Flag | Description |
|---|---|
| `--reprocess-failed` | Retry URLs previously marked as failed in `link_log` |
| `--no-playwright` | Skip Playwright fallback; JS-gated pages are classified as `js_required` |
| `--links-file PATH` | Override links CSV path (default: `pipeline/links.csv`) |

### `all` flags

| Flag | Description |
|---|---|
| `--continue-on-error` | Keep running remaining steps even if one step fails |
| `--no-browser` | Suppress auto-open of dashboard after a successful `all` run |

> **Note on `all` and interactive features:** when `all` completes successfully and `--no-browser` was not passed, it opens the dashboard via `_open_dashboard_via_server()` — a minimal `http.server.SimpleHTTPRequestHandler` instance bound to `127.0.0.1:8420` by default. This server can display all dashboard pages but does **not** implement the `/api/research`, `/api/skip`, `/api/ingest`, or `/api/run_links` POST endpoints, since those live in `serve_dashboard.py`'s `_DashboardHandler`, not in this lighter handler. If you want the Research Queue's Research/Skip buttons or the manual ingest bookmarklet to work, run `python main.py serve` (which starts the full `serve_dashboard.py` server) either instead of, or after, `all` — both can be combined as `python main.py all --no-browser && python main.py serve` for a single unattended-then-interactive sequence.

### `serve` flags

| Flag | Default | Description |
|---|---|---|
| `--port PORT` | `8420` | Dashboard server port |
| `--page PAGE` | `progress.html` | Page opened in browser on server start |
| `--no-browser` | Off | Suppress auto-open browser |

---

## Appendix B — Folder & Repository Structure

```
PROJECT_ROOT/
├── main.py                             # Unified CLI entry point
├── requirements.txt                    # Python dependencies
├── .env                                # API key — gitignored
├── .gitignore
├── INDEX.md                            # Append-only note index
├── topics_index.json                   # JSON topic mirror (committed)
├── registry.db                         # SQLite DB — gitignored
│
├── dashboard/                          # Generated HTML dashboard pages
│   ├── index.html                      # Main dashboard (generated)
│   ├── ingest.html                     # Manual ingest UI
│   ├── link_review.html                # Link fetch log + filter
│   ├── progress.html                   # Live run progress
│   ├── progress.json                   # Written during pipeline run
│   └── research_queue.html             # Manual research queue
│
├── pipeline/                           # Pipeline code
│   ├── config.py                       # Central configuration
│   ├── db.py                           # SQLite schema + query helpers
│   ├── orchestrator.py                 # Email pipeline orchestrator
│   ├── link_orchestrator.py            # Link pipeline orchestrator
│   ├── serve_dashboard.py              # HTTP server + API endpoints
│   ├── progress_writer.py              # progress.json writer
│   ├── relink_notes.py                 # Wikilink injection helpers (shared with local_writer) + batch relink CLI
│   ├── reorder_index.py                # Re-sort INDEX.md (recurring maintenance)
│   ├── links.csv                       # Saved article URLs
│   │
│   ├── dashboard/                      # All dashboard HTML generators
│   │   ├── generate_dashboard.py       # Generates dashboard/index.html
│   │   ├── generate_link_review.py     # Generates dashboard/link_review.html
│   │   └── generate_research_queue.py  # Generates dashboard/research_queue.html
│   │
│   ├── fixes/                          # One-off troubleshooting / retrofit scripts
│   │   ├── cleanup_duplicate_images.py # One-off: remove duplicate images
│   │   ├── retrofit_related_notes.py   # One-off: re-trim Related Notes sections
│   │   ├── retag_existing_notes.py     # One-off: sanitise space-containing tags
│   │   ├── diagnose_topic_index.py     # One-off: find/repair corrupted embeddings
│   │   └── fix_asset_links.py          # One-off: repair broken assets/<slug>/ image links
│   │
│   ├── agents/
│   │   ├── ingestion.py                # Agent 1: Gmail fetch
│   │   ├── classification.py           # Agent 1.5: Content classification
│   │   ├── summarisation.py            # Agent 2: AI summarisation
│   │   ├── topic_linking.py            # Agent 3: Semantic topic linking
│   │   ├── image_extraction.py         # Agent 4: Image download + filter
│   │   ├── research.py                 # Agent 5: Topic research
│   │   ├── logging_agent.py            # Agent 6: Logging
│   │   ├── local_writer.py             # Agent 7: Note assembly + write
│   │   ├── gmail_label.py              # Agent 8: Gmail label application
│   │   ├── git_backup.py               # Agent 9: Git backup
│   │   └── link_ingestion.py           # Link fetch + fallback chain
│   │
│   └── manual_content/                 # Queued manual ingest JSON files
│       └── processed/                  # Moved here after processing
│
├── notes/                              # Generated Markdown notes (OneDrive primary)
│   └── assets/                         # Extracted newsletter images
│       └── {message_slug}/
│           ├── image.png
│           └── manifest.json
│
├── logs/                               # Session-level fetch logs
│   ├── link_fetch_{timestamp}.log
│   └── link_fetch_{timestamp}.json
│
└── secrets/                            # OAuth2 credentials — gitignored
    ├── credentials-personal.json
    ├── token-personal.json
    ├── credentials-work.json
    └── token-work.json
```

### What is committed to GitHub

- All `pipeline/` code
- `dashboard/*.html` (generated)
- `INDEX.md`, `topics_index.json`
- `notes/**/*.md` (note content)
- `requirements.txt`, `README.md`, `main.py`

### What is gitignored

- `registry.db` (binary, local only)
- `.env` (API key)
- `secrets/` (OAuth2 tokens)
- `notes/assets/` (binary images)
- `__pycache__/`, `*.pyc`
- `pipeline/manual_content/` (transient queue)
- `logs/` (session logs)

---

## Appendix C — Gmail Multi-Account Setup Guide

### Overview

Each Gmail account requires its own Google Cloud project OAuth2 credentials. You cannot reuse credentials across accounts unless they are managed under the same Google Cloud project (which is permitted).

### Steps per account

**1. Create a Google Cloud project**

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g., "Newsletter Pipeline Personal")
3. Enable the Gmail API: APIs & Services → Enable APIs → search "Gmail API" → Enable

**2. Create OAuth2 credentials**

1. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
2. Application type: Desktop App
3. Download the JSON file → save as `secrets/credentials-personal.json`

**3. Configure OAuth consent screen**

1. APIs & Services → OAuth consent screen
2. User type: External
3. Add your Gmail address as a test user
4. Scopes: `https://www.googleapis.com/auth/gmail.modify`

**4. Configure `config.py`**

```python
GMAIL_ACCOUNTS = [
    {
        "alias":            "personal",
        "credentials_file": str(SECRETS_DIR / "credentials-personal.json"),
        "token_file":       str(SECRETS_DIR / "token-personal.json"),
        "newsletter_label": "newsletters",   # Gmail label to filter by
        "sender_allowlist": [                # If non-empty, ignores label filter
            "author@newsletter.example.com",
        ],
        "sender_blocklist": [],
    },
]
```

**5. Authenticate (first run)**

```bash
python main.py emails --account personal --dry-run
```

A browser window will open for OAuth consent. After approval, `token-personal.json` is created in `secrets/`.

**6. Add a second account**

Repeat steps 1–5 with `alias: "work"` and separate credential/token files. Both accounts will be processed on each `python main.py emails` run.

### Notes

- The `sender_allowlist` bypasses Gmail label filtering entirely — only senders in the list are fetched
- The `sender_blocklist` skips matching senders even if they would otherwise pass the label or allowlist
- `GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]` is required for Agent 8 to apply labels; use `gmail.readonly` if you want read-only mode (Agent 8 will fail silently)

---

## Appendix D — Obsidian Setup Guide (Windows + Linux)

### Windows (primary)

1. Download and install [Obsidian](https://obsidian.md)
2. Open Obsidian → "Open folder as vault"
3. Navigate to `notes/` within your project root (e.g., `C:\Users\<username>\OneDrive\Documents\newsletter-pipeline\notes`)
4. Obsidian will index all `.md` files and resolve `[[wikilinks]]` automatically

**Recommended Obsidian settings:**
- Files and Links → Default location for new notes: `notes/`
- Files and Links → Attachment folder path: `assets/`
- Editor → Readable line length: On

**Recommended community plugins:**
- Dataview — for querying `topics_index.json` and building custom views
- Calendar — for browsing notes by date

### Linux / WSL

1. Install Obsidian via AppImage or Flatpak
2. Mount or symlink the OneDrive notes directory:
   ```bash
   # If using rclone for OneDrive sync:
   rclone mount onedrive:Documents/newsletter-pipeline/notes ~/notes
   ```
3. Open Obsidian → "Open folder as vault" → select the mounted/synced `notes/` directory

**Note:** `registry.db` is not required in the vault. Notes are standalone Markdown files — Obsidian only needs the `notes/` directory.

### iPhone (read-only)

Use [Obsidian for iOS](https://obsidian.md/mobile) with iCloud sync, or use a read-only setup via [Working Copy](https://workingcopyapp.com) (git clone of the GitHub repository) combined with Obsidian's "Open in Obsidian" shortcut for read access to committed notes.

---

## Appendix E — Topic Linking & Wikilink Design

### Design goals

1. Every note must be linkable to other notes on shared topics — automatically, without manual tagging
2. Every tag must become a first-class Obsidian `[[wikilink]]` — reliably, not just when it happens to appear in prose
3. Related notes must be surfaced within the note itself, not only through Obsidian's backlinks panel

### Wikilink injection — two passes

**Pass 1 (reliable): Tags section**
Every tag extracted by Agent 2 is listed in an explicit `## Tags` section as a `[[wikilink]]`. This is guaranteed to run regardless of the note's prose content. Tags use hyphenated format for Obsidian compatibility.

```markdown
## Tags
[[rag-pipelines]] [[vector-databases]] [[llm-fine-tuning]]
```

**Pass 2 (best-effort, scoped): Inline injection**
After the note body is assembled, `_inject_wikilinks_in_scope()` in Agent 7 scans the `## Summary` and `## Key Takeaways` sections only for exact tag phrase matches and wraps them in `[[...]]`. The scope is intentionally restricted to these two sections — running the pass over the full note caused multi-word tag phrases in `## Related Notes` to fragment (e.g. `agent architecture` → `agent [[architecture]]`) and tool names in `## Mentions` to acquire unwanted links.

This pass frequently finds zero matches because LLM-generated summaries rarely reproduce the exact extracted tag string. That is expected — Pass 1 is the canonical, reliable mechanism. Pass 2 is a supplementary bonus for in-sentence occurrences.

Note: the LLM prompt for Agent 2 explicitly forbids `[[wikilinks]]` in the `summary` and `key_takeaways` output fields. This prevents the LLM from injecting its own bracket syntax into prose, which historically caused fragmentation before the scope restriction was added.

### Topic index update sequence

1. Agent 7 writes the note to disk
2. Agent 3 calls `update_topic_index(tags, note_file, account_alias)`:
   - New tag: insert row with tag, first_seen, first_seen_account, note reference, embedding
   - Existing tag: append note reference to `note_files` JSON array
3. Agent 7 calls `update_topics_json()` to mirror the update to `topics_index.json`

### Related note discovery

For each new note, Agent 3 embeds all tags and computes cosine similarity against the full embedding corpus in `topic_index`. Notes with similarity ≥ `SIMILARITY_THRESHOLD` (0.75) are returned as related, excluding the current note.

```
new tag: "retrieval-augmented-generation"
existing tag: "rag-pipelines" → similarity: 0.87 → RELATED
existing tag: "product-management" → similarity: 0.23 → not related
```

Related notes are rendered in the `## Related Notes` section with shared tags as plain text (not as wikilinks — see Appendix E for why):
```markdown
## Related Notes
- [[2026-05-15-personal-ai-weekly]] — shared tags: rag-pipelines, embeddings
```

### Retroactive relinking (`relink_notes.py`)

If the topic index grows substantially after older notes were written (e.g., after importing a backlog of newsletters), `python main.py relink` re-runs Pass 1 and Pass 2 over all existing notes, injecting any new wikilinks that can be resolved against the current topic index.

---

## Appendix F — Link Pipeline Design

### Overview

The link pipeline processes saved article URLs from two sources:
- `pipeline/links.csv` — manually curated URLs with optional labels and dates
- `pipeline/manual_content/*.json` — content pasted via the bookmarklet/ingest UI

It shares agents 2–7 with the email pipeline. The link ingestion agent produces article dicts using the same schema as email dicts, so all downstream agents are reused without modification.

### links.csv format

```csv
url,label,added_date
https://blog.example.com/article,ai agents,2026-06-18
https://realpython.com/django-setup,,2026-01-15
```

- `label` — optional user-supplied category tag
- `added_date` — optional; defaults to fetch date if omitted
- URLs already in `link_log` are skipped (deduplication)

### Processing flow

```
links.csv
    ↓
[link_ingestion: ingest_links()]
    ↓ (article dicts)
[orchestrator: for each article]
    ↓
Agent 2 (Summarisation) → Agent 3 (Topic Linking) → Agent 4 (Image Extraction)
    ↓
Agent 5 (Research, if new topics) → Agent 7 (Local Writer) → Agent 6 (Logging)
```

### Manual content JSON format

Written by `POST /api/ingest` and read by `ingest_manual()`:

```json
{
  "url":        "https://example.com/article",
  "body_text":  "Full article text...",
  "title":      "Article Title",
  "label":      "ai agents",
  "added_date": "2026-06-26"
}
```

The filename is always `ingest_{YYYYMMDDTHHMMSSZ}.json` — never derived from client input.

---

## Appendix G — Link Fetch Reliability: Six-Way Classification, RSS & Playwright Fallbacks

### Fetch status classification

Every URL processed by the link pipeline is classified into one of six statuses:

| Status | Meaning | Saved? |
|---|---|---|
| `fetched` | Full content retrieved (≥ 300 words) | Yes |
| `partial` | Some content retrieved (80–299 words); paywall preview | Yes, flagged |
| `js_required` | 0 words after all fallbacks; JS rendering not available | No |
| `blocked` | HTTP 401/403/429; bot protection | No |
| `paywalled` | Content present but clearly gated; below useful threshold | No |
| `failed` | Network error, DNS failure, timeout | No, retried next run |

`partial` content (e.g., Medium member-only preview paragraphs) is saved as a note because even 100–250 words of genuine preview prose is more useful than nothing. The note is flagged in the link review page for manual follow-up.

### Three-layer fetch chain

For each URL, the pipeline attempts fetches in this order, moving to the next layer only if the previous returned 0 words:

**Layer 1: Direct HTTP fetch (requests + BeautifulSoup)**
- Fast (~100ms), works for most plain-HTML sites
- User-Agent mimics Chrome desktop to avoid basic bot detection
- Readability-style content extraction: strips nav, sidebars, footers, ads

**Layer 2: Substack RSS fallback**
- Applied only when Layer 1 returns 0 words AND the URL is Substack-hosted
- Fetches `{base_url}/feed` and parses `<content:encoded>` for the matching article
- Scans up to 100 feed items; older posts may no longer be in the feed
- Text-only: no images are recoverable via this path
- Fast (~200ms); zero JavaScript dependency

**Layer 3: Playwright headless browser**
- Applied when Layer 1 (and Layer 2 if applicable) return 0 words
- Launches Chromium, navigates to the URL, waits for `networkidle`
- Returns fully-rendered HTML including images (Agent 4 can extract them)
- Slow (~3–8s per page); optional dependency — if Playwright is not installed, this layer is silently skipped
- User-Agent overridden to avoid "HeadlessChrome" detection marker
- Disabled with `--no-playwright` flag

### Fallback tracking

Both `via_rss_fallback` and `via_playwright_fallback` are persisted as columns on every `link_log` row (not only when fallbacks are used). The dashboard shows 30-day fallback rates:

```
Playwright fallback: 12% (24 of 200 links)
RSS fallback:         8% (16 of 200 links)
Any fallback:        18% needed recovery
```

---

## Appendix H — Ingestion Performance & Index Maintenance

### Deduplication strategy

**Email pipeline:** Deduplication is based on Gmail Message-ID (the `Message-ID` header), fetched once per run as a set from `processing_log`. Message-IDs are checked before any API call is made. Cross-account deduplication is also applied — if the same Message-ID appears in both the personal and work account (forwarded mail), only the first occurrence is processed.

**Link pipeline:** Deduplication is based on URL, normalised and stored as a SHA-256 hash in `link_log.url_hash`. URLs with any status (success, failed, partial) are skipped on subsequent runs. To retry a failed URL, delete its row from `link_log` or use the link review page.

### Index ordering

`INDEX.md` is append-only during normal pipeline operation. If notes are inserted out of date order (e.g., after processing a backlog), `python main.py reorder-index` re-sorts the file by received date. The sorted file is safe to commit to GitHub.

### Topic index maintenance

The topic index grows monotonically — topics are never automatically removed. If a tag is found to be low-quality or erroneous, it can be deleted directly from `registry.db`:

```sql
DELETE FROM topic_index WHERE tag = 'erroneous-tag';
```

After deletion, run `relink` to update note wikilinks if the tag was linked elsewhere.

### Performance characteristics (typical)

| Operation | Time per item |
|---|---|
| Gmail fetch + parse | ~50ms |
| Heuristic classification | < 1ms |
| LLM classification (Haiku) | ~500ms |
| LLM summarisation (Haiku) | ~1–2s |
| Embedding generation (local) | ~20ms for 5 tags |
| Image download + filter | ~100–500ms per image |
| Note write | < 5ms |
| Link fetch (direct HTTP) | ~200–500ms |
| Link fetch (Playwright) | ~3–8s |

---

## Appendix I — Link Review & Manual Research Queue

### Link Review page (`dashboard/link_review.html`)

Generated by `pipeline/dashboard/generate_link_review.py` after each link pipeline run. Provides a filterable, searchable table of all URLs ever processed, with columns:

- Title / URL
- Domain
- Fetch status (colour-coded badge)
- HTTP status code
- Word count
- Fetch timestamp
- Detail (error message or fallback used)

**Filter buttons:** All / Fetched / Partial / JS Required / Blocked / Paywalled / Failed

The Link Review page is static HTML — no server required to view it. Regenerated on each run.

### Research Queue (`dashboard/research_queue.html`)

Generated by `pipeline/dashboard/generate_research_queue.py`. Displays all topics in `research_queue` with their status (pending / done / skipped) and paginated at 25 rows per page.

**Interactive features (requires `serve_dashboard.py`):**

- **Select topics** with checkboxes (current page or all)
- **Research selected** — calls `POST /api/research` with selected topic names; Claude Sonnet + web search generates a summary for each; research is appended to the corresponding note
- **Skip selected** — marks topics as skipped (not relevant enough to research)
- **Filter by status** — All / Pending / Done / Skipped
- **Live cost tracking** — running cost shown in session header updates after each research call

The page updates visually on completion without requiring a reload: badge colours change (orange → green for done, grey for skipped) and the summary is shown inline below the topic row.

---

## Appendix J — Live Progress UI Design

### `dashboard/progress.html`

A polling-based live progress viewer that reads `dashboard/progress.json` every 2 seconds while the pipeline is running. No WebSocket dependency — plain `setInterval` + `fetch`.

**Displayed metrics (updated per email):**
- Current email: subject, account, sender
- Stage indicator: Ingesting / Classifying / Summarising / Linking / Extracting / Researching / Writing
- Counts: processed, skipped, failed, total
- Running cost (USD)
- Current run duration

**`progress.json` schema:**
```json
{
  "status":       "running",
  "current":      {"subject": "...", "account": "personal", "stage": "summarising"},
  "counts":       {"processed": 3, "skipped": 1, "failed": 0, "total": 12},
  "cost_usd":     0.00423,
  "started_at":   "2026-06-18T08:00:00Z",
  "updated_at":   "2026-06-18T08:01:23Z"
}
```

When the run completes, `status` is set to `"done"` and polling stops automatically. The progress file is reset at the start of each new run.

The `pipeline/progress_writer.py` module is imported by the orchestrator and called after each email completes. It writes atomically (write temp file, rename) to avoid partial reads by the dashboard.

---

## Appendix K — Dashboard Design

### `dashboard/index.html`

Fully self-contained static HTML generated by `pipeline/dashboard/generate_dashboard.py`. Can be opened directly in a browser (no server required) or served via `serve_dashboard.py`. Regenerated at the end of each pipeline run.

**Visual design:** Dark theme (`#0f1117` background), DM Mono + Sora fonts, Chart.js 4.4 for the cost/email bar+line chart.

**Sections:**

| Section | Content |
|---|---|
| Header | Pipeline title, links to sub-pages, last-generated timestamp |
| Summary cards | Total processed, total cost, unique topics, total images, cache hit rate |
| Cost over time | Bar + line chart: daily cost (bars) + email count (line), last 30 days |
| Per-account stats | Cost and email count per configured Gmail account |
| Classification breakdown | Count by stage and outcome |
| Tag cloud | Top 20 tags by frequency; chip size proportional to count |
| Recent emails | Table: subject, account, processed date, status badge, cost, tokens, images, duration |
| Link pipeline overview | Total URLs, fetched, partial, blocked, paywalled, JS required, failed |
| Fallback usage (30 days) | Playwright fallback %, RSS fallback %, any fallback % |
| Link fetch log | Table: title/URL, domain, status badge, HTTP code, words, fetch time, detail |
| Research queue summary | Pending, done, skipped counts; queue all-time cost; link to full queue page |

**Navigation links in header:**
- Progress → `progress.html`
- Link Review → `link_review.html`
- Research Queue → `research_queue.html`
- Manual Ingest → `ingest.html`

### `dashboard/ingest.html`

Manual article ingest UI. Contains a form (URL, article text, title, label, date) that POSTs to `/api/ingest` and optionally triggers a link pipeline run via `/api/run_links`. Requires `serve_dashboard.py` to be running.

The bookmarklet code is displayed on this page for easy copy-paste into the browser bookmarks bar.

---

## Appendix L — Pre-Production Cleanup & Go-Live Checklist

Use this checklist before the first production run or after a major refactor.

### Configuration

- [ ] `ANTHROPIC_API_KEY` is set in `.env` and confirmed valid
- [ ] `config.py` has been validated: `python pipeline/config.py` returns no errors
- [ ] All account `credentials_file` paths exist in `secrets/`
- [ ] `sender_allowlist` entries are correct (test with `--dry-run` first)
- [ ] `sender_blocklist` entries are correct
- [ ] `CLASSIFIER_CONFIDENCE_THRESHOLD` is set to desired value (start with 0.75)
- [ ] `MAX_RESEARCH_TOPICS_PER_RUN` is set (0 = manual only is recommended for first runs)

### Authentication

- [ ] OAuth2 token obtained for each account via `--dry-run` browser flow
- [ ] `--dry-run` output shows correct email counts and labels for each account
- [ ] Gmail labels "newsletters" (or configured label) exist in each account

### Infrastructure

- [ ] `registry.db` initialised: `python pipeline/db.py` (or first pipeline run)
- [ ] `notes/` directory exists and is inside OneDrive sync boundary
- [ ] `notes/assets/` directory exists
- [ ] Git remote configured: `git remote -v` shows correct GitHub URL
- [ ] GitHub Personal Access Token stored in Windows Credential Manager
- [ ] Playwright installed: `playwright install chromium`
- [ ] `python main.py serve --no-browser` confirms server starts without error

### First run

- [ ] Run with `--no-backup --no-research` first to validate the core pipeline
- [ ] Review `dashboard/index.html` for expected email counts and costs
- [ ] Review `dashboard/link_review.html` if running links
- [ ] Check `notes/` for generated Markdown files
- [ ] Confirm wikilinks resolve correctly in Obsidian
- [ ] Run `python main.py serve` and test Research Queue with one topic

### Scheduling (Windows Task Scheduler)

- [ ] Task 1: `python main.py all --no-browser` — daily at desired time
- [ ] Task 2: `python pipeline/agents/git_backup.py` — 30 minutes after Task 1
- [ ] Both tasks configured with correct working directory and Python path
- [ ] Both tasks set to "Run whether user is logged on or not"
- [ ] Test tasks manually via "Run" before relying on schedule

### Ongoing

- [ ] `.gitignore` confirmed: `registry.db`, `.env`, `secrets/`, `notes/assets/` are not tracked
- [ ] Review "AI Review" Gmail label weekly during classifier tuning period
- [ ] Monitor dashboard cost chart — alert if daily cost unexpectedly spikes
- [ ] Check Research Queue after each run with many new topics

---

## Appendix M — Obsidian Knowledge Extraction Workflows

### Why this matters

The pipeline's output is only as useful as your ability to query and rediscover it. A vault of well-structured Markdown notes that nobody revisits is not meaningfully different from an inbox nobody revisits — the format changed, but the underlying problem (accumulation without retrieval) hasn't been solved. This appendix covers the practical workflows for actually extracting value from the knowledge base over time, in increasing order of setup effort and power.

### Level 1 — Native search and Graph View (zero setup)

Every note's YAML frontmatter and `[[wikilinks]]` are immediately searchable using Obsidian's built-in search syntax, with no plugins required.

**Tag search:**
```
tag:#rag-pipelines
```
Returns every note containing that tag, including notes where it only appears via the auto-generated Tags section.

**Combined search** (tag + date range, useful for "what did I read about X recently"):
```
tag:#product-management received:2026-05
```

**Graph View** (`Ctrl/Cmd+G` or the graph icon in the left ribbon) visualises the wikilink structure across all notes. For a topic-focused vault like this one, two settings matter most:

- **Filter box:** type `tag:#your-topic` to isolate just the notes and links touching that topic, hiding the rest of the graph
- **Color groups** (Graph View settings → Groups → New group): assign a search term (e.g. `tag:#ai-agents`) and a colour, so that topic's cluster is visually distinct. Repeat for your 5–10 most active topics. Over weeks, clusters that grow dense visually tell you where your reading attention has actually concentrated — often a useful signal independent of what you intended to focus on.

**Local Graph View** (open from a single note, "Open local graph" command) shows only the notes connected to the one you're currently reading, at a configurable depth (1 = direct links only, 2 = links of links, etc.). This is the most direct payoff of the pipeline's `## Related Notes` section and inline wikilinks — while reading one note, the local graph shows you exactly what else in your knowledge base touches the same ground, without a manual search.

### Level 2 — Dataview plugin (recommended; one-time install)

Install via Settings → Community Plugins → Browse → search "Dataview" → Install → Enable. Dataview indexes your vault's YAML frontmatter and tags, and lets you write live, self-updating queries directly inside any note.

This is the natural next step for this pipeline specifically because every note already has clean, structured frontmatter:

```yaml
---
source_account: personal
sender: author@newsletter.example.com
received: 2026-06-18
processed: 2026-06-18T14:32:00+00:00
tags: ["rag-pipelines", "vector-databases"]
---
```

Dataview queries that frontmatter directly — no extra tagging or setup work needed beyond what the pipeline already produces.

**Suggested starter dashboard.** Create a new note, e.g. `Dashboard.md`, and add:

````markdown
## Recently processed

```dataview
TABLE source_account AS "Account", received AS "Received"
FROM "notes"
SORT processed DESC
LIMIT 20
```

## By topic: RAG pipelines

```dataview
TABLE source_account AS "Account", received AS "Received"
FROM #rag-pipelines
SORT received DESC
```

## Notes per account

```dataview
TABLE length(rows) AS "Count"
FROM "notes"
GROUP BY source_account
```

## Notes with no tags (data quality check)

```dataview
LIST
FROM "notes"
WHERE length(tags) = 0
```
````

The last query is genuinely useful for spotting cases where Agent 2's summarisation produced no tags (e.g. due to a malformed JSON response that fell back to an empty structured dict — see Risk R4 in the main document) — these notes are otherwise invisible in topic-based browsing.

**Why Dataview over manually maintaining `INDEX.md`:** `INDEX.md` is useful as a Git-visible, plain-text audit trail (and it's what gets committed to GitHub), but it's static and append-only. Dataview queries are live — they reflect the current state of the vault every time you open the note, including notes added after `INDEX.md` was last regenerated, and they support filtering/grouping/sorting that a flat Markdown table cannot.

### Level 3 — Obsidian Bases (native, no plugin, since v1.9.10)

Bases is Obsidian's own built-in database feature, introduced as a core feature without requiring a community plugin. It overlaps with Dataview's table use case but through a GUI rather than a query language:

1. Command Palette → "Bases: New base"
2. Filter by tag (`file has tag "rag-pipelines"`) or folder
3. Add frontmatter fields as columns (`source_account`, `received`)
4. Sort/group through the UI — no syntax to write

**When to prefer Bases over Dataview:** quick ad-hoc browsing, especially on mobile (Bases works smoothly there; Dataview's mobile experience is more limited). **When to prefer Dataview:** anything requiring computed values, multi-condition filtering, or aggregation (e.g. "count of notes per topic per month") — Dataview's query language is materially more expressive. Many users run both side by side; they are not mutually exclusive, and Bases queries can't yet do everything Dataview can as of this writing.

### Level 4 — Periodic review workflow

A lightweight habit that compounds: once a week or every two weeks, open the Dataview "Recently processed" query above, skim the list, and for any note that seems worth revisiting, add a `status:: reviewed` inline field (Dataview inline field syntax) or a `#reviewed` tag. Over time this builds a second, smaller layer — "things I've actually internalised" — distinct from "things the pipeline captured." The pipeline solves capture; this step is what closes the loop on retention.

A query to support this:

````markdown
## Captured but not yet reviewed

```dataview
LIST
FROM "notes"
WHERE !contains(file.tags, "#reviewed")
SORT received DESC
```
````

### A note on Obsidian Publish / GitHub Pages

If you ever want to make curated parts of the knowledge base browsable outside Obsidian (e.g. a personal "now reading" page), the notes are already plain Markdown with YAML frontmatter, so they're compatible with static site generators like Quartz, or Obsidian's own Publish service, without reformatting. This is out of scope for the current design but worth knowing the door is open, since no Obsidian-specific syntax beyond standard `[[wikilinks]]` is used.

---

## Appendix N — Storage Optimisation & Data Quality Maintenance

This appendix documents two data quality issues that emerge as the knowledge base grows past the first few dozen notes, the root cause of each, the fix applied, and the one-off scripts provided to clean up data that accumulated before the fix.

### N.1 — Duplicate image storage

**Symptom:** The same image (a newsletter's header logo, footer banner, sponsor graphic, or author headshot) appears saved multiple times across different note asset folders, inflating `notes/assets/` well beyond what the actual unique image content would require.

**Root cause:** `agents/image_extraction.py`'s `_make_filename()` derives the saved filename from `md5(source_url)[:10]`. This is deterministic per URL — the same URL always produces the same filename — but the save location is `notes/assets/{message_slug}/`, where `message_slug` is derived from the unique Gmail Message-ID of *each* email. There is no cross-message check: every email gets its own asset folder, so an image reused across N newsletter issues gets downloaded and saved N times into N different folders, even though the bytes are identical.

This is compounded by a second factor: many email service providers (ESPs) append a per-send tracking query string to otherwise-identical image URLs (e.g. `?utm_campaign=2026-06-18`), meaning even *URL-based* deduplication would frequently miss real duplicates, since the URL itself differs send-to-send even though the image does not.

**Fix:** Content-hash based deduplication (SHA-256 of the downloaded — and, where applicable, WEBP-to-GIF converted — image bytes), tracked in a new `image_dedup_index` SQLite table. Before saving a new image, the pipeline checks whether an identical-content image has already been saved anywhere in the vault; if so, the note references the existing canonical file instead of writing a new copy.

Full implementation: see `patch_01_config_additions.py` (new config flags), `patch_02_db_additions.py` (new table + helpers), and `patch_03_image_extraction_dedup.py` (the actual save-path change in `image_extraction.py`).

**Applying the fix to new runs:** apply the three patches above to your `pipeline/` codebase. `IMAGE_DEDUP_ENABLED = True` in `config.py` is the only runtime toggle; no other configuration is required.

**Cleaning up existing duplicates:** the patches above only prevent *future* duplicates — they do not retroactively touch images already saved. `cleanup_duplicate_images.py` is a standalone, one-off script that scans the entire existing `notes/assets/` tree, groups files by content hash, and removes redundant copies (keeping the oldest copy of each as canonical):

```bash
cd pipeline
python fixes/cleanup_duplicate_images.py --dry-run
# Review the report: duplicate groups, redundant file count, recoverable space
python fixes/cleanup_duplicate_images.py --apply
# Optionally also rewrite note Markdown to point at the canonical copy:
python fixes/cleanup_duplicate_images.py --apply --rewrite-links
```

`--dry-run` is the default behaviour and never modifies files; `--apply` requires explicit confirmation (`yes`) before deleting anything, given the destructive nature of the operation. `--rewrite-links` additionally scans `notes/*.md` for references to each deleted duplicate's filename and updates them to point at the canonical copy — without this flag, older notes that referenced a now-deleted duplicate will show a broken image embed (acceptable for one-off banner/logo images that aren't unique content; use `--rewrite-links` if precision matters to you).

### N.2 — Unbounded Related Notes sections

**Symptom:** The `## Related Notes` section of some notes lists dozens to hundreds of links, making the section unreadable and defeating its purpose as a curated jumping-off point.

**Root cause:** `agents/topic_linking.py`'s `find_related_notes()` included a note in the results if **any single tag** matched an existing topic at cosine similarity ≥ `SIMILARITY_THRESHOLD` (default 0.75) — there was no minimum number of shared tags required, and critically, **no upper bound** on how many notes could be returned. As the topic index grows, broad or frequently-recurring tags (e.g. "ai", "product management") accumulate associations with dozens or hundreds of notes. Any new note sharing even one such tag pulled in every one of those notes as "related," regardless of how weak that single-tag overlap actually was as a relevance signal.

**Fix:** Two independent, configurable controls now apply (see `RELATED_NOTES_MIN_SHARED_TAGS` and `RELATED_NOTES_MAX_RESULTS` in Section 6, Data Design, and Appendix A):

- **Quality floor** (`RELATED_NOTES_MIN_SHARED_TAGS`, default `2`): a note must share at least this many distinct tags to qualify at all. This removes weak, coincidental single-tag matches at the source.
- **Quantity ceiling** (`RELATED_NOTES_MAX_RESULTS`, default `10`): even among qualifying notes, only the top-N (sorted by shared-tag count descending) are rendered. This guarantees the section can never grow unbounded, regardless of how strongly a topic is shared across the vault.

Both values are deliberately configurable rather than hardcoded, since the right balance shifts as the vault grows — a 50-note vault might reasonably allow more permissive matching than a 1,000-note vault, where even strong single-topic matches need a tighter ceiling to stay useful.

Full implementation: see `patch_05_topic_linking_related_notes_cap.py`, which extends `find_related_notes()` with two new optional keyword arguments defaulting to the config values — both existing production call sites (in `orchestrator.py` and `link_orchestrator.py`) require no changes, since they call the function positionally and pick up the new defaults automatically.

**Cleaning up existing notes:** like the image dedup fix, this patch only affects notes written *after* it's applied — it does not retroactively re-trim already-bloated `## Related Notes` sections in existing notes. Unlike `relink_notes.py` (which only handles inline wikilink injection into prose and never touches the Related Notes section), `retrofit_related_notes.py` is a new standalone script purpose-built for this:

```bash
cd pipeline
python fixes/retrofit_related_notes.py --dry-run
# Reports old count → new count per note, and overall % reduction
python fixes/retrofit_related_notes.py --apply
```

The script reads each existing note's `tags` from its YAML frontmatter, re-runs `find_related_notes()` under the current threshold/cap settings, and replaces the note's `## Related Notes` section in place (full replacement, not append — so the script is idempotent and safe to re-run after further config changes).

### N.3 — Choosing your own threshold and cap values

Both `RELATED_NOTES_MIN_SHARED_TAGS` and `RELATED_NOTES_MAX_RESULTS` are personal-preference settings more than correctness settings — there's no universally "right" value, only a trade-off between precision (fewer, stronger matches) and recall (more matches, including weaker ones) that depends on how you intend to use the Related Notes section:

| If you treat Related Notes as... | Consider |
|---|---|
| A quick skim while reading — "what else touches this" | Lower `MAX_RESULTS` (5–7), keep `MIN_SHARED_TAGS` at 2 |
| A jumping-off point for deeper research sessions | Higher `MAX_RESULTS` (10–15) |
| Primarily useful for very specific multi-tag overlaps | Raise `MIN_SHARED_TAGS` to 3 |
| Still valuable even with single coincidental tag matches | Lower `MIN_SHARED_TAGS` to 1 (restores original behaviour, combined with a cap) |

Re-running `retrofit_related_notes.py --apply` after changing these values in `config.py` will re-trim all existing notes to match the new settings — the script is safe to run repeatedly as you tune these numbers.

### N.4 — Invalid Obsidian tags and fragmented wikilinks

**Symptom A — Invalid tags:** Tags appear in red or with a warning icon in Obsidian's frontmatter editor. Clicking a tag in the Tags section does nothing, or the tag pane shows no notes under it. `tag:#rag-pipelines` finds notes correctly but `tag:#rag pipelines` (with a space) finds nothing. Dataview queries using tag-based filtering silently return empty results.

**Root cause A:** Obsidian tags cannot contain spaces — the valid character set is letters, numbers, underscores, and hyphens. A tag like `rag pipelines` is parsed as the tag `#rag` followed by the plain word `pipelines`, breaking all tag-based navigation. The original summarisation prompt instructed the LLM to generate "2–4 word topic labels" with example values that contained spaces (e.g. `"rag pipelines"`), and no sanitisation step existed to convert them.

**Fix A:** Two layers. First, the `_SYSTEM_PROMPT` in `agents/summarisation.py` now explicitly requires hyphens between words and provides hyphenated examples (`"rag-pipelines"`, `"agent-architecture"`). Second, `_sanitise_tag()` in `_normalise()` runs on every tag regardless of LLM output, converting spaces to hyphens, stripping invalid characters, collapsing multiple hyphens, and de-duplicating collisions.

**Symptom B — Fragmented inline wikilinks:** The `## Related Notes` section shows entries like `agent [[architecture]]` instead of `[[agent-architecture]]`, or `[[open-source]] [[LLMs]]` instead of `[[open-source-llms]]` in note prose. `## Mentions` tool names occasionally appear as `[[Claude]] Code` with partial wikilinks.

**Root cause B:** Two distinct sub-causes. The LLM was previously adding `[[wikilinks]]` directly into `summary` and `key_takeaways` prose, which the injection pass then treated as already-linked text, creating fragmentation. Separately, the wikilink injection pass ran over the entire assembled note including `## Related Notes`, `## Mentions`, `## Images`, and `## Context` — causing multi-word phrases in those sections to be partially matched and incorrectly linked.

**Fix B:** The `_SYSTEM_PROMPT` now explicitly forbids `[[wikilinks]]` in `summary` and `key_takeaways` fields. The injection pass is replaced with `_inject_wikilinks_in_scope()` in `agents/local_writer.py`, which restricts injection to `## Summary` and `## Key Takeaways` sections only, leaving all other sections untouched.

**Retroactive fix for existing notes:**

```bash
cd pipeline

# Step 1: repair any corrupted embedding rows before running retag
python fixes/diagnose_topic_index.py
python fixes/diagnose_topic_index.py --apply-reembed

# Step 2: sanitise tags — rewrites frontmatter, Tags section, inline wikilinks,
#          and rebuilds topic_index + topics_index.json
python fixes/retag_existing_notes.py --dry-run
python fixes/retag_existing_notes.py --apply

# Step 3: re-trim Related Notes under the new sanitised tag strings
python fixes/retrofit_related_notes.py --dry-run
python fixes/retrofit_related_notes.py --apply
```

Run these in order — `retag_existing_notes.py` must complete before `retrofit_related_notes.py`, because the latter reads frontmatter tags (which retag rewrites) and queries `topic_index` (which retag rebuilds with sanitised keys). Running in the wrong order produces stale Related Notes sections computed against pre-sanitisation tag strings.

Fragmented `[[wikilinks]]` in `## Summary` prose from before the prompt fix (e.g. `[[open-source]] [[LLMs]]`) are not retroactively repaired — the fragmentation pattern is not safely automatable without knowing which `[[...]]` instances were correct injections vs LLM-generated ones. These are cosmetic (they create stub pages in Obsidian on click rather than breaking anything) and do not warrant bulk modification of thousands of notes.