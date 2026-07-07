# orchestrator.py — Newsletter AI Pipeline v1.0
# Main entry point. Wires all agents together in the correct sequence.
#
# Agent execution order per email:
#   1   Ingestion       — fetch emails from all Gmail accounts
#   1.5 Classification  — filter marketing, extract editorial from mixed
#   2   Summarisation   — extract structured note data via LLM
#   3   Topic Linking   — find related notes, identify new topics
#   4   Image Extraction — download and save content images
#   5   Research        — web search for new topics (conditional)
#   7   Local Writer    — write .md note + update INDEX.md + topics_index.json
#   8   Gmail Label     — apply "AI Processed" or "AI Review" label
#   6   Logging         — write processing_log row (runs last per email)
#
#   9   Git Backup      — runs as a SEPARATE scheduled task, not here
#
# CLI flags:
#   --dry-run          Fetch and classify; no API calls, no writes, no labels
#   --no-images        Skip Agent 4 entirely for this run
#   --no-classify      Skip Agent 1.5; all emails go straight to summarisation
#   --limit N          Cap emails per account per run (useful for testing)
#   --bootstrap        Force full historical processing (ignore processed_ids)
#   --account ALIAS    Process only one account alias
#   --no-backup        Skip logging git_backup_status (already separate task)
# =============================================================================

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_pipeline_dir = Path(__file__).resolve().parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import GMAIL_ACCOUNTS, MAX_RESEARCH_TOPICS_PER_RUN
from db import initialise_db, get_processed_ids, get_connection, add_to_research_queue

from agents.ingestion        import ingest_all_accounts
from agents.classification   import classify
from agents.summarisation    import summarise
from agents.topic_linking    import find_related_notes, get_new_topics, update_topic_index
from agents.image_extraction import extract_images
from agents.research         import research_new_topics, aggregate_usage as aggregate_research_usage
from agents.local_writer     import write_note, update_index, update_topics_json
from agents.gmail_label      import apply_label_by_status, clear_label_cache
from agents.logging_agent    import log_email_result, log_run_summary
import progress_writer as pw


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Newsletter AI Processing Pipeline v4.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Auth test:     python orchestrator.py --account personal --dry-run --limit 3\n"
            "  Bootstrap:     python orchestrator.py --bootstrap --limit 10\n"
            "  Normal run:    python orchestrator.py\n"
        ),
    )
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--no-images",   action="store_true")
    parser.add_argument("--no-classify", action="store_true")
    parser.add_argument("--no-research", action="store_true",
                        help="Skip Agent 5 (web research). New topics are written to "
                             "the research queue instead of being discarded, so they "
                             "can be reviewed and researched selectively via the "
                             "research queue UI.")
    parser.add_argument("--limit",       type=int, default=None, metavar="N")
    parser.add_argument("--bootstrap",   action="store_true")
    parser.add_argument("--account",     type=str, default=None, metavar="ALIAS")
    parser.add_argument("--no-backup",    action="store_true")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Skip dashboard regeneration after run "
                             "(used by main.py all to suppress mid-run regeneration)")
    return parser.parse_args()


