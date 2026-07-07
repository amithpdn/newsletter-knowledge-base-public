# link_orchestrator.py — Knowledge Base Pipeline v1.0
# Entry point for processing saved article links.
#
# Reads URLs from pipeline/links.csv, fetches and extracts content,
# then passes each article through the same downstream agents used by
# the newsletter pipeline:
#
#   1L  Link Ingestion   — fetch, extract, classify fetch status
#   1.5 Classification   — filter low-quality / irrelevant content
#   2   Summarisation    — extract structured note data via LLM
#   3   Topic Linking    — find related notes in the unified index
#   4   Image Extraction — download and save content images
#   5   Research         — web search for new topics (conditional)
#   7   Local File Writer — write .md note + update index files
#   6   Logging          — write to processing_log + link_log
#
# Agent 8 (Gmail Label) and Agent 9 (Git Backup) are intentionally
# excluded — there is no Gmail inbox to label for links, and the
# existing git_backup.py scheduled task covers backup for both pipelines.
#
# CLI flags:
#   --links-file PATH      Path to CSV file (default: pipeline/links.csv)
#   --dry-run              Fetch and log status only — no summarisation, no notes
#   --limit N              Process at most N links per run
#   --reprocess-failed     Retry URLs previously marked "failed"
#   --no-research          Skip Agent 5 (web research for new topics)
#   --no-images            Skip Agent 4 (image extraction)
#   --no-classify          Skip Agent 1.5 (classification)
#   --no-playwright        Skip the Playwright headless-browser fallback for
#                          0-word pages (JS-gated pages go straight to
#                          js_required instead of attempting browser rendering)
#
# Usage:
#   # Dry run — see which links fetch successfully
#   python pipeline\link_orchestrator.py --dry-run
#
#   # Process up to 10 links, skip research
#   python pipeline\link_orchestrator.py --limit 10 --no-research
#
#   # Full run
#   python pipeline\link_orchestrator.py
#
#   # Retry links that failed last run (e.g. network was down)
#   python pipeline\link_orchestrator.py --reprocess-failed
#
#   # Faster run without the Playwright fallback (skip JS-gated page recovery)
#   python pipeline\link_orchestrator.py --no-playwright
# =============================================================================

# link_orchestrator.py — Knowledge Base Pipeline v1.0
# Entry point for processing saved article links.
#
# Reads URLs from pipeline/links.csv, fetches and extracts content,
# then passes each article through the same downstream agents used by
# the newsletter pipeline:
#
#   1L  Link Ingestion   — fetch, extract, classify fetch status
#   1.5 Classification   — filter low-quality / irrelevant content
#   2   Summarisation    — extract structured note data via LLM
#   3   Topic Linking    — find related notes in the unified index
#   4   Image Extraction — download and save content images
#   5   Research         — web search for new topics (conditional)
#   7   Local File Writer — write .md note + update index files
#   6   Logging          — write to processing_log + link_log
#
# Agent 8 (Gmail Label) and Agent 9 (Git Backup) are intentionally
# excluded — there is no Gmail inbox to label for links, and the
# existing git_backup.py scheduled task covers backup for both pipelines.
#
# CLI flags:
#   --links-file PATH      Path to CSV file (default: pipeline/links.csv)
#   --dry-run              Fetch and log status only — no summarisation, no notes
#   --limit N              Process at most N links per run
#   --reprocess-failed     Retry URLs previously marked "failed"
#   --no-research          Skip Agent 5 (web research for new topics)
#   --no-images            Skip Agent 4 (image extraction)
#   --no-classify          Skip Agent 1.5 (classification)
#   --no-playwright        Skip the Playwright headless-browser fallback for
#                          0-word pages (JS-gated pages go straight to
#                          js_required instead of attempting browser rendering)
#   --no-dashboard         Skip dashboard regeneration at end of run (used
#                          internally by main.py all to suppress mid-run
#                          regeneration; the all command runs it explicitly)
#
# Usage:
#   # Dry run — see which links fetch successfully
#   python pipeline\link_orchestrator.py --dry-run
#
#   # Process up to 10 links, skip research
#   python pipeline\link_orchestrator.py --limit 10 --no-research
#
#   # Full run
#   python pipeline\link_orchestrator.py
#
#   # Retry links that failed last run (e.g. network was down)
#   python pipeline\link_orchestrator.py --reprocess-failed
#
#   # Faster run without the Playwright fallback (skip JS-gated page recovery)
#   python pipeline\link_orchestrator.py --no-playwright
# =============================================================================

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_pipeline_dir = Path(__file__).resolve().parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import GMAIL_ACCOUNTS, MAX_RESEARCH_TOPICS_PER_RUN
from db import initialise_db, get_connection, add_to_research_queue

