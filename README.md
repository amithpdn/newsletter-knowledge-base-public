# Newsletter AI Processing Pipeline

A personal agentic pipeline that ingests newsletters from multiple Gmail accounts, processes saved article links, filters marketing content, generates structured Markdown notes with AI summaries, and tags every note with Obsidian `[[wikilinks]]`.

Built with Python and the Claude API. Primary storage is OneDrive (auto-synced). This repository is a code and index backup — `registry.db`, secrets, and `.env` are gitignored and never pushed.

---

## What it does

- Fetches newsletters from one or more Gmail accounts using OAuth2
- Classifies emails as editorial, marketing, or mixed using a three-stage pipeline (heuristic → LLM → editorial extraction)
- Generates structured Markdown notes: summary, key takeaways, mentions (tools / papers / people), tags
- Tags every note with Obsidian `[[wikilinks]]` and links related notes via semantic similarity
- Extracts and saves newsletter images (filters trackers, spacers, and oversized files)
- Fetches and summarises saved article links from `links.csv` with a multi-layer fetch chain (direct → RSS → Playwright)
- Researches new topics on-demand using Claude + web search and appends context sections to notes
- Maintains a local HTML dashboard with run stats, cost tracking, fallback metrics, and a research queue UI
- Backs up code and indexes to GitHub via a scheduled Git agent

---

## Pipeline overview

```
Gmail → [Agent 1: Ingestion] → [Agent 1.5: Classification] → [Agent 2: Summarisation]
     → [Agent 3: Topic Linking] → [Agent 4: Image Extraction] → [Agent 5: Research]
     → [Agent 7: Local Writer] → [Agent 8: Gmail Label] → [Agent 6: Logging]
     → [Agent 9: Git Backup]

links.csv / bookmarklet → [Link Ingestion] → same Agent 2–7 chain
```

---

## Folder structure

```
.
├── INDEX.md                        # Append-only table of all processed notes
├── main.py                         # Unified CLI entry point
├── requirements.txt
├── topics_index.json               # JSON mirror of topic index (Git-visible)
├── registry.db                     # SQLite DB — gitignored (local + OneDrive only)
├── dashboard/
│   ├── index.html                  # Main stats dashboard (generated)
│   ├── ingest.html                 # Manual ingest UI
│   ├── link_review.html            # Filterable link fetch log
│   ├── progress.html               # Live run progress viewer
│   ├── progress.json               # Written by pipeline during runs
│   └── research_queue.html         # Manual research queue UI
├── pipeline/
│   ├── config.py                   # Central configuration
│   ├── db.py                       # SQLite schema and query helpers
│   ├── orchestrator.py             # Email pipeline orchestrator
│   ├── link_orchestrator.py        # Link pipeline orchestrator
│   ├── serve_dashboard.py          # Local HTTP server + API endpoints
│   ├── progress_writer.py          # Writes progress.json during runs
│   ├── relink_notes.py             # Wikilink injection helpers (imported by local_writer) + batch relink CLI
│   ├── reorder_index.py            # Re-sort INDEX.md by date (recurring maintenance)
│   ├── links.csv                   # Saved article URLs for link pipeline
│   ├── dashboard/                  # All dashboard HTML generators
│   │   ├── generate_dashboard.py       # Generates dashboard/index.html
│   │   ├── generate_link_review.py     # Generates dashboard/link_review.html
│   │   └── generate_research_queue.py  # Generates dashboard/research_queue.html
│   ├── fixes/                      # One-off troubleshooting / retrofit scripts
│   │   ├── cleanup_duplicate_images.py # One-off: remove duplicate images from notes/assets/
│   │   ├── retrofit_related_notes.py   # One-off: re-trim Related Notes sections to new cap
│   │   ├── retag_existing_notes.py     # One-off: sanitise space-containing tags in existing notes
│   │   ├── diagnose_topic_index.py     # One-off: find and repair corrupted embedding rows
│   │   └── fix_asset_links.py          # One-off: repair broken assets/<slug>/ image links in notes
│   ├── agents/
│   │   ├── ingestion.py            # Agent 1: Gmail fetch
│   │   ├── classification.py       # Agent 1.5: Content classification
│   │   ├── summarisation.py        # Agent 2: AI summarisation
│   │   ├── topic_linking.py        # Agent 3: Semantic topic linking
│   │   ├── image_extraction.py     # Agent 4: Image download + filter
│   │   ├── research.py             # Agent 5: Topic research
│   │   ├── logging_agent.py        # Agent 6: Cost + run logging
│   │   ├── local_writer.py         # Agent 7: Note assembly + write
│   │   ├── gmail_label.py          # Agent 8: Gmail label application
│   │   ├── git_backup.py           # Agent 9: Git add/commit/push
│   │   └── link_ingestion.py       # Link fetch + fallback chain
├── notes/
│   └── assets/                     # Extracted newsletter images
├── logs/                           # Session-level fetch logs
└── secrets/                        # OAuth2 tokens — gitignored
    ├── credentials-personal.json
    ├── token-personal.json
    ├── credentials-work.json
    └── token-work.json
```

