# progress_writer.py — Knowledge Base Pipeline v5.2
# Live Progress Writer (shared by orchestrator.py and link_orchestrator.py)
#
# Writes the current run's progress to dashboard/progress.json after every
# state change (item start, agent-step change, item completion, run finish).
#
# Reading progress.json:
#   dashboard/progress.html polls this file via fetch() every ~1.5s and
#   updates the page in place — no full reload, no flicker.
#
#   fetch() of local files is blocked by browser CORS policy under file://
#   URLs, so progress.html must be served over HTTP. Run:
#       python pipeline\serve_dashboard.py
#   which starts a tiny stdlib HTTP server on http://127.0.0.1:8420/ serving
#   the dashboard/ folder (covers both progress.html and index.html).
#
# Call sequence from an orchestrator:
#   start_run("email", total=12, run_label="personal, work")
#   for each item:
#       set_current_item(i, subject, account)
#       set_step("Classifying")       # called again at each agent boundary
#       ...
#       record_completed(subject, status, cost, duration)
#   finish_run(stats)
#
# On unexpected failure:
#   fail_run("error message")
#
# Usage (standalone test):
#   cd pipeline && python progress_writer.py
# =============================================================================

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_pipeline_dir = Path(__file__).resolve().parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import DASHBOARD_DIR

PROGRESS_JSON_PATH = DASHBOARD_DIR / "progress.json"

# Number of completed items kept in the "recent items" list shown on the page
MAX_RECENT_ITEMS = 8

# ---------------------------------------------------------------------------
# Module-level state for the current run
# ---------------------------------------------------------------------------
# A fresh dict is created by start_run(). All other functions mutate this
# dict and call _write() to persist it. If a function is called before
# start_run() (shouldn't normally happen), it is a safe no-op.

_state: dict = {}


# ---------------------------------------------------------------------------
# Internal write helper
# ---------------------------------------------------------------------------

