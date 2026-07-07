# agents/logging_agent.py — Newsletter AI Pipeline v4.0
# Agent 6: Logging & Cost Capture
#
# Responsibilities:
#   - Accept raw token usage data from all upstream agents
#   - Calculate USD cost per email using the pricing table in config.py
#   - Assemble a complete processing_log record and write it to registry.db
#   - Write image_log entries for each asset (accepted and rejected)
#   - Provide a helper to build the run-level summary record
#
# This agent is called by the orchestrator after each email completes
# (success, skip, or failure). It is the single place where cost is
# calculated — no other agent does arithmetic on token counts.
#
# All DB writes delegate to the helper functions in db.py so that
# SQL statements remain centralised and this agent stays focused on
# data assembly and cost logic.
#
# Usage (standalone test):
#   cd pipeline && python agents/logging_agent.py
# =============================================================================

import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config import SUMMARISATION_MODEL, RESEARCH_MODEL, calculate_cost
from db import upsert_processing_record, insert_run_summary, insert_image_log_entries


# ---------------------------------------------------------------------------
# Email record assembly and logging
# ---------------------------------------------------------------------------

def log_email_result(
    email:          dict,
    status:         str,
    duration_secs:  float,
    summarise_usage: dict | None   = None,
    classify_usage:  dict | None   = None,
    research_usage:  dict | None   = None,
    saved_assets:    list[dict]    = None,
    note_path:       Path | None   = None,
) -> float:
    """
    Assemble and persist a processing_log record for one email.

    Aggregates token usage from the three API-consuming agents
    (classification, summarisation, research), calculates total USD cost,
    and writes a single row to processing_log via db.upsert_processing_record.

    Also writes image_log rows for accepted assets if saved_assets is provided.

    Args:
        email:            Email dict from Agent 1/1.5. Must contain:
                          message_id, account_alias, sender, subject,
                          received_date.
                          Classification keys (if Agent 1.5 ran):
                          classification, classification_stage,
                          confidence_score, heuristic_score,
                          heuristic_signals, marketing_sections.
        status:           Final status string:
                          "success" | "failed" | "skipped_marketing" |
                          "skipped_duplicate" | "skipped_blocklist"
        duration_secs:    Wall-clock seconds from email start to finish.
        summarise_usage:  Token usage dict from Agent 2, or None if
                          summarisation was skipped (e.g. marketing email).
                          Keys: input_tokens, output_tokens,
                                cache_creation_tokens, cache_read_tokens
        classify_usage:   Token usage dict from Agent 1.5 LLM calls, or None
                          if classification resolved at heuristic stage.
                          Same keys as summarise_usage.
        research_usage:   Aggregated token usage from Agent 5 across all
                          topics researched for this email, or None.
                          Use research.aggregate_usage() to produce this.
        saved_assets:     List of accepted image asset dicts from Agent 4.
                          Each must contain message_id, account_alias,
                          filename, source_url, etc. Pass [] or None if
                          images were disabled or none were saved.
        note_path:        Path of the written note from Agent 7, or None
                          if the note was not written.

    Returns:
        Total USD cost calculated for this email (float).
        Returned so the orchestrator can accumulate run-level cost totals
        without re-reading the DB.
    """
    now = datetime.now(timezone.utc).isoformat()

    # -----------------------------------------------------------------------
    # Aggregate token usage across all agents for this email
    # -----------------------------------------------------------------------
    total_usage = _aggregate_usage(summarise_usage, classify_usage, research_usage)

    # -----------------------------------------------------------------------
    # Calculate cost
    # -----------------------------------------------------------------------
    # Summarisation cost (Haiku, batch discount applies for nightly runs)
    summ_cost     = calculate_cost(SUMMARISATION_MODEL, summarise_usage or {}, batch=True)

    # Classification cost (Haiku, standard rate — synchronous, not batched)
    classify_cost = calculate_cost(SUMMARISATION_MODEL, classify_usage or {}, batch=False)

    # Research cost (Sonnet, standard rate — web search is always synchronous)
    research_cost = calculate_cost(RESEARCH_MODEL, research_usage or {}, batch=False)

    total_cost = summ_cost + classify_cost + research_cost

    # -----------------------------------------------------------------------
    # Count images
    # -----------------------------------------------------------------------
    assets        = saved_assets or []
    images_saved  = len(assets)
    # images_disabled is 1 if the email dict carries the flag set by orchestrator
    images_disabled = int(email.get("images_disabled", 0))

    # -----------------------------------------------------------------------
    # Assemble the record
    # -----------------------------------------------------------------------
    record = {
        # Identity
        "message_id":            email.get("message_id", ""),
        "account_alias":         email.get("account_alias", ""),
        # Email metadata
        "sender":                email.get("sender", ""),
        "subject":               email.get("subject", ""),
        "received_date":         email.get("received_date", ""),
        "processed_at":          now,
        # Performance
        "duration_seconds":      round(duration_secs, 3),
        # API usage
        "model_used":            SUMMARISATION_MODEL,
        "input_tokens":          total_usage["input_tokens"],
        "output_tokens":         total_usage["output_tokens"],
        "cache_creation_tokens": total_usage["cache_creation_tokens"],
        "cache_read_tokens":     total_usage["cache_read_tokens"],
        "cost_usd":              round(total_cost, 6),
        # Classification (may be None if Agent 1.5 was skipped)
        "classification":        email.get("classification"),
        "classification_stage":  email.get("classification_stage"),
        "confidence_score":      email.get("confidence_score"),
        "heuristic_score":       email.get("heuristic_score"),
        "heuristic_signals":     email.get("heuristic_signals"),
        "marketing_sections":    email.get("marketing_sections"),
        # Images
        "images_found":          email.get("images_found", 0),
        "images_saved":          images_saved,
        "images_filtered":       email.get("images_filtered", 0),
        "images_disabled":       images_disabled,
        # Output
        "note_path":             str(note_path) if note_path else None,
        # Final status
        "status":                status,
    }

    upsert_processing_record(record)

    # -----------------------------------------------------------------------
    # Image log entries (accepted assets only)
    # -----------------------------------------------------------------------
    if assets:
        _write_image_log(assets, now)

    return total_cost


# ---------------------------------------------------------------------------
# Run summary logging
# ---------------------------------------------------------------------------

def log_run_summary(
    run_started_at:           str,
    accounts_processed:       list[str],
    stats:                    dict,
    images_enabled:           bool,
    git_backup_status:        str = "pending",
) -> None:
    """
    Write a run_summary row at the end of a pipeline run.

    Args:
        run_started_at:     ISO 8601 UTC timestamp of run start.
        accounts_processed: List of account alias strings processed in this run.
        stats:              Dict of counters accumulated by the orchestrator:
                              processed        — emails successfully processed
                              failed           — emails that raised exceptions
                              skipped_marketing — emails skipped by Agent 1.5
                              skipped_duplicate — emails deduped in-memory
                              total_input      — total input tokens across run
                              total_output     — total output tokens across run
                              total_cache_reads — total cache_read tokens
                              total_cost       — total USD cost
                              images_saved     — total images saved
                              images_filtered  — total images rejected
        images_enabled:     Whether image extraction was active for this run.
        git_backup_status:  Status of the Git backup agent:
                              "pending" (default — backup runs separately),
                              "success", "failed", "skipped"
    """
    run_completed_at = datetime.now(timezone.utc).isoformat()

    total_input  = stats.get("total_input", 0)
    total_cache  = stats.get("total_cache_reads", 0)
    cache_hit_rate = (
        round(total_cache / total_input, 4) if total_input > 0 else 0.0
    )

    summary = {
        "run_started_at":           run_started_at,
        "run_completed_at":         run_completed_at,
        "accounts_processed":       str(accounts_processed),
        "emails_processed":         stats.get("processed", 0),
        "emails_failed":            stats.get("failed", 0),
        "emails_skipped_marketing": stats.get("skipped_marketing", 0),
        "emails_skipped_duplicate": stats.get("skipped_duplicate", 0),
        "total_input_tokens":       total_input,
        "total_output_tokens":      stats.get("total_output", 0),
        "total_cache_reads":        total_cache,
        "total_cost_usd":           round(stats.get("total_cost", 0.0), 6),
        "cache_hit_rate":           cache_hit_rate,
        "total_images_saved":       stats.get("images_saved", 0),
        "total_images_filtered":    stats.get("images_filtered", 0),
        "images_enabled":           int(images_enabled),
        "git_backup_status":        git_backup_status,
    }

    insert_run_summary(summary)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _aggregate_usage(*usage_dicts) -> dict:
    """
    Sum token counts across multiple usage dicts.
    Accepts any number of dicts or None values — None entries are ignored.

    Returns a single usage dict with totals for all four token types.
    """
    totals = {
        "input_tokens":          0,
        "output_tokens":         0,
        "cache_creation_tokens": 0,
        "cache_read_tokens":     0,
    }
    for usage in usage_dicts:
        if not usage:
            continue
        for key in totals:
            totals[key] += usage.get(key, 0)
    return totals