---

## Quick start

### 1. Prerequisites

- Python 3.11+
- Git for Windows (for the backup agent)
- Playwright browsers: `playwright install chromium`
- A Google Cloud project with Gmail API enabled (one OAuth2 app per account)
- An Anthropic API key

### 2. Clone and install

```bash
git clone https://github.com/<your-username>/newsletter-knowledge-base-public.git
cd newsletter-knowledge-base-public
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure

1. Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`
2. Copy `pipeline/config.example.py` to `pipeline/config.py`, then edit `pipeline/config.py`:
   - Add your Gmail accounts to `GMAIL_ACCOUNTS`
   - Place OAuth2 credentials JSON files in `secrets/`
3. Validate: `python pipeline/config.py`

### 4. Authenticate Gmail

```bash
python main.py emails --account personal --dry-run
# Follow the browser OAuth flow for each account
```

### 5. Run

```bash
# Process newsletters from all accounts
python main.py emails

# Process saved article links
python main.py links

# Run both (emails → links → regenerate all dashboards)
python main.py all

# Start the dashboard server
python main.py serve
```

---

## Run everything in one command

`python main.py all` already chains emails → links → regenerates **all three** dashboard files (`index.html`, `link_review.html`, `research_queue.html`) in a single invocation, then auto-opens the dashboard in your browser when it finishes:

```bash
python main.py all
```

This is the command to use for routine runs (e.g. from Task Scheduler). It covers everything in steps 1–3 of "Run" above without needing three separate commands.

**One caveat:** the browser tab that `all` opens automatically is a minimal static file server — it can *display* the dashboard pages, but it does not implement the `/api/research`, `/api/skip`, `/api/ingest`, or `/api/run_links` endpoints. If you want the **interactive** features to work in the same session — the Research/Skip buttons on `research_queue.html`, or the manual ingest bookmarklet on `ingest.html` — run the full server (`python main.py serve`) instead of, or after, relying on `all`'s auto-opened tab:

```bash
# Full pipeline + dashboards, with interactive features available afterward
python main.py all --no-browser
python main.py serve
```

The second command starts the full API-backed server (default port `8420`) and opens the dashboard for you, with Research Queue and manual ingest fully functional. `--no-browser` on the first command just avoids opening two browser tabs back to back.

For unattended/scheduled runs where you don't need the interactive UI immediately, plain `python main.py all` is sufficient — the dashboards are regenerated and viewable as static HTML either way.

---

## CLI reference

```
python main.py [command] [options]

Commands:
  emails       Fetch and process emails from all configured Gmail accounts (default)
  links        Fetch and summarise URLs from links.csv + manual_content/
  all          Run emails → links → regenerate all dashboard HTML files
  dashboard    Regenerate all dashboard HTML files (index, link_review, research_queue)
  queue        Regenerate dashboard/research_queue.html only
  serve        Start the local dashboard server (default port 8420)

Shared flags (emails, links, all):
  --dry-run            Fetch and log only — no LLM calls, no notes written
  --no-images          Skip image extraction (Agent 4)
  --no-classify        Skip classification — all items go directly to summarisation
  --no-research        Skip topic research (Agent 5); new topics go to queue
  --limit N            Process at most N emails/links per run

Email flags (emails, all):
  --account ALIAS      Process one account only (e.g. personal, work)
  --no-backup          Skip Git backup after run
  --bootstrap          Reprocess all historical emails (ignores processing log)

Link flags (links, all):
  --reprocess-failed   Retry URLs previously marked as failed
  --no-playwright      Skip Playwright fallback (JS-gated pages → js_required)
  --links-file PATH    Override links CSV path (default: pipeline/links.csv)

all flags:
  --continue-on-error  Keep running remaining steps even if one fails

serve flags:
  --port PORT          Dashboard server port (default: 8420)
  --page PAGE          Page to open in browser (default: progress.html)
  --no-browser         Don't auto-open a browser tab
```