from agents.link_ingestion  import (
    ingest_links, ingest_manual, get_processed_urls,
    DEFAULT_LINKS_FILE, MANUAL_CONTENT_DIR,
)
from agents.classification  import classify
from agents.summarisation   import summarise
from agents.topic_linking   import find_related_notes, get_new_topics, update_topic_index
from agents.image_extraction import extract_images
from agents.research        import research_new_topics, aggregate_usage as aggregate_research_usage
from agents.local_writer    import write_note, update_index, update_topics_json
from agents.logging_agent   import log_email_result, log_run_summary
import progress_writer as pw


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Knowledge Base Pipeline — Article Link Processor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Dry run (check which links fetch):  python link_orchestrator.py --dry-run\n"
            "  Process 10 links, skip research:    python link_orchestrator.py --limit 10 --no-research\n"
            "  Full run:                           python link_orchestrator.py\n"
            "  Retry failed URLs:                  python link_orchestrator.py --reprocess-failed\n"
            "  Skip Playwright fallback (faster):  python link_orchestrator.py --no-playwright\n"
        ),
    )
    parser.add_argument("--links-file",       type=Path,  default=DEFAULT_LINKS_FILE,
                        help="Path to links CSV file")
    parser.add_argument("--dry-run",          action="store_true",
                        help="Fetch and log status only — no LLM calls, no notes written")
    parser.add_argument("--limit",            type=int,   default=None, metavar="N",
                        help="Max links to process per run")
    parser.add_argument("--reprocess-failed", action="store_true",
                        help="Retry URLs previously marked as failed")
    parser.add_argument("--no-research",      action="store_true",
                        help="Skip Agent 5 — no web research for new topics")
    parser.add_argument("--no-images",        action="store_true",
                        help="Skip Agent 4 — no image extraction")
    parser.add_argument("--no-classify",      action="store_true",
                        help="Skip Agent 1.5 — all articles go directly to summarisation")
    parser.add_argument("--no-playwright",    action="store_true",
                        help="Skip the Playwright headless-browser fallback for 0-word pages "
                             "(JS-gated pages go straight to js_required status instead)")
    parser.add_argument("--no-dashboard",     action="store_true",
                        help="Skip dashboard regeneration after run "
                             "(used by main.py all to suppress mid-run regeneration)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Per-article processing
# ---------------------------------------------------------------------------

def _process_article(article: dict, args: argparse.Namespace) -> tuple[str, float]:
    """
    Run the full downstream agent sequence for one article.

    Uses a dummy account dict (no allowlist/blocklist) since articles
    don't come from a Gmail account — the classification agent works
    purely on content signals for link-sourced articles.

    Returns (status, cost_usd).
    """
    t_start = time.monotonic()
    article["images_disabled"] = int(args.no_images)

    # Dummy account — no sender allowlist/blocklist applies to links
    dummy_account = {
        "alias":            "links",
        "sender_allowlist": [],
        "sender_blocklist": [],
    }

    # -----------------------------------------------------------------------
    # Agent 1.5 — Classification
    # Optional for links — articles are generally editorial, but classification
    # still catches low-quality or marketing-heavy content (sponsored posts, etc.)
    # -----------------------------------------------------------------------
    classify_usage = None

    if not args.no_classify:
        pw.set_step("Classifying")
        article = classify(article, dummy_account)
        classify_usage = article.pop("_classification_usage", None)

        if article.get("action") == "skip":
            status   = "skipped_marketing"
            duration = time.monotonic() - t_start
            pw.set_step("Logging")
            cost = log_email_result(
                email          = article,
                status         = status,
                duration_secs  = duration,
                classify_usage = classify_usage,
            )
            return status, cost

    # -----------------------------------------------------------------------
    # Dry run exits here
    # -----------------------------------------------------------------------
    if args.dry_run:
        print(f"    [dry-run] Would process | classification={article.get('classification','—')}")
        return "dry_run", 0.0

    # -----------------------------------------------------------------------
    # Agent 2 — Summarisation
    # Inject the CSV label as a hint in the subject so the model produces
    # better-targeted tags for short or ambiguous articles.
    # -----------------------------------------------------------------------
    pw.set_step("Summarising")
    label = article.get("label", "")
    if label and label not in (article.get("subject") or ""):
        article["subject"] = f"{article.get('subject', '')} [{label}]".strip()

    summ_result = summarise(article)
    structured  = summ_result["structured"]
    summ_usage  = summ_result["usage"]
    tags        = structured.get("tags", [])

    # -----------------------------------------------------------------------
    # Agent 3 — Topic Linking
    # -----------------------------------------------------------------------
    pw.set_step("Linking topics")
    from agents.local_writer import _make_filename  # noqa: PLC0415
    target_filename = _make_filename(article)

    related_notes = find_related_notes(tags, target_filename)
    new_topics    = get_new_topics(tags)

    # -----------------------------------------------------------------------
    # Agent 4 — Image Extraction
    # -----------------------------------------------------------------------
    pw.set_step("Extracting images")
    saved_assets             = extract_images(article, enable_images=not args.no_images)
    article["images_found"]  = len(saved_assets)
    article["images_filtered"] = 0

    # -----------------------------------------------------------------------
    # Agent 5 — Research (conditional)
    # -----------------------------------------------------------------------
    # Queue trigger: when --no-research is passed OR MAX_RESEARCH_TOPICS_PER_RUN
    # is 0, new topics are written to research_queue instead of being discarded.
    _research_disabled = args.no_research or MAX_RESEARCH_TOPICS_PER_RUN == 0

    if new_topics and _research_disabled and not args.dry_run:
        for _t in new_topics:
            add_to_research_queue(
                topic          = _t,
                note_path      = str(target_filename),
                source_account = article.get("account_alias", "links"),
                source_subject = article.get("subject"),
            )
        print(f"    [queue] {len(new_topics)} topic(s) added to research queue "
              f"(--no-research / MAX_RESEARCH_TOPICS_PER_RUN=0)")

    if new_topics and not _research_disabled:
        pw.set_step("Researching")
    research       = research_new_topics(new_topics) if (new_topics and not _research_disabled) else {}
    research_usage = aggregate_research_usage(research) if research else None

    # -----------------------------------------------------------------------
    # Agent 7 — Local File Writer
    # Adds source_url to the note body via an extra section
    #
    # FIX: update_topics_json() now runs BEFORE write_note(), matching the
    # same fix applied to orchestrator.py. See that file's comment for
    # the full rationale — in short, write_note() reads topics_index.json
    # for its best-effort inline wikilink pass, so this article's own tags
    # need to be written to that file first for the pass to see them.
    # -----------------------------------------------------------------------
    update_topics_json(tags, target_filename, article["account_alias"])

    pw.set_step("Writing note")
    note_path = write_note(
        email         = article,
        structured    = structured,
        related_notes = related_notes,
        research      = research,
        saved_assets  = saved_assets,
    )

    # Append source URL to note (links have a URL; emails have a sender instead)
    _append_source_url(
        note_path,
        article.get("source_url", ""),
        is_partial=article.get("is_partial", False),
        via_rss_fallback=article.get("via_rss_fallback", False),
        via_playwright_fallback=article.get("via_playwright_fallback", False),
    )

    update_index(article, structured, note_path)
    update_topic_index(tags, note_path.name, article["account_alias"])

    # -----------------------------------------------------------------------
    # Agent 6 — Logging
    # -----------------------------------------------------------------------
    pw.set_step("Logging")
    duration = time.monotonic() - t_start
    cost = log_email_result(
        email           = article,
        status          = "success",
        duration_secs   = duration,
        summarise_usage = summ_usage,
        classify_usage  = classify_usage,
        research_usage  = research_usage,
        saved_assets    = saved_assets,
        note_path       = note_path,
    )

    return "success", cost


def _append_source_url(
    note_path: Path,
    source_url: str,
    is_partial: bool = False,
    via_rss_fallback: bool = False,
    via_playwright_fallback: bool = False,
) -> None:
    """
    Append a ## Source section with the original URL to the written note,
    plus warning/info banners for partial content and/or fallback
    recovery, so neither is ever mistaken for a normally-fetched full page.

    This is specific to link-sourced notes — email notes use sender instead.
    Called after write_note() so the wikilink injection has already run.

    Args:
        note_path:        Path to the already-written note file.
        source_url:        Original article URL.
        is_partial:        If True, the extracted content was below
                           MIN_CONTENT_WORDS — likely a free preview before
                           a paywall, not the full article. A warning
                           banner is added so this is never mistaken for a
                           complete summary.
        via_rss_fallback:  If True, content was recovered via the Substack
                           RSS feed fallback rather than the normal page
                           fetch (which returned 0 words). This is purely
                           informational — RSS-recovered content is
                           typically the genuine full post body, not a
                           preview, so no quality caveat is implied. Noted
                           anyway for transparency about how the note was
                           produced.
        via_playwright_fallback: If True, content was recovered by rendering
                           the page in a real headless browser rather than
                           the normal anonymous fetch. Like via_rss_fallback,
                           this is informational, not a quality warning —
                           Playwright recovery is generally the MORE
                           complete of the two fallbacks (real rendered
                           HTML, working images), so the banner says so
                           explicitly rather than reading as a caveat.

    NOTE: via_rss_fallback and via_playwright_fallback are not mutually
    exclusive in principle (the chain tries Playwright first, falls back to
    RSS only if Playwright didn't recover content — see link_ingestion.py),
    so both banners can appear together in the rare case both code paths
    left a flag set, though in practice only one is normally True per note.
    """
    if not source_url:
        return
    try:
        existing = note_path.read_text(encoding="utf-8")
        if "## Source" in existing:
            return  # already appended (e.g. re-run) — don't duplicate

        addition = f"\n\n## Source\n[{source_url}]({source_url})\n"

        if via_playwright_fallback:
            addition += (
                "\n> ℹ️ **Recovered via headless browser (Playwright)** — the page "
                "returned no extractable content via a normal fetch (likely "
                "requires JavaScript to render), so this note was generated from "
                "a real rendered version of the page instead. This is generally "
                "the MORE complete of the two recovery methods — full rendered "
                "content, including images where applicable.\n"
            )

        if via_rss_fallback:
            addition += (
                "\n> ℹ️ **Recovered via RSS feed** — the page itself returned no "
                "extractable content and the headless-browser fallback was "
                "unavailable, disabled, or unsuccessful, so this note was "
                "generated from the publication's RSS feed instead. Content is "
                "typically the full post text, but does not include images.\n"
            )

        if is_partial:
            addition += (
                "\n> ⚠️ **Partial content** — this note was generated from a "
                "preview/excerpt only (likely a metered paywall, e.g. a "
                "Medium-family site). The summary above reflects the "
                "available preview text, not the complete article. "
                "Visit the source link for the full piece.\n"
            )

        note_path.write_text(existing.rstrip() + addition, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Token accumulation helper (mirrors orchestrator.py)
# ---------------------------------------------------------------------------

def _read_article_log_fields(message_id: str) -> dict:
    """
    Re-read token counts and duration from processing_log after each article.
    Mirrors _read_email_log_fields in orchestrator.py — see that function
    for the rationale (DB is the source of truth for the run summary and
    for the live progress page's "Recent Items" duration column).
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args      = _parse_args()
    run_start = datetime.now(timezone.utc).isoformat()

    print(f"\n{'=' * 65}")
    print(f"  Knowledge Base Pipeline — Link Processor")
    print(f"  Started: {run_start}")
    print(f"  File:    {args.links_file}")
    print(f"  Flags:   dry_run={args.dry_run} | limit={args.limit} | "
          f"no_research={args.no_research} | no_images={args.no_images} | "
          f"reprocess_failed={args.reprocess_failed} | no_playwright={args.no_playwright}")
    print(f"{'=' * 65}\n")

    initialise_db()

    # Agent 1L — Link Ingestion
    try:
        processed_urls = get_processed_urls(reprocess_failed=args.reprocess_failed)
        articles = ingest_links(
            csv_path          = args.links_file,
            processed_urls    = processed_urls,
            limit             = args.limit,
            reprocess_failed  = args.reprocess_failed,
            use_playwright    = not args.no_playwright,
        )
        # Merge manually-pasted articles from manual_content/ (written by
        # /api/ingest in serve_dashboard.py). These bypass the fetch entirely
        # and run through the same downstream agents. The limit applies to
        # the combined total so a large manual batch doesn't overwhelm a run.
        remaining_limit = max(0, args.limit - len(articles)) if args.limit else None
        manual_articles = ingest_manual(
            processed_urls = processed_urls | {a["source_url"] for a in articles},
            limit          = remaining_limit,
        )
        articles = articles + manual_articles
    except Exception as exc:
        pw.fail_run(f"{type(exc).__name__}: {exc}")
        raise

    if not articles:
        print("\n[pipeline] No new articles to process. Run complete.\n")
        pw.start_run("links", total=0, run_label=args.links_file.name)
        pw.finish_run({"processed": 0, "failed": 0, "skipped_marketing": 0,
                       "images_saved": 0, "total_cost": 0.0})
        return 0

    print(f"\n[pipeline] Processing {len(articles)} article(s)...\n")

    pw.start_run("links", total=len(articles), run_label=args.links_file.name)

    stats = {
        "processed":         0,
        "failed":            0,
        "skipped_marketing": 0,
        "total_input":       0,
        "total_output":      0,
        "total_cache_reads": 0,
        "total_cost":        0.0,
        "images_saved":      0,
        # Counted here (not just relying on the per-item recent_items list
        # in progress.json) so finish_run() has an authoritative final
        # total to write, matching the pattern already used for
        # images_saved — see progress_writer.finish_run().
        "via_rss_fallback":        0,
        "via_playwright_fallback": 0,
        "via_manual_paste":        0,
    }

    for i, article in enumerate(articles, 1):
        title_preview = (article.get("subject") or article.get("source_url", ""))[:55]
        print(f"[{i:>3}/{len(articles)}] {title_preview}")

        pw.set_current_item(i, title_preview, "links")

        try:
            status, cost = _process_article(article, args)
        except Exception as exc:
            print(f"    [pipeline] ERROR: {type(exc).__name__}: {exc}")
            cost = log_email_result(
                email         = article,
                status        = "failed",
                duration_secs = 0.0,
            )
            status = "failed"

        if status == "success":
            stats["processed"]    += 1
            stats["images_saved"] += article.get("images_found", 0)
        elif status == "skipped_marketing":
            stats["skipped_marketing"] += 1
        elif status == "failed":
            stats["failed"] += 1

        # Fallback usage — tracked regardless of status, since blocked/
        # failed/paywalled/js_required outcomes can still have attempted a
        # fallback (link_ingestion.py's classification stages run before
        # status is finalised here). article still carries these keys from
        # ingest_links() — never popped, so safe to read at this point.
        item_via_rss        = bool(article.get("via_rss_fallback", False))
        item_via_playwright = bool(article.get("via_playwright_fallback", False))
        item_via_manual     = bool(article.get("via_manual_paste", False))
        if item_via_rss:
            stats["via_rss_fallback"] += 1
        if item_via_playwright:
            stats["via_playwright_fallback"] += 1
        if item_via_manual:
            stats["via_manual_paste"] += 1

        duration_secs = 0.0
        if status != "dry_run":
            fields = _read_article_log_fields(article.get("message_id", ""))
            stats["total_input"]       += fields["input_tokens"]
            stats["total_output"]      += fields["output_tokens"]
            stats["total_cache_reads"] += fields["cache_read_tokens"]
            stats["total_cost"]        += cost
            duration_secs = fields["duration_seconds"]

        pw.record_completed(
            title_preview, status, cost, duration_secs,
            via_rss_fallback=item_via_rss,
            via_playwright_fallback=item_via_playwright,
            via_manual_paste=item_via_manual,
        )

        print()

    # Consistency guard
    if stats["total_input"] == 0 and stats["total_output"] == 0:
        stats["total_cost"] = 0.0

    # Run summary (reuses existing run_summary table)
    if not args.dry_run:
        log_run_summary(
            run_started_at     = run_start,
            accounts_processed = ["links"],
            stats              = stats,
            images_enabled     = not args.no_images,
            git_backup_status  = "pending",
        )

    # Regenerate dashboard (skipped when --no-dashboard is set, e.g. when
    # called from main.py all which runs the dashboard step explicitly at the end)
    if not args.dry_run and not args.no_dashboard:
        try:
            from dashboard.generate_dashboard import generate as generate_dashboard
            print("[pipeline] Generating dashboard...")
            generate_dashboard()
        except Exception as exc:
            print(f"[pipeline] Dashboard generation failed (non-fatal): {exc}")

    pw.finish_run(stats)

    _print_report(stats, args)
    return 1 if stats["failed"] > 0 else 0


def _print_report(stats: dict, args: argparse.Namespace) -> None:
    print(f"{'=' * 65}")
    print(f"  Run complete")
    print(f"{'─' * 65}")
    print(f"  Processed:           {stats['processed']}")
    print(f"  Skipped (marketing): {stats['skipped_marketing']}")
    print(f"  Failed:              {stats['failed']}")
    print(f"  Images saved:        {stats['images_saved']}")
    print(f"  Tokens in / out:     {stats['total_input']:,} / {stats['total_output']:,}")
    print(f"  Total cost (USD):    ${stats['total_cost']:.5f}")
    if args.dry_run:
        print(f"\n  [dry-run] No files written, no LLM calls made.")
    print(f"{'=' * 65}\n")


if __name__ == "__main__":
    sys.exit(main())
