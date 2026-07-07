# serve_dashboard.py — Knowledge Base Pipeline
# Local HTTP server for dashboard, live progress page, and research queue UI.
#
# Serves DASHBOARD_DIR (dashboard/) as a static file tree on 127.0.0.1:8420.
# Only binds to loopback — never reachable from outside the local machine.
#
# POST endpoints (support the research queue browser UI):
#
#   POST /api/research
#     Body: {"topics": ["causal ml", "rag pipelines"]}
#     For each topic:
#       1. Validates it exists in research_queue with status='pending'
#       2. Calls research_topic() → Anthropic API (Sonnet, web search)
#       3. Patches "## Context: {topic}" section into the source note file
#       4. Marks queue row as done with cost and timestamp
#     Returns: {"results": [{topic, status, summary, cost_usd, error?}, ...]}
#     One topic is researched per request iteration; all selected topics are
#     sent in a single POST so the server processes them sequentially without
#     needing the client to manage retry state.
#
#   POST /api/skip
#     Body: {"topics": ["causal ml"]}
#     Marks selected queue rows as skipped (no research call, no cost).
#     Returns: {"skipped": ["causal ml"]}
#
#   POST /api/ingest
#     Body: {"url": "https://...", "body_text": "...", "title": "...",
#            "label": "...", "added_date": "2026-06-26"}
#     Writes a JSON file to pipeline/manual_content/ for processing by
#     link_orchestrator.py on the next run (or immediately via /api/run_links).
#     Returns: {"status": "queued", "file": "...", "url": "..."}
#     Source of truth for the bookmarklet — browser sends selected text +
#     current page URL; server writes it to the drop folder.
#
#   POST /api/run_links
#     Body: {} (empty)
#     Triggers python main.py links as a non-blocking subprocess so the
#     caller gets an immediate acknowledgement rather than waiting for the
#     full pipeline run. Returns: {"status": "started"}
#
# Security notes:
#   - Topic strings from the POST body are validated against research_queue
#     before any action — unknown topics are rejected with a per-topic error
#   - No file paths or shell commands are accepted from the client
#   - The only external action triggered is calling the Anthropic API via the
#     existing research_topic() function, which has its own error handling
#   - 127.0.0.1 binding means this is inaccessible from any other machine
#
# Usage:
#   python pipeline\serve_dashboard.py                        # opens progress.html
#   python pipeline\serve_dashboard.py --page index.html      # opens dashboard
#   python pipeline\serve_dashboard.py --page research_queue.html
#   python pipeline\serve_dashboard.py --port 8500            # different port
#   python pipeline\serve_dashboard.py --no-browser           # skip auto-open
# =============================================================================

import argparse
import http.server
import json
import socketserver
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_pipeline_dir = Path(__file__).resolve().parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import DASHBOARD_DIR, NOTES_DIR, PROJECT_ROOT

MANUAL_CONTENT_DIR = PROJECT_ROOT / "pipeline" / "manual_content"
from db import get_research_queue, mark_researched, mark_skipped

DEFAULT_PORT = 8420


# ---------------------------------------------------------------------------
# Note patching helper
# ---------------------------------------------------------------------------