---

## Manual article ingest (bookmarklet)

Start the dashboard server, then use the bookmarklet at `dashboard/ingest.html` to paste article content from any page that can't be fetched automatically. Content is queued in `pipeline/manual_content/` and processed on the next `python main.py links` run.

---

## Configuration highlights

| Setting | Default | Description |
|---|---|---|
| `SUMMARISATION_MODEL` | `claude-haiku-4-5` | Model for summarisation (cost-optimised) |
| `RESEARCH_MODEL` | `claude-sonnet-4-6` | Model for topic research (quality-optimised) |
| `CLASSIFIER_CONFIDENCE_THRESHOLD` | `0.75` | LLM confidence below which email goes to "AI Review" |
| `HEURISTIC_SKIP_THRESHOLD` | `8` | Heuristic score above which email is skipped immediately |
| `SIMILARITY_THRESHOLD` | `0.75` | Cosine similarity for topic linking |
| `RELATED_NOTES_MIN_SHARED_TAGS` | `2` | Minimum shared tags before a note is rendered as "related" |
| `RELATED_NOTES_MAX_RESULTS` | `10` | Hard cap on related notes rendered per note |
| `IMAGE_MIN_DIMENSION` | `100px` | Minimum image dimension (rejects spacers and trackers) |
| `IMAGE_DEDUP_ENABLED` | `True` | Content-hash deduplication for repeated newsletter images |
| `MAX_RESEARCH_TOPICS_PER_RUN` | `0` (disabled) | Cap on auto-research topics per run |

---

## Known limitations & maintenance

**Invalid Obsidian tags (spaces in tag names).** Obsidian tags cannot contain spaces — a tag like `rag pipelines` registers as invalid and breaks tag-based search, Graph View filtering, and Dataview queries. The summarisation prompt now explicitly instructs the LLM to use hyphens (`rag-pipelines`), and a `_sanitise_tag()` safety net runs on every tag regardless. To fix already-written notes:

```bash
cd pipeline
python fixes/diagnose_topic_index.py             # check for corrupted embeddings first
python fixes/diagnose_topic_index.py --apply-reembed  # fix any corrupted rows
python fixes/retag_existing_notes.py --dry-run   # preview tag renames
python fixes/retag_existing_notes.py --apply     # rewrite tags in all notes + rebuild topic_index
python fixes/retrofit_related_notes.py --apply   # re-trim Related Notes under new tag strings
```

**Fragmented inline wikilinks in prose.** Previously the wikilink injection pass ran over the entire assembled note, causing multi-word tags in `## Related Notes` to fragment (e.g. `agent architecture` → `agent [[architecture]]`) and tool names in `## Mentions` to acquire unwanted links. The injection pass is now scoped to `## Summary` and `## Key Takeaways` only. Already-written notes with fragmented links in `## Related Notes` can be fixed by running `fixes/retrofit_related_notes.py --apply`, which regenerates those sections cleanly.

**Duplicate images across notes.** Newsletters commonly reuse the same header logo, footer banner, or sponsor graphic across every issue. `IMAGE_DEDUP_ENABLED` (default on) hashes image content and reuses the first saved copy for any duplicate. To clean up duplicates that accumulated before this was introduced:

```bash
cd pipeline
python fixes/cleanup_duplicate_images.py --dry-run
python fixes/cleanup_duplicate_images.py --apply --rewrite-links
```

**Related Notes section growing unbounded.** `RELATED_NOTES_MIN_SHARED_TAGS` (default 2) and `RELATED_NOTES_MAX_RESULTS` (default 10) cap the Related Notes section going forward. To retroactively re-trim already-written notes:

```bash
cd pipeline
python fixes/retrofit_related_notes.py --dry-run
python fixes/retrofit_related_notes.py --apply
```

---

## Cost profile

