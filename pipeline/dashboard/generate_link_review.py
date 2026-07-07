# pipeline/dashboard/generate_link_review.py — Newsletter AI Pipeline
# Generates dashboard/link_review.html — a filterable review page for all
# link_log rows that were NOT successfully fetched, plus marketing skips.
#
# Covers:
#   partial        — below MIN_CONTENT_WORDS (preview only, saved with warning)
#   blocked        — HTTP 401/403/407/429 (bot protection)
#   paywalled      — below MIN_PREVIEW_WORDS (too thin to keep)
#   js_required    — 0 words after all fallbacks exhausted
#   failed         — network error / timeout (retryable via --reprocess-failed)
#   skipped_mktg   — classified as marketing by Agent 1.5 (from processing_log)
#   manual         — content pasted manually via /api/ingest + bookmarklet (informational)
#
# The generated page has client-side filter buttons so you can focus on one
# status at a time without re-running this script.
#
# Usage:
#   python main.py dashboard      (regenerates all three pages)
#   python pipeline\dashboard\generate_link_review.py   (this page only)
#
# Output: dashboard/link_review.html (opens in any browser; no server needed)
# =============================================================================

import html as _html
import sys
from datetime import datetime, timezone
from pathlib import Path

# This script lives in pipeline/dashboard/ — add pipeline/ to sys.path so
# config.py and db.py resolve when run standalone from anywhere.
_pipeline_dir = Path(__file__).resolve().parent.parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import DASHBOARD_DIR
from db import initialise_db, get_links_by_status, get_skipped_marketing_links, get_manual_ingested_links

OUTPUT_PATH = DASHBOARD_DIR / "link_review.html"

# Status display config: (fg_colour, bg_colour, label)
_BADGE = {
    "partial":          ("#2563eb", "#dbeafe", "Partial"),
    "blocked":          ("#dc2626", "#fee2e2", "Blocked"),
    "paywalled":        ("#d97706", "#fef3c7", "Paywalled"),
    "js_required":      ("#7c3aed", "#ede9fe", "JS Required"),
    "failed":           ("#6b7280", "#f3f4f6", "Failed"),
    "skipped_mktg":     ("#b45309", "#fef3c7", "Mktg Skip"),
    "manual":           ("#0891b2", "#cffafe", "Manual"),   # cyan — informational, not a failure
}


def _badge(status: str) -> str:
    fg, bg, label = _BADGE.get(status, ("#374151", "#f9fafb", status.title()))
    return (
        f'<span class="badge" '
        f'style="background:{bg};color:{fg}">'
        f'{label}</span>'
    )


def _fmt_ts(ts: str) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M")
    except Exception:
        return ts[:16]


def _row(item: dict, status_key: str) -> str:
    # html.escape() with quote=True — page titles, labels, and error strings
    # come from external web pages / LLM output; a stray double quote or
    # angle bracket would otherwise break the row's attribute markup.
    url     = _html.escape(item.get("url", ""), quote=True)
    title   = _html.escape((item.get("page_title") or item.get("subject") or
               item.get("label") or item.get("url") or "")[:60], quote=True)
    raw_url = item.get("url", "")
    domain  = _html.escape(
        raw_url.split("/")[2].replace("www.", "") if "//" in raw_url else raw_url[:30],
        quote=True,
    )
    words   = item.get("word_count", 0) or 0
    ts      = _fmt_ts(item.get("fetch_attempted_at") or item.get("processed_at", ""))
    err     = _html.escape(
        (item.get("error_message") or item.get("classification") or "")[:50], quote=True
    )
    http    = item.get("http_status_code") or "—"
    label   = _html.escape(item.get("label", "") or "", quote=True)

    # Fallback / source indicators for link_log rows
    pw_tag      = ""
    rss_tag     = ""
    manual_tag  = ""
    if status_key != "skipped_mktg":
        if item.get("via_playwright_fallback"):
            pw_tag     = ' <span style="color:#7c3aed;font-size:10px">⚡PW</span>'
        if item.get("via_rss_fallback"):
            rss_tag    = ' <span style="color:#ea580c;font-size:10px">⚡RSS</span>'
        if item.get("via_manual_paste"):
            manual_tag = ' <span style="color:#0891b2;font-size:10px">✏ Manual</span>'

    # The entire row carries data-status so JS filter can show/hide by class
    return f"""
        <tr class="row-{status_key}" data-status="{status_key}">
          <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            <a href="{url}" target="_blank"
               style="color:var(--accent2);text-decoration:none"
               title="{url}">{title}</a>{pw_tag}{rss_tag}{manual_tag}
            {f'<div style="color:#6b7280;font-size:11px">{label}</div>' if label else ''}
          </td>
          <td style="color:#6b7280;font-size:12px;white-space:nowrap">{domain}</td>
          <td>{_badge(status_key)}</td>
          <td class="num" style="color:#6b7280;font-size:12px">{http}</td>
          <td class="num">{words:,}</td>
          <td style="color:#6b7280;font-size:12px;white-space:nowrap">{ts}</td>
          <td style="color:#9ca3af;font-size:11px;max-width:200px;overflow:hidden;
              text-overflow:ellipsis;white-space:nowrap"
              title="{err}">{err}</td>
        </tr>"""


