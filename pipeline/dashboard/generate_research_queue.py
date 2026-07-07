# pipeline/dashboard/generate_research_queue.py — Newsletter AI Pipeline
# Generates dashboard/research_queue.html — an interactive page for reviewing
# and selectively researching topics identified by Agent 3 (Topic Linking)
# but not yet researched (because --no-research was passed or
# MAX_RESEARCH_TOPICS_PER_RUN was 0 when the pipeline ran).
#
# The generated page:
#   - Shows all pending, done, and skipped topics from research_queue in db
#   - Lets you select topics with checkboxes and click "Research Selected"
#   - Calls POST /api/research on serve_dashboard.py for selected topics
#   - Calls POST /api/skip for dismissed topics
#   - Updates the table in-place as each topic completes (no page reload)
#   - Shows per-topic cost and a running total
#
# REQUIRES serve_dashboard.py to be running for the Research/Skip buttons
# to work (they call /api/research and /api/skip). The page can be opened
# as a static file too — checkboxes and filtering work without a server, but
# the action buttons won't fire without serve_dashboard.py running.
#
# Usage:
#   python main.py dashboard      (regenerates all three pages)
#   python pipeline\dashboard\generate_research_queue.py   (this page only)
#
# Then open with the server:
#   python pipeline\serve_dashboard.py --page research_queue.html
#
# Output: dashboard/research_queue.html
# =============================================================================

import sys
from datetime import datetime, timezone
from pathlib import Path

# This script lives in pipeline/dashboard/ — add pipeline/ to sys.path so
# config.py and db.py resolve when run standalone from anywhere.
_pipeline_dir = Path(__file__).resolve().parent.parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import DASHBOARD_DIR
from db import initialise_db, get_research_queue, get_queue_stats

OUTPUT_PATH = DASHBOARD_DIR / "research_queue.html"


def _fmt_ts(ts: str) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M")
    except Exception:
        return ts[:16]


def _status_badge(status: str) -> str:
    cfg = {
        "pending":  ("#d97706", "#fef3c7", "Pending"),
        "done":     ("#16a34a", "#dcfce7", "Done"),
        "skipped":  ("#6b7280", "#f3f4f6", "Skipped"),
    }
    fg, bg, label = cfg.get(status, ("#374151", "#f9fafb", status.title()))
    return (
        f'<span class="badge" style="background:{bg};color:{fg}">{label}</span>'
    )


def _topic_row(row: dict) -> str:
    topic   = row.get("topic", "")
    status  = row.get("status", "pending")
    subject = (row.get("source_subject") or "—")[:55]
    account = row.get("source_account") or "—"
    ts      = _fmt_ts(row.get("queued_at", ""))
    done_ts = _fmt_ts(row.get("researched_at") or "")
    cost    = row.get("cost_usd")
    note    = Path(row.get("note_path") or "").name or "—"

    cost_str  = f"${cost:.4f}" if cost is not None else "—"
    done_info = f"{done_ts}  {cost_str}" if status == "done" else ""
    checkbox  = (
        f'<input type="checkbox" class="topic-cb" data-topic="{topic}">'
        if status == "pending" else ""
    )

    return f"""
      <tr class="queue-row" data-status="{status}" id="row-{topic.replace(' ', '_')}">
        <td style="width:32px">{checkbox}</td>
        <td>
          <span class="topic-label">{topic}</span>
          <div id="summary-{topic.replace(' ', '_')}"
               class="summary-block" style="display:none"></div>
        </td>
        <td style="color:#6b7280;font-size:12px;max-width:220px;overflow:hidden;
            text-overflow:ellipsis;white-space:nowrap"
            title="{subject}">{subject}</td>
        <td style="color:#6b7280;font-size:12px">{account}</td>
        <td style="color:#6b7280;font-size:12px">{ts}</td>
        <td>{_status_badge(status)}</td>
        <td style="color:#6b7280;font-size:12px;white-space:nowrap">
          {done_info or note}
        </td>
      </tr>"""


def generate() -> Path:
    """Query research_queue and write research_queue.html."""
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    initialise_db()

    all_rows = get_research_queue()   # all statuses, newest first
    stats    = get_queue_stats()
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y at %H:%M UTC")

    pending_count = stats.get("pending", 0)
    done_count    = stats.get("done", 0)
    skipped_count = stats.get("skipped", 0)
    total_cost    = stats.get("total_cost_usd", 0.0)

    table_rows = "".join(_topic_row(r) for r in all_rows)

    # Pre-compute the conditional table block — nested f-strings inside
    # f-string expressions are not supported in Python < 3.12.
    if all_rows:
        _table_block = f"""
  <table>
    <thead>
      <tr>
        <th></th>
        <th>Topic</th>
        <th>Source Article / Email</th>
        <th>Account</th>
        <th>Queued</th>
        <th>Status</th>
        <th>Researched / Note</th>
      </tr>
    </thead>
    <tbody id="queue-body">
      {table_rows}
    </tbody>
  </table>

<div class="pagination" id="q-pagination">
  <div class="page-info" id="q-page-info"></div>
  <div class="page-btns" id="q-page-btns"></div>
</div>"""
    else:
        _table_block = '<div class="empty">Research queue is empty — run a pipeline with --no-research or set MAX_RESEARCH_TOPICS_PER_RUN=0 to populate it.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Research Queue — Newsletter Pipeline</title>
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
      --danger:  #f87171;
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
      padding-bottom: 20px; margin-bottom: 24px;
    }}
    .header h1 {{ font-size: 22px; font-weight: 600; color: #fff; }}
    .header h1 span {{ color: var(--accent); }}
    .header .meta {{ font-size: 11px; color: var(--muted); }}
    .nav-link {{
      display: inline-block; margin-bottom: 20px;
      color: var(--accent2); font-size: 12px; text-decoration: none;
    }}
    .nav-link:hover {{ text-decoration: underline; }}

    /* ---- Stat cards ---- */
    .stat-strip {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }}
    .stat {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; padding: 12px 18px; min-width: 120px;
    }}
    .stat-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase;
                   letter-spacing: .05em; }}
    .stat-value {{ font-size: 22px; font-weight: 600; color: #fff; margin-top: 2px; }}

    /* ---- Toolbar ---- */
    .toolbar {{
      display: flex; gap: 10px; align-items: center;
      flex-wrap: wrap; margin-bottom: 16px;
    }}
    .btn {{
      padding: 7px 16px; border-radius: 7px; border: none;
      font-family: var(--sans); font-size: 13px; font-weight: 600;
      cursor: pointer; transition: opacity .15s;
    }}
    .btn:hover {{ opacity: .85; }}
    .btn:disabled {{ opacity: .4; cursor: not-allowed; }}
    .btn-primary  {{ background: var(--accent); color: #0f172a; }}
    .btn-skip     {{ background: var(--surface); color: var(--muted);
                     border: 1px solid var(--border); }}
    .btn-select   {{ background: transparent; color: var(--accent2);
                     border: 1px solid var(--accent2); font-size: 12px;
                     padding: 5px 12px; }}
    .filter-bar   {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }}
    .filter-btn {{
      padding: 4px 12px; border-radius: 9999px; border: none;
      cursor: pointer; font-family: var(--sans); font-size: 12px;
      font-weight: 600; opacity: 0.6; transition: opacity .15s;
    }}
    .filter-btn.active, .filter-btn:hover {{ opacity: 1; }}

    /* ---- Status bar ---- */
    #status-bar {{
      padding: 10px 16px; border-radius: 8px; margin-bottom: 16px;
      font-size: 13px; display: none;
    }}
    #status-bar.running  {{ background: #1e3a5f; border: 1px solid #2563eb; color: #93c5fd; }}
    #status-bar.done     {{ background: #14532d; border: 1px solid #16a34a; color: #86efac; }}
    #status-bar.error    {{ background: #450a0a; border: 1px solid #dc2626; color: #fca5a5; }}

    /* ---- Table ---- */
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
    td {{ padding: 10px 12px; border-bottom: 1px solid var(--border);
          vertical-align: top; }}
    tr:last-child td {{ border-bottom: none; }}
    tr.hidden {{ display: none; }}
    tr.researching td {{ background: #1e2d40; }}
    tr.done-flash td {{ background: #14532d; transition: background 1.5s; }}

    .topic-label {{ font-weight: 600; color: #fff; }}
    .badge {{
      display: inline-block; padding: 2px 8px;
      border-radius: 9999px; font-size: 11px; font-weight: 600;
    }}
    .summary-block {{
      margin-top: 8px; padding: 10px 12px;
      background: #0f172a; border-radius: 6px;
      font-size: 12px; color: #cbd5e1; line-height: 1.7;
      border-left: 2px solid var(--accent);
    }}
    .spinner {{
      display: inline-block; width: 12px; height: 12px;
      border: 2px solid #334155; border-top-color: var(--accent);
      border-radius: 50%; animation: spin .7s linear infinite;
      vertical-align: middle; margin-right: 6px;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .empty {{ padding: 48px; text-align: center; color: var(--muted); font-size: 13px; }}

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
    .cost-total {{
      margin-top: 14px; font-size: 12px; color: var(--muted);
      text-align: right;
    }}
    #running-cost {{ color: var(--accent3, #fbbf24); font-weight: 600; }}
    input[type=checkbox] {{ width: 15px; height: 15px; cursor: pointer; accent-color: var(--accent); }}
  </style>
</head>
<body>

<div class="header">
  <h1>Research <span>Queue</span></h1>
  <div class="meta">Generated {generated_at}</div>
</div>

<a href="index.html" class="nav-link">← Back to Dashboard</a>

<!-- ---- Stat strip ---- -->
<div class="stat-strip">
  <div class="stat">
    <div class="stat-label">Pending</div>
    <div class="stat-value" id="stat-pending">{pending_count}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Done</div>
    <div class="stat-value" style="color:var(--accent)">{done_count}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Skipped</div>
    <div class="stat-value" style="color:var(--muted)">{skipped_count}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Queue Cost</div>
    <div class="stat-value" style="font-size:18px">${total_cost:.4f}</div>
  </div>
</div>

<!-- ---- Toolbar ---- -->
<div class="toolbar">
  <button class="btn btn-primary" id="btn-research" disabled
          onclick="researchSelected()">
    Research Selected
  </button>
  <button class="btn btn-skip" id="btn-skip" disabled
          onclick="skipSelected()">
    Skip Selected
  </button>
  <button class="btn btn-select" onclick="selectAll()">Select All Pending</button>
  <button class="btn btn-select" onclick="selectNone()">Clear Selection</button>
</div>

<!-- ---- Status bar ---- -->
<div id="status-bar"></div>

<!-- ---- Filter buttons ---- -->
<div class="filter-bar">
  <button class="filter-btn active"
          style="background:#374151;color:#fff"
          data-filter="all">All ({len(all_rows)})</button>
  <button class="filter-btn"
          style="background:#fef3c7;color:#d97706"
          data-filter="pending">Pending ({pending_count})</button>
  <button class="filter-btn"
          style="background:#dcfce7;color:#16a34a"
          data-filter="done">Done ({done_count})</button>
  <button class="filter-btn"
          style="background:#f3f4f6;color:#6b7280"
          data-filter="skipped">Skipped ({skipped_count})</button>
</div>

<!-- ---- Queue table ---- -->
<div class="panel">
  {_table_block}
</div>

<div class="cost-total">
  This session: <span id="running-cost">$0.0000</span>
</div>

<script>
  var sessionCost = 0;

  // ---- Selection management ----
  function getChecked() {{
    return Array.from(document.querySelectorAll('.topic-cb:checked'))
                .map(cb => cb.getAttribute('data-topic'));
  }}

  function updateButtons() {{
    var checked = getChecked().length > 0;
    document.getElementById('btn-research').disabled = !checked;
    document.getElementById('btn-skip').disabled     = !checked;
  }}

  document.addEventListener('change', function(e) {{
    if (e.target.classList.contains('topic-cb')) updateButtons();
  }});

  function selectAll() {{
    document.querySelectorAll('.topic-cb').forEach(function(cb) {{
      cb.checked = true;
    }});
    updateButtons();
  }}

  function selectNone() {{
    document.querySelectorAll('.topic-cb').forEach(function(cb) {{
      cb.checked = false;
    }});
    updateButtons();
  }}

  // ---- Filter ----
  document.querySelectorAll('.filter-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      this.classList.add('active');
      var filter = this.getAttribute('data-filter');
      document.querySelectorAll('#queue-body tr.queue-row').forEach(function(row) {{
        if (filter === 'all' || row.getAttribute('data-status') === filter) {{
          row.classList.remove('hidden');
        }} else {{
          row.classList.add('hidden');
        }}
      }});
    }});
  }});

  // ---- Status bar helpers ----
  function setStatus(msg, type) {{
    var bar = document.getElementById('status-bar');
    bar.textContent = msg;
    bar.className   = type;
    bar.style.display = msg ? 'block' : 'none';
  }}

  // ---- Research action ----
  async function researchSelected() {{
    var topics = getChecked();
    if (!topics.length) return;

    var researchBtn = document.getElementById('btn-research');
    var skipBtn     = document.getElementById('btn-skip');
    researchBtn.disabled = true;
    skipBtn.disabled     = true;
    setStatus('<span class="spinner"></span>Researching ' + topics.length + ' topic(s) — this may take up to ' + (topics.length * 20) + 's…', 'running');

    // Mark rows as in-progress visually
    topics.forEach(function(topic) {{
      var row = document.getElementById('row-' + topic.replace(/ /g, '_'));
      if (row) row.classList.add('researching');
    }});

    try {{
      var resp = await fetch('/api/research', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{topics: topics}})
      }});

      if (!resp.ok) {{
        setStatus('Server error: ' + resp.status, 'error');
        return;
      }}

      var data = await resp.json();
      var successCount = 0;
      var errorCount   = 0;

      data.results.forEach(function(result) {{
        var row = document.getElementById('row-' + result.topic.replace(/ /g, '_'));
        row && row.classList.remove('researching');

        if (result.status === 'done') {{
          successCount++;
          sessionCost += (result.cost_usd || 0);

          // Show summary inline
          var summaryDiv = document.getElementById('summary-' + result.topic.replace(/ /g, '_'));
          if (summaryDiv && result.summary) {{
            summaryDiv.textContent = result.summary;
            summaryDiv.style.display = 'block';
          }}

          // Update row status badge
          if (row) {{
            row.setAttribute('data-status', 'done');
            var badge = row.querySelector('.badge');
            if (badge) {{
              badge.style.background = '#dcfce7';
              badge.style.color      = '#16a34a';
              badge.textContent      = 'Done';
            }}
            // Remove checkbox
            var cb = row.querySelector('.topic-cb');
            if (cb) cb.remove();

            // Flash green
            row.classList.add('done-flash');
          }}
        }} else {{
          errorCount++;
          if (row) {{
            row.classList.remove('researching');
            var badge = row.querySelector('.badge');
            if (badge) {{
              badge.style.background = '#fee2e2';
              badge.style.color      = '#dc2626';
              badge.textContent      = 'Error';
            }}
          }}
        }}
      }});

      document.getElementById('running-cost').textContent =
        '$' + sessionCost.toFixed(4);

      var statPending = document.getElementById('stat-pending');
      if (statPending) {{
        var current = parseInt(statPending.textContent) || 0;
        statPending.textContent = Math.max(0, current - successCount);
      }}

      if (errorCount > 0) {{
        setStatus(successCount + ' researched, ' + errorCount + ' failed — check console for details.', 'error');
      }} else {{
        setStatus(successCount + ' topic(s) researched. Notes have been updated.', 'done');
      }}

    }} catch (err) {{
      setStatus('Request failed: ' + err.message + ' — is serve_dashboard.py running?', 'error');
      topics.forEach(function(topic) {{
        var row = document.getElementById('row-' + topic.replace(/ /g, '_'));
        if (row) row.classList.remove('researching');
      }});
    }} finally {{
      updateButtons();
    }}
  }}

  // ---- Skip action ----
  async function skipSelected() {{
    var topics = getChecked();
    if (!topics.length) return;

    try {{
      await fetch('/api/skip', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{topics: topics}})
      }});

      topics.forEach(function(topic) {{
        var row = document.getElementById('row-' + topic.replace(/ /g, '_'));
        if (row) {{
          row.setAttribute('data-status', 'skipped');
          var badge = row.querySelector('.badge');
          if (badge) {{
            badge.style.background = '#f3f4f6';
            badge.style.color      = '#6b7280';
            badge.textContent      = 'Skipped';
          }}
          var cb = row.querySelector('.topic-cb');
          if (cb) cb.remove();
        }}
      }});

      var statPending = document.getElementById('stat-pending');
      if (statPending) {{
        var current = parseInt(statPending.textContent) || 0;
        statPending.textContent = Math.max(0, current - topics.length);
      }}

      setStatus(topics.length + ' topic(s) skipped.', 'done');
      updateButtons();

    }} catch (err) {{
      setStatus('Skip failed: ' + err.message + ' — is serve_dashboard.py running?', 'error');
    }}
  }}

  // ---- Pagination (25 rows/page) ----
  var ROWS_PER_PAGE = 25;
  var currentPage   = 1;

  function getVisibleRows() {{
    return Array.from(document.querySelectorAll('#queue-body tr.queue-row'))
                .filter(function(r) {{ return !r.classList.contains('filter-hidden'); }});
  }}

  function renderPage(page) {{
    var visible = getVisibleRows();
    var total   = visible.length;
    var pages   = Math.max(1, Math.ceil(total / ROWS_PER_PAGE));
    page        = Math.min(Math.max(1, page), pages);
    currentPage = page;
    var start   = (page - 1) * ROWS_PER_PAGE;
    var end     = start + ROWS_PER_PAGE;
    document.querySelectorAll('#queue-body tr.queue-row').forEach(function(r) {{
      r.classList.remove('hidden');
    }});
    document.querySelectorAll('#queue-body tr.queue-row.filter-hidden').forEach(function(r) {{
      r.classList.add('hidden');
    }});
    visible.forEach(function(r, i) {{
      if (i >= start && i < end) {{ r.classList.remove('hidden'); }}
      else                       {{ r.classList.add('hidden');    }}
    }});
    var infoEl = document.getElementById('q-page-info');
    if (infoEl) {{
      var from = total === 0 ? 0 : start + 1;
      var to   = Math.min(end, total);
      infoEl.textContent = total === 0 ? 'No results'
        : 'Showing ' + from + '–' + to + ' of ' + total;
    }}
    var btnsEl = document.getElementById('q-page-btns');
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

  // Override filter buttons — use filter-hidden so pagination tracks visibility
  document.querySelectorAll('.filter-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      this.classList.add('active');
      var filter = this.getAttribute('data-filter');
      document.querySelectorAll('#queue-body tr.queue-row').forEach(function(row) {{
        if (filter === 'all' || row.getAttribute('data-status') === filter) {{
          row.classList.remove('filter-hidden');
        }} else {{
          row.classList.add('filter-hidden');
        }}
      }});
      renderPage(1);
    }});
  }});

  // Override selectAll — only selects rows visible on the current page
  function selectAll() {{
    document.querySelectorAll('#queue-body tr.queue-row:not(.hidden) .topic-cb')
            .forEach(function(cb) {{ cb.checked = true; }});
    updateButtons();
  }}

  renderPage(1);
</script>

</body>
</html>"""

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"[generate_research_queue] Written: {OUTPUT_PATH}  "
          f"({stats.get('pending', 0)} pending, "
          f"{stats.get('done', 0)} done, "
          f"{stats.get('skipped', 0)} skipped)")
    return OUTPUT_PATH


if __name__ == "__main__":
    generate()
