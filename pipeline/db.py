# =============================================================================
# db.py — Newsletter AI Pipeline v4.0
# SQLite database layer: schema initialisation, connection management,
# and shared query helpers used across all agents.
#
# Database file: {PROJECT_ROOT}/registry.db
# The DB is gitignored — it lives only in the local OneDrive folder.
# OneDrive syncs it to cloud as part of normal Documents backup.
#
# Usage:
#   from db import initialise_db, get_connection, get_processed_ids
#
#   initialise_db()           # call once at pipeline startup
#   conn = get_connection()   # get a connection for direct queries
#   ids  = get_processed_ids()  # set of already-processed Message-IDs
# =============================================================================

import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
# db.py may be imported from orchestrator.py (in pipeline/) or from agents/
# (one level deeper). Resolve config from the pipeline root regardless.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from config import DB_PATH


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    """
    Open and return a SQLite connection to registry.db.

    - row_factory is set to sqlite3.Row so columns are accessible by name
      (e.g. row["message_id"]) as well as by index.
    - WAL journal mode is enabled for better concurrent read performance
      (relevant if the dashboard reads while the pipeline writes).
    - Foreign key enforcement is ON — the image_log table references
      processing_log and this ensures referential integrity is checked.

    Callers are responsible for closing the connection.
    Example:
        conn = get_connection()
        try:
            rows = conn.execute("SELECT * FROM processing_log").fetchall()
        finally:
            conn.close()
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_CREATE_PROCESSING_LOG = """
CREATE TABLE IF NOT EXISTS processing_log (
    -- Identity
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id            TEXT UNIQUE NOT NULL,   -- RFC 2822 Message-ID header
    account_alias         TEXT NOT NULL,           -- e.g. "personal", "work"

    -- Email metadata
    sender                TEXT,
    subject               TEXT,
    received_date         TEXT,                    -- raw Date header value
    processed_at          TEXT,                    -- ISO 8601 UTC timestamp

    -- Performance
    duration_seconds      REAL,

    -- API usage
    model_used            TEXT,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,       -- tokens written to cache
    cache_read_tokens     INTEGER DEFAULT 0,       -- tokens served from cache
    cost_usd              REAL    DEFAULT 0.0,

    -- Agent 1.5: classification
    classification        TEXT,                    -- "editorial"|"marketing"|"mixed"|"blocked"
    classification_stage  TEXT,                    -- "allowlist"|"blocklist"|"heuristic"|"llm"|"llm_extracted"
    confidence_score      REAL,                    -- 0.0–1.0 from LLM classifier
    heuristic_score       INTEGER,                 -- raw heuristic score (Stage 1)
    heuristic_signals     TEXT,                    -- JSON list of triggered signal names
    marketing_sections    TEXT,                    -- "extracted" if Stage 3 ran, else NULL

    -- Agent 4: images
    images_found          INTEGER DEFAULT 0,       -- total <img> tags parsed
    images_saved          INTEGER DEFAULT 0,       -- passed filter and written to disk
    images_filtered       INTEGER DEFAULT 0,       -- rejected by filter rules
    images_disabled       INTEGER DEFAULT 0,       -- 1 if --no-images flag was set

    -- Agent 7: output
    note_path             TEXT,                    -- full Windows path of written .md file

    -- Final status
    status                TEXT                     -- "success"|"failed"|
                                                   -- "skipped_marketing"|
                                                   -- "skipped_duplicate"|
                                                   -- "skipped_blocklist"
);
"""

_CREATE_RUN_SUMMARY = """
CREATE TABLE IF NOT EXISTS run_summary (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started_at           TEXT,
    run_completed_at         TEXT,
    accounts_processed       TEXT,                 -- JSON list of account aliases
    emails_processed         INTEGER DEFAULT 0,
    emails_failed            INTEGER DEFAULT 0,
    emails_skipped_marketing INTEGER DEFAULT 0,
    emails_skipped_duplicate INTEGER DEFAULT 0,
    total_input_tokens       INTEGER DEFAULT 0,
    total_output_tokens      INTEGER DEFAULT 0,
    total_cache_reads        INTEGER DEFAULT 0,
    total_cost_usd           REAL    DEFAULT 0.0,
    cache_hit_rate           REAL    DEFAULT 0.0,  -- cache_reads / total_input
    total_images_saved       INTEGER DEFAULT 0,
    total_images_filtered    INTEGER DEFAULT 0,
    images_enabled           INTEGER DEFAULT 1,    -- 0 if --no-images
    git_backup_status        TEXT                  -- "success"|"failed"|"pending"|"skipped"
);
"""

_CREATE_TOPIC_INDEX = """
CREATE TABLE IF NOT EXISTS topic_index (
    tag                TEXT PRIMARY KEY,           -- normalised lowercase tag
    first_seen         TEXT,                       -- ISO 8601 UTC
    first_seen_account TEXT,                       -- account alias
    note_files         TEXT,                       -- JSON: [{"file": "...", "account_alias": "..."}]
    embedding_vector   BLOB                        -- pickled numpy float32 array (384-dim)
);
"""

_CREATE_IMAGE_LOG = """
CREATE TABLE IF NOT EXISTS image_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       TEXT NOT NULL,
    account_alias    TEXT NOT NULL,
    filename         TEXT,                         -- saved filename (MD5-based)
    source_url       TEXT,
    source_type      TEXT,                         -- "inline"|"cid"|"external"
    original_format  TEXT,                         -- "PNG"|"JPEG"|"GIF"|"WEBP"
    saved_format     TEXT,                         -- may differ if WEBP→GIF conversion ran
    size_bytes       INTEGER,
    width_px         INTEGER,
    height_px        INTEGER,
    alt_text         TEXT,
    filter_result    TEXT,                         -- "accepted"|reason for rejection
    local_path       TEXT,                         -- full Windows path of saved image
    processed_at     TEXT,
    FOREIGN KEY (message_id) REFERENCES processing_log(message_id)
);
"""

# Maps a content hash (SHA-256 of the downloaded image bytes, AFTER any
# WEBP→GIF conversion) to the first saved copy of that image.
#
# This is content-based, not URL-based, deliberately: many newsletter
# platforms serve the identical logo/banner from a per-send tracking URL
# (different query string per email), so URL-based dedup alone would miss
# most real duplicates. Hashing the downloaded bytes catches these.
# ---------------------------------------------------------------------------
 
_CREATE_IMAGE_DEDUP_INDEX = """
CREATE TABLE IF NOT EXISTS image_dedup_index (
    content_hash    TEXT PRIMARY KEY,   -- SHA-256 of saved image bytes
    canonical_path  TEXT NOT NULL,      -- relative path to the first saved copy
    first_seen_at   TEXT NOT NULL,      -- ISO 8601 UTC timestamp
    first_message_id TEXT,              -- message_id that first produced this image
    reuse_count     INTEGER DEFAULT 0   -- number of times this hash was matched again
);
"""


_CREATE_GIT_BACKUP_LOG = """
CREATE TABLE IF NOT EXISTS git_backup_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at         TEXT,
    files_staged   INTEGER DEFAULT 0,
    commit_hash    TEXT,
    push_status    TEXT,                           -- "success"|"failed"|"nothing_to_commit"
    error_message  TEXT
);
"""

# Added in v5.0 — tracks every URL fetch attempt from the link pipeline.
# Separate from processing_log so fetch failures (blocked, paywalled) are
# visible independently of the main email processing audit trail.
_CREATE_LINK_LOG = """
CREATE TABLE IF NOT EXISTS link_log (
    url                       TEXT PRIMARY KEY,      -- original URL from links.csv
    link_id                   TEXT UNIQUE NOT NULL,  -- "link:{md5[:16]}" — FK to processing_log
    label                     TEXT,                  -- seed tag from CSV
    added_date                TEXT,                  -- date added to links.csv
    fetch_status              TEXT NOT NULL,         -- "fetched"|"partial"|"blocked"|"paywalled"|"js_required"|"failed"|"manual"
    http_status_code          INTEGER,                -- raw HTTP response code
    page_title                TEXT,                  -- extracted <title> or og:title
    word_count                INTEGER DEFAULT 0,     -- word count after content extraction
    fetch_attempted_at        TEXT,                  -- ISO 8601 UTC timestamp
    error_message             TEXT,                  -- details if fetch_status != "fetched"
    via_rss_fallback          INTEGER DEFAULT 0,     -- 1 if content was recovered via the Substack RSS fallback, else 0
    via_playwright_fallback   INTEGER DEFAULT 0,     -- 1 if content was recovered via the Playwright headless-browser fallback, else 0
    via_manual_paste          INTEGER DEFAULT 0       -- 1 if content was pasted manually via /api/ingest (bypasses fetch entirely)
);
"""

# Columns added to link_log after its original release. Each entry is
# (column_name, column_ddl_fragment) and is applied via ALTER TABLE ADD
# COLUMN for any existing registry.db that predates the column — see
# _migrate_link_log_columns() below. CREATE TABLE IF NOT EXISTS above
# already covers brand-new databases; this list only matters for upgrades.
_LINK_LOG_COLUMN_MIGRATIONS = [
    ("via_rss_fallback",        "INTEGER DEFAULT 0"),
    ("via_playwright_fallback", "INTEGER DEFAULT 0"),
    ("via_manual_paste",        "INTEGER DEFAULT 0"),  # added with /api/ingest endpoint
]

# ---------------------------------------------------------------------------
# research_queue — manual research queue
# ---------------------------------------------------------------------------
# Populated when --no-research is passed OR MAX_RESEARCH_TOPICS_PER_RUN == 0.
# Instead of silently discarding new topics that would have triggered Agent 5,
# they are written here so they can be reviewed and researched selectively via
# the research queue browser UI (dashboard/research_queue.html) and
# pipeline/serve_dashboard.py's POST /api/research endpoint.
#
# status values:
#   'pending'   — identified, not yet researched
#   'done'      — researched; Context section patched into note_path
#   'skipped'   — explicitly dismissed via the UI without researching
_CREATE_RESEARCH_QUEUE = """
CREATE TABLE IF NOT EXISTS research_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT NOT NULL,
    note_path       TEXT,           -- absolute path to .md note where Context section will be patched
    source_account  TEXT,           -- 'personal' / 'work' / 'links' — for display context
    source_subject  TEXT,           -- article/email title that surfaced this topic
    queued_at       TEXT NOT NULL,  -- ISO 8601 UTC
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'skipped'
    researched_at   TEXT,           -- ISO 8601 UTC, set when status → 'done'
    cost_usd        REAL            -- Sonnet cost for the research call, set when done
);
"""

_ALL_TABLES = [
    _CREATE_PROCESSING_LOG,
    _CREATE_RUN_SUMMARY,
    _CREATE_TOPIC_INDEX,
    _CREATE_IMAGE_LOG,
    _CREATE_GIT_BACKUP_LOG,
    _CREATE_LINK_LOG,              # link pipeline fetch log
    _CREATE_RESEARCH_QUEUE,        # manual research queue
    _CREATE_IMAGE_DEDUP_INDEX,     # image deduplication index
]


def _migrate_link_log_columns(conn: sqlite3.Connection) -> None:
    """
    Add any link_log columns that don't yet exist on this database file.

    CREATE TABLE IF NOT EXISTS only applies the full schema to a brand-new
    table — it does nothing for a table that already exists with an older
    column set. Anyone who set up registry.db before via_rss_fallback /
    via_playwright_fallback existed would otherwise hit "no such column"
    errors the first time log_link_fetch() tries to write them.

    Existing rows backfill to the column default (0 / "no fallback used")
    rather than NULL or guessed values, since the original fetch attempts
    that produced those rows genuinely predate fallback tracking — there's
    no way to know in hindsight whether a fallback was used, and treating
    them as "not via a fallback" is the conservative, honest default.
    """
    existing_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(link_log)").fetchall()
    }
    for column_name, ddl_fragment in _LINK_LOG_COLUMN_MIGRATIONS:
        if column_name not in existing_cols:
            conn.execute(f"ALTER TABLE link_log ADD COLUMN {column_name} {ddl_fragment}")
            print(f"[db] Migrated link_log: added column '{column_name}' "
                  f"(existing rows backfilled to default 0)")


def initialise_db():
    """
    Create all tables if they don't exist. Safe to call on every pipeline run —
    uses CREATE TABLE IF NOT EXISTS so existing data is never touched.

    Also runs lightweight column migrations for tables that existed before
    certain columns were added (see _migrate_link_log_columns), and ensures
    the DB file's parent directory exists (relevant on first run before the
    project folder has been created by the setup script).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        for ddl in _ALL_TABLES:
            conn.execute(ddl)
        _migrate_link_log_columns(conn)
        conn.commit()
        print(f"[db] registry.db initialised at {DB_PATH}")
    finally:
        conn.close()