- ~80% of emails are resolved at Stage 0 or 1 (no API cost)
- Summarisation: Claude Haiku 4.5 — ~$0.001–0.003 per newsletter
- Classification (ambiguous emails only): Claude Haiku 4.5 — ~$0.0002 per call
- Research: Claude Sonnet 4.6 — only for new topics, on-demand
- Topic embedding: `all-MiniLM-L6-v2` — fully local, zero API cost

---

## Security notes

- `.env`, `secrets/`, and `registry.db` are gitignored — never committed
- OAuth2 tokens are stored locally in `secrets/` and synced via OneDrive only
- The dashboard server binds to `127.0.0.1` only — not accessible externally
- Manual ingest filenames are timestamp-derived — no client input is used in file paths

---

## Technology stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| AI / LLM | Anthropic Claude API (Haiku 4.5, Sonnet 4.6) |
| Embeddings | `sentence-transformers` — `all-MiniLM-L6-v2` (local) |
| Gmail | Google Gmail API v1 (OAuth2) |
| Web fetch | `requests` + `BeautifulSoup4` |
| JS rendering | `playwright` (Chromium headless) |
| Database | SQLite (`registry.db`) |
| Storage | Windows OneDrive (primary) + GitHub (code/index backup) |
| Dashboard | Vanilla HTML/JS + Chart.js (self-contained, no build step) |
| Note format | Obsidian-compatible Markdown with YAML frontmatter |

---

## Obsidian setup

Point your Obsidian vault at the `notes/` directory (or the project root). All generated notes include:
- YAML frontmatter (`source_account`, `sender`, `received`, `tags`)
- `[[wikilinks]]` for every tag in a dedicated Tags section — e.g. `[[rag-pipelines]] [[agent-architecture]]`
- Inline wikilinks injected into Summary and Key Takeaways prose where exact tag matches occur
- Related notes linked by semantic similarity (capped and threshold-filtered — see Configuration highlights)
- Embedded images from `notes/assets/`

All tags use hyphenated format (`rag-pipelines`, not `rag pipelines`) for full Obsidian compatibility — valid in tag search, Graph View, and Dataview queries.

See the design document for detailed Obsidian setup instructions (Windows and Linux/WSL).

---

## Using Obsidian to explore and extract knowledge

The pipeline generates the raw material — Markdown notes, tags, wikilinks. Obsidian is where that material becomes useful. A few ways to actually work with the accumulated knowledge base, in increasing order of power:

**Tag search and Graph View (built-in, no setup).** Search `tag:#rag-pipelines` in the search pane to see every note touching a topic. Open Graph View and filter with the same `tag:` syntax to see how that topic's notes interconnect — useful for spotting clusters you've unconsciously been reading about for months. Color groups (Graph View settings → New group) let you assign a colour per major topic area so the cluster shapes become visually obvious at a glance.

**Dataview plugin (recommended — install via Community Plugins).** Since every note has structured YAML frontmatter (`tags`, `source_account`, `received`), Dataview can turn the vault into a live, queryable table without you touching any code. Example: a single query block dropped into any note can list every note tagged with a given topic, sorted by date, pulling straight from frontmatter:

```dataview
TABLE source_account AS "Account", received AS "Received"
FROM #rag-pipelines
SORT received DESC
```

This is the fastest way to answer "what have I read about X, and when" without manually scrolling `INDEX.md`.

**Obsidian Bases (built-in since v1.9.10, no plugin required).** If you'd rather not learn Dataview's query syntax, Bases gives a similar spreadsheet-style filtered table through a GUI, built directly on your notes' YAML frontmatter. Good for quick browsing; Dataview remains more powerful for complex filtering or aggregation.

**Related Notes + wikilinks for serendipitous discovery.** Each note's `## Related Notes` section (capped and quality-filtered — see `RELATED_NOTES_MIN_SHARED_TAGS` / `RELATED_NOTES_MAX_RESULTS`) and inline `[[wikilinks]]` are meant for in-the-moment exploration: while reading one note, follow a link to a related one rather than searching. This is the cheapest way to surface connections you wouldn't have thought to search for.

For a more detailed walkthrough — including suggested dashboard queries, a recommended starter Dataview setup, and a worked example — see **Appendix M** in the design document.

---

## License

MIT