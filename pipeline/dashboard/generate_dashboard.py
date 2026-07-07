# =============================================================================
# pipeline/dashboard/generate_dashboard.py — Newsletter AI Pipeline v4.0
# Dashboard Generator
#
# Queries registry.db and writes a self-contained dashboard/index.html.
# No server required — open the HTML file directly in a browser or Obsidian.
#
# Run manually:
#   python pipeline\dashboard\generate_dashboard.py
#
# Or add to orchestrator by calling generate() at end of each run.
#
# Usage (standalone):
#   cd C:\Users\<username>\OneDrive\Documents\newsletter-pipeline
#   python pipeline\dashboard\generate_dashboard.py
# =============================================================================

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_dashboard_dir = Path(__file__).resolve().parent
_pipeline_dir  = _dashboard_dir.parent
for _p in (_pipeline_dir,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config import PROJECT_ROOT, DASHBOARD_DIR
from db import get_connection, get_run_stats, get_recent_runs, get_topic_count, initialise_db, get_link_stats, get_recent_links, get_fallback_stats_30d, get_queue_stats

OUTPUT_PATH = DASHBOARD_DIR / "index.html"


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------

def _get_cost_by_day(days: int = 30) -> list[dict]:
    """Daily cost and email count for the last N days."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT
                DATE(processed_at) AS day,
                SUM(cost_usd)       AS cost,
                COUNT(*)            AS emails
            FROM processing_log
            WHERE status = 'success'
              AND processed_at >= DATE('now', ?)
            GROUP BY DATE(processed_at)
            ORDER BY day ASC
        """, (f"-{days} days",)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_cost_by_account() -> list[dict]:
    """Total cost and email count per account alias."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT
                account_alias,
                COUNT(*)       AS emails,
                SUM(cost_usd)  AS total_cost,
                SUM(images_saved) AS images
            FROM processing_log
            WHERE status = 'success'
            GROUP BY account_alias
            ORDER BY total_cost DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_classification_breakdown() -> dict:
    """Count of emails by classification stage and outcome."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT
                classification,
                classification_stage,
                COUNT(*) AS cnt
            FROM processing_log
            WHERE classification IS NOT NULL
            GROUP BY classification, classification_stage
            ORDER BY cnt DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_top_tags(limit: int = 20) -> list[dict]:
    """Most frequently occurring tags across all notes."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT tag, note_files
            FROM topic_index
            ORDER BY tag ASC
        """).fetchall()

        tag_counts = []
        for row in rows:
            try:
                files = json.loads(row["note_files"] or "[]")
                tag_counts.append({"tag": row["tag"], "count": len(files)})
            except Exception:
                pass

        tag_counts.sort(key=lambda x: x["count"], reverse=True)
        return tag_counts[:limit]
    finally:
        conn.close()


def _get_recent_emails(limit: int = 20) -> list[dict]:
    """Most recently processed emails for the activity table."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT
                account_alias, subject, sender,
                processed_at, status, cost_usd,
                input_tokens, output_tokens, cache_read_tokens,
                images_saved, classification, duration_seconds
            FROM processing_log
            ORDER BY processed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_cache_efficiency() -> dict:
    """Overall cache hit rate across all runs."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT
                COALESCE(SUM(input_tokens), 0)      AS total_input,
                COALESCE(SUM(cache_read_tokens), 0) AS total_cached,
                COALESCE(SUM(output_tokens), 0)     AS total_output
            FROM processing_log
            WHERE status = 'success'
        """).fetchone()
        if row and row["total_input"] > 0:
            rate = round(row["total_cached"] / row["total_input"] * 100, 1)
        else:
            rate = 0.0
        return dict(row) | {"cache_hit_pct": rate}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _fmt_cost(val) -> str:
    try:
        return f"${float(val):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def _fmt_ts(ts: str) -> str:
    """Format an ISO timestamp to a readable short form."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M")
    except Exception:
        return ts[:16]