# ---------------------------------------------------------------------------
# Shared query helpers
# ---------------------------------------------------------------------------

def get_processed_ids() -> set[str]:
    """
    Return the set of all Message-IDs that have been successfully processed
    or deliberately skipped. Used by Agent 1 to skip already-seen emails.

    Includes statuses: success, skipped_marketing, skipped_blocklist.
    Does NOT include "failed" — failed emails are retried on the next run.
    Does NOT include "skipped_duplicate" — deduplication is handled in-memory
    per run, not via this set.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT message_id FROM processing_log
               WHERE status IN ('success', 'skipped_marketing', 'skipped_blocklist')"""
        ).fetchall()
        return {row["message_id"] for row in rows}
    finally:
        conn.close()


def get_latest_processed_date(account_alias: str | None = None) -> str | None:
    """
    Return the most recent processed_at date (YYYY-MM-DD) among successfully
    processed or skipped emails, optionally scoped to one account.

    Used by Agent 1 to bound the Gmail search query with `after:` so the
    API doesn't re-return the entire historical backlog on every run —
    only messages received on or after roughly the last successful run.

    NOTE: this deliberately uses processed_at (an ISO 8601 string written
    by the pipeline itself, e.g. "2026-06-07T14:32:00Z") rather than
    received_date. received_date is stored as the raw RFC 2822 Gmail
    header string (e.g. "Sat, 07 Jun 2026 14:32:00 +0800") and is not
    safely sliceable into YYYY-MM-DD with a plain substring — its day/month
    order and presence of a leading weekday name vary. processed_at is
    always ISO 8601 since the pipeline writes it itself, so SUBSTR(...,1,10)
    is reliable there. The two dates differ by at most a few days (time
    between an email arriving and the pipeline next running), which the
    caller's safety buffer already accounts for.

    Args:
        account_alias: If provided, only consider rows for this account.
                       If None, consider all accounts (global most-recent date).

    Returns:
        "YYYY-MM-DD" string, or None if no processed emails exist yet
        (first-ever run — caller should fall back to no date bound / bootstrap).
    """
    conn = get_connection()
    try:
        if account_alias:
            row = conn.execute(
                """SELECT MAX(SUBSTR(processed_at, 1, 10)) AS latest
                   FROM processing_log
                   WHERE account_alias = ?
                     AND status IN ('success', 'skipped_marketing', 'skipped_blocklist')
                     AND processed_at IS NOT NULL""",
                (account_alias,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT MAX(SUBSTR(processed_at, 1, 10)) AS latest
                   FROM processing_log
                   WHERE status IN ('success', 'skipped_marketing', 'skipped_blocklist')
                     AND processed_at IS NOT NULL"""
            ).fetchone()
        return row["latest"] if row and row["latest"] else None
    finally:
        conn.close()


def get_all_message_ids() -> set[str]:
    """
    Return ALL Message-IDs ever recorded, regardless of status.
    Used for strict deduplication where even failed attempts should not be retried
    (e.g. if you want to quarantine consistently-failing emails).
    """
    conn = get_connection()
    try:
        rows = conn.execute("SELECT message_id FROM processing_log").fetchall()
        return {row["message_id"] for row in rows}
    finally:
        conn.close()


def get_run_stats() -> dict:
    """
    Return aggregate statistics across all completed runs.
    Useful for the dashboard and for quick health checks.

    Returns a dict with keys:
        total_processed, total_skipped_marketing, total_failed,
        total_cost_usd, total_images_saved, latest_run_at
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                COUNT(CASE WHEN status = 'success'           THEN 1 END) AS total_processed,
                COUNT(CASE WHEN status = 'skipped_marketing' THEN 1 END) AS total_skipped_marketing,
                COUNT(CASE WHEN status = 'failed'            THEN 1 END) AS total_failed,
                COALESCE(SUM(cost_usd), 0)                               AS total_cost_usd,
                COALESCE(SUM(images_saved), 0)                           AS total_images_saved,
                MAX(processed_at)                                        AS latest_run_at
            FROM processing_log
        """).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_recent_runs(n: int = 10) -> list[dict]:
    """
    Return the most recent n run summary rows, newest first.
    Used by the dashboard to display a run history table.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM run_summary
               ORDER BY run_started_at DESC
               LIMIT ?""",
            (n,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_topic_count() -> int:
    """Return the total number of unique tags in the topic index."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM topic_index").fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def upsert_processing_record(record: dict):
    """
    Insert or replace a processing_log row.
    Accepts a dict whose keys map to processing_log columns.
    Missing keys default to NULL/0 — no KeyError is raised for optional fields.

    Typically called by Agent 6 (Logging Agent) after each email completes.
    """
    fields = [
        "message_id", "account_alias", "sender", "subject",
        "received_date", "processed_at", "duration_seconds", "model_used",
        "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "cost_usd",
        "classification", "classification_stage", "confidence_score",
        "heuristic_score", "heuristic_signals", "marketing_sections",
        "images_found", "images_saved", "images_filtered", "images_disabled",
        "note_path", "status",
    ]
    placeholders = ", ".join(f":{f}" for f in fields)
    columns      = ", ".join(fields)

    # Build a normalised record with None defaults for missing keys
    normalised = {f: record.get(f) for f in fields}

    conn = get_connection()
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO processing_log ({columns}) VALUES ({placeholders})",
            normalised
        )
        conn.commit()
    finally:
        conn.close()