def _patch_note_with_context(note_path: str, topic: str, summary: str) -> bool:
    """
    Append a "## Context: {topic}" section to an existing note file.

    Inserts immediately before "## Tags" if that section exists, otherwise
    appends to the end of the file. Idempotent — skips silently if a Context
    section for this topic already exists in the note.

    Args:
        note_path: Absolute or PROJECT_ROOT-relative path to the .md file.
        topic:     The topic label (used as the section heading).
        summary:   The researched summary text.

    Returns:
        True if the note was patched (or already patched), False if the file
        could not be found or written.
    """
    try:
        # Resolve path — note_path may be stored relative to NOTES_DIR
        path = Path(note_path)
        if not path.is_absolute():
            path = NOTES_DIR / path
        if not path.exists():
            print(f"  [serve] WARNING: note not found for patching: {path}")
            return False

        content = path.read_text(encoding="utf-8")

        # Idempotency check — don't double-insert
        marker = f"## Context: {topic}"
        if marker in content:
            print(f"  [serve] Context section already exists for '{topic}' — skipping patch")
            return True

        section = (
            f"\n## Context: {topic} *(new topic — manually researched)*\n"
            f"{summary}\n"
        )

        # Insert before ## Tags if it exists, otherwise append
        if "\n## Tags" in content:
            content = content.replace("\n## Tags", section + "\n## Tags", 1)
        else:
            content = content.rstrip("\n") + "\n" + section

        path.write_text(content, encoding="utf-8")
        print(f"  [serve] Patched Context section for '{topic}' into {path.name}")
        return True

    except Exception as exc:
        print(f"  [serve] ERROR patching note for '{topic}': {exc}")
        return False


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """
    Static file server for DASHBOARD_DIR, extended with two POST endpoints
    for the research queue UI. All other requests are handled by
    SimpleHTTPRequestHandler's GET/HEAD logic unchanged.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def log_message(self, format, *args):
        # Suppress per-request logging for GET/HEAD (progress.html polls
        # every ~1.5s, which would flood the terminal). POST requests are
        # logged explicitly inside do_POST for visibility.
        pass

    def end_headers(self):
        # Prevent caching of JSON files (progress.json, api responses)
        if self.path.endswith(".json") or self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        """Send a JSON response with CORS header (needed for bookmarklet)."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict | None:
        """Read and parse the POST request body as JSON. Returns None on error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            print(f"  [serve] Bad request body: {exc}")
            return None

    def do_OPTIONS(self):
        """Handle CORS preflight for the bookmarklet (cross-origin POST)."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/research":
            self._handle_research()
        elif self.path == "/api/skip":
            self._handle_skip()
        elif self.path == "/api/ingest":
            self._handle_ingest()
        elif self.path == "/api/run_links":
            self._handle_run_links()
        else:
            self.send_error(404, "Unknown endpoint")

    def _handle_research(self):
        """
        POST /api/research — research selected topics from the queue.

        Processes one topic at a time sequentially. Each topic is validated
        against the queue before any API call is made. Results (success or
        error per topic) are batched and returned together once all are done.
        Expects this can take 10–60s total depending on topic count — the
        research_queue.html page shows a spinner per topic during this time.
        """
        # Lazy import — avoid loading Anthropic SDK at server startup since
        # the server is also used just for progress.html with no research.
        from agents.research import research_topic
        from config import calculate_cost, RESEARCH_MODEL

        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        requested_topics = body.get("topics", [])
        if not requested_topics:
            self._send_json(400, {"error": "No topics specified"})
            return

        print(f"\n[serve] POST /api/research — {len(requested_topics)} topic(s) requested")

        # Validate all topics exist in the queue as 'pending' before touching the API
        pending_in_queue = {
            row["topic"].lower(): row
            for row in get_research_queue(status="pending")
        }

        results = []
        for topic in requested_topics:
            queue_row = pending_in_queue.get(topic.lower())
            if not queue_row:
                print(f"  [serve] SKIP '{topic}' — not found in pending queue")
                results.append({
                    "topic":  topic,
                    "status": "error",
                    "error":  "Topic not found in pending research queue",
                })
                continue

            print(f"  [serve] Researching: '{topic}'")
            try:
                result  = research_topic(topic)
                summary = result.get("summary", "")
                usage   = result.get("usage", {})
                cost    = calculate_cost(RESEARCH_MODEL, usage, batch=False)

                if result.get("error"):
                    print(f"  [serve] FAILED '{topic}' — API error: {result['error']}")
                    results.append({
                        "topic":  topic,
                        "status": "error",
                        "error":  result["error"],
                    })
                    continue   # leave topic as 'pending' in the queue

                # Patch the context section into the source note
                note_path  = queue_row.get("note_path")
                patch_ok   = _patch_note_with_context(note_path, topic, summary) if note_path else False

                mark_researched(topic, cost)

                results.append({
                    "topic":    topic,
                    "status":   "done",
                    "summary":  summary,
                    "cost_usd": cost,
                    "patched":  patch_ok,
                })
                print(f"  [serve] Done: '{topic}' — cost ${cost:.4f}")

            except Exception as exc:
                print(f"  [serve] ERROR researching '{topic}': {exc}")
                results.append({
                    "topic":  topic,
                    "status": "error",
                    "error":  str(exc),
                })

        self._send_json(200, {"results": results})

    def _handle_skip(self):
        """POST /api/skip — mark selected queue topics as skipped."""
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        topics = body.get("topics", [])
        print(f"[serve] POST /api/skip — {len(topics)} topic(s)")
        for topic in topics:
            mark_skipped(topic)
        self._send_json(200, {"skipped": topics})


    def _handle_ingest(self):
        """
        POST /api/ingest — accept manually-pasted article content from the
        browser bookmarklet and write it to pipeline/manual_content/ for
        processing by link_orchestrator.py on the next run.

        Expected body:
            {
              "url":        "https://example.com/article",   # required
              "body_text":  "Full article text...",           # required
              "title":      "Article Title",                  # optional
              "label":      "ai agents",                      # optional
              "added_date": "2026-06-26"                      # optional
            }

        Returns:
            {"status": "queued", "file": "...", "url": "..."}   on success
            {"error": "..."}                                      on failure

        Security: url and body_text are validated for presence. No file paths
        or shell commands are accepted from the client. The written file name
        is derived from a timestamp, not from any client-supplied value.
        """
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        url = (body.get("url") or "").strip()
        body_text = (body.get("body_text") or "").strip()

        if not url or not url.startswith("http"):
            self._send_json(400, {"error": "url is required and must start with http"})
            return
        if not body_text:
            self._send_json(400, {"error": "body_text is required and must not be empty"})
            return

        MANUAL_CONTENT_DIR.mkdir(parents=True, exist_ok=True)

        # Filename is timestamp-based — never derived from client input
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"ingest_{ts}.json"
        output_path = MANUAL_CONTENT_DIR / filename

        payload = {
            "url":        url,
            "body_text":  body_text,
            "title":      (body.get("title") or "").strip(),
            "label":      (body.get("label") or "").strip(),
            "added_date": (body.get("added_date") or "").strip()
                          or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

        try:
            output_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            word_count = len(body_text.split())
            print(f"[serve] POST /api/ingest — queued: {url[:60]} "
                  f"({word_count:,} words) → {filename}")
            self._send_json(200, {"status": "queued", "file": filename, "url": url})
        except Exception as exc:
            print(f"[serve] ERROR writing ingest file: {exc}")
            self._send_json(500, {"error": str(exc)})

    def _handle_run_links(self):
        """
        POST /api/run_links — trigger python main.py links as a background
        subprocess and return immediately. The pipeline run proceeds
        independently; check dashboard/index.html or progress.html for results.

        This allows the bookmarklet to optionally kick off processing right
        after queuing content, without the browser waiting for the full run.
        """
        body = self._read_json_body()
        if body is None:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        # Find main.py relative to this file (pipeline/../main.py)
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        if not main_py.exists():
            self._send_json(500, {"error": f"main.py not found at {main_py}"})
            return

        try:
            subprocess.Popen(
                [sys.executable, str(main_py), "links"],
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            print("[serve] POST /api/run_links — started python main.py links")
            self._send_json(200, {"status": "started"})
        except Exception as exc:
            print(f"[serve] ERROR starting link pipeline: {exc}")
            self._send_json(500, {"error": str(exc)})


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the dashboard folder over HTTP on localhost."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, metavar="PORT",
        help=f"Port to listen on (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--page", type=str, default="progress.html", metavar="PAGE",
        help="Page to open in browser (default: progress.html)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Don't auto-open a browser tab",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    url = f"http://127.0.0.1:{args.port}/{args.page}"

    print(f"\n{'=' * 60}")
    print(f"  Dashboard server")
    print(f"{'─' * 60}")
    print(f"  Serving: {DASHBOARD_DIR}")
    print(f"  URL:     {url}")
    print(f"  API:     POST /api/research  — run research for selected topics")
    print(f"           POST /api/skip      — mark selected topics as skipped")
    print(f"           POST /api/ingest    — queue manually-pasted article content")
    print(f"           POST /api/run_links — trigger link pipeline in background")
    print(f"  Stop with Ctrl+C")
    print(f"{'=' * 60}\n")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        with socketserver.TCPServer(("127.0.0.1", args.port), _DashboardHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve_dashboard] Stopped.")
    except OSError as exc:
        print(f"\n[serve_dashboard] ERROR: Could not bind to port {args.port}: {exc}")
        print(f"  Try a different port: python serve_dashboard.py --port {args.port + 1}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
