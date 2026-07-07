# =============================================================================
# main.py — Newsletter AI Pipeline v1.0
# Unified entry point for all pipeline operations.
#
# Replaces running individual scripts directly. All flags from the individual
# scripts are preserved and passed through to their respective modules.
#
# Usage:
#   python main.py                         # run email pipeline (default)
#   python main.py emails                  # same as above, explicit
#   python main.py links                   # run link pipeline
#   python main.py all                     # emails → links → generate dashboards
#   python main.py dashboard               # regenerate all dashboard HTML files
#   python main.py serve                   # start dashboard server
#   python main.py queue                   # regenerate research_queue.html
#   python main.py --help                  # full usage
#
# Run from project root:
#   python main.py [command] [flags]
#
# Examples:
#   python main.py emails --dry-run --limit 5
#   python main.py emails --account personal --no-research
#   python main.py links --dry-run --limit 10
#   python main.py links --reprocess-failed --no-playwright
#   python main.py all --no-research --no-images
#   python main.py serve --page research_queue.html
#   python main.py serve --port 8421 --no-browser
#   python main.py dashboard
#   python main.py queue
# =============================================================================

from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap — must happen before any pipeline imports
# ---------------------------------------------------------------------------

_ROOT     = Path(__file__).resolve().parent
_PIPELINE = _ROOT / "pipeline"