def _write() -> None:
    """
    Serialise _state to dashboard/progress.json.
    Creates dashboard/ if it doesn't exist yet.

    Written on every state change so progress.html (polling via fetch())
    always reflects the current step within ~1.5 seconds.
    """
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_JSON_PATH.write_text(
        json.dumps(_state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def start_run(pipeline: str, total: int, run_label: str = "") -> None:
    """
    Initialise progress state at the start of a run. Call once before
    the per-item processing loop begins.

    Args:
        pipeline:  "email" | "links" — identifies which pipeline is running.
                   Shown in the progress page header.
        total:     Total number of items to process this run. May be 0
                   (e.g. no new emails) — the page will show "Nothing to
                   process" and immediately reflect status="complete" once
                   finish_run() is called.
        run_label: Optional human-readable label, e.g. account aliases
                   ("personal, work") or links file name.
    """
    global _state
    _state = {
        "pipeline":         pipeline,
        "run_label":        run_label,
        "status":           "running",          # "running" | "complete" | "error"
        "run_started_at":   datetime.now(timezone.utc).isoformat(),
        "run_completed_at": None,
        "current_index":    0,
        "total":            total,
        "current_subject":  "",
        "current_account":  "",
        "current_step":     "Starting...",
        "running_cost":     0.0,
        "stats": {
            "processed":         0,
            "failed":            0,
            "skipped_marketing": 0,
            "images_saved":      0,
            # Link-pipeline-only counters — stay at 0 for email runs, since
            # record_completed() is only ever called with non-default
            # fallback args from link_orchestrator.py. Tracked here (not
            # just in registry.db) so the LIVE page can show fallback
            # usage for the run in progress, not just after it's done and
            # written to link_log.
            "via_rss_fallback":        0,
            "via_playwright_fallback": 0,
            "via_manual_paste":        0,
        },
        "recent_items": [],
    }
    _write()


def set_current_item(index: int, subject: str, account: str = "") -> None:
    """
    Mark the start of processing for item `index` (1-based).

    Call this once per item, before any agent-step calls. Resets
    current_step to "Starting..." so the page doesn't show the previous
    item's last step momentarily.

    Args:
        index:   1-based position of this item in the run (e.g. 3 of 12).
        subject: Item title/subject — truncated to 80 chars for display.
        account: Account alias or "links" — shown next to the subject.
    """
    if not _state:
        return
    _state["current_index"]   = index
    _state["current_subject"] = (subject or "")[:80]
    _state["current_account"] = account
    _state["current_step"]    = "Starting..."
    _write()


def set_step(step_name: str) -> None:
    """
    Update the current agent step for the item currently being processed.

    Call this at each agent boundary, e.g.:
        set_step("Classifying")
        set_step("Summarising")
        set_step("Linking topics")
        set_step("Extracting images")
        set_step("Researching")
        set_step("Writing note")
        set_step("Labelling")        # email pipeline only
        set_step("Logging")

    Args:
        step_name: Short human-readable step label shown on the progress page.
    """
    if not _state:
        return
    _state["current_step"] = step_name
    _write()


def record_completed(
    subject: str,
    status: str,
    cost: float = 0.0,
    duration: float = 0.0,
    via_rss_fallback: bool = False,
    via_playwright_fallback: bool = False,
    via_manual_paste: bool = False,
) -> None:
    """
    Record that an item has finished processing. Updates running totals
    and prepends the item to the recent-items list (capped at
    MAX_RECENT_ITEMS).

    Args:
        subject:  Item title/subject — truncated to 70 chars for display.
        status:   "success" | "failed" | "skipped_marketing" |
                  "skipped_blocklist" | "dry_run"
        cost:     USD cost for this item (0.0 for skips/dry-run).
        duration: Wall-clock seconds for this item.
        via_rss_fallback:        Link pipeline only — True if this item's
                                 content was recovered via the Substack RSS
                                 fallback. Always False for email items;
                                 orchestrator.py never passes this. Tracked
                                 as a running count in stats so the live
                                 progress page can show fallback usage for
                                 the run currently in progress.
        via_playwright_fallback: Link pipeline only — same as above, for
                                 the Playwright headless-browser fallback.
        via_manual_paste:        Link pipeline only — True if this item's
                                 content was supplied via ingest_manual()
                                 rather than fetched. Always False for email
                                 and CSV-fetched link items.
    """
    if not _state:
        return

    _state["running_cost"] = round(_state.get("running_cost", 0.0) + cost, 6)

    stats = _state.setdefault("stats", {})
    if status == "success":
        stats["processed"] = stats.get("processed", 0) + 1
    elif status == "failed":
        stats["failed"] = stats.get("failed", 0) + 1
    elif status in ("skipped_marketing", "skipped_blocklist"):
        stats["skipped_marketing"] = stats.get("skipped_marketing", 0) + 1
    # "dry_run" intentionally not counted, matching orchestrator stats logic

    if via_rss_fallback:
        stats["via_rss_fallback"] = stats.get("via_rss_fallback", 0) + 1
    if via_playwright_fallback:
        stats["via_playwright_fallback"] = stats.get("via_playwright_fallback", 0) + 1
    if via_manual_paste:
        stats["via_manual_paste"] = stats.get("via_manual_paste", 0) + 1

    item = {
        "subject":  (subject or "")[:70],
        "status":   status,
        "cost":     round(cost, 6),
        "duration": round(duration, 1),
        "via_rss_fallback":        bool(via_rss_fallback),
        "via_playwright_fallback": bool(via_playwright_fallback),
        "via_manual_paste":        bool(via_manual_paste),
    }
    recent = _state.setdefault("recent_items", [])
    recent.insert(0, item)
    del recent[MAX_RECENT_ITEMS:]

    _write()


def finish_run(stats: dict | None = None) -> None:
    """
    Mark the run as complete. Call once after the per-item loop ends.

    Args:
        stats: Optional final stats dict from the orchestrator (the same
               dict passed to log_run_summary). If provided, overwrites
               the incrementally-tracked stats and running_cost with the
               authoritative final values — this corrects for any drift
               (e.g. images_saved is only known accurately at the end).
               If None, the incrementally-tracked values are kept as-is.
    """
    if not _state:
        return
    _state["status"]           = "complete"
    _state["current_step"]     = "Done"
    _state["run_completed_at"] = datetime.now(timezone.utc).isoformat()

    if stats:
        # via_rss_fallback / via_playwright_fallback: link pipeline's stats
        # dict carries these (see link_orchestrator.py main()); the email
        # pipeline's stats dict never will. Falling back to the
        # incrementally-tracked count (already correct, built up via
        # record_completed() calls during the run) rather than defaulting
        # to 0 means a caller that forgets to pass these doesn't silently
        # erase an otherwise-accurate running total at the last moment.
        prior_stats = _state.get("stats", {})
        _state["stats"] = {
            "processed":         stats.get("processed", 0),
            "failed":            stats.get("failed", 0),
            "skipped_marketing": stats.get("skipped_marketing", 0),
            "images_saved":      stats.get("images_saved", 0),
            "via_rss_fallback":        stats.get("via_rss_fallback",        prior_stats.get("via_rss_fallback", 0)),
            "via_playwright_fallback": stats.get("via_playwright_fallback", prior_stats.get("via_playwright_fallback", 0)),
            "via_manual_paste":        stats.get("via_manual_paste",        prior_stats.get("via_manual_paste", 0)),
        }
        _state["running_cost"] = round(stats.get("total_cost", 0.0), 6)

    _write()


def fail_run(error_message: str) -> None:
    """
    Mark the run as having failed with an unhandled error (e.g. ingestion
    itself raised, before the per-item loop could start).

    Safe to call even if start_run() was never called — initialises a
    minimal state so progress.html can still render an error banner.

    Args:
        error_message: Short description shown on the progress page.
    """
    global _state
    if not _state:
        _state = {
            "pipeline":     "unknown",
            "run_label":    "",
            "total":        0,
            "current_index": 0,
            "current_subject": "",
            "current_account": "",
            "running_cost": 0.0,
            "stats":        {},
            "recent_items": [],
            "run_started_at": datetime.now(timezone.utc).isoformat(),
        }
    _state["status"]           = "error"
    _state["current_step"]     = f"Error: {error_message[:120]}"
    _state["run_completed_at"] = datetime.now(timezone.utc).isoformat()
    _write()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Simulate a short run so dashboard/progress.html has something to display.
    Run this, then open dashboard/progress.html in a browser.

    Run: python progress_writer.py
    """
    import time

    print("=== Progress Writer — standalone test ===\n")
    print(f"Writing to: {PROGRESS_JSON_PATH}\n")

    start_run("email", total=3, run_label="personal")
    print("Wrote initial state.")
    print("In another terminal run: python serve_dashboard.py")
    print("Then open http://127.0.0.1:8420/progress.html to watch this "
          "simulate a run (polls every ~1.5s).\n")

    items = [
        ("AI Weekly: GPT updates", "personal", "success", 0.0021, 4.2),
        ("Flash Sale — 50% off!",  "personal", "skipped_marketing", 0.0008, 0.6),
        ("How PMs use AI",         "personal", "success", 0.0034, 6.1),
    ]

    steps = ["Classifying", "Summarising", "Linking topics",
             "Extracting images", "Researching", "Writing note",
             "Labelling", "Logging"]

    for i, (subject, account, status, cost, duration) in enumerate(items, 1):
        set_current_item(i, subject, account)
        print(f"[{i}/3] {subject}")
        for step in steps:
            set_step(step)
            print(f"    {step}")
            time.sleep(0.4)
        record_completed(subject, status, cost, duration)
        print(f"    → {status} (${cost:.4f})\n")

    finish_run({
        "processed": 2, "failed": 0, "skipped_marketing": 1,
        "images_saved": 1, "total_cost": 0.0063,
    })

    print("Done. progress.json written with status='complete'.")