def _write_image_log(assets: list[dict], processed_at: str) -> None:
    """
    Prepare and write image_log entries for all accepted assets.

    Adds processed_at to each entry (the asset dicts from Agent 4 may
    have their own timestamp, but we normalise to the logging time here
    so all assets from one email share the same timestamp in the log).
    """
    entries = []
    for asset in assets:
        entries.append({
            "message_id":      asset.get("message_id", ""),
            "account_alias":   asset.get("account_alias", ""),
            "filename":        asset.get("filename"),
            "source_url":      asset.get("source_url"),
            "source_type":     asset.get("source_type", "external"),
            "original_format": asset.get("original_format"),
            "saved_format":    asset.get("saved_format"),
            "size_bytes":      asset.get("size_bytes"),
            "width_px":        asset.get("width_px"),
            "height_px":       asset.get("height_px"),
            "alt_text":        asset.get("alt_text", ""),
            "filter_result":   asset.get("filter_result", "accepted"),
            "local_path":      asset.get("local_path"),
            "processed_at":    processed_at,
        })

    insert_image_log_entries(entries)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Test logging with synthetic data — writes one processing_log row and
    one run_summary row, then reads them back to verify.

    Run: python agents/logging_agent.py
    """
    import time
    from db import initialise_db, get_connection

    print("=== Logging Agent — standalone test ===\n")

    initialise_db()

    # Synthetic email with classification metadata
    test_email = {
        "message_id":           "<test-log-001@example.com>",
        "account_alias":        "personal",
        "sender":               "author@newsletter.example.com",
        "subject":              "AI Weekly Test Email",
        "received_date":        "Sat, 07 Jun 2026 14:32:00 +0800",
        "classification":       "editorial",
        "classification_stage": "heuristic",
        "confidence_score":     1.0,
        "heuristic_score":      1,
        "heuristic_signals":    '["unsubscribe_footer"]',
        "marketing_sections":   None,
        "images_found":         3,
        "images_filtered":      2,
        "images_disabled":      0,
    }

    test_summarise_usage = {
        "input_tokens": 850, "output_tokens": 220,
        "cache_creation_tokens": 600, "cache_read_tokens": 0,
    }
    test_classify_usage = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
    }
    test_research_usage = {
        "input_tokens": 420, "output_tokens": 180,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
    }
    test_assets = [
        {
            "message_id":      "<test-log-001@example.com>",
            "account_alias":   "personal",
            "filename":        "a3f9d12e01.png",
            "source_url":      "https://example.com/diagram.png",
            "source_type":     "external",
            "original_format": "PNG",
            "saved_format":    "PNG",
            "size_bytes":      48_000,
            "width_px":        800,
            "height_px":       400,
            "alt_text":        "workflow diagram",
            "filter_result":   "accepted",
            "local_path":      r"C:\Users\test\notes\assets\abc123\a3f9d12e01.png",
        },
    ]

    cost = log_email_result(
        email            = test_email,
        status           = "success",
        duration_secs    = 4.237,
        summarise_usage  = test_summarise_usage,
        classify_usage   = test_classify_usage,
        research_usage   = test_research_usage,
        saved_assets     = test_assets,
        note_path        = Path(r"C:\Users\test\notes\2026-06-07-personal-ai-weekly.md"),
    )

    print(f"Email logged. Calculated cost: ${cost:.6f}\n")

    # Write a synthetic run summary
    run_start = datetime.now(timezone.utc).isoformat()
    time.sleep(0.01)

    log_run_summary(
        run_started_at     = run_start,
        accounts_processed = ["personal"],
        stats = {
            "processed":          1,
            "failed":             0,
            "skipped_marketing":  0,
            "skipped_duplicate":  0,
            "total_input":        1_270,
            "total_output":       400,
            "total_cache_reads":  0,
            "total_cost":         cost,
            "images_saved":       1,
            "images_filtered":    2,
        },
        images_enabled     = True,
        git_backup_status  = "pending",
    )

    print("Run summary logged.\n")

    # Read back and verify
    conn = get_connection()
    row  = conn.execute(
        "SELECT * FROM processing_log WHERE message_id = ?",
        ("<test-log-001@example.com>",)
    ).fetchone()
    run  = conn.execute(
        "SELECT * FROM run_summary ORDER BY id DESC LIMIT 1"
    ).fetchone()
    img  = conn.execute(
        "SELECT * FROM image_log WHERE message_id = ?",
        ("<test-log-001@example.com>",)
    ).fetchone()
    conn.close()

    print(f"{'─' * 60}")
    print(f"processing_log row:")
    print(f"  message_id   : {row['message_id']}")
    print(f"  status       : {row['status']}")
    print(f"  cost_usd     : ${row['cost_usd']:.6f}")
    print(f"  input_tokens : {row['input_tokens']}")
    print(f"  images_saved : {row['images_saved']}")
    print(f"  duration     : {row['duration_seconds']}s")
    print(f"\nrun_summary row:")
    print(f"  emails_processed : {run['emails_processed']}")
    print(f"  total_cost_usd   : ${run['total_cost_usd']:.6f}")
    print(f"  git_backup       : {run['git_backup_status']}")
    print(f"\nimage_log row:")
    print(f"  filename   : {img['filename']}")
    print(f"  dimensions : {img['width_px']}x{img['height_px']}")
    print(f"  format     : {img['saved_format']}")
    print(f"{'─' * 60}")

    # Clean up test rows
    conn = get_connection()
    conn.execute("DELETE FROM processing_log WHERE message_id = '<test-log-001@example.com>'")
    conn.execute("DELETE FROM image_log WHERE message_id = '<test-log-001@example.com>'")
    conn.commit()
    conn.close()
    print("\nTest rows cleaned up.")
    print("Test complete.")