for _p in (_PIPELINE, _PIPELINE / "dashboard"):
    p_str = str(_p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    """Print a clearly visible section separator."""
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print(f"{'=' * 65}\n")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_emails(args: argparse.Namespace) -> int:
    """
    Run the email ingestion pipeline (orchestrator.py).
    Processes new newsletters from all configured Gmail accounts.
    """
    print("\n── emails ──")

    fwd: list[str] = []
    if args.dry_run:        fwd += ["--dry-run"]
    if args.no_images:      fwd += ["--no-images"]
    if args.no_classify:    fwd += ["--no-classify"]
    if args.no_research:    fwd += ["--no-research"]
    if args.no_backup:      fwd += ["--no-backup"]
    if args.bootstrap:      fwd += ["--bootstrap"]
    if args.limit:          fwd += ["--limit", str(args.limit)]
    if args.account:        fwd += ["--account", args.account]
    # Suppress per-step dashboard regeneration when called from cmd_all,
    # so the explicit dashboard step at the end produces the final state.
    if getattr(args, "_suppress_dashboard", False):
        fwd += ["--no-dashboard"]

    result = subprocess.run(
        [sys.executable, str(_PIPELINE / "orchestrator.py")] + fwd
    )
    return result.returncode


def cmd_links(args: argparse.Namespace) -> int:
    """
    Run the article link pipeline (link_orchestrator.py).
    Fetches, summarises, and writes notes for URLs in links.csv.
    """
    print("\n── links ──")

    fwd: list[str] = []
    if args.dry_run:           fwd += ["--dry-run"]
    if args.no_images:         fwd += ["--no-images"]
    if args.no_classify:       fwd += ["--no-classify"]
    if args.no_research:       fwd += ["--no-research"]
    if args.no_playwright:     fwd += ["--no-playwright"]
    if args.reprocess_failed:  fwd += ["--reprocess-failed"]
    if args.limit:             fwd += ["--limit", str(args.limit)]
    if args.links_file:        fwd += ["--links-file", str(args.links_file)]
    # Suppress per-step dashboard regeneration when called from cmd_all,
    # so the explicit dashboard step at the end produces the final state.
    if getattr(args, "_suppress_dashboard", False):
        fwd += ["--no-dashboard"]

    result = subprocess.run(
        [sys.executable, str(_PIPELINE / "link_orchestrator.py")] + fwd
    )
    return result.returncode


def cmd_dashboard(args: argparse.Namespace) -> int:
    """
    Regenerate all dashboard HTML files from registry.db.
    Writes: dashboard/index.html, link_review.html, research_queue.html
    """
    _section("Dashboard Generator")

    rc = 0

    # generate_dashboard.py lives under pipeline/dashboard/
    try:
        import generate_dashboard
        generate_dashboard.generate()
        _ok("dashboard/index.html written")
    except Exception as exc:
        _err(f"generate_dashboard failed: {exc}")
        rc = 1

    try:
        import generate_link_review
        generate_link_review.generate()
        _ok("dashboard/link_review.html written")
    except Exception as exc:
        _err(f"generate_link_review failed: {exc}")
        rc = 1

    try:
        import generate_research_queue
        generate_research_queue.generate()
        _ok("dashboard/research_queue.html written")
    except Exception as exc:
        _err(f"generate_research_queue failed: {exc}")
        rc = 1

    if rc == 0:
        print(f"\n  Open: file:///{(_ROOT / 'dashboard' / 'index.html').as_posix()}")

    return rc


def cmd_queue(args: argparse.Namespace) -> int:
    """
    Regenerate dashboard/research_queue.html only.
    Useful after a pipeline run with --no-research to review queued topics.
    """
    _section("Research Queue Generator")

    try:
        import generate_research_queue
        generate_research_queue.generate()
        _ok("dashboard/research_queue.html written")
        return 0
    except Exception as exc:
        _err(f"generate_research_queue failed: {exc}")
        return 1


def cmd_serve(args: argparse.Namespace) -> int:
    """
    Start the dashboard HTTP server (serve_dashboard.py).
    Required for the Research and Skip buttons in research_queue.html to work.
    Blocks until Ctrl+C.

    Invoked via subprocess.run() (not import) to avoid any module-level
    name collision with main() definitions in other pipeline scripts.
    """
    _section("Dashboard Server")

    fwd: list[str] = []
    if args.port:       fwd += ["--port", str(args.port)]
    if args.page:       fwd += ["--page", args.page]
    if args.no_browser: fwd += ["--no-browser"]

    result = subprocess.run(
        [sys.executable, str(_PIPELINE / "serve_dashboard.py")] + fwd
    )
    return result.returncode or 0


def _open_dashboard_via_server(args: argparse.Namespace) -> None:
    """
    Start a minimal HTTP server on the main thread, open the browser, then
    block with serve_forever() until Ctrl+C.

    Previously used a daemon thread, which caused ERR_CONNECTION_REFUSED:
    the daemon thread dies the instant the main thread exits, so the server
    was gone before the browser could connect.  Running serve_forever() on
    the main thread (same as serve_dashboard.py) keeps the process alive.

    No import of serve_dashboard.py is needed, avoiding any main() collision.
    """
    import socketserver
    import http.server
    from config import DASHBOARD_DIR

    port = getattr(args, "port", None) or 8420

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(DASHBOARD_DIR), **kw)
        def log_message(self, fmt, *a):
            pass  # suppress per-request console noise

    url = f"http://127.0.0.1:{port}/index.html"

    try:
        with socketserver.TCPServer(("127.0.0.1", port), _Handler) as httpd:
            print(f"\n  Opening dashboard: {url}")
            print(f"  Stop with Ctrl+C\n")
            webbrowser.open(url)
            httpd.serve_forever()          # blocks here — keeps server alive
    except OSError as exc:
        # Port already in use (e.g. serve_dashboard.py is already running).
        # Just open the browser; the existing server will handle the request.
        print(f"\n  Port {port} already in use — opening {url} via existing server.")
        webbrowser.open(url)
    except KeyboardInterrupt:
        print("\n  [server] Stopped.")