def _status_badge(status: str) -> str:
    colours = {
        "success":           ("#16a34a", "#dcfce7"),
        "skipped_marketing": ("#d97706", "#fef3c7"),
        "skipped_blocklist": ("#6b7280", "#f3f4f6"),
        "failed":            ("#dc2626", "#fee2e2"),
        "skipped_duplicate": ("#6b7280", "#f3f4f6"),
    }
    fg, bg = colours.get(status, ("#374151", "#f9fafb"))
    label  = status.replace("_", " ").title()
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:9999px;font-size:11px;font-weight:600;'
        f'letter-spacing:0.3px">{label}</span>'
    )


def generate() -> Path:
    """
    Query the database and write dashboard/index.html.
    Returns the path of the written file.
    """
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    # BUGFIX: initialise_db() was previously only called in the __main__
    # block, so it never ran when generate() was imported by main.py or the
    # orchestrators — on a fresh registry.db, `python main.py dashboard`
    # failed with "no such table: processing_log" while the other two
    # generators (which call initialise_db() inside generate()) succeeded.
    # initialise_db() is idempotent, so calling it here is always safe.
    initialise_db()

    # -----------------------------------------------------------------------
    # Collect all data
    # -----------------------------------------------------------------------
    stats          = get_run_stats()
    recent_runs    = get_recent_runs(n=15)
    topic_count    = get_topic_count()
    daily_costs    = _get_cost_by_day(30)
    by_account     = _get_cost_by_account()
    classification = _get_classification_breakdown()
    top_tags       = _get_top_tags(20)
    recent_emails  = _get_recent_emails(20)
    cache_eff      = _get_cache_efficiency()
    link_stats     = get_link_stats()
    recent_links   = get_recent_links(25)
    fallback_30d   = get_fallback_stats_30d()
    queue_stats    = get_queue_stats()
    generated_at   = datetime.now(timezone.utc).strftime("%d %b %Y at %H:%M UTC")

    # -----------------------------------------------------------------------
    # Chart data for the sparkline (daily cost last 30 days)
    # -----------------------------------------------------------------------
    chart_labels = [d["day"] for d in daily_costs]
    chart_costs  = [round(float(d["cost"] or 0), 5) for d in daily_costs]
    chart_emails = [int(d["emails"] or 0) for d in daily_costs]

    # -----------------------------------------------------------------------
    # Stat card values
    # -----------------------------------------------------------------------
    total_processed = stats.get("total_processed", 0)
    total_skipped   = stats.get("total_skipped_marketing", 0)
    total_failed    = stats.get("total_failed", 0)
    total_cost      = stats.get("total_cost_usd", 0.0)
    total_images    = stats.get("total_images_saved", 0)
    latest_run      = _fmt_ts(stats.get("latest_run_at", ""))
    cache_pct       = cache_eff.get("cache_hit_pct", 0.0)

    # -----------------------------------------------------------------------
    # Build recent runs table rows
    # -----------------------------------------------------------------------
    run_rows = ""
    for run in recent_runs:
        started  = _fmt_ts(run.get("run_started_at", ""))
        accounts = run.get("accounts_processed", "[]").strip("[]").replace("'", "").replace('"', "")
        proc     = run.get("emails_processed", 0)
        skip     = run.get("emails_skipped_marketing", 0)
        fail     = run.get("emails_failed", 0)
        cost     = _fmt_cost(run.get("total_cost_usd", 0))
        imgs     = run.get("total_images_saved", 0)
        cache_r  = run.get("total_cache_reads", 0)
        inp      = run.get("total_input_tokens", 0)
        c_rate   = f"{round(cache_r/inp*100,1)}%" if inp else "—"
        git      = run.get("git_backup_status", "—")
        git_col  = {"success": "#16a34a", "failed": "#dc2626",
                    "pending": "#d97706", "skipped": "#9ca3af"}.get(git, "#9ca3af")

        run_rows += f"""
        <tr>
          <td>{started}</td>
          <td style="color:#6b7280;font-size:12px">{accounts}</td>
          <td class="num">{proc}</td>
          <td class="num" style="color:#d97706">{skip}</td>
          <td class="num" style="color:#dc2626">{fail}</td>
          <td class="num mono">{cost}</td>
          <td class="num">{imgs}</td>
          <td class="num">{c_rate}</td>
          <td><span style="color:{git_col};font-size:12px;font-weight:600">{git}</span></td>
        </tr>"""

    # -----------------------------------------------------------------------
    # Build recent emails table rows
    # -----------------------------------------------------------------------
    email_rows = ""
    for em in recent_emails:
        subj    = (em.get("subject") or "(no subject)")[:52]
        acct    = em.get("account_alias", "—")
        ts      = _fmt_ts(em.get("processed_at", ""))
        status  = em.get("status", "—")
        cost    = _fmt_cost(em.get("cost_usd", 0))
        inp     = em.get("input_tokens", 0) or 0
        out     = em.get("output_tokens", 0) or 0
        cached  = em.get("cache_read_tokens", 0) or 0
        imgs    = em.get("images_saved", 0) or 0
        dur     = em.get("duration_seconds")
        dur_str = f"{dur:.1f}s" if dur else "—"

        email_rows += f"""
        <tr>
          <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
              title="{em.get('subject','')}">{subj}</td>
          <td><span class="acct-tag acct-{acct}">{acct}</span></td>
          <td>{ts}</td>
          <td>{_status_badge(status)}</td>
          <td class="num mono">{cost}</td>
          <td class="num" style="color:#6b7280;font-size:12px">{inp:,} / {out:,} / {cached:,}</td>
          <td class="num">{imgs}</td>
          <td class="num" style="color:#9ca3af;font-size:12px">{dur_str}</td>
        </tr>"""

    # -----------------------------------------------------------------------
    # Tag cloud HTML
    # -----------------------------------------------------------------------
    max_count  = max((t["count"] for t in top_tags), default=1)
    tag_cloud  = ""
    for tag in top_tags:
        size  = 12 + int((tag["count"] / max_count) * 14)
        alpha = 0.4 + (tag["count"] / max_count) * 0.6
        tag_cloud += (
            f'<span class="tag-chip" style="font-size:{size}px;opacity:{alpha:.2f}">'
            f'{tag["tag"]} <sup>{tag["count"]}</sup></span> '
        )

    # -----------------------------------------------------------------------
    # Classification breakdown mini table
    # -----------------------------------------------------------------------
    cls_rows = ""
    for c in classification:
        cls_rows += (
            f'<tr><td>{c.get("classification","—")}</td>'
            f'<td style="color:#9ca3af;font-size:12px">{c.get("classification_stage","—")}</td>'
            f'<td class="num">{c.get("cnt",0)}</td></tr>'
        )

    # -----------------------------------------------------------------------
    # Account breakdown cards
    # -----------------------------------------------------------------------
    acct_cards = ""
    for acct in by_account:
        acct_cards += f"""
        <div class="acct-card">
          <div class="acct-name">{acct['account_alias']}</div>
          <div class="acct-stat">{acct['emails']} <span>notes</span></div>
          <div class="acct-stat">{_fmt_cost(acct['total_cost'])} <span>spent</span></div>
          <div class="acct-stat">{acct['images']} <span>images</span></div>
        </div>"""

    # -----------------------------------------------------------------------
    # Link pipeline stat values
    # -----------------------------------------------------------------------
    lnk_total       = link_stats.get("total_links",       0)
    lnk_fetched     = link_stats.get("total_fetched",      0)
    lnk_partial     = link_stats.get("total_partial",      0)
    lnk_blocked     = link_stats.get("total_blocked",      0)
    lnk_paywalled   = link_stats.get("total_paywalled",    0)
    lnk_js_required = link_stats.get("total_js_required",  0)
    lnk_failed      = link_stats.get("total_failed",       0)
    # "Success" includes partial — it's still usable content, just incomplete
    lnk_success_pct = round((lnk_fetched + lnk_partial) / lnk_total * 100, 1) if lnk_total else 0.0

    # -----------------------------------------------------------------------
    # 30-day fallback usage stat values — separate window from the all-time
    # link_stats above, see get_fallback_stats_30d() for rationale.
    # -----------------------------------------------------------------------
    fb_total_30d            = fallback_30d.get("total_links_30d", 0)
    fb_rss_pct_30d          = fallback_30d.get("via_rss_fallback_pct_30d", 0.0)
    fb_rss_count_30d        = fallback_30d.get("via_rss_fallback_count_30d", 0)
    fb_playwright_pct_30d   = fallback_30d.get("via_playwright_fallback_pct_30d", 0.0)
    fb_playwright_count_30d = fallback_30d.get("via_playwright_fallback_count_30d", 0)
    fb_any_pct_30d          = fallback_30d.get("any_fallback_pct_30d", 0.0)

    q_pending   = queue_stats.get("pending",       0)
    q_done      = queue_stats.get("done",          0)
    q_skipped   = queue_stats.get("skipped",       0)
    q_cost      = queue_stats.get("total_cost_usd", 0.0)

    # -----------------------------------------------------------------------
    # Link fetch status table rows
    # -----------------------------------------------------------------------
    _fetch_badge = {
        "fetched":     ("#16a34a", "#dcfce7", "Fetched"),
        "partial":     ("#2563eb", "#dbeafe", "Partial"),
        "blocked":     ("#dc2626", "#fee2e2", "Blocked"),
        "paywalled":   ("#d97706", "#fef3c7", "Paywalled"),
        "js_required": ("#7c3aed", "#ede9fe", "JS Required"),
        "failed":      ("#6b7280", "#f3f4f6", "Failed"),
    }

    link_rows = ""
    for lnk in recent_links:
        status  = lnk.get("fetch_status", "—")
        fg, bg, label = _fetch_badge.get(status, ("#374151", "#f9fafb", status.title()))
        badge   = (f'<span style="background:{bg};color:{fg};padding:2px 8px;'
                   f'border-radius:9999px;font-size:11px;font-weight:600">{label}</span>')
        url     = lnk.get("url", "")
        title   = (lnk.get("page_title") or lnk.get("label") or url)[:55]
        domain  = url.split("/")[2].replace("www.", "") if "//" in url else url[:30]
        words   = lnk.get("word_count", 0) or 0
        ts      = _fmt_ts(lnk.get("fetch_attempted_at", ""))
        err     = (lnk.get("error_message") or "")[:40]
        http    = lnk.get("http_status_code") or "—"

        link_rows += f"""
        <tr>
          <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
              title="{url}"><a href="{url}" style="color:var(--accent2);text-decoration:none"
              target="_blank">{title}</a></td>
          <td style="color:#6b7280;font-size:12px">{domain}</td>
          <td>{badge}</td>
          <td class="num" style="color:#6b7280;font-size:12px">{http}</td>
          <td class="num">{words:,}</td>
          <td style="color:#6b7280;font-size:12px">{ts}</td>
          <td style="color:#9ca3af;font-size:11px;max-width:200px;overflow:hidden;
              text-overflow:ellipsis;white-space:nowrap">{err}</td>
        </tr>"""

    # -----------------------------------------------------------------------
    # Research Queue pending badge (shown in header when topics are waiting)
    # -----------------------------------------------------------------------
    _queue_badge = (
        f' <span style="background:#fef3c7;color:#d97706;padding:1px 6px;'
        f'border-radius:9999px;font-size:10px;font-weight:600">{q_pending}</span>'
    ) if q_pending else ""

    # -----------------------------------------------------------------------
    # Assemble full HTML
    # -----------------------------------------------------------------------
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Newsletter Pipeline — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Sora:wght@300;400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:        #0f1117;
    --surface:   #181c25;
    --surface2:  #1e2333;
    --border:    #2a3047;
    --text:      #e2e8f0;
    --muted:     #64748b;
    --accent:    #6ee7b7;
    --accent2:   #7dd3fc;
    --accent3:   #fbbf24;
    --danger:    #f87171;
    --mono:      'DM Mono', monospace;
    --sans:      'Sora', sans-serif;
  }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.6;
    padding: 32px 24px 64px;
  }}

  /* ---- Header ---- */
  .header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 36px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 20px;
  }}
  .header h1 {{
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.5px;
    color: #fff;
  }}
  .header h1 span {{ color: var(--accent); }}
  .header-right {{
    display: flex;
    gap: 16px;
    align-items: center;
  }}
  .header-right a {{
    font-size: 12px;
    color: var(--accent2);
    text-decoration: none;
    white-space: nowrap;
  }}
  .header-right a:hover {{ text-decoration: underline; }}
  .header .meta {{
    font-size: 11px;
    color: var(--muted);
    font-family: var(--mono);
  }}

  /* ---- Stat cards ---- */
  .cards {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 14px;
    margin-bottom: 32px;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 16px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }}
  .card:hover {{ border-color: #3d4f6b; }}
  .card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--card-accent, var(--accent));
  }}
  .card-label {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.8px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 8px;
  }}
  .card-value {{
    font-size: 28px;
    font-weight: 600;
    color: #fff;
    font-family: var(--mono);
    letter-spacing: -1px;
    line-height: 1;
  }}
  .card-sub {{
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
  }}

  /* ---- Section titles ---- */
  .section-title {{
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.8px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .section-title::after {{
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }}

  /* ---- Layout grid ---- */
  .row {{
    display: grid;
    gap: 16px;
    margin-bottom: 24px;
  }}
  .row-2  {{ grid-template-columns: 1fr 1fr; }}
  .row-3  {{ grid-template-columns: 2fr 1fr 1fr; }}
  .row-32 {{ grid-template-columns: 3fr 2fr; }}
  @media (max-width: 900px) {{
    .row-2, .row-3, .row-32 {{ grid-template-columns: 1fr; }}
  }}

  /* ---- Panels ---- */
  .panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
  }}

  /* ---- Tables ---- */
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    color: var(--muted);
    padding: 0 8px 10px;
    border-bottom: 1px solid var(--border);
  }}
  td {{
    padding: 8px 8px;
    border-bottom: 1px solid #1e2636;
    font-size: 12.5px;
    color: #cbd5e1;
    vertical-align: middle;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #1a2035; }}
  td.num {{ text-align: right; }}
  td.mono {{ font-family: var(--mono); }}

  /* ---- Chart ---- */
  .chart-wrap {{
    position: relative;
    height: 160px;
  }}

  /* ---- Tag cloud ---- */
  .tag-cloud {{
    line-height: 2.2;
  }}
  .tag-chip {{
    display: inline-block;
    background: var(--surface2);
    color: var(--accent2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 2px 8px;
    margin: 2px 3px;
    font-family: var(--mono);
    font-size: 12px;
    transition: background 0.15s, border-color 0.15s;
    cursor: default;
  }}
  .tag-chip:hover {{
    background: #252e45;
    border-color: var(--accent2);
  }}
  .tag-chip sup {{
    color: var(--muted);
    font-size: 9px;
    margin-left: 2px;
  }}

  /* ---- Account cards ---- */
  .acct-cards {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .acct-card {{
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 18px;
    min-width: 130px;
    flex: 1;
  }}
  .acct-name {{
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
    margin-bottom: 8px;
    font-family: var(--mono);
  }}
  .acct-stat {{
    font-size: 18px;
    font-weight: 600;
    color: #fff;
    font-family: var(--mono);
    letter-spacing: -0.5px;
    line-height: 1.3;
  }}
  .acct-stat span {{
    font-size: 10px;
    font-weight: 400;
    color: var(--muted);
    display: block;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}

  /* ---- Account tags in table ---- */
  .acct-tag {{
    display: inline-block;
    padding: 1px 7px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    font-family: var(--mono);
    background: var(--surface2);
    color: var(--accent2);
    border: 1px solid var(--border);
  }}

  /* ---- Cache bar ---- */
  .cache-bar-wrap {{
    margin-top: 12px;
  }}
  .cache-bar-track {{
    height: 8px;
    background: var(--surface2);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 4px;
  }}
  .cache-bar-fill {{
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    border-radius: 4px;
    transition: width 1s ease;
  }}
  .cache-label {{
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
    font-family: var(--mono);
  }}

  /* ---- Scrollable tables ---- */
  .scroll-table {{ overflow-x: auto; }}

  /* ---- Empty state ---- */
  .empty {{
    text-align: center;
    color: var(--muted);
    padding: 32px 0;
    font-size: 13px;
  }}
</style>
</head>
<body>

<!-- ======== HEADER ======== -->
<div class="header">
  <h1>Newsletter Pipeline <span>Dashboard</span></h1>
  <div class="header-right">
    <a href="progress.html">⚡ Live Progress</a>
    <a href="link_review.html">🔍 Link Review</a>
    <a href="research_queue.html">🔬 Research Queue{_queue_badge}</a>
    <div class="meta">Generated {generated_at}</div>
  </div>
</div>

<!-- ======== STAT CARDS ======== -->
<div class="cards">
  <div class="card" style="--card-accent: var(--accent)">
    <div class="card-label">Notes Written</div>
    <div class="card-value">{total_processed}</div>
    <div class="card-sub">all time</div>
  </div>
  <div class="card" style="--card-accent: var(--accent3)">
    <div class="card-label">Total Cost</div>
    <div class="card-value" style="font-size:22px">{_fmt_cost(total_cost)}</div>
    <div class="card-sub">all time USD</div>
  </div>
  <div class="card" style="--card-accent: var(--accent2)">
    <div class="card-label">Topics Indexed</div>
    <div class="card-value">{topic_count}</div>
    <div class="card-sub">unique tags</div>
  </div>
  <div class="card" style="--card-accent: #a78bfa">
    <div class="card-label">Images Saved</div>
    <div class="card-value">{total_images}</div>
    <div class="card-sub">all time</div>
  </div>
  <div class="card" style="--card-accent: #fb923c">
    <div class="card-label">Skipped</div>
    <div class="card-value" style="color:#fbbf24">{total_skipped}</div>
    <div class="card-sub">marketing emails</div>
  </div>
  <div class="card" style="--card-accent: var(--danger)">
    <div class="card-label">Failed</div>
    <div class="card-value" style="color:{'var(--danger)' if total_failed > 0 else 'var(--muted)'}">{ total_failed}</div>
    <div class="card-sub">will retry</div>
  </div>
  <div class="card" style="--card-accent: var(--accent)">
    <div class="card-label">Cache Hit Rate</div>
    <div class="card-value" style="font-size:22px">{cache_pct}%</div>
    <div class="card-sub">input tokens served from cache</div>
  </div>
  <div class="card" style="--card-accent: var(--muted)">
    <div class="card-label">Last Run</div>
    <div class="card-value" style="font-size:14px;letter-spacing:0;line-height:1.5">{latest_run}</div>
    <div class="card-sub">&nbsp;</div>
  </div>
  <div class="card" style="--card-accent:#d97706">
    <div class="card-label">Research Queue</div>
    <div class="card-value">{q_pending}</div>
    <div class="card-sub">pending · <a href="research_queue.html"
         style="color:var(--accent2);text-decoration:none">review →</a></div>
  </div>
</div>

<!-- ======== ROW: CHART + ACCOUNTS + CLASSIFICATION ======== -->
<div class="row row-3">

  <!-- Daily cost chart -->
  <div class="panel">
    <div class="section-title">Daily Cost — Last 30 Days</div>
    {'<div class="chart-wrap"><canvas id="costChart"></canvas></div>' if chart_costs else '<div class="empty">No data yet</div>'}
  </div>

  <!-- Account breakdown -->
  <div class="panel">
    <div class="section-title">By Account</div>
    {'<div class="acct-cards">' + acct_cards + '</div>' if acct_cards else '<div class="empty">No data yet</div>'}
  </div>

  <!-- Classification -->
  <div class="panel">
    <div class="section-title">Classification</div>
    {'<div class="scroll-table"><table><thead><tr><th>Result</th><th>Stage</th><th style="text-align:right">Count</th></tr></thead><tbody>' + cls_rows + '</tbody></table></div>' if cls_rows else '<div class="empty">No data yet</div>'}

    <!-- Cache efficiency bar -->
    <div class="cache-bar-wrap" style="margin-top:20px">
      <div class="section-title" style="margin-bottom:6px">Prompt Cache</div>
      <div class="cache-bar-track">
        <div class="cache-bar-fill" style="width:{min(cache_pct, 100)}%"></div>
      </div>
      <div class="cache-label">
        <span>{cache_eff.get("total_cached", 0):,} cached tokens</span>
        <span>{cache_pct}% hit rate</span>
      </div>
    </div>
  </div>

</div>

<!-- ======== TAG CLOUD ======== -->
<div class="row">
  <div class="panel">
    <div class="section-title">Topic Index — Top {len(top_tags)} Tags</div>
    {'<div class="tag-cloud">' + tag_cloud + '</div>' if tag_cloud else '<div class="empty">No tags yet — run the pipeline first</div>'}
  </div>
</div>

<!-- ======== RUN HISTORY TABLE ======== -->
<div class="row">
  <div class="panel">
    <div class="section-title">Run History (last {len(recent_runs)})</div>
    {'<div class="scroll-table"><table><thead><tr><th>Started</th><th>Accounts</th><th style="text-align:right">Processed</th><th style="text-align:right">Skipped</th><th style="text-align:right">Failed</th><th style="text-align:right">Cost</th><th style="text-align:right">Images</th><th style="text-align:right">Cache</th><th>Git</th></tr></thead><tbody>' + run_rows + '</tbody></table></div>' if run_rows else '<div class="empty">No runs recorded yet</div>'}
  </div>
</div>

<!-- ======== RECENT EMAILS TABLE ======== -->
<div class="row">
  <div class="panel">
    <div class="section-title">Recent Emails (last {len(recent_emails)})</div>
    {'<div class="scroll-table"><table><thead><tr><th>Subject</th><th>Account</th><th>Processed</th><th>Status</th><th style="text-align:right">Cost</th><th style="text-align:right">Tokens (in/out/cached)</th><th style="text-align:right">Images</th><th style="text-align:right">Duration</th></tr></thead><tbody>' + email_rows + '</tbody></table></div>' if email_rows else '<div class="empty">No emails processed yet — run the pipeline first</div>'}
  </div>
</div>

<!-- ======== LINK PIPELINE SECTION ======== -->
<div class="row" style="margin-top:12px">
  <div class="panel">
    <div class="section-title">Link Pipeline — Fetch Overview</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:12px;margin-bottom:20px">
      <div class="acct-card">
        <div class="acct-name" style="color:var(--accent3)">Total URLs</div>
        <div class="acct-stat">{lnk_total} <span>attempted</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:#16a34a">Fetched</div>
        <div class="acct-stat">{lnk_fetched} <span>{lnk_success_pct}% usable</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:#2563eb">Partial</div>
        <div class="acct-stat">{lnk_partial} <span>preview only</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:#dc2626">Blocked</div>
        <div class="acct-stat">{lnk_blocked} <span>bot protection</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:#d97706">Paywalled</div>
        <div class="acct-stat">{lnk_paywalled} <span>too thin to keep</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:#7c3aed">JS Required</div>
        <div class="acct-stat">{lnk_js_required} <span>needs review</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:var(--muted)">Failed</div>
        <div class="acct-stat">{lnk_failed} <span>will retry</span></div>
      </div>
    </div>

    <div class="section-title" style="font-size:13px;margin-top:4px">Fallback Usage — Last 30 Days{(' (' + str(fb_total_30d) + ' URLs)') if fb_total_30d else ''}</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px">
      <div class="acct-card">
        <div class="acct-name" style="color:#7c3aed">Playwright fallback</div>
        <div class="acct-stat">{fb_playwright_pct_30d}% <span>{fb_playwright_count_30d} of {fb_total_30d} links</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:#ea580c">RSS fallback</div>
        <div class="acct-stat">{fb_rss_pct_30d}% <span>{fb_rss_count_30d} of {fb_total_30d} links</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:var(--muted)">Any fallback</div>
        <div class="acct-stat">{fb_any_pct_30d}% <span>needed recovery</span></div>
      </div>
    </div>
    {('<div class="empty" style="margin-top:8px">No links fetched in the last 30 days</div>') if not fb_total_30d else ''}
  </div>
</div>

<!-- ======== LINK FETCH LOG TABLE ======== -->
<div class="row">
  <div class="panel">
    <div class="section-title" style="display:flex;justify-content:space-between;align-items:center">
      Link Fetch Log (last {len(recent_links)})
      <a href="link_review.html"
         style="font-size:12px;font-weight:400;color:var(--accent2);text-decoration:none">
        View full review page →</a>
    </div>
    {'<div class="scroll-table"><table><thead><tr><th>Title / URL</th><th>Domain</th><th>Status</th><th style="text-align:right">HTTP</th><th style="text-align:right">Words</th><th>Attempted</th><th>Detail</th></tr></thead><tbody>' + link_rows + '</tbody></table></div>' if link_rows else '<div class="empty">No links processed yet — run link_orchestrator.py first</div>'}
  </div>
</div>

<!-- ======== RESEARCH QUEUE SUMMARY ======== -->
<div class="row" style="margin-top:12px">
  <div class="panel">
    <div class="section-title" style="display:flex;justify-content:space-between;align-items:center">
      Research Queue
      <a href="research_queue.html"
         style="font-size:12px;font-weight:400;color:var(--accent2);text-decoration:none">
        Open queue →</a>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px">
      <div class="acct-card">
        <div class="acct-name" style="color:#d97706">Pending</div>
        <div class="acct-stat">{q_pending} <span>topics</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:#16a34a">Researched</div>
        <div class="acct-stat">{q_done} <span>topics</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:var(--muted)">Skipped</div>
        <div class="acct-stat">{q_skipped} <span>topics</span></div>
      </div>
      <div class="acct-card">
        <div class="acct-name" style="color:var(--accent3)">Queue Cost</div>
        <div class="acct-stat">${q_cost:.4f} <span>all time</span></div>
      </div>
    </div>
    {('<div class="empty" style="margin-top:8px">Queue is empty — run with --no-research to populate it</div>') if not (q_pending + q_done + q_skipped) else ''}
  </div>
</div>

<!-- ======== CHART INIT ======== -->
<script>
(function() {{
  const labels = {json.dumps(chart_labels)};
  const costs  = {json.dumps(chart_costs)};
  const emails = {json.dumps(chart_emails)};

  if (!labels.length) return;

  const ctx = document.getElementById('costChart');
  if (!ctx) return;

  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels,
      datasets: [
        {{
          label: 'Cost (USD)',
          data: costs,
          backgroundColor: 'rgba(110, 231, 183, 0.25)',
          borderColor: 'rgba(110, 231, 183, 0.85)',
          borderWidth: 1.5,
          borderRadius: 3,
          yAxisID: 'y',
        }},
        {{
          label: 'Emails',
          data: emails,
          type: 'line',
          borderColor: 'rgba(125, 211, 252, 0.7)',
          backgroundColor: 'transparent',
          borderWidth: 1.5,
          pointRadius: 3,
          pointBackgroundColor: '#7dd3fc',
          tension: 0.3,
          yAxisID: 'y2',
        }},
      ],
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{
          labels: {{
            color: '#64748b',
            font: {{ size: 11 }},
            boxWidth: 12,
          }},
        }},
        tooltip: {{
          backgroundColor: '#1e2333',
          borderColor: '#2a3047',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#94a3b8',
          callbacks: {{
            label: ctx => ctx.dataset.label === 'Cost (USD)'
              ? ` ${{ctx.parsed.y.toFixed(5)}}`
              : ` ${{ctx.parsed.y}} emails`,
          }},
        }},
      }},
      scales: {{
        x: {{
          ticks: {{ color: '#475569', font: {{ size: 10 }}, maxRotation: 45 }},
          grid:  {{ color: '#1e2636' }},
        }},
        y: {{
          position: 'left',
          ticks: {{
            color: '#64748b',
            font: {{ size: 10 }},
            callback: v => '$' + v.toFixed(4),
          }},
          grid: {{ color: '#1e2636' }},
        }},
        y2: {{
          position: 'right',
          ticks: {{ color: '#475569', font: {{ size: 10 }} }},
          grid:  {{ display: false }},
        }},
      }},
    }},
  }});
}})();
</script>

</body>
</html>"""

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"[dashboard] Written → {OUTPUT_PATH}")
    print(f"[dashboard] Open in browser: file:///{OUTPUT_PATH.as_posix()}")
    return OUTPUT_PATH


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    initialise_db()
    generate()