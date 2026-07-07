# agents/link_ingestion.py — Knowledge Base Pipeline v1.0
# Agent 1L: Link Ingestion
#
# Responsibilities:
#   - Read URLs from a CSV input file (pipeline/links.csv by default)
#   - Skip URLs already processed in prior runs (deduplication via link_log)
#   - Fetch each page with browser-like headers
#   - Extract clean article text using a Readability-style heuristic
#     (strip nav, sidebars, footers, ads — keep article body)
#   - Two-layer fallback chain for pages that return 0 words via the normal
#     fetch, tried in this order:
#       1. Playwright headless browser (any domain, gated by --no-playwright)
#          — tried FIRST because it recovers real rendered HTML, including
#          working <img> tags that Agent 4 (image extraction) can use
#          downstream. Generally the more complete recovery of the two.
#       2. Substack RSS feed (Substack-hosted pages only, predictable at
#          {host}/feed) — tried only if Playwright didn't recover content
#          (disabled, unavailable, or genuinely failed). Text-only: no
#          images are recoverable via this path. Best-effort — not all
#          posts remain in the feed.
#     Both fallback outcomes (via_rss_fallback, via_playwright_fallback) are
#     persisted as columns on the link_log row for every fetch attempt, not
#     just carried in-memory — see log_link_fetch().
#   - Detect and classify fetch failures:
#       fetched      — full content retrieved (>= MIN_CONTENT_WORDS)
#       partial      — some content retrieved but below full-confidence
#                      threshold (e.g. Medium-style free preview before a
#                      paywall gate) — saved anyway, flagged as incomplete
#       js_required  — page returned 0 words after extraction AND both
#                      fallbacks (Playwright, then Substack RSS where
#                      applicable) found no recoverable content; almost
#                      always means the site requires JavaScript rendering
#                      to produce its real content — NOT the same as a
#                      subscription paywall, flagged separately for manual
#                      review/re-fetch
#       blocked      — server returned 403/401/429 (bot protection)
#       failed       — network error, timeout, or DNS failure
#   - Return a list of article dicts using the same schema as email dicts
#     so all downstream agents (classification, summarisation, topic linking,
#     image extraction, research, local writer) work unchanged
#
# ingest_manual() — companion function for manually-pasted content:
#   - Reads JSON files from pipeline/manual_content/ written by the
#     /api/ingest endpoint in serve_dashboard.py (or placed manually)
#   - Builds article dicts with fetch_status="manual", via_manual_paste=True
#   - Moves processed files to pipeline/manual_content/processed/ to prevent
#     re-processing on subsequent runs
#   - Zero network calls — content is already present in the JSON payload
#   - Write a session-level run log per call: logs/link_fetch_{timestamp}.log
#     (human-readable, per-URL detail + run summary) and a companion .json
#     summary, in addition to the durable per-URL rows in link_log — see
#     _RunLogger and the ingest_links() docstring.
#
# Zero LLM calls — all processing is local.
#
# Usage (standalone test):
#   cd pipeline && python agents/link_ingestion.py
# =============================================================================

import csv
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config import PROJECT_ROOT, LOGS_DIR
from db import get_connection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LINKS_FILE   = _pipeline_dir / "links.csv"
MANUAL_CONTENT_DIR   = _pipeline_dir / "manual_content"

# Minimum word count after extraction to consider an article "fetched"
# with FULL confidence (no caveats).
MIN_CONTENT_WORDS = 300

# Minimum word count to still save as a PARTIAL/preview article rather than
# discarding entirely. Sites like Medium and its partner network
# (gitconnected.com, gopubby.com, etc.) serve a free preview of an article
# before a "Member-only story" gate — typically 100-250 words. That preview
# is genuine, readable content; discarding it entirely loses information
# that's still useful to have in the knowledge base, even if incomplete.
# Below this threshold, content is too thin to be worth keeping at all
# (likely just a page title + nav scraps, not real preview prose).
MIN_PREVIEW_WORDS = 80

# ---------------------------------------------------------------------------
# Substack RSS fallback
#
# Substack-hosted blogs commonly serve a JavaScript-dependent shell to
# non-browser HTTP clients (requests/BeautifulSoup), which is why these
# pages frequently extract to 0 words. However, Substack publications
# almost universally expose a full-content RSS feed at a predictable path
# ({base_url}/feed), and that feed's <content:encoded> element typically
# contains the COMPLETE post body — including for posts that would show a
# paywall on the rendered web page, because feed delivery historically
# isn't gated the same way the web view is.
#
# This is a targeted fallback, not a primary fetch path: it is only
# attempted when (a) the URL's domain is recognised as Substack-hosted and
# (b) the normal requests-based extraction returned 0 words. The vast
# majority of URLs never touch this code path at all.
# ---------------------------------------------------------------------------

# Domain patterns that reliably indicate a Substack-hosted publication.
# Covers both custom domains Substack still serves under the hood (detected
# via the generator meta tag at fetch time — see _is_substack_html) and the
# common *.substack.com / known-alias hostname patterns seen in practice.
_SUBSTACK_HOST_HINTS = ("substack.com",)

# Substack RSS feed XML namespaces (standard RSS 2.0 + Content module)
_RSS_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
}

# Maximum number of feed items to scan when looking for a URL match.
# Substack feeds typically list the most recent ~20-50 posts; older posts
# fall off the feed entirely, so this fallback only helps for reasonably
# recent articles — which is the common case for newsletter content anyway.
_RSS_FEED_ITEM_SCAN_LIMIT = 100

# ---------------------------------------------------------------------------
# Playwright headless-browser fallback
#
# A second, heavier fallback layer for 0-word pages that the RSS fallback
# either doesn't apply to (non-Substack domains) or couldn't recover
# (post too old to still be in the feed). A real headless browser executes
# the page's JavaScript and waits for content to render, then hands back
# the fully-rendered HTML — fixing the root cause (no JS execution) rather
# than working around it the way the RSS fallback does.
#
# This is intentionally the LAST resort in the fallback chain:
#   requests (fast, ~100ms) → RSS feed (fast, Substack-only) →
#   Playwright (slow, ~3-8s per page, works on any JS-gated site)
#
# Playwright is an optional dependency. If it isn't installed, this
# fallback is silently skipped (logged once per run, not per-URL) rather
# than crashing the pipeline — the rest of link_ingestion has zero hard
# dependency on it.
# ---------------------------------------------------------------------------

# How long to wait for the page to finish its initial JS-driven render
# before giving up and extracting whatever DOM state exists. Substack and
# similar SSR-with-JS-hydration sites typically finish well under this.
PLAYWRIGHT_TIMEOUT_MS = 15_000

# Wait strategy passed to page.goto(). "networkidle" waits until there have
# been no network connections for 500ms — more reliable than a fixed sleep
# for sites that lazy-load content, at the cost of being slower than
# "domcontentloaded" on pages with persistent background connections
# (analytics beacons, websockets, etc).
PLAYWRIGHT_WAIT_UNTIL = "networkidle"

# Browser-level User-Agent override — Playwright's default Chromium UA
# includes a "HeadlessChrome" marker that some sites specifically detect
# and block. Overriding it to a standard desktop Chrome UA avoids that
# specific detection vector without claiming to be a different browser.
# Kept as an independent constant (not derived from _HEADERS below) since
# _HEADERS is defined later in this file and Python doesn't resolve
# forward references in module-level constant assignments.
_PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class _PlaywrightBrowserManager:
    """
    Lazily launches and reuses a single headless Chromium instance across
    an entire ingest_links() run, rather than paying browser startup cost
    (typically 1-2s) on every fallback invocation.

    Usage pattern:
        manager = _PlaywrightBrowserManager()
        html = manager.fetch(url)   # launches browser on first call only
        ...
        manager.close()             # call once at the end of the run

    If the playwright package isn't installed, or browser binaries haven't
    been downloaded (`playwright install chromium`), is_available() returns
    False after the first attempt and every fetch() call returns None
    immediately without retrying the failed import/launch.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._unavailable_reason: str | None = None
        self._attempted_launch = False

    def _ensure_launched(self) -> bool:
        """
        Launch the browser on first use. Returns True if a usable browser
        context is ready, False if Playwright is unavailable for any reason
        (not installed, browser binaries missing, launch failure).
        """
        if self._context is not None:
            return True
        if self._attempted_launch:
            # Already tried and failed this run — don't retry per-URL.
            return False

        self._attempted_launch = True

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._unavailable_reason = (
                "playwright package not installed. Install with: "
                "pip install playwright --break-system-packages && "
                "playwright install chromium"
            )
            return False

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                user_agent=_PLAYWRIGHT_USER_AGENT,
                viewport={"width": 1280, "height": 1024},
            )
            return True
        except Exception as exc:
            # Most commonly: browser binaries not downloaded yet. Playwright
            # raises a fairly verbose error in that case; we surface a
            # trimmed, actionable version rather than the full traceback.
            self._unavailable_reason = (
                f"Playwright browser launch failed: {exc}. "
                f"If this is a missing-binary error, run: "
                f"playwright install chromium"
            )
            self._context = None
            return False

    def is_available(self) -> bool:
        """
        Check whether the browser is (or can be) launched, without
        necessarily launching it yet. Triggers the lazy launch attempt.
        """
        return self._ensure_launched()

    @property
    def unavailable_reason(self) -> str | None:
        """Human-readable reason the browser couldn't be used, if any."""
        return self._unavailable_reason

    def fetch(self, url: str) -> str | None:
        """
        Load a URL in the headless browser, wait for JS rendering to
        settle, and return the fully-rendered HTML.

        Returns None if the browser is unavailable, or if navigation fails
        (timeout, DNS error, the page itself errors out, etc) — callers
        treat None the same as any other fallback-exhausted case.
        """
        if not self._ensure_launched():
            return None

        page = None
        try:
            page = self._context.new_page()
            page.goto(url, timeout=PLAYWRIGHT_TIMEOUT_MS, wait_until=PLAYWRIGHT_WAIT_UNTIL)
            return page.content()
        except Exception as exc:
            # Covers Playwright's TimeoutError and any other navigation
            # failure. Logged by the caller, not here, to keep this class
            # free of per-URL print statements.
            self._last_fetch_error = str(exc)
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass  # best-effort cleanup; never let this mask the real result

    def close(self) -> None:
        """
        Tear down the browser, context, and Playwright driver process.
        Call once after the full ingest_links() run completes. Safe to
        call even if the browser was never launched.
        """
        for resource, name in (
            (self._context, "context"),
            (self._browser, "browser"),
            (self._playwright, "playwright"),
        ):
            if resource is not None:
                try:
                    resource.stop() if name == "playwright" else resource.close()
                except Exception:
                    pass  # best-effort cleanup on shutdown
        self._context = None
        self._browser = None
        self._playwright = None


# Request timeout in seconds
FETCH_TIMEOUT = 20

# Delay between requests (seconds) — polite crawling
REQUEST_DELAY = 1.5

# Browser-like headers to reduce bot detection rejections
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "DNT":             "1",
}

# HTML tags whose content is always stripped (nav, boilerplate, tracking)
_STRIP_TAGS = {
    "nav", "header", "footer", "aside", "script", "style", "noscript",
    "iframe", "form", "button", "input", "select", "textarea",
    "advertisement", "ads", "cookie-banner", "popup",
}

# CSS class / id substrings that signal boilerplate containers
_BOILERPLATE_PATTERNS = [
    "nav", "menu", "sidebar", "footer", "header", "banner", "advertisement",
    "cookie", "popup", "modal", "overlay", "subscribe", "newsletter-signup",
    "related-posts", "share", "social", "comment", "breadcrumb", "pagination",
    "tag-cloud", "widget", "promo", "advert", "sponsor",
]

# HTTP status codes that indicate bot blocking
_BLOCKED_STATUS_CODES = {401, 403, 407, 429}


# ---------------------------------------------------------------------------
# Session-level run log
# ---------------------------------------------------------------------------
#
# Separate from link_log in registry.db. The database is the durable,
# queryable record across all runs (what get_link_stats() / the dashboard
# read from); this is a plain-text, human-readable log of ONE run, written
# to its own timestamped file so a specific session's activity can be
# reviewed (e.g. "what happened on the run I kicked off this morning")
# without writing SQL against registry.db.
#
# One file per call to ingest_links() — not appended across runs. Filename
# pattern: logs/link_fetch_{UTC timestamp}.log, e.g.
# logs/link_fetch_2026-06-21T140000Z.log
# ---------------------------------------------------------------------------

class _RunLogger:
    """
    Accumulates per-URL fetch outcomes during a single ingest_links() run
    and writes them to a single timestamped log file, followed by a
    run-level summary block, when the run finishes.

    Usage:
        logger = _RunLogger()
        for url in urls:
            ...
            logger.record(url=..., fetch_status=..., word_count=...,
                          via_rss_fallback=..., via_playwright_fallback=...,
                          http_status_code=..., error_message=...)
        logger.finalise()   # writes the file; safe to call even with 0 entries
    """

    def __init__(self, logs_dir: Path = LOGS_DIR):
        self.logs_dir   = logs_dir
        self.started_at = datetime.now(timezone.utc)
        self.entries: list[dict] = []
        # Filename timestamp uses a filesystem-safe format (no colons) —
        # Windows doesn't allow ':' in filenames. ISO 8601 basic format
        # without separators, with a trailing Z to keep it readable as UTC.
        self._timestamp_str = self.started_at.strftime("%Y-%m-%dT%H%M%SZ")
        self.log_path = self.logs_dir / f"link_fetch_{self._timestamp_str}.log"

    def record(
        self,
        url:                      str,
        fetch_status:             str,
        word_count:               int,
        via_rss_fallback:         bool,
        via_playwright_fallback:  bool,
        http_status_code:         int | None = None,
        error_message:            str | None = None,
        page_title:               str | None = None,
    ) -> None:
        """Record one URL's fetch outcome. Call once per URL processed."""
        self.entries.append({
            "url":                      url,
            "fetch_status":             fetch_status,
            "word_count":               word_count,
            "via_rss_fallback":         bool(via_rss_fallback),
            "via_playwright_fallback":  bool(via_playwright_fallback),
            "http_status_code":         http_status_code,
            "error_message":            error_message,
            "page_title":               page_title,
            "recorded_at":              datetime.now(timezone.utc).isoformat(),
        })

    def _build_summary(self) -> dict:
        """
        Compute run-level totals from the accumulated entries, including
        the % of links that needed each fallback — the same figure the
        dashboard tile shows, computed here independently from in-memory
        data rather than re-querying the DB, since this log is meant to
        be readable standalone without registry.db.
        """
        total = len(self.entries)
        if total == 0:
            return {
                "total_urls": 0,
                "by_status": {},
                "via_rss_fallback_count": 0,
                "via_playwright_fallback_count": 0,
                "via_rss_fallback_pct": 0.0,
                "via_playwright_fallback_pct": 0.0,
                "any_fallback_pct": 0.0,
            }

        by_status: dict[str, int] = {}
        rss_count = 0
        pw_count  = 0
        any_fallback_count = 0
        for e in self.entries:
            by_status[e["fetch_status"]] = by_status.get(e["fetch_status"], 0) + 1
            if e["via_rss_fallback"]:
                rss_count += 1
            if e["via_playwright_fallback"]:
                pw_count += 1
            if e["via_rss_fallback"] or e["via_playwright_fallback"]:
                any_fallback_count += 1

        return {
            "total_urls":                    total,
            "by_status":                     by_status,
            "via_rss_fallback_count":        rss_count,
            "via_playwright_fallback_count": pw_count,
            "via_rss_fallback_pct":          round(100 * rss_count / total, 1),
            "via_playwright_fallback_pct":   round(100 * pw_count / total, 1),
            "any_fallback_pct":              round(100 * any_fallback_count / total, 1),
        }

    def finalise(self) -> Path | None:
        """
        Write the log file: one line per URL in the order processed,
        followed by a run-level summary block. Returns the path written,
        or None if there were zero entries (no file is written for an
        empty run — nothing happened, nothing to log).

        Errors writing the log file are caught and logged to console as a
        warning, never raised — a logging failure must not fail the run
        that's already completed successfully otherwise.
        """
        if not self.entries:
            return None

        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            ended_at = datetime.now(timezone.utc)
            summary  = self._build_summary()

            lines = []
            lines.append("=" * 79)
            lines.append("LINK INGESTION RUN LOG")
            lines.append("=" * 79)
            lines.append(f"Started:  {self.started_at.isoformat()}")
            lines.append(f"Finished: {ended_at.isoformat()}")
            lines.append(f"Duration: {(ended_at - self.started_at).total_seconds():.1f}s")
            lines.append("")
            lines.append("-" * 79)
            lines.append("PER-URL DETAIL")
            lines.append("-" * 79)
            for i, e in enumerate(self.entries, 1):
                fallback_tag = ""
                if e["via_playwright_fallback"]:
                    fallback_tag = " [via Playwright]"
                elif e["via_rss_fallback"]:
                    fallback_tag = " [via RSS]"
                title_part = f' | "{e["page_title"][:60]}"' if e.get("page_title") else ""
                error_part = f' | {e["error_message"]}' if e.get("error_message") else ""
                lines.append(
                    f"[{i:>3}/{len(self.entries)}] {e['fetch_status']:<12}"
                    f" | {e['word_count']:>5} words{fallback_tag} | {e['url']}"
                    f"{title_part}{error_part}"
                )
            lines.append("")
            lines.append("-" * 79)
            lines.append("RUN SUMMARY")
            lines.append("-" * 79)
            lines.append(f"Total URLs processed     : {summary['total_urls']}")
            for status, count in sorted(summary["by_status"].items()):
                lines.append(f"  {status:<14}         : {count}")
            lines.append("")
            lines.append(f"Recovered via RSS fallback        : {summary['via_rss_fallback_count']} "
                          f"({summary['via_rss_fallback_pct']}%)")
            lines.append(f"Recovered via Playwright fallback  : {summary['via_playwright_fallback_count']} "
                          f"({summary['via_playwright_fallback_pct']}%)")
            lines.append(f"Recovered via any fallback         : {summary['any_fallback_pct']}% of URLs this run")
            lines.append("=" * 79)

            self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            # Also write a machine-readable JSON summary alongside the
            # human-readable .log, sharing the same base filename. This is
            # what the dashboard tile reads (see generate_dashboard.py) —
            # parsing the .log text would be brittle; the .json is the
            # stable contract. The dashboard tile itself is sourced from
            # registry.db (so it covers a rolling 30-day window across many
            # runs, not just the latest one) but per-run JSON summaries are
            # written here too in case a future feature wants per-run
            # figures without querying the DB.
            summary_path = self.log_path.with_suffix(".json")
            summary_payload = {
                "started_at":  self.started_at.isoformat(),
                "finished_at": ended_at.isoformat(),
                "log_file":    self.log_path.name,
                **summary,
            }
            summary_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

            print(f"[link_ingestion] Run log written: {self.log_path}")
            return self.log_path

        except Exception as exc:
            print(f"[link_ingestion] WARNING: failed to write run log: {exc}")
            return None


# ---------------------------------------------------------------------------
# URL deduplication
# ---------------------------------------------------------------------------

def get_processed_urls(reprocess_failed: bool = False) -> set[str]:
    """
    Return the set of URLs already in link_log that should not be retried.

    By default includes: fetched, partial, blocked, paywalled, js_required.
    Excludes "failed" so transient network errors are retried on the next run.

    "partial" is included here (not retried) because re-fetching a
    Medium-style metered preview will almost always return the same
    preview, not more content — there's nothing to gain from retrying.

    "js_required" is included here (not retried) for the same reason as
    "blocked"/"paywalled": it's not a transient failure, so automatic
    retries won't help. Use --reprocess-failed to retry "failed" URLs only;
    js_required/blocked/paywalled/partial URLs require either a manual fix
    (e.g. a different fetch method) or are expected to stay that way.

    Args:
        reprocess_failed: If True, also retry previously failed URLs.
                          Passed via --reprocess-failed CLI flag.

    Returns:
        Set of URL strings to skip during ingestion.
    """
    _STABLE_STATUSES = "'fetched','partial','blocked','paywalled','js_required'"

    conn = get_connection()
    try:
        if reprocess_failed:
            # Skip stable statuses, retry failed
            rows = conn.execute(
                f"SELECT url FROM link_log WHERE fetch_status IN ({_STABLE_STATUSES})"
            ).fetchall()
        else:
            # Skip all previously attempted URLs, including failed
            rows = conn.execute(
                f"SELECT url FROM link_log WHERE fetch_status IN ({_STABLE_STATUSES},'failed')"
            ).fetchall()
        return {row["url"] for row in rows}
    except Exception:
        # Table may not exist yet on first run — return empty set
        return set()
    finally:
        conn.close()