def cmd_all(args: argparse.Namespace) -> int:
    """
    Run the full pipeline in sequence:
      1. Email pipeline   (orchestrator.py)
      2. Link pipeline    (link_orchestrator.py)  — with --no-dashboard suppression
      3. Dashboard regeneration (all three HTML files)

    Stops on first failure unless --continue-on-error is set.
    """
    _section("Full Pipeline Run")

    # Suppress per-step dashboard generation inside link_orchestrator.py —
    # the explicit dashboard step at the end regenerates everything once in
    # the correct final state. cmd_links() reads this flag and forwards
    # --no-dashboard to the subprocess.
    args._suppress_dashboard = True

    steps = [
        ("emails",    cmd_emails),
        ("links",     cmd_links),
        ("dashboard", cmd_dashboard),
    ]

    overall_rc = 0
    for name, fn in steps:
        print(f"\n── Step: {name} ──\n")
        rc = fn(args)
        if rc != 0:
            _err(f"Step '{name}' exited with code {rc}")
            overall_rc = rc
            if not args.continue_on_error:
                _err("Stopping. Pass --continue-on-error to run remaining steps anyway.")
                return overall_rc
        else:
            _ok(f"Step '{name}' completed")

    if overall_rc == 0 and not args.no_browser:
        _open_dashboard_via_server(args)

    return overall_rc


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Newsletter AI Pipeline v1.0 — unified entry point",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            "  emails     Run email ingestion pipeline (default)\n"
            "  links      Run article link pipeline\n"
            "  all        emails → links → dashboard (full run)\n"
            "  dashboard  Regenerate all dashboard HTML files\n"
            "  queue      Regenerate research_queue.html only\n"
            "  serve      Start dashboard HTTP server\n"
            "\n"
            "Examples:\n"
            "  python main.py                              # run email pipeline\n"
            "  python main.py emails --dry-run --limit 5\n"
            "  python main.py emails --account personal --no-research\n"
            "  python main.py links --dry-run\n"
            "  python main.py links --reprocess-failed --no-playwright\n"
            "  python main.py all --no-research --no-images\n"
            "  python main.py all --continue-on-error\n"
            "  python main.py dashboard\n"
            "  python main.py queue\n"
            "  python main.py serve\n"
            "  python main.py serve --page research_queue.html\n"
            "  python main.py serve --port 8421 --no-browser\n"
        ),
    )

    # ── Sub-command ──────────────────────────────────────────────────────────
    parser.add_argument(
        "command",
        nargs="?",
        default="emails",
        choices=["emails", "links", "all", "dashboard", "queue", "serve"],
        metavar="COMMAND",
        help="What to run (default: emails)",
    )

    # ── Shared flags (emails + links + all) ──────────────────────────────────
    shared = parser.add_argument_group("shared flags — emails, links, all")
    shared.add_argument("--dry-run",      action="store_true",
                        help="Fetch and log only — no LLM calls, no notes written")
    shared.add_argument("--no-images",    action="store_true",
                        help="Skip image extraction (Agent 4)")
    shared.add_argument("--no-classify",  action="store_true",
                        help="Skip classification — all items go to summarisation")
    shared.add_argument("--no-research",  action="store_true",
                        help="Skip web research (Agent 5); new topics go to queue")
    shared.add_argument("--limit",        type=int, default=None, metavar="N",
                        help="Process at most N emails/links per run")

    # ── Email-only flags ─────────────────────────────────────────────────────
    email_grp = parser.add_argument_group("email flags — emails, all")
    email_grp.add_argument("--account",   type=str, default=None, metavar="ALIAS",
                            help="Process one account only (e.g. personal, work)")
    email_grp.add_argument("--no-backup", action="store_true",
                            help="Skip git backup after run")
    email_grp.add_argument("--bootstrap", action="store_true",
                            help="Bootstrap mode: reprocess all historical emails")

    # ── Link-only flags ──────────────────────────────────────────────────────
    link_grp = parser.add_argument_group("link flags — links, all")
    link_grp.add_argument("--reprocess-failed", action="store_true",
                           help="Retry URLs previously marked as failed")
    link_grp.add_argument("--no-playwright",     action="store_true",
                           help="Skip Playwright fallback (JS-gated pages → js_required)")
    link_grp.add_argument("--links-file",        type=Path, default=None, metavar="PATH",
                           help="Override links CSV path (default: pipeline/links.csv)")

    # ── all-only flags ───────────────────────────────────────────────────────
    all_grp = parser.add_argument_group("all flags")
    all_grp.add_argument("--continue-on-error", action="store_true",
                          help="Keep running remaining steps even if one fails")

    # ── serve flags ──────────────────────────────────────────────────────────
    serve_grp = parser.add_argument_group("serve flags")
    serve_grp.add_argument("--port",       type=int, default=None, metavar="PORT",
                            help="Port for dashboard server (default: 8420)")
    serve_grp.add_argument("--page",       type=str, default=None, metavar="PAGE",
                            help="Page to open in browser (default: progress.html)")
    serve_grp.add_argument("--no-browser", action="store_true",
                            help="Don't auto-open a browser tab (applies to serve and all)")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = _build_parser()
    args   = parser.parse_args()

    dispatch = {
        "emails":    cmd_emails,
        "links":     cmd_links,
        "all":       cmd_all,
        "dashboard": cmd_dashboard,
        "queue":     cmd_queue,
        "serve":     cmd_serve,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return 1

    return fn(args)


if __name__ == "__main__":
    sys.exit(main())