def generate() -> Path:
    """Query link_log + processing_log and write link_review.html."""
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    initialise_db()

    # Fetch all non-success link_log rows
    review_statuses = ["partial", "blocked", "paywalled", "js_required", "failed"]
    link_rows_data  = get_links_by_status(review_statuses)

    # Marketing skips come from processing_log (classified after fetch)
    mktg_rows_data    = get_skipped_marketing_links()
    # Manually-pasted articles (informational — not failures, shown separately)
    manual_rows_data  = get_manual_ingested_links()

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y at %H:%M UTC")

    # Build per-status counts for the filter buttons
    counts: dict[str, int] = {s: 0 for s in review_statuses}
    counts["skipped_mktg"] = len(mktg_rows_data)
    counts["manual"]       = len(manual_rows_data)
    for row in link_rows_data:
        s = row.get("fetch_status", "")
        if s in counts:
            counts[s] += 1
    total = sum(counts.values())

    # Build all table rows
    all_rows = ""
    for row in link_rows_data:
        s = row.get("fetch_status", "")
        if s in review_statuses:
            all_rows += _row(row, s)
    for row in mktg_rows_data:
        # Map processing_log fields to display dict
        display = {
            "url":           "",   # no URL stored for email-classified items
            "page_title":    row.get("subject", ""),
            "label":         row.get("sender", ""),
            "fetch_attempted_at": row.get("processed_at", ""),
            "classification":    row.get("classification", ""),
        }
        all_rows += _row(display, "skipped_mktg")
    for row in manual_rows_data:
        all_rows += _row(row, "manual")

    # Filter button HTML
    def _filter_btn(status: str, label: str, fg: str, bg: str) -> str:
        count = counts.get(status, 0)
        return (
            f'<button class="filter-btn" data-filter="{status}" '
            f'style="--btn-fg:{fg};--btn-bg:{bg}">'
            f'{label} <span class="count">{count}</span></button>'
        )

    filter_buttons = (
        '<button class="filter-btn active" data-filter="all" '
        'style="--btn-fg:#fff;--btn-bg:#374151">'
        f'All <span class="count">{total}</span></button>'
        + _filter_btn("partial",      "Partial",     "#2563eb", "#dbeafe")
        + _filter_btn("blocked",      "Blocked",     "#dc2626", "#fee2e2")
        + _filter_btn("paywalled",    "Paywalled",   "#d97706", "#fef3c7")
        + _filter_btn("js_required",  "JS Required", "#7c3aed", "#ede9fe")
        + _filter_btn("failed",       "Failed",      "#6b7280", "#f3f4f6")
        + _filter_btn("skipped_mktg", "Mktg Skip",  "#b45309", "#fef3c7")
        + _filter_btn("manual",        "Manual",     "#0891b2", "#cffafe")
    )

    # Pre-compute the conditional table block — nested f-strings inside
    # f-string expressions are not supported in Python < 3.12, so build
    # this before the outer f-string starts and reference it as a variable.
    if all_rows:
        _table_block = f"""
  <table>
    <thead>
      <tr>
        <th>Title / URL</th>
        <th>Domain</th>
        <th>Status</th>
        <th style="text-align:right">HTTP</th>
        <th style="text-align:right">Words</th>
        <th>Date</th>
        <th>Error / Classification</th>
      </tr>
    </thead>
    <tbody id="review-body">
      {all_rows}
    </tbody>
  </table>"""
    else:
        _table_block = '<div class="empty">No review items found — all links fetched successfully.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Link Review — Newsletter Pipeline</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600&family=DM+Mono&display=swap"
        rel="stylesheet">
  <style>
    :root {{
      --bg:      #0f172a;
      --surface: #1e293b;
      --border:  #334155;
      --text:    #e2e8f0;
      --muted:   #64748b;
      --accent:  #6ee7b7;
      --accent2: #7dd3fc;
      --mono:    'DM Mono', monospace;
      --sans:    'Sora', sans-serif;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg); color: var(--text);
      font-family: var(--sans); font-size: 14px;
      line-height: 1.6; padding: 32px 24px 64px;
    }}
    .header {{
      display: flex; align-items: baseline;
      justify-content: space-between;
      border-bottom: 1px solid var(--border);
      padding-bottom: 20px; margin-bottom: 28px;
    }}
    .header h1 {{ font-size: 22px; font-weight: 600; color: #fff; }}
    .header h1 span {{ color: var(--accent); }}
    .header .meta {{ font-size: 11px; color: var(--muted); }}
    .nav-link {{
      display: inline-block; margin-bottom: 20px;
      color: var(--accent2); font-size: 12px; text-decoration: none;
    }}
    .nav-link:hover {{ text-decoration: underline; }}
    .filter-bar {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }}
    .filter-btn {{
      padding: 5px 12px; border-radius: 9999px; border: none;
      cursor: pointer; font-family: var(--sans); font-size: 12px;
      font-weight: 600; color: var(--btn-fg); background: var(--btn-bg);
      opacity: 0.65; transition: opacity .15s;
    }}
    .filter-btn.active, .filter-btn:hover {{ opacity: 1; }}
    .filter-btn .count {{
      display: inline-block;
      background: rgba(0,0,0,.15);
      border-radius: 9999px; padding: 0 5px; font-size: 10px;
    }}
    .panel {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 10px; overflow: hidden;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    thead tr {{ background: #0f172a; }}
    th {{
      padding: 10px 12px; text-align: left;
      font-size: 11px; font-weight: 600;
      color: var(--muted); text-transform: uppercase;
      letter-spacing: .05em; border-bottom: 1px solid var(--border);
    }}
    td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr.hidden {{ display: none; }}
    td.num {{ text-align: right; font-family: var(--mono); }}
    .badge {{
      display: inline-block; padding: 2px 8px;
      border-radius: 9999px; font-size: 11px; font-weight: 600;
    }}

    /* ---- Pagination ---- */
    .pagination {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 12px 16px; border-top: 1px solid var(--border);
      background: var(--surface); font-size: 12px; color: var(--muted);
      flex-wrap: wrap; gap: 8px;
    }}
    .pagination .page-info {{ font-family: var(--mono); }}
    .page-btns {{ display: flex; gap: 4px; flex-wrap: wrap; }}
    .page-btn {{
      padding: 4px 10px; border-radius: 6px; border: 1px solid var(--border);
      background: transparent; color: var(--muted); cursor: pointer;
      font-family: var(--sans); font-size: 12px; transition: all .15s;
    }}
    .page-btn:hover {{ border-color: var(--accent2); color: var(--accent2); }}
    .page-btn.active {{
      background: var(--accent2); color: #0f172a;
      border-color: var(--accent2); font-weight: 600;
    }}
    .page-btn:disabled {{ opacity: .3; cursor: not-allowed; }}
    .empty {{
      padding: 48px; text-align: center;
      color: var(--muted); font-size: 13px;
    }}
    .search-input {{
      padding: 5px 12px; border-radius: 9999px;
      border: 1px solid var(--border);
      background: var(--surface); color: var(--text);
      font-family: var(--mono); font-size: 12px;
      outline: none; min-width: 220px;
      transition: border-color .15s;
    }}
    .search-input:focus {{ border-color: var(--accent2); }}
    .search-input::placeholder {{ color: var(--muted); }}
  </style>
</head>
<body>

<div class="header">
  <h1>Link <span>Review</span></h1>
  <div class="meta">Generated {generated_at}</div>
</div>

<a href="index.html" class="nav-link">← Back to Dashboard</a>

<div class="filter-bar">
  {filter_buttons}
  <input id="lr-search" class="search-input" type="search"
         placeholder="Search URL or domain…" autocomplete="off">
</div>

<div class="panel">
  {_table_block}

<div class="pagination" id="lr-pagination">
  <div class="page-info" id="lr-page-info"></div>
  <div class="page-btns" id="lr-page-btns"></div>
</div>
</div>

<script>
// Client-side filter + pagination
  var buttons = document.querySelectorAll('.filter-btn');
  var rows    = document.querySelectorAll('#review-body tr[data-status]');

  var ROWS_PER_PAGE = 50;
  var currentPage   = 1;

  function getVisibleRows() {{
    return Array.from(rows).filter(function(r) {{
      return !r.classList.contains('filter-hidden') &&
             !r.classList.contains('search-hidden');
    }});
  }}

  // Search: match against title text (col 0), domain text (col 1), AND the
  // real link href — so pasting a URL or path fragment matches even though
  // the full URL is only present in the anchor's href/title attributes.
  //
  // Searchable text is indexed ONCE at load time instead of re-reading
  // textContent for every row on every keystroke (630+ rows).
  var searchText = Array.from(rows).map(function(row) {{
    var cells  = row.querySelectorAll('td');
    var anchor = row.querySelector('td a');
    return [
      cells[0] ? cells[0].textContent : '',
      cells[1] ? cells[1].textContent : '',
      anchor   ? (anchor.getAttribute('href') || '') : ''
    ].join(' ').toLowerCase();
  }});

  var searchInput = document.getElementById('lr-search');
  if (searchInput) {{
    searchInput.addEventListener('input', function() {{
      var term = this.value.trim().toLowerCase();
      rows.forEach(function(row, i) {{
        if (!term || searchText[i].includes(term)) {{
          row.classList.remove('search-hidden');
        }} else {{
          row.classList.add('search-hidden');
        }}
      }});
      renderPage(1);
    }});
  }}

  function renderPage(page) {{
    var visible = getVisibleRows();
    var total   = visible.length;
    var pages   = Math.max(1, Math.ceil(total / ROWS_PER_PAGE));
    page        = Math.min(Math.max(1, page), pages);
    currentPage = page;
    var start   = (page - 1) * ROWS_PER_PAGE;
    var end     = start + ROWS_PER_PAGE;
    // BUGFIX: previously `hidden` was removed from all rows and re-applied
    // only to filter-hidden rows — rows excluded by SEARCH never got hidden
    // again, so the search box appeared to do nothing. Hide everything
    // first, then unhide only the current page slice of visible rows.
    Array.from(rows).forEach(function(r) {{ r.classList.add('hidden'); }});
    visible.forEach(function(r, i) {{
      if (i >= start && i < end) {{ r.classList.remove('hidden'); }}
    }});
    var infoEl = document.getElementById('lr-page-info');
    if (infoEl) {{
      var from = total === 0 ? 0 : start + 1;
      var to   = Math.min(end, total);
      infoEl.textContent = total === 0 ? 'No results'
        : 'Showing ' + from + '–' + to + ' of ' + total;
    }}
    var btnsEl = document.getElementById('lr-page-btns');
    if (!btnsEl) return;
    btnsEl.innerHTML = '';
    function makeBtn(label, target, disabled, active) {{
      var b = document.createElement('button');
      b.className = 'page-btn' + (active ? ' active' : '');
      b.textContent = label; b.disabled = disabled;
      if (!disabled) b.onclick = function() {{ renderPage(target); }};
      return b;
    }}
    var wh = 3, pf = Math.max(1, page - wh), pt = Math.min(pages, page + wh);
    btnsEl.appendChild(makeBtn('‹', page - 1, page <= 1, false));
    if (pf > 1) {{
      btnsEl.appendChild(makeBtn('1', 1, false, false));
      if (pf > 2) {{ var e = document.createElement('span'); e.textContent = '…'; e.style.cssText = 'padding:4px 6px;color:var(--muted)'; btnsEl.appendChild(e); }}
    }}
    for (var p = pf; p <= pt; p++) btnsEl.appendChild(makeBtn(p, p, false, p === page));
    if (pt < pages) {{
      if (pt < pages - 1) {{ var e2 = document.createElement('span'); e2.textContent = '…'; e2.style.cssText = 'padding:4px 6px;color:var(--muted)'; btnsEl.appendChild(e2); }}
      btnsEl.appendChild(makeBtn(pages, pages, false, false));
    }}
    btnsEl.appendChild(makeBtn('›', page + 1, page >= pages, false));
  }}

  buttons.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var filter = this.getAttribute('data-filter');
      buttons.forEach(function(b) {{ b.classList.remove('active'); }});
      this.classList.add('active');
      rows.forEach(function(row) {{
        if (filter === 'all' || row.getAttribute('data-status') === filter) {{
          row.classList.remove('filter-hidden');
        }} else {{
          row.classList.add('filter-hidden');
        }}
      }});
      renderPage(1);
    }});
  }});

  renderPage(1);
</script>

</body>
</html>"""

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"[generate_link_review] Written: {OUTPUT_PATH}  ({total} items)")
    return OUTPUT_PATH


if __name__ == "__main__":
    generate()