def _process_email(email: dict, args: argparse.Namespace, account: dict) -> tuple[str, float]:
    """
    Run the full agent sequence for one email.

    Returns (status, cost_usd).
    status: "success" | "failed" | "skipped_marketing" | "skipped_blocklist" | "dry_run"
    """
    t_start = time.monotonic()
    email["images_disabled"] = int(args.no_images)

    # ------------------------------------------------------------------
    # Agent 1.5 — Classification
    # ------------------------------------------------------------------
    classify_usage = None

    if not args.no_classify:
        pw.set_step("Classifying")
        email = classify(email, account)
        classify_usage = email.pop("_classification_usage", None)

        if email.get("action") == "skip":
            status   = ("skipped_blocklist"
                        if email.get("classification_stage") == "blocklist"
                        else "skipped_marketing")
            duration = time.monotonic() - t_start

            if status == "skipped_marketing" and not args.dry_run:
                pw.set_step("Labelling")
                apply_label_by_status(
                    email["service"], email["gmail_id"],
                    email["account_alias"], status,
                )

            pw.set_step("Logging")
            cost = log_email_result(
                email          = email,
                status         = status,
                duration_secs  = duration,
                classify_usage = classify_usage,
            )
            return status, cost

    # ------------------------------------------------------------------
    # Dry run — exit before any writes
    # ------------------------------------------------------------------
    if args.dry_run:
        classification = email.get("classification", "not_classified")
        print(f"    [dry-run] Would process | classification={classification}")
        return "dry_run", 0.0

    # ------------------------------------------------------------------
    # Agent 2 — Summarisation
    # ------------------------------------------------------------------
    pw.set_step("Summarising")
    summ_result = summarise(email)
    structured  = summ_result["structured"]
    summ_usage  = summ_result["usage"]
    tags        = structured.get("tags", [])

    # ------------------------------------------------------------------
    # Agent 3 — Topic Linking
    # ------------------------------------------------------------------
    pw.set_step("Linking topics")
    from agents.local_writer import _make_filename  # noqa: PLC0415
    target_filename = _make_filename(email)
    related_notes   = find_related_notes(tags, target_filename)
    new_topics      = get_new_topics(tags)

    # ------------------------------------------------------------------
    # Agent 4 — Image Extraction
    # ------------------------------------------------------------------
    pw.set_step("Extracting images")
    saved_assets             = extract_images(email, enable_images=not args.no_images)
    email["images_found"]    = len(saved_assets)
    email["images_filtered"] = 0

    # ------------------------------------------------------------------
    # Agent 5 — Research (conditional)
    # ------------------------------------------------------------------
    # Queue trigger: when --no-research is passed OR MAX_RESEARCH_TOPICS_PER_RUN
    # is 0, new topics are written to research_queue instead of being discarded.
    # This preserves them for selective manual research via the queue UI, rather
    # than losing them silently. On a normal run (neither condition), auto-research
    # fires as usual and the queue is not written to.
    _research_disabled = args.no_research or MAX_RESEARCH_TOPICS_PER_RUN == 0

    if new_topics and _research_disabled and not args.dry_run:
        for _t in new_topics:
            add_to_research_queue(
                topic          = _t,
                note_path      = str(target_filename),
                source_account = email.get("account_alias"),
                source_subject = email.get("subject"),
            )
        print(f"    [queue] {len(new_topics)} topic(s) added to research queue "
              f"(--no-research / MAX_RESEARCH_TOPICS_PER_RUN=0)")

    if new_topics and not _research_disabled:
        pw.set_step("Researching")
    research       = research_new_topics(new_topics) if (new_topics and not _research_disabled) else {}
    research_usage = aggregate_research_usage(research) if research else None

    # ------------------------------------------------------------------
    # Agent 7 — Local File Writer
    #
    # FIX: update_topics_json() now runs BEFORE write_note(), not after.
    # write_note() reads topics_index.json to drive the best-effort inline
    # wikilink injection pass — previously this email's own newly-extracted
    # tags were written to topics_index.json only AFTER the note was
    # already saved, so the inline pass never had a chance to see them.
    # (The reliable ## Tags section in the note body is unaffected by this
    # ordering either way — it always wikilinks every tag unconditionally.)
    # ------------------------------------------------------------------
    update_topics_json(tags, target_filename, email["account_alias"])

    pw.set_step("Writing note")
    note_path = write_note(
        email         = email,
        structured    = structured,
        related_notes = related_notes,
        research      = research,
        saved_assets  = saved_assets,
    )
    update_index(email, structured, note_path)
    update_topic_index(tags, note_path.name, email["account_alias"])

    # ------------------------------------------------------------------
    # Agent 8 — Gmail Label (after file write confirmed)
    # ------------------------------------------------------------------
    pw.set_step("Labelling")
    apply_label_by_status(
        email["service"], email["gmail_id"],
        email["account_alias"], "success",
    )

    # ------------------------------------------------------------------
    # Agent 6 — Logging (always last)
    # ------------------------------------------------------------------
    pw.set_step("Logging")
    duration = time.monotonic() - t_start
    cost = log_email_result(
        email           = email,
        status          = "success",
        duration_secs   = duration,
        summarise_usage = summ_usage,
        classify_usage  = classify_usage,
        research_usage  = research_usage,
        saved_assets    = saved_assets,
        note_path       = note_path,
    )

    return "success", cost


def _read_email_log_fields(message_id: str) -> dict:
    """
    Re-read token counts and duration from processing_log after each email.

    Re-reading from the DB rather than threading values through the call
    stack guarantees the run summary always matches what was actually
    logged — even when an agent fails partway and writes partial data.
    Dry-run emails produce no DB row so this safely returns zeros.

    Also used by progress_writer to display per-item duration in the
    "Recent Items" table on the live progress page.
    """
    empty = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "duration_seconds": 0.0}
    if not message_id:
        return empty

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, duration_seconds "
            "FROM processing_log WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    finally:
        conn.close()

    if row:
        return {
            "input_tokens":      row["input_tokens"]      or 0,
            "output_tokens":     row["output_tokens"]      or 0,
            "cache_read_tokens": row["cache_read_tokens"]  or 0,
            "duration_seconds":  row["duration_seconds"]   or 0.0,
        }
    return empty


def main() -> int:
    args      = _parse_args()
    run_start = datetime.now(timezone.utc).isoformat()

    print(f"\n{'=' * 65}")
    print(f"  Newsletter AI Pipeline v1.0")
    print(f"  Started: {run_start}")
    print(f"  Flags:   dry_run={args.dry_run} | no_images={args.no_images} | "
          f"no_classify={args.no_classify} | limit={args.limit} | "
          f"bootstrap={args.bootstrap} | account={args.account or 'all'}")
    print(f"{'=' * 65}\n")

    initialise_db()
    clear_label_cache()

    accounts = GMAIL_ACCOUNTS
    if args.account:
        accounts = [a for a in GMAIL_ACCOUNTS if a["alias"] == args.account]
        if not accounts:
            print(f"ERROR: No account with alias '{args.account}' in config.py")
            print(f"  Available: {[a['alias'] for a in GMAIL_ACCOUNTS]}")
            return 1

    processed_ids = get_processed_ids() if not args.bootstrap else set()

    print(f"[pipeline] Fetching emails from {len(accounts)} account(s)...")
    try:
        all_emails = ingest_all_accounts(accounts, processed_ids, limit=args.limit, bootstrap=args.bootstrap)
    except Exception as exc:
        pw.fail_run(f"{type(exc).__name__}: {exc}")
        raise

    if not all_emails:
        print("\n[pipeline] No new emails to process. Run complete.\n")
        _write_empty_run_summary(run_start, accounts, args)
        pw.start_run("email", total=0, run_label=", ".join(a["alias"] for a in accounts))
        pw.finish_run({"processed": 0, "failed": 0, "skipped_marketing": 0,
                       "images_saved": 0, "total_cost": 0.0})
        return 0

    print(f"\n[pipeline] Processing {len(all_emails)} email(s)...\n")

    pw.start_run("email", total=len(all_emails),
                  run_label=", ".join(a["alias"] for a in accounts))

    # -----------------------------------------------------------------------
    # Per-email stats counters
    # -----------------------------------------------------------------------
    stats = {
        "processed":         0,    # emails with status == "success"
        "failed":            0,    # emails with status == "failed"
        "skipped_marketing": 0,    # emails skipped by Agent 1.5
        "skipped_blocklist": 0,    # emails skipped by blocklist
        "skipped_duplicate": 0,    # in-memory dedups (counted pre-loop)
        # FIX: token totals now populated per-email from processing_log
        # Previously missing — caused total_input_tokens=0 in run_summary
        "total_input":       0,
        "total_output":      0,
        "total_cache_reads": 0,
        "total_cost":        0.0,
        "images_saved":      0,
        "images_filtered":   0,
    }

    for i, email in enumerate(all_emails, 1):
        subject_preview = (email.get("subject") or "(no subject)")[:55]
        print(
            f"[{i:>3}/{len(all_emails)}] "
            f"[{email['account_alias']}] "
            f"{subject_preview}"
        )

        pw.set_current_item(i, subject_preview, email["account_alias"])

        account = next(
            (a for a in GMAIL_ACCOUNTS if a["alias"] == email["account_alias"]),
            {"alias": email["account_alias"], "sender_allowlist": [], "sender_blocklist": []},
        )

        try:
            status, cost = _process_email(email, args, account)
        except Exception as exc:
            print(f"    [pipeline] ERROR: {type(exc).__name__}: {exc}")
            cost = log_email_result(
                email         = email,
                status        = "failed",
                duration_secs = 0.0,
            )
            status = "failed"

        # -----------------------------------------------------------------------
        # FIX: Status counters — "dry_run" intentionally excluded from all buckets.
        # Previously "skipped_marketing" emails were being counted as "processed"
        # because the success branch was not the only path that incremented a counter.
        # -----------------------------------------------------------------------
        if status == "success":
            stats["processed"]    += 1
            stats["images_saved"] += email.get("images_found", 0)
        elif status == "skipped_marketing":
            stats["skipped_marketing"] += 1
        elif status == "skipped_blocklist":
            stats["skipped_blocklist"] += 1
        elif status == "failed":
            stats["failed"] += 1
        # "dry_run" → no counter incremented

        # -----------------------------------------------------------------------
        # FIX: Accumulate token totals from processing_log after each email.
        # Previously the orchestrator never populated total_input / total_output,
        # so run_summary always recorded 0 tokens even when cost > 0.
        # We re-read from DB to pick up tokens for all statuses (success,
        # skipped_marketing, failed) without duplicating cost logic here.
        # Also used to get duration_seconds for the live progress page.
        # -----------------------------------------------------------------------
        duration_secs = 0.0
        if status != "dry_run":
            fields = _read_email_log_fields(email.get("message_id", ""))
            stats["total_input"]       += fields["input_tokens"]
            stats["total_output"]      += fields["output_tokens"]
            stats["total_cache_reads"] += fields["cache_read_tokens"]
            stats["total_cost"]        += cost
            duration_secs = fields["duration_seconds"]

        pw.record_completed(subject_preview, status, cost, duration_secs)

        print()  # blank line between emails

    # -----------------------------------------------------------------------
    # FIX: Consistency guard — if zero tokens were recorded across the whole
    # run, force cost to zero to prevent a corrupted run_summary row.
    # Protects against edge cases where cost accumulates before token logging.
    # -----------------------------------------------------------------------
    if stats["total_input"] == 0 and stats["total_output"] == 0:
        stats["total_cost"] = 0.0

    # -----------------------------------------------------------------------
    # Write run summary and print report
    # -----------------------------------------------------------------------
    if not args.dry_run:
        git_status = "skipped" if args.no_backup else "pending"
        log_run_summary(
            run_started_at     = run_start,
            accounts_processed = [a["alias"] for a in accounts],
            stats              = stats,
            images_enabled     = not args.no_images,
            git_backup_status  = git_status,
        )

    pw.finish_run(stats)

    _print_run_report(stats, args)

    # Regenerate dashboard (skipped when --no-dashboard is set, e.g. when
    # called from main.py all which runs the dashboard step explicitly at the end)
    if not args.dry_run and not args.no_dashboard:
        try:
            from dashboard.generate_dashboard import generate as generate_dashboard
            print("[pipeline] Generating dashboard...")
            generate_dashboard()
        except Exception as exc:
            print(f"[pipeline] Dashboard generation failed (non-fatal): {exc}")

    return 1 if stats["failed"] > 0 else 0


