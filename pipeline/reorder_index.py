# =============================================================================
# pipeline/reorder_index.py — Knowledge Base Pipeline v5.4
# INDEX.md Reorder Utility
#
# INDEX.md rows are appended in PROCESSING order (whichever email/article
# happens to be handled next — interleaved across accounts, link batches,
# and run-to-run), not in chronological RECEIVED order. Over many runs this
# makes the table visually out of order even though every row's Date column
# is itself correct.
#
# This script re-sorts the existing rows by the Date column (descending —
# newest first, matching how most people scan a chronological index) and
# rewrites INDEX.md in place. It does not touch any note files, the
# database, or topics_index.json — it only reorders rows in INDEX.md.
#
# Safe to run repeatedly. Parses the existing table, sorts, rewrites.
# Malformed or unparseable rows are left untouched and appended at the end
# under a "Needs Review" marker rather than silently dropped.
#
# Usage:
#   # Run after any pipeline execution to tidy INDEX.md
#   python pipeline\reorder_index.py
#
#   # Preview the reordered result without writing
#   python pipeline\reorder_index.py --dry-run
#
#   # Sort oldest-first instead of newest-first
#   python pipeline\reorder_index.py --ascending
# =============================================================================

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_pipeline_dir = Path(__file__).resolve().parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import INDEX_MD

# Expected header — written by local_writer.py's update_index().
# If INDEX.md's actual header differs (e.g. you customised it), the parser
# below still works off the table separator line, not this constant — this
# is only used when (re)writing the header on output.
_HEADER_TITLE = "# Newsletter Pipeline — Note Index"
_HEADER_ROW   = "| Date | Subject | Account | Tags |"
_SEPARATOR_ROW = "|------|---------|---------|------|"

# Matches a markdown table data row: | col | col | col | col |
_ROW_PATTERN = re.compile(r"^\|(.+)\|\s*$")

# Matches a YYYY-MM-DD date at the start of the first column
_DATE_PATTERN = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*$")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reorder INDEX.md rows by date.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Run after a pipeline execution:  python reorder_index.py\n"
            "  Preview without writing:         python reorder_index.py --dry-run\n"
            "  Oldest first instead of newest:  python reorder_index.py --ascending\n"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the reordered table without writing to INDEX.md",
    )
    parser.add_argument(
        "--ascending", action="store_true",
        help="Sort oldest-first instead of the default newest-first",
    )
    return parser.parse_args()


def _read_rows(index_path: Path) -> tuple[list[str], list[str]]:
    """
    Read INDEX.md and split it into (title_lines, data_rows).

    title_lines: everything up to and including the table separator row
                 (title, blank line, header row, separator row) — preserved
                 verbatim on rewrite.
    data_rows:   every subsequent non-empty markdown table row.

    Returns ([], []) if the file doesn't exist yet.
    """
    if not index_path.exists():
        return [], []

    lines = index_path.read_text(encoding="utf-8").splitlines()

    # Find the separator row (the line of dashes) — everything up to and
    # including it is the "header block" we preserve as-is.
    sep_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^\|[\s\-:|]+\|\s*$", line):
            sep_idx = i
            break

    if sep_idx is None:
        # No table structure found at all — treat whole file as unparseable,
        # caller will fall back to writing a fresh header.
        return [], []

    title_lines = lines[: sep_idx + 1]
    data_rows   = [l for l in lines[sep_idx + 1:] if l.strip()]

    return title_lines, data_rows


def _row_sort_key(row: str) -> str:
    """
    Extract the date string from a data row's first column for sorting.

    Returns the row's date if parseable, otherwise an empty string so
    unparseable rows sort first under ascending and last under descending
    (kept visible rather than silently reordered into the middle).
    """
    match = _ROW_PATTERN.match(row)
    if not match:
        return ""

    columns = match.group(1).split("|")
    if not columns:
        return ""

    first_col = columns[0]
    date_match = _DATE_PATTERN.match(first_col)
    return date_match.group(1) if date_match else ""


def reorder_index(index_path: Path = INDEX_MD, ascending: bool = False) -> dict:
    """
    Read INDEX.md, sort its data rows by date, rewrite the file.

    Args:
        index_path: Path to INDEX.md (defaults to config.INDEX_MD).
        ascending:  If True, oldest-first. Default is newest-first.

    Returns:
        Summary dict: {total_rows, dated_rows, undated_rows}
    """
    title_lines, data_rows = _read_rows(index_path)

    if not data_rows:
        print(f"[reorder_index] No data rows found in {index_path.name} — nothing to do.")
        return {"total_rows": 0, "dated_rows": 0, "undated_rows": 0}

    dated_rows   = [r for r in data_rows if _row_sort_key(r)]
    undated_rows = [r for r in data_rows if not _row_sort_key(r)]

    dated_rows.sort(key=_row_sort_key, reverse=not ascending)

    if not title_lines:
        title_lines = [_HEADER_TITLE, "", _HEADER_ROW, _SEPARATOR_ROW]

    output_lines = list(title_lines) + dated_rows

    if undated_rows:
        output_lines.append("")
        output_lines.append("<!-- Rows below could not be date-sorted — review manually -->")
        output_lines.extend(undated_rows)

    output_text = "\n".join(output_lines) + "\n"

    print(f"[reorder_index] {len(dated_rows)} row(s) sorted "
          f"({'ascending' if ascending else 'descending'}), "
          f"{len(undated_rows)} row(s) unparseable")

    return {
        "total_rows":   len(data_rows),
        "dated_rows":   len(dated_rows),
        "undated_rows": len(undated_rows),
        "output_text":  output_text,
    }


if __name__ == "__main__":
    args = _parse_args()

    result = reorder_index(ascending=args.ascending)

    if result["total_rows"] == 0:
        sys.exit(0)

    if args.dry_run:
        print(f"\n{'=' * 55}")
        print("  DRY RUN — preview only, INDEX.md not modified")
        print(f"{'=' * 55}\n")
        print(result["output_text"])
    else:
        INDEX_MD.write_text(result["output_text"], encoding="utf-8")
        print(f"[reorder_index] INDEX.md rewritten: "
              f"{result['dated_rows']} row(s) reordered, "
              f"{result['undated_rows']} unparseable row(s) appended at end")