def insert_run_summary(summary: dict):
    """
    Insert a run_summary row. Called by the orchestrator at the end of each run.
    """
    fields = [
        "run_started_at", "run_completed_at", "accounts_processed",
        "emails_processed", "emails_failed", "emails_skipped_marketing",
        "emails_skipped_duplicate", "total_input_tokens", "total_output_tokens",
        "total_cache_reads", "total_cost_usd", "cache_hit_rate",
        "total_images_saved", "total_images_filtered",
        "images_enabled", "git_backup_status",
    ]
    placeholders = ", ".join(f":{f}" for f in fields)
    columns      = ", ".join(fields)
    normalised   = {f: summary.get(f) for f in fields}

    conn = get_connection()
    try:
        conn.execute(
            f"INSERT INTO run_summary ({columns}) VALUES ({placeholders})",
            normalised
        )
        conn.commit()
    finally:
        conn.close()


def insert_image_log_entries(entries: list[dict]):
    """
    Bulk-insert image_log rows for a single email's assets.
    Each entry should contain keys matching image_log columns.
    Silently skips entries missing message_id (FK would fail).
    """
    if not entries:
        return

    fields = [
        "message_id", "account_alias", "filename", "source_url",
        "source_type", "original_format", "saved_format",
        "size_bytes", "width_px", "height_px", "alt_text",
        "filter_result", "local_path", "processed_at",
    ]
    placeholders = ", ".join(f":{f}" for f in fields)
    columns      = ", ".join(fields)

    conn = get_connection()
    try:
        for entry in entries:
            if not entry.get("message_id"):
                continue
            normalised = {f: entry.get(f) for f in fields}
            conn.execute(
                f"INSERT INTO image_log ({columns}) VALUES ({placeholders})",
                normalised
            )
        conn.commit()
    finally:
        conn.close()

def get_image_by_hash(content_hash: str) -> dict | None:
    """
    Look up whether an image with this content hash has already been saved.
 
    Args:
        content_hash: SHA-256 hex digest of the downloaded (post-conversion)
                       image bytes.
 
    Returns:
        dict with keys {canonical_path, first_seen_at, first_message_id,
        reuse_count} if a match exists, else None.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT canonical_path, first_seen_at, first_message_id, reuse_count
               FROM image_dedup_index WHERE content_hash = ?""",
            (content_hash,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
 
 
def register_image_hash(
    content_hash:  str,
    canonical_path: str,
    message_id:    str,
) -> None:
    """
    Record a newly-saved image's content hash so future duplicates can be
    detected. Call this once, the first time an image is actually written
    to disk (not on cache hits — see bump_image_reuse for that case).
 
    Uses INSERT OR IGNORE so this is safe to call even if a race produces
    a duplicate insert attempt — the first writer wins.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO image_dedup_index
               (content_hash, canonical_path, first_seen_at, first_message_id, reuse_count)
               VALUES (?, ?, ?, ?, 0)""",
            (content_hash, canonical_path, now, message_id)
        )
        conn.commit()
    finally:
        conn.close()
 
 
def bump_image_reuse(content_hash: str) -> None:
    """
    Increment the reuse_count for a content hash that was matched again
    (i.e. a duplicate was detected and skipped rather than re-saved).
    Used for dashboard reporting on dedup effectiveness.
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE image_dedup_index SET reuse_count = reuse_count + 1 WHERE content_hash = ?",
            (content_hash,)
        )
        conn.commit()
    finally:
        conn.close()
 
 
def get_dedup_stats() -> dict:
    """
    Summary stats for the dashboard: how many unique images are tracked,
    and how many total downloads were avoided via dedup.
 
    Returns:
        dict with keys: unique_images, total_reuses, bytes_saved_estimate
        (bytes_saved_estimate is None — actual size isn't tracked per hash
        to keep the index lightweight; join against image_log if needed).
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*)                    AS unique_images,
                COALESCE(SUM(reuse_count), 0) AS total_reuses
            FROM image_dedup_index
        """).fetchone()
        return dict(row) if row else {"unique_images": 0, "total_reuses": 0}
    finally:
        conn.close()

def insert_git_backup_entry(entry: dict):
    """Record the outcome of a git backup run in git_backup_log."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO git_backup_log
               (run_at, files_staged, commit_hash, push_status, error_message)
               VALUES (:run_at, :files_staged, :commit_hash, :push_status, :error_message)""",
            {
                "run_at":        entry.get("run_at"),
                "files_staged":  entry.get("files_staged", 0),
                "commit_hash":   entry.get("commit_hash"),
                "push_status":   entry.get("push_status"),
                "error_message": entry.get("error_message"),
            }
        )
        conn.commit()
    finally:
        conn.close()


def get_link_stats() -> dict:
    """
    Return aggregate fetch statistics from link_log.
    Used by the dashboard and CLI health check.

    Returns a dict with keys:
        total_fetched, total_partial, total_blocked, total_paywalled,
        total_js_required, total_failed, total_links, latest_fetch_at,
        total_via_rss_fallback, total_via_playwright_fallback
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                COUNT(CASE WHEN fetch_status = 'fetched'      THEN 1 END) AS total_fetched,
                COUNT(CASE WHEN fetch_status = 'partial'      THEN 1 END) AS total_partial,
                COUNT(CASE WHEN fetch_status = 'blocked'      THEN 1 END) AS total_blocked,
                COUNT(CASE WHEN fetch_status = 'paywalled'    THEN 1 END) AS total_paywalled,
                COUNT(CASE WHEN fetch_status = 'js_required'  THEN 1 END) AS total_js_required,
                COUNT(CASE WHEN fetch_status = 'failed'       THEN 1 END) AS total_failed,
                COUNT(*)                                                  AS total_links,
                MAX(fetch_attempted_at)                                   AS latest_fetch_at,
                COUNT(CASE WHEN via_rss_fallback = 1        THEN 1 END)   AS total_via_rss_fallback,
                COUNT(CASE WHEN via_playwright_fallback = 1 THEN 1 END)   AS total_via_playwright_fallback,
                COUNT(CASE WHEN via_manual_paste = 1         THEN 1 END)   AS total_via_manual_paste
            FROM link_log
        """).fetchone()
        return dict(row) if row else {}
    except Exception:
        # link_log may not exist if db was initialised before the link pipeline existed
        return {}
    finally:
        conn.close()


def get_fallback_stats_30d() -> dict:
    """
    Return fallback-usage statistics scoped to the last 30 days, for the
    dashboard's "X% of links needed Playwright" tile.

    Deliberately a separate function from get_link_stats() (which is
    all-time) rather than a parameter on it — the dashboard wants a rolling
    recent-activity figure specifically, not a configurable window, and
    keeping it separate makes the SQL easier to read and reuse elsewhere
    (e.g. progress.html could call the same underlying figure).

    Window is anchored to "now" at query time using SQLite's datetime()
    function directly in SQL (not computed in Python) so the figure is
    always correct relative to when the dashboard is actually generated,
    not when the calling code happened to import datetime.

    Returns a dict with keys:
        total_links_30d, via_rss_fallback_count_30d,
        via_playwright_fallback_count_30d, via_rss_fallback_pct_30d,
        via_playwright_fallback_pct_30d, any_fallback_pct_30d
    All percentage keys are 0.0 (not None) when total_links_30d is 0, so
    callers can render them directly without a None-check.
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*)                                                  AS total_links_30d,
                COUNT(CASE WHEN via_rss_fallback = 1        THEN 1 END)   AS via_rss_fallback_count_30d,
                COUNT(CASE WHEN via_playwright_fallback = 1 THEN 1 END)   AS via_playwright_fallback_count_30d,
                COUNT(CASE WHEN via_rss_fallback = 1
                            OR via_playwright_fallback = 1   THEN 1 END)  AS any_fallback_count_30d
            FROM link_log
            WHERE fetch_attempted_at >= datetime('now', '-30 days')
        """).fetchone()

        result = dict(row) if row else {
            "total_links_30d": 0, "via_rss_fallback_count_30d": 0,
            "via_playwright_fallback_count_30d": 0, "any_fallback_count_30d": 0,
        }

        total = result.get("total_links_30d", 0) or 0
        if total > 0:
            result["via_rss_fallback_pct_30d"]        = round(100 * result["via_rss_fallback_count_30d"] / total, 1)
            result["via_playwright_fallback_pct_30d"] = round(100 * result["via_playwright_fallback_count_30d"] / total, 1)
            result["any_fallback_pct_30d"]             = round(100 * result["any_fallback_count_30d"] / total, 1)
        else:
            result["via_rss_fallback_pct_30d"]        = 0.0
            result["via_playwright_fallback_pct_30d"] = 0.0
            result["any_fallback_pct_30d"]             = 0.0

        return result
    except Exception:
        # link_log may not exist, or may predate via_rss_fallback /
        # via_playwright_fallback on an unmigrated database (shouldn't
        # happen since initialise_db() migrates on every startup, but
        # fail safe rather than crash dashboard generation either way).
        return {
            "total_links_30d": 0, "via_rss_fallback_count_30d": 0,
            "via_playwright_fallback_count_30d": 0, "any_fallback_count_30d": 0,
            "via_rss_fallback_pct_30d": 0.0, "via_playwright_fallback_pct_30d": 0.0,
            "any_fallback_pct_30d": 0.0,
        }
    finally:
        conn.close()


def add_to_research_queue(
    topic:          str,
    note_path:      str | None,
    source_account: str | None,
    source_subject: str | None,
) -> None:
    """
    Add a single topic to research_queue with status='pending'.

    Called by orchestrators when --no-research is passed or
    MAX_RESEARCH_TOPICS_PER_RUN == 0, instead of discarding new topics.

    Silently skips if the topic is already pending or done in the queue —
    no duplicate rows for the same topic string (case-insensitive), since
    the same tag can surface across multiple articles/emails and should only
    ever appear once in the pending list. If it's 'skipped', re-queues it
    (user dismissed it before; the new occurrence is worth reconsidering).
    """
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT status FROM research_queue WHERE LOWER(topic) = LOWER(?)",
            (topic,),
        ).fetchone()

        if existing and existing["status"] in ("pending", "done"):
            return   # already queued or already researched — nothing to do

        now = datetime.now(timezone.utc).isoformat()

        if existing and existing["status"] == "skipped":
            # Re-queue a previously skipped topic
            conn.execute(
                """UPDATE research_queue
                      SET status = 'pending', queued_at = ?,
                          note_path = ?, source_account = ?, source_subject = ?,
                          researched_at = NULL, cost_usd = NULL
                    WHERE LOWER(topic) = LOWER(?)""",
                (now, note_path, source_account, source_subject, topic),
            )
        else:
            conn.execute(
                """INSERT INTO research_queue
                       (topic, note_path, source_account, source_subject, queued_at, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (topic, note_path, source_account, source_subject, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_research_queue(status: str | None = None) -> list[dict]:
    """
    Return research queue rows, newest-first.

    Args:
        status: Filter by status ('pending', 'done', 'skipped').
                If None, return all rows.
    """
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                """SELECT id, topic, note_path, source_account, source_subject,
                          queued_at, status, researched_at, cost_usd
                     FROM research_queue
                    WHERE status = ?
                    ORDER BY queued_at DESC""",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, topic, note_path, source_account, source_subject,
                          queued_at, status, researched_at, cost_usd
                     FROM research_queue
                    ORDER BY queued_at DESC""",
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def mark_researched(topic: str, cost_usd: float) -> None:
    """Mark a queue row as done after a successful research call."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE research_queue
                  SET status = 'done', researched_at = ?, cost_usd = ?
                WHERE LOWER(topic) = LOWER(?)""",
            (now, cost_usd, topic),
        )
        conn.commit()
    finally:
        conn.close()


def mark_skipped(topic: str) -> None:
    """Mark a queue row as skipped (user dismissed it via the UI)."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE research_queue SET status = 'skipped' WHERE LOWER(topic) = LOWER(?)",
            (topic,),
        )
        conn.commit()
    finally:
        conn.close()


def get_queue_stats() -> dict:
    """
    Return aggregate counts for the research queue.
    Used by generate_research_queue.py and the index.html stat card.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT
                   COUNT(CASE WHEN status = 'pending'  THEN 1 END) AS pending,
                   COUNT(CASE WHEN status = 'done'     THEN 1 END) AS done,
                   COUNT(CASE WHEN status = 'skipped'  THEN 1 END) AS skipped,
                   COUNT(*)                                         AS total,
                   COALESCE(SUM(CASE WHEN status = 'done' THEN cost_usd END), 0.0) AS total_cost_usd
               FROM research_queue"""
        ).fetchone()
        return dict(row) if row else {
            "pending": 0, "done": 0, "skipped": 0,
            "total": 0, "total_cost_usd": 0.0,
        }
    except Exception:
        return {"pending": 0, "done": 0, "skipped": 0,
                "total": 0, "total_cost_usd": 0.0}
    finally:
        conn.close()


def get_links_by_status(
    statuses: list[str] | None = None,
) -> list[dict]:
    """
    Return link_log rows filtered by fetch_status, for the link review page.

    Args:
        statuses: List of fetch_status values to include. If None or empty,
                  return all rows. Valid values: 'partial', 'blocked',
                  'paywalled', 'js_required', 'failed'.
                  'skipped_marketing' rows come from processing_log, not
                  link_log — handled separately in generate_link_review.py.
    """
    conn = get_connection()
    try:
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            rows = conn.execute(
                f"""SELECT url, label, page_title, fetch_status,
                           http_status_code, word_count,
                           fetch_attempted_at, error_message,
                           via_rss_fallback, via_playwright_fallback, via_manual_paste
                      FROM link_log
                     WHERE fetch_status IN ({placeholders})
                     ORDER BY fetch_attempted_at DESC""",
                statuses,
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT url, label, page_title, fetch_status,
                          http_status_code, word_count,
                          fetch_attempted_at, error_message,
                          via_rss_fallback, via_playwright_fallback, via_manual_paste
                     FROM link_log
                    ORDER BY fetch_attempted_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def get_skipped_marketing_links() -> list[dict]:
    """
    Return link-pipeline articles that were skipped as marketing
    (status = 'skipped_marketing' in processing_log, account_alias = 'links').
    These live in processing_log, not link_log, since classification happens
    after the fetch step.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT message_id, subject, sender, received_date,
                      processed_at, classification, confidence_score,
                      heuristic_score
                 FROM processing_log
                WHERE account_alias = 'links'
                  AND status = 'skipped_marketing'
                ORDER BY processed_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()



def get_manual_ingested_links() -> list[dict]:
    """
    Return link_log rows where fetch_status = 'manual' (content that was
    pasted via the /api/ingest bookmarklet endpoint rather than fetched).
    Used by generate_link_review.py to show an informational audit trail
    of manually-ingested articles alongside the failure categories.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT url, label, page_title, fetch_status,
                      http_status_code, word_count,
                      fetch_attempted_at, error_message,
                      via_rss_fallback, via_playwright_fallback, via_manual_paste
                 FROM link_log
                WHERE fetch_status = 'manual'
                ORDER BY fetch_attempted_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()

def get_recent_links(limit: int = 20) -> list[dict]:
    """
    Return the most recently attempted links, newest first.
    Used by the dashboard link status table.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT url, label, page_title, fetch_status,
                   http_status_code, word_count,
                   fetch_attempted_at, error_message,
                   via_rss_fallback, via_playwright_fallback, via_manual_paste
            FROM link_log
            ORDER BY fetch_attempted_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI: quick health check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"DB path: {DB_PATH}")
    initialise_db()

    stats = get_run_stats()
    if stats:
        print(f"\nAggregate stats:")
        print(f"  Emails processed      : {stats.get('total_processed', 0)}")
        print(f"  Emails skipped (mktg) : {stats.get('total_skipped_marketing', 0)}")
        print(f"  Emails failed         : {stats.get('total_failed', 0)}")
        print(f"  Total cost (USD)      : ${stats.get('total_cost_usd', 0.0):.4f}")
        print(f"  Images saved          : {stats.get('total_images_saved', 0)}")
        print(f"  Latest run            : {stats.get('latest_run_at', 'never')}")
        print(f"  Topics in index       : {get_topic_count()}")
    else:
        print("No processing records yet — database is empty.")

    link_stats = get_link_stats()
    if link_stats.get("total_links", 0) > 0:
        print(f"\nLink pipeline stats:")
        print(f"  Total URLs attempted  : {link_stats.get('total_links', 0)}")
        print(f"  Fetched (full)        : {link_stats.get('total_fetched', 0)}")
        print(f"  Partial (preview only): {link_stats.get('total_partial', 0)}")
        print(f"  Blocked               : {link_stats.get('total_blocked', 0)}")
        print(f"  Paywalled             : {link_stats.get('total_paywalled', 0)}")
        print(f"  JS required (0 words) : {link_stats.get('total_js_required', 0)}")
        print(f"  Failed                : {link_stats.get('total_failed', 0)}")
        print(f"  Via RSS fallback      : {link_stats.get('total_via_rss_fallback', 0)}")
        print(f"  Via Playwright        : {link_stats.get('total_via_playwright_fallback', 0)}")
        print(f"  Latest fetch          : {link_stats.get('latest_fetch_at', 'never')}")