def _write_empty_run_summary(
    run_start: str,
    accounts:  list[dict],
    args:      argparse.Namespace,
) -> None:
    """Write a zero-value run summary when there are no emails to process."""
    if not args.dry_run:
        log_run_summary(
            run_started_at     = run_start,
            accounts_processed = [a["alias"] for a in accounts],
            stats              = {k: 0 for k in [
                "processed", "failed", "skipped_marketing", "skipped_blocklist",
                "skipped_duplicate", "total_input", "total_output",
                "total_cache_reads", "total_cost", "images_saved", "images_filtered",
            ]},
            images_enabled    = not args.no_images,
            git_backup_status = "skipped" if args.no_backup else "pending",
        )


def _print_run_report(stats: dict, args: argparse.Namespace) -> None:
    """Print a human-readable summary to stdout at end of run."""
    print(f"{'=' * 65}")
    print(f"  Run complete")
    print(f"{'─' * 65}")
    print(f"  Processed:           {stats['processed']}")
    print(f"  Skipped (marketing): {stats['skipped_marketing']} | "
          f"Skipped (blocklist): {stats['skipped_blocklist']}")
    print(f"  Failed:              {stats['failed']}")
    print(f"  Images saved:        {stats['images_saved']}")
    print(f"  Tokens in / out:     {stats['total_input']:,} / {stats['total_output']:,}")
    print(f"  Cache reads:         {stats['total_cache_reads']:,}")
    print(f"  Total cost (USD):    ${stats['total_cost']:.5f}")
    if args.dry_run:
        print(f"\n  [dry-run] No files written, no labels applied, no DB records written.")
    print(f"{'=' * 65}\n")


if __name__ == "__main__":
    sys.exit(main())