def get_manual_processed_urls() -> set[str]:
    """
    Return the set of URLs already successfully ingested via ingest_manual()
    (i.e. fetch_status = 'manual' in link_log).

    Used exclusively by ingest_manual() for deduplication. Intentionally
    narrower than get_processed_urls() — blocked, paywalled, js_required,
    and partial rows are NOT included, because the entire point of manual
    paste is to recover content the automated fetch pipeline could not get.
    A prior failed/blocked/js_required row is a reason TO process via paste,
    not a reason to skip.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT url FROM link_log WHERE fetch_status = 'manual'"
        ).fetchall()
        return {row["url"] for row in rows}
    except Exception:
        return set()
    finally:
        conn.close()


def make_link_id(url: str) -> str:
    """
    Generate a stable unique ID for a URL, used as the message_id equivalent
    in processing_log and as the link_log primary key.

    Format: "link:{md5_hex[:16]}"
    Example: "link:a3f9d12e01b2c3d4"
    """
    return "link:" + hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CSV reader
# ---------------------------------------------------------------------------

def read_links_csv(csv_path: Path) -> list[dict]:
    """
    Read URLs from the links CSV file.

    Expected columns (header row required):
        url         — required; the article URL
        label       — optional; seed tag(s) for the summarisation agent
        added_date  — optional; ISO date string (YYYY-MM-DD)

    Extra columns are ignored. Rows with empty or non-http URLs are skipped.

    Returns:
        List of dicts with keys: url, label, added_date
    """
    if not csv_path.exists():
        print(f"[link_ingestion] Links file not found: {csv_path}")
        print(f"  Create it with columns: url, label, added_date")
        return []

    links = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            url = row.get("url", "").strip()
            if not url:
                continue
            if not url.startswith("http"):
                print(f"  [link_ingestion] Row {i}: skipping non-http URL: {url[:60]}")
                continue
            links.append({
                "url":        url,
                "label":      row.get("label", "").strip(),
                "added_date": row.get("added_date", "").strip(),
            })

    print(f"[link_ingestion] Read {len(links)} URL(s) from {csv_path.name}")
    return links


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_url(url: str) -> tuple[str | None, str, int, str | None]:
    """
    Fetch a URL and return its HTML content.

    Returns:
        (html_content, fetch_status, http_status_code, error_detail)
        fetch_status:  "ok" | "blocked" | "failed"
        html_content:  None if fetch failed.
        error_detail:  None on success. On failure, a specific reason string
                       distinguishing timeout / DNS failure / connection
                       refused / other network error — NOT just "HTTP 0".

    BUG FIX (v5.6): previously every requests exception (Timeout,
    ConnectionError, generic RequestException) was collapsed into the same
    (None, "failed", 0) return value, with the caller logging error_message
    as a literal "HTTP 0" string regardless of which exception actually
    fired. This made it impossible to distinguish "this domain is just slow
    and 20s wasn't enough" from "this domain's DNS doesn't resolve at all"
    from "connection actively refused" when reviewing link_log after a run
    — all three produced identical, undiagnosable log rows. Each exception
    branch now returns a distinct, specific error_detail string.
    """
    try:
        response = requests.get(
            url,
            headers=_HEADERS,
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
            stream=False,
        )

        if response.status_code in _BLOCKED_STATUS_CODES:
            return None, "blocked", response.status_code, None

        if response.status_code != 200:
            return None, "failed", response.status_code, None

        # Validate content type — only process HTML pages
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None, "failed", response.status_code, f"Non-HTML content-type: {content_type or '(none)'}"

        return response.text, "ok", response.status_code, None

    except requests.exceptions.Timeout:
        return None, "failed", 0, f"Timeout after {FETCH_TIMEOUT}s — server too slow to respond"
    except requests.exceptions.TooManyRedirects:
        return None, "blocked", 0, "Too many redirects — possible redirect loop or bot-detection bounce"
    except requests.exceptions.SSLError as exc:
        return None, "failed", 0, f"SSL/TLS error: {exc}"
    except requests.exceptions.ConnectionError as exc:
        # ConnectionError covers DNS resolution failures, connection refused,
        # and connection reset — all genuinely different root causes, so we
        # surface requests' own message rather than a single generic label.
        return None, "failed", 0, f"Connection error: {exc}"
    except requests.exceptions.RequestException as exc:
        return None, "failed", 0, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Substack RSS fallback
# ---------------------------------------------------------------------------

def _is_substack_html(html: str | None) -> bool:
    """
    Detect whether a fetched (possibly empty-shell) HTML page is
    Substack-hosted, including custom domains that don't contain
    "substack.com" in the URL itself.

    Substack always emits a recognisable <meta name="generator"
    content="Substack"> tag (or an og:site_name / similar marker) even in
    the stripped-down shell served to non-browser clients, since that part
    of the <head> doesn't depend on JS rendering. This is far more reliable
    than URL-based detection alone, which would miss any custom domain
    (e.g. a blog that doesn't have "substack.com" anywhere in its hostname).

    Args:
        html: Raw HTML string, or None if the fetch failed entirely.

    Returns:
        True if Substack markers are present, False otherwise (including
        when html is None — nothing to detect).
    """
    if not html:
        return False
    # Cheap substring checks on the raw HTML — avoids a full BeautifulSoup
    # parse just for detection. Substack's shell HTML reliably contains
    # one or both of these regardless of JS execution state.
    lowered = html[:5000].lower()  # generator/meta tags are always in <head>
    return (
        'content="substack"' in lowered
        or "substackcdn.com" in lowered
        or "substack-post-media" in lowered
    )


def _build_substack_feed_url(article_url: str) -> str:
    """
    Construct the RSS feed URL for a Substack-hosted article URL.

    Substack publications — whether on a *.substack.com subdomain or a
    custom domain — universally expose their feed at {scheme}://{host}/feed,
    regardless of the specific post path. This holds for custom domains
    like blog.bytebytego.com just as much as for *.substack.com addresses.

    Args:
        article_url: The original article URL (e.g.
                     "https://blog.bytebytego.com/p/how-llms-see-the-world").

    Returns:
        Feed URL string (e.g. "https://blog.bytebytego.com/feed").
    """
    parsed = urlparse(article_url)
    return f"{parsed.scheme}://{parsed.netloc}/feed"


def _fetch_substack_feed(feed_url: str) -> str | None:
    """
    Fetch the raw RSS feed XML for a Substack publication.

    Uses the same browser-like headers as the main fetch path. Feed
    endpoints are far less likely to be JS-gated or bot-blocked than the
    rendered HTML page, since feed delivery is meant for RSS readers,
    not browsers — but failures here are still handled gracefully.

    Returns:
        Raw XML string, or None on any failure (network error, non-200
        status, non-XML content type).
    """
    try:
        response = requests.get(
            feed_url, headers=_HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True,
        )
        if response.status_code != 200:
            return None
        content_type = response.headers.get("Content-Type", "")
        if "xml" not in content_type and "rss" not in content_type:
            # Some misconfigured servers omit a proper content-type header;
            # fall back to a cheap content sniff rather than rejecting outright.
            if not response.text.lstrip().startswith("<?xml") and "<rss" not in response.text[:500]:
                return None
        return response.text
    except requests.exceptions.RequestException:
        return None


def _extract_from_substack_feed(article_url: str) -> tuple[str, str] | None:
    """
    Attempt to retrieve full article content for a Substack URL via its
    RSS feed, as a fallback when the normal HTML fetch returns 0 words.

    Matching strategy: Substack feed <item><link> elements contain the
    canonical post URL, which is compared against article_url after
    normalising both (stripping query strings, trailing slashes, and
    scheme differences) since feed links and the originally-saved URL can
    differ in minor formatting even though they point to the same post.

    Args:
        article_url: The original article URL that returned 0 words via
                     the normal fetch path.

    Returns:
        (clean_text, page_title) tuple if a matching feed item with usable
        content was found, otherwise None (caller falls through to the
        existing js_required handling — this is a best-effort bonus, not
        a guaranteed recovery path).
    """
    feed_url = _build_substack_feed_url(article_url)
    xml_text = _fetch_substack_feed(feed_url)
    if not xml_text:
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    target_normalised = _normalise_url_for_match(article_url)

    items = root.findall(".//item")[:_RSS_FEED_ITEM_SCAN_LIMIT]
    for item in items:
        link_el = item.find("link")
        if link_el is None or not link_el.text:
            continue

        if _normalise_url_for_match(link_el.text) != target_normalised:
            continue

        # Found the matching item — extract title and full content.
        title_el = item.find("title")
        page_title = (title_el.text or "").strip() if title_el is not None else ""

        # content:encoded carries the full HTML body; fall back to
        # <description> if a feed omits the content namespace (rare, but
        # some self-hosted/migrated Substack feeds vary slightly).
        content_el = item.find("content:encoded", _RSS_NS)
        body_html = content_el.text if content_el is not None and content_el.text else None

        if not body_html:
            desc_el = item.find("description")
            body_html = desc_el.text if desc_el is not None and desc_el.text else None

        if not body_html:
            return None  # matched the post but the feed had no body content

        # Run the body HTML fragment through the same extraction logic used
        # for full pages — content:encoded is itself a chunk of article HTML
        # (paragraphs, headings, etc.), so the same paragraph/heading
        # extraction applies cleanly without needing a separate code path.
        clean_text, _ = extract_article_text(f"<html><body>{body_html}</body></html>", article_url)

        if clean_text:
            return clean_text, page_title

        return None

    return None  # no matching item found in the feed (post too old, or feed doesn't include it)


def _normalise_url_for_match(url: str) -> str:
    """
    Normalise a URL for fuzzy equality comparison between a saved article
    URL and a feed item's <link> value, which can differ in trivial ways
    (http vs https, trailing slash, query string, www. prefix) while
    pointing at the same post.
    """
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

def extract_article_text(html: str, url: str) -> tuple[str, str]:
    """
    Extract clean article text and title from raw HTML.

    Strategy:
      1. Strip known boilerplate tags and class/id patterns
      2. Find the main content container (article, main, or largest div)
      3. Extract text, collapse whitespace, return clean prose

    Args:
        html: Raw HTML string from the fetched page.
        url:  Source URL (used for domain-specific heuristics).

    Returns:
        (clean_text, page_title) tuple.
        clean_text is empty string if extraction produces nothing useful.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return "", ""

    # Extract title
    title_tag = soup.find("title")
    page_title = title_tag.get_text(strip=True) if title_tag else ""

    # Also try og:title for cleaner titles
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        page_title = og_title["content"].strip()

    # Remove known boilerplate tags entirely
    for tag_name in _STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remove elements with boilerplate class/id names.
    #
    # BUG FIX: previously this called element.decompose() while still
    # iterating the find_all(True) result list. Decomposing a parent element
    # mid-loop can leave already-removed child elements in a broken state
    # where element.attrs is None instead of a dict, causing
    # AttributeError: 'NoneType' object has no attribute 'get' on the next
    # element.get("class", ...) call. Fixed by:
    #   1. Snapshotting which elements to remove BEFORE decomposing any of
    #      them, so the removal decisions are made against the original
    #      intact tree, not a tree that's being mutated mid-iteration.
    #   2. Guarding each element with a None-check before calling .get(),
    #      since find_all(True) can also match non-Tag nodes (comments,
    #      processing instructions) depending on the parser, which have no
    #      .attrs at all.
    to_remove = []
    for element in soup.find_all(True):
        if getattr(element, "attrs", None) is None:
            continue  # not a real Tag with attributes — skip safely
        el_classes = " ".join(element.get("class") or []).lower()
        el_id      = (element.get("id") or "").lower()
        combined   = el_classes + " " + el_id
        if any(pattern in combined for pattern in _BOILERPLATE_PATTERNS):
            to_remove.append(element)

    for element in to_remove:
        # An earlier removal in this same pass may have already decomposed
        # an ancestor of this element, which decomposes this element too as
        # a side effect. Guard against double-decompose.
        if element.parent is not None:
            element.decompose()

    # Find the main content container in priority order
    main_content = (
        soup.find("article") or
        soup.find("main") or
        soup.find(id=re.compile(r"(content|article|post|story|body)", re.I)) or
        soup.find(class_=re.compile(r"(article|post|content|story|entry)", re.I)) or
        soup.find("div", class_=re.compile(r"(article|post|content)", re.I))
    )

    # Fall back to full body if no container found
    target = main_content or soup.find("body") or soup

    # Extract text with single newlines between block elements
    lines = []
    for element in target.find_all(["p", "h1", "h2", "h3", "h4", "li", "blockquote"]):
        text = element.get_text(separator=" ", strip=True)
        if text and len(text) > 20:   # skip fragments and single words
            lines.append(text)

    # Join and normalise whitespace
    clean_text = "\n\n".join(lines)
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()

    return clean_text, page_title


def _count_words(text: str) -> int:
    return len(text.split()) if text else 0


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def log_link_fetch(entry: dict) -> None:
    """
    Write or update a row in link_log after a fetch attempt.

    Upserts by URL so re-runs with --reprocess-failed update the existing row.

    via_rss_fallback / via_playwright_fallback are optional in `entry` —
    call sites that short-circuit before any fallback is attempted (e.g.
    "blocked" or "failed" on the very first fetch) never set them, so they
    default to 0 here rather than requiring every call site to pass them
    explicitly. This also means existing INSERT OR REPLACE behaviour is
    unchanged for callers written before these columns existed.
    """
    entry = {
        **entry,
        "via_rss_fallback":        int(bool(entry.get("via_rss_fallback", False))),
        "via_playwright_fallback": int(bool(entry.get("via_playwright_fallback", False))),
        "via_manual_paste":        int(bool(entry.get("via_manual_paste", False))),
    }
    conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO link_log (
                url, link_id, label, added_date,
                fetch_status, http_status_code,
                page_title, word_count,
                fetch_attempted_at, error_message,
                via_rss_fallback, via_playwright_fallback, via_manual_paste
            ) VALUES (
                :url, :link_id, :label, :added_date,
                :fetch_status, :http_status_code,
                :page_title, :word_count,
                :fetch_attempted_at, :error_message,
                :via_rss_fallback, :via_playwright_fallback, :via_manual_paste
            )
        """, entry)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def ingest_links(
    csv_path:          Path           = DEFAULT_LINKS_FILE,
    processed_urls:    set[str]       = None,
    limit:             int | None     = None,
    reprocess_failed:  bool           = False,
    use_playwright:    bool           = True,
) -> list[dict]:
    """
    Read URLs from the CSV file, fetch and extract content, and return a list
    of article dicts compatible with the downstream email processing agents.

    Args:
        csv_path:         Path to the links CSV file.
        processed_urls:   Set of URLs to skip (pre-populated from link_log).
                          If None, fetched from DB automatically.
        limit:            Optional cap on articles processed per run.
        reprocess_failed: If True, retry URLs previously marked "failed".
        use_playwright:   If True (default), try a headless browser FIRST
                          for pages that return 0 words after the normal
                          fetch — ahead of the Substack RSS fallback, since
                          Playwright recovers real rendered HTML (including
                          images) rather than just text. If Playwright is
                          disabled, unavailable, or still finds 0 words, the
                          Substack RSS fallback is tried next where
                          applicable. If False, pages go straight to the RSS
                          fallback (if applicable) without attempting
                          Playwright at all. Set via --no-playwright.
                          The browser is launched lazily — only paid for if
                          at least one URL actually needs it — and reused
                          across the whole run, closed at the end.

    Returns:
        List of article dicts. Each dict contains the same keys as an email
        dict so downstream agents require zero modification:
          message_id      — "link:{md5}" stable ID
          account_alias   — "links" (fixed alias for all link-sourced notes)
          subject         — page title (used as note title)
          sender          — domain name of the URL
          received_date   — added_date from CSV, or today's date
          body_text       — extracted article text
          body_html       — raw HTML (for Agent 4 image extraction)
          service         — None (no Gmail service for links)
          gmail_id        — None
          source_url      — original URL (extra key, not in email dicts)
          label           — seed tag from CSV (extra key)
          is_partial               — True if word count is below MIN_CONTENT_WORDS (extra key)
          via_rss_fallback          — True if content came from the Substack RSS fallback (extra key)
          via_playwright_fallback   — True if content came from the Playwright fallback (extra key)

    Every fetch attempt — including blocked, failed, paywalled, and
    js_required outcomes, not just successes that produce an article dict —
    is also persisted to the link_log table via log_link_fetch(), including
    via_rss_fallback and via_playwright_fallback as their own columns. This
    is the durable record; the article dict above is only for items that
    proceed to downstream processing in this run.

    Additionally, every call to this function writes a session-level run
    log: a timestamped, human-readable .log file at
    {PROJECT_ROOT}/logs/link_fetch_{UTC timestamp}.log, listing every URL's
    outcome in processing order plus a run summary (totals by status, and
    the % of URLs that needed each fallback this run). A companion .json
    file with the same base name holds the same summary in a
    machine-readable form. This is separate from link_log — link_log is the
    cross-run, queryable database record; the run log is a self-contained
    record of THIS run, useful for reviewing a specific session without
    writing SQL. See _RunLogger.
    """
    if processed_urls is None:
        processed_urls = get_processed_urls(reprocess_failed=reprocess_failed)

    all_links = read_links_csv(csv_path)
    if not all_links:
        return []

    # Filter already-processed URLs
    new_links = [l for l in all_links if l["url"] not in processed_urls]
    print(f"[link_ingestion] {len(new_links)} new URL(s) to process "
          f"({len(all_links) - len(new_links)} already processed)")

    if limit:
        new_links = new_links[:limit]
        print(f"[link_ingestion] Capped at {limit} URL(s) per --limit flag")

    articles = []
    now      = datetime.now(timezone.utc).isoformat()

    # Lazily-launched, reused across the whole run. Created unconditionally
    # (cheap — just an object, no browser process yet) so the fallback
    # chain below can always call browser_manager.fetch() without needing
    # an extra None-check; is_available() handles the use_playwright=False
    # and not-installed cases transparently.
    browser_manager = _PlaywrightBrowserManager() if use_playwright else None
    playwright_unavailable_logged = False

    # Session-level run log — one timestamped file for this call to
    # ingest_links(), separate from (and in addition to) the durable
    # per-URL rows written to link_log via log_link_fetch() below. See
    # the _RunLogger docstring for the rationale.
    run_logger = _RunLogger()

    try:
        for i, link in enumerate(new_links, 1):
            url        = link["url"]
            label      = link.get("label", "")
            added_date = link.get("added_date", "") or now[:10]
            domain     = urlparse(url).netloc.replace("www.", "")
            link_id    = make_link_id(url)

            print(f"  [{i:>3}/{len(new_links)}] {url[:70]}")

            # Fetch
            html, fetch_status, http_code, error_detail = fetch_url(url)

            if fetch_status == "blocked":
                print(f"    → Blocked (HTTP {http_code})" + (f" — {error_detail}" if error_detail else ""))
                log_link_fetch({
                    "url":                url,
                    "link_id":            link_id,
                    "label":              label,
                    "added_date":         added_date,
                    "fetch_status":       "blocked",
                    "http_status_code":   http_code,
                    "page_title":         None,
                    "word_count":         0,
                    "fetch_attempted_at": now,
                    "error_message":      error_detail or f"HTTP {http_code}",
                })
                run_logger.record(
                    url=url, fetch_status="blocked", word_count=0,
                    via_rss_fallback=False, via_playwright_fallback=False,
                    http_status_code=http_code, error_message=error_detail or f"HTTP {http_code}",
                )
                continue

            if fetch_status == "failed" or html is None:
                print(f"    → Failed (HTTP {http_code})" + (f" — {error_detail}" if error_detail else ""))
                log_link_fetch({
                    "url":                url,
                    "link_id":            link_id,
                    "label":              label,
                    "added_date":         added_date,
                    "fetch_status":       "failed",
                    "http_status_code":   http_code,
                    "page_title":         None,
                    "word_count":         0,
                    "fetch_attempted_at": now,
                    "error_message":      error_detail or f"HTTP {http_code}",
                })
                run_logger.record(
                    url=url, fetch_status="failed", word_count=0,
                    via_rss_fallback=False, via_playwright_fallback=False,
                    http_status_code=http_code, error_message=error_detail or f"HTTP {http_code}",
                )
                continue

            # Extract
            body_text, page_title = extract_article_text(html, url)
            word_count = _count_words(body_text)

            # -------------------------------------------------------------------
            # Three-way content classification
            #
            # 1. Zero words → "js_required". This is NOT a subscription paywall;
            #    it almost always means the page needs JavaScript to render its
            #    real content and requests/BeautifulSoup only saw an empty shell.
            #    Distinguishing this from "paywalled" matters because the fix is
            #    different: a JS-gated page might be fully readable in a normal
            #    browser, whereas a real paywall genuinely has no free content.
            #    Not auto-retried (same as paywalled) — flag for manual review.
            #
            # 2. Some words but below MIN_CONTENT_WORDS → "partial". Common for
            #    Medium and Medium-partner sites (gitconnected.com, gopubby.com,
            #    etc.) which serve a genuine free preview (~100-250 words)
            #    before a "Member-only story" gate. That preview is real,
            #    readable content — worth keeping in the knowledge base even
            #    though it's incomplete, rather than discarding it outright.
            #    The note gets an explicit "partial / preview only" marker so
            #    it's never mistaken for the full article.
            #
            # 3. MIN_CONTENT_WORDS or more → "fetched". Full confidence, no
            #    caveats.
            # -------------------------------------------------------------------

            # ---------------------------------------------------------------------
            # Playwright headless-browser fallback — tried FIRST among the two
            # fallbacks, ahead of the Substack RSS feed fallback below.
            #
            # Rationale: RSS recovery only ever produces a feed's text content
            # (<content:encoded> / <description>), which carries no real <img>
            # tags for Agent 4 (image extraction) to find downstream — the
            # body_html the pipeline ends up with is a synthetic text wrapper.
            # Playwright renders the actual page in a real browser, so it
            # recovers real DOM with working <img src=...> tags AND is
            # generally a more faithful, complete extraction than a feed's
            # excerpt field. Putting Playwright first means a page that would
            # have short-circuited on a partial RSS match now gets the richer
            # result instead.
            #
            # This applies to ANY domain (JS-gating isn't Substack-specific),
            # and is deliberately the most expensive step in the chain
            # (~3-8s per page vs ~100-300ms for RSS), so it's gated behind the
            # use_playwright flag (--no-playwright to disable).
            # ---------------------------------------------------------------------
            via_playwright_fallback = False
            if word_count == 0 and use_playwright:
                if not browser_manager.is_available():
                    if not playwright_unavailable_logged:
                        print(f"    → Playwright unavailable: {browser_manager.unavailable_reason}")
                        print(f"      (this message prints once per run, not per-URL)")
                        playwright_unavailable_logged = True
                else:
                    print(f"    → 0 words — trying Playwright headless browser...")
                    rendered_html = browser_manager.fetch(url)
                    if rendered_html:
                        pw_text, pw_title = extract_article_text(rendered_html, url)
                        pw_word_count = _count_words(pw_text)
                        if pw_word_count > 0:
                            html             = rendered_html
                            body_text        = pw_text
                            page_title       = pw_title or page_title
                            word_count       = pw_word_count
                            via_playwright_fallback = True
                            print(f"    → Playwright recovered {word_count:,} words "
                                  f"(real rendered HTML — image extraction can run on this)")
                        else:
                            print(f"    → Playwright rendered the page but extraction still found 0 words")
                    else:
                        print(f"    → Playwright navigation failed (timeout or error)")

            # -----------------------------------------------------------------
            # Substack RSS fallback — only reached if STILL 0 words after the
            # Playwright attempt above (i.e. Playwright was disabled via
            # --no-playwright, unavailable on this machine, or genuinely
            # failed to recover content). Only attempted on Substack-hosted
            # pages. See _extract_from_substack_feed() for full rationale.
            # Best-effort, not guaranteed: the feed may not include older
            # posts, may lack a matching item, or the publication may not be
            # Substack-hosted at all. No images are recoverable via this
            # path — text content only.
            # -----------------------------------------------------------------
            via_rss_fallback = False
            if word_count == 0 and _is_substack_html(html):
                print(f"    → Still 0 words, Substack detected — trying RSS feed fallback...")
                rss_result = _extract_from_substack_feed(url)
                if rss_result:
                    body_text, page_title = rss_result
                    word_count = _count_words(body_text)
                    via_rss_fallback = True
                    print(f"    → RSS fallback recovered {word_count:,} words "
                          f"(text only — no images available via this path)")
                else:
                    print(f"    → RSS fallback found no usable match")

            if word_count == 0:
                print(f"    → JS required / empty shell (0 words) — flagging for manual review")
                log_link_fetch({
                    "url":                url,
                    "link_id":            link_id,
                    "label":              label,
                    "added_date":         added_date,
                    "fetch_status":       "js_required",
                    "http_status_code":   http_code,
                    "page_title":         page_title or None,
                    "word_count":         0,
                    "fetch_attempted_at": now,
                    "error_message":      "0 words extracted after exhausting all fallbacks "
                                           "(direct fetch, Playwright headless browser where enabled, "
                                           "RSS feed where applicable)",
                    "via_rss_fallback":        via_rss_fallback,
                    "via_playwright_fallback": via_playwright_fallback,
                })
                run_logger.record(
                    url=url, fetch_status="js_required", word_count=0,
                    via_rss_fallback=via_rss_fallback, via_playwright_fallback=via_playwright_fallback,
                    http_status_code=http_code, page_title=page_title or None,
                    error_message="0 words after exhausting all fallbacks",
                )
                continue

            if word_count < MIN_PREVIEW_WORDS:
                print(f"    → Too thin to keep ({word_count} words, below {MIN_PREVIEW_WORDS}-word preview floor)")
                log_link_fetch({
                    "url":                url,
                    "link_id":            link_id,
                    "label":              label,
                    "added_date":         added_date,
                    "fetch_status":       "paywalled",
                    "http_status_code":   http_code,
                    "page_title":         page_title or None,
                    "word_count":         word_count,
                    "fetch_attempted_at": now,
                    "error_message":      f"Only {word_count} words extracted — below preview floor",
                    "via_rss_fallback":        via_rss_fallback,
                    "via_playwright_fallback": via_playwright_fallback,
                })
                run_logger.record(
                    url=url, fetch_status="paywalled", word_count=word_count,
                    via_rss_fallback=via_rss_fallback, via_playwright_fallback=via_playwright_fallback,
                    http_status_code=http_code, page_title=page_title or None,
                    error_message=f"Only {word_count} words — below preview floor",
                )
                continue

            is_partial = word_count < MIN_CONTENT_WORDS
            fetch_status_label = "partial" if is_partial else "fetched"

            if is_partial:
                print(f"    → Partial / preview only: {word_count:,} words | \"{(page_title or url)[:50]}\"")
            else:
                print(f"    → Fetched: {word_count:,} words | \"{(page_title or url)[:55]}\"")

            # Build an informational note (not an error) when content was
            # recovered via a fallback rather than the normal HTML fetch.
            # Multiple notes can combine where applicable (e.g. is_partial
            # could in principle co-occur with a fallback, though this is rare
            # since Playwright/RSS recovery is usually all-or-nothing per page).
            note_parts = []
            if via_playwright_fallback:
                note_parts.append("Recovered via Playwright headless browser fallback "
                                   "(normal fetch returned 0 words)")
            if via_rss_fallback:
                note_parts.append("Recovered via Substack RSS feed fallback "
                                   "(normal fetch and Playwright fallback both returned 0 words, "
                                   "or Playwright was unavailable/disabled)")
            if is_partial:
                note_parts.append("Preview/excerpt only — likely a metered paywall (e.g. Medium-family site)")
            info_note = " | ".join(note_parts) if note_parts else None

            log_link_fetch({
                "url":                url,
                "link_id":            link_id,
                "label":              label,
                "added_date":         added_date,
                "fetch_status":       fetch_status_label,
                "http_status_code":   http_code,
                "page_title":         page_title,
                "word_count":         word_count,
                "fetch_attempted_at": now,
                "error_message":      info_note,
                "via_rss_fallback":        via_rss_fallback,
                "via_playwright_fallback": via_playwright_fallback,
            })
            run_logger.record(
                url=url, fetch_status=fetch_status_label, word_count=word_count,
                via_rss_fallback=via_rss_fallback, via_playwright_fallback=via_playwright_fallback,
                http_status_code=http_code, page_title=page_title, error_message=info_note,
            )

            # Build article dict — same schema as email dict for downstream compat
            articles.append({
                # Standard email-compatible keys
                "message_id":    link_id,
                "account_alias": "links",
                "subject":       page_title or domain,
                "sender":        domain,
                "received_date": added_date,
                "body_text":     body_text,
                "body_html":     html,
                "service":       None,     # no Gmail service for links
                "gmail_id":      None,
                # Link-specific extra keys
                "source_url":             url,
                "label":                  label,
                "is_partial":             is_partial,              # used by link_orchestrator to mark the note
                "via_rss_fallback":       via_rss_fallback,         # used by link_orchestrator to mark the note
                "via_playwright_fallback": via_playwright_fallback, # used by link_orchestrator to mark the note
            })

            # Polite delay between requests
            if i < len(new_links):
                time.sleep(REQUEST_DELAY)

    finally:
        if browser_manager is not None:
            browser_manager.close()
        # Always attempt to write the run log, even if the loop above
        # raised partway through — partial progress is still worth having
        # a record of. finalise() is itself defensive (catches its own
        # exceptions, never raises) so this can't mask the original error.
        run_logger.finalise()

    print(f"\n[link_ingestion] Done: {len(articles)} article(s) ready for processing")
    return articles




# ---------------------------------------------------------------------------
# Manual content ingestion
# ---------------------------------------------------------------------------

def ingest_manual(
    manual_dir:     Path       = MANUAL_CONTENT_DIR,
    processed_urls: set[str]   = None,
    limit:          int | None = None,
) -> list[dict]:
    """
    Read pre-fetched article content from JSON files in manual_content/ and
    return article dicts in the same schema as ingest_links().

    Called by link_orchestrator.py alongside ingest_links() so manually-
    pasted articles run through the same downstream agents (classification,
    summarisation, topic linking, image extraction, research, writer) with
    no changes to those agents.

    Each JSON file must contain:
        url        — str, required  (used as the dedup key in link_log)
        body_text  — str, required  (the pasted article content)
        title      — str, optional  (page title; falls back to domain)
        label      — str, optional  (seed tag hint for summarisation)
        added_date — str, optional  (ISO date; defaults to today)

    After processing, the file is moved to manual_content/processed/ so it
    is never picked up again even if --reprocess-failed is passed (manual
    pastes are one-shot by design — re-paste to re-process).

    Args:
        manual_dir:     Path to the manual_content/ folder.
        processed_urls: Ignored — ingest_manual() always calls
                        get_manual_processed_urls() internally so that only
                        prior successful manual pastes are skipped. URLs with
                        fetch_status blocked/paywalled/js_required are NOT
                        skipped — manual paste exists to recover exactly those.
        limit:          Optional cap, shared with the CSV-sourced batch.

    Returns:
        List of article dicts ready for downstream processing.
    """
    if not manual_dir.exists():
        return []

    json_files = sorted(manual_dir.glob("*.json"))
    if not json_files:
        return []

    # Run budget already exhausted by the CSV batch — nothing to do. Exits
    # before the dedup DB query and the "Found N file(s)" log line, which
    # would otherwise be misleading.
    if limit is not None and limit <= 0:
        return []

    # Use the manual-only dedup set — see get_manual_processed_urls().
    # The processed_urls arg from link_orchestrator includes blocked/paywalled/
    # js_required which would wrongly skip URLs we want to recover via paste.
    manual_done_urls = get_manual_processed_urls()

    processed_dir = manual_dir / "processed"
    processed_dir.mkdir(exist_ok=True)

    articles = []
    now = datetime.now(timezone.utc).isoformat()
    skipped = 0

    print(f"[link_ingestion] Found {len(json_files)} manual content file(s) in {manual_dir.name}/")

    for json_file in json_files:
        # BUGFIX: was `if limit and ...` — when link_orchestrator has already
        # consumed the full --limit with CSV articles it passes limit=0
        # (remaining budget), which is falsy, so the guard never fired and
        # every queued manual file was processed despite the exhausted cap.
        # `is not None` treats 0 as "no budget left" and None as "no limit".
        if limit is not None and len(articles) >= limit:
            break

        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  [manual] Skipping {json_file.name} — JSON parse error: {exc}")
            continue

        url = payload.get("url", "").strip()
        if not url or not url.startswith("http"):
            print(f"  [manual] Skipping {json_file.name} — missing or invalid url field")
            continue

        if url in manual_done_urls:
            print(f"  [manual] Skipping {url[:60]} — already ingested manually")
            json_file.rename(processed_dir / json_file.name)
            skipped += 1
            continue

        body_text  = payload.get("body_text", "").strip()
        if not body_text:
            print(f"  [manual] Skipping {json_file.name} — body_text is empty")
            continue

        title      = payload.get("title", "").strip()
        label      = payload.get("label", "").strip()
        added_date = payload.get("added_date", "").strip() or now[:10]
        domain     = urlparse(url).netloc.replace("www.", "")
        link_id    = make_link_id(url)
        word_count = _count_words(body_text)

        print(f"  [manual] {url[:70]}")
        print(f"    → {word_count:,} words, title: {(title or domain)[:50]}")

        # Write to link_log so dedup works on subsequent runs
        log_link_fetch({
            "url":                url,
            "link_id":            link_id,
            "label":              label,
            "added_date":         added_date,
            "fetch_status":       "manual",
            "http_status_code":   None,
            "page_title":         title or domain,
            "word_count":         word_count,
            "fetch_attempted_at": now,
            "error_message":      None,
            "via_rss_fallback":   False,
            "via_playwright_fallback": False,
            "via_manual_paste":   True,
        })

        articles.append({
            # Standard email-compatible keys
            "message_id":    link_id,
            "account_alias": "links",
            "subject":       title or domain,
            "sender":        domain,
            "received_date": added_date,
            "body_text":     body_text,
            "body_html":     "",          # no HTML for manual pastes
            "service":       None,
            "gmail_id":      None,
            # Link-specific extra keys
            "source_url":              url,
            "label":                   label,
            "is_partial":              False,          # manual paste = full content by definition
            "via_rss_fallback":        False,
            "via_playwright_fallback": False,
            "via_manual_paste":        True,
        })

        # Move to processed/ — prevents re-processing even across runs
        json_file.rename(processed_dir / json_file.name)

    if articles or skipped:
        print(f"[link_ingestion] Manual: {len(articles)} article(s) ready, {skipped} skipped\n")

    return articles

# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Test with 2 real public URLs — no CSV file needed for this test.
    Run: python agents/link_ingestion.py
    """
    print("=== Link Ingestion Agent — standalone test ===\n")

    # Temporarily write a small test CSV
    test_csv = _pipeline_dir / "links_test_temp.csv"
    test_csv.write_text(
        "url,label,added_date\n"
        "https://martinfowler.com/bliki/TwoHardThings.html,software engineering,2026-06-01\n"
        "https://httpbin.org/status/403,test blocked,2026-06-01\n",
        encoding="utf-8",
    )

    try:
        articles = ingest_links(
            csv_path       = test_csv,
            processed_urls = set(),
            limit          = 3,
        )

        print(f"\nResults: {len(articles)} article(s) returned\n")
        for a in articles:
            print(f"  Title  : {a['subject']}")
            print(f"  Domain : {a['sender']}")
            print(f"  Words  : {len(a['body_text'].split()):,}")
            print(f"  ID     : {a['message_id']}")
            print()
    finally:
        test_csv.unlink(missing_ok=True)

    print("Test complete.")
