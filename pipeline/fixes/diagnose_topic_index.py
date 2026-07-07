# =============================================================================
# pipeline/fixes/diagnose_topic_index.py — Newsletter AI Pipeline
#
# ONE-OFF diagnostic/repair script. Not part of the regular pipeline run.
#
# Scans every row in topic_index and attempts to deserialise its
# embedding_vector blob with pickle.loads() (matching the real
# _deserialise_embedding() in agents/topic_linking.py). Reports which
# rows are corrupted (truncated/unreadable pickle data) and offers two
# repair options:
#
#   1. --apply-delete   : delete corrupted rows entirely. The tag survives
#                          on its actual notes (frontmatter is untouched),
#                          but loses its place in topic linking/Related
#                          Notes until the next time a note with that tag
#                          is processed (which will re-create the row with
#                          a fresh, valid embedding).
#
#   2. --apply-reembed  : re-compute the embedding for the corrupted row's
#                          tag text and write a fresh, valid blob in place,
#                          preserving note_files/first_seen as-is. This is
#                          the better option in almost all cases, since it
#                          fixes the row without losing any history.
#
# Run --apply-reembed BEFORE running retag_existing_notes.py or
# retrofit_related_notes.py, since both scripts read embedding_vector and
# will hit the same crash on any row this script doesn't first fix.
#
# Usage:
#   cd pipeline
#   python fixes/diagnose_topic_index.py                  # report only, no changes
#   python fixes/diagnose_topic_index.py --apply-reembed   # fix in place (recommended)
#   python fixes/diagnose_topic_index.py --apply-delete    # remove corrupted rows
# =============================================================================

import argparse
import pickle
import sys
from pathlib import Path

# This script lives in pipeline/fixes/ — add pipeline/ to sys.path so that
# config.py, db.py, and agents/ resolve when run standalone from anywhere.
_pipeline_dir = Path(__file__).resolve().parent.parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

import numpy as np

from db import get_connection


def _try_deserialise(blob: bytes) -> tuple[bool, str]:
    """
    Attempt to deserialise an embedding blob exactly as
    agents/topic_linking.py's _deserialise_embedding() does.

    Returns:
        (is_valid, error_message)
    """
    if blob is None:
        return False, "embedding_vector is NULL"
    try:
        vec = pickle.loads(blob).astype(np.float32)
        if vec.size == 0:
            return False, "deserialised to an empty array"
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def scan() -> tuple[list[dict], list[dict]]:
    """
    Returns (healthy_rows, corrupted_rows), each a list of dicts with
    tag, note_files, and (for corrupted) the error message.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT tag, first_seen, first_seen_account, note_files, embedding_vector FROM topic_index"
        ).fetchall()
    finally:
        conn.close()

    healthy, corrupted = [], []
    for row in rows:
        is_valid, error = _try_deserialise(row["embedding_vector"])
        entry = dict(row)
        if is_valid:
            healthy.append(entry)
        else:
            entry["error"] = error
            corrupted.append(entry)

    return healthy, corrupted


def report(healthy: list[dict], corrupted: list[dict]) -> None:
    print(f"\n{'=' * 70}")
    print(f"  topic_index Integrity Report")
    print(f"{'=' * 70}\n")
    print(f"  Total rows:      {len(healthy) + len(corrupted)}")
    print(f"  Healthy:         {len(healthy)}")
    print(f"  Corrupted:       {len(corrupted)}\n")

    if corrupted:
        print(f"  Corrupted rows:")
        for row in corrupted:
            note_count = len(row.get("note_files") and __import__("json").loads(row["note_files"]) or [])
            print(f"    - \"{row['tag']}\"  ({note_count} linked note(s))")
            print(f"        error: {row['error']}")
        print()
    print(f"{'=' * 70}\n")


def reembed_corrupted(corrupted: list[dict]) -> int:
    """Re-compute and write a fresh, valid embedding for each corrupted row."""
    from sentence_transformers import SentenceTransformer

    if not corrupted:
        return 0

    print("[diagnose] Loading embedding model 'all-MiniLM-L6-v2'...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    tags = [row["tag"] for row in corrupted]
    embeddings = model.encode(tags)

    conn = get_connection()
    try:
        for row, emb in zip(corrupted, embeddings):
            blob = pickle.dumps(np.asarray(emb, dtype=np.float32))
            conn.execute(
                "UPDATE topic_index SET embedding_vector = ? WHERE tag = ?",
                (blob, row["tag"]),
            )
        conn.commit()
    finally:
        conn.close()

    return len(corrupted)


def delete_corrupted(corrupted: list[dict]) -> int:
    """Delete corrupted rows entirely from topic_index."""
    if not corrupted:
        return 0

    conn = get_connection()
    try:
        for row in corrupted:
            conn.execute("DELETE FROM topic_index WHERE tag = ?", (row["tag"],))
        conn.commit()
    finally:
        conn.close()

    return len(corrupted)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose and optionally repair corrupted embedding_vector "
                     "rows in topic_index (e.g. 'pickle data was truncated' errors)."
    )
    parser.add_argument("--apply-reembed", action="store_true",
                         help="Re-compute and write a fresh embedding for each "
                              "corrupted row (recommended — preserves history)")
    parser.add_argument("--apply-delete", action="store_true",
                         help="Delete corrupted rows entirely (loses topic-linking "
                              "history for that tag until it next appears in a note)")
    args = parser.parse_args()

    if args.apply_reembed and args.apply_delete:
        print("ERROR: choose only one of --apply-reembed or --apply-delete.")
        return 1

    healthy, corrupted = scan()
    report(healthy, corrupted)

    if not corrupted:
        print("No corrupted rows found. Safe to proceed with retag_existing_notes.py "
              "and retrofit_related_notes.py.")
        return 0

    if not args.apply_reembed and not args.apply_delete:
        print("Report only — no changes made. Re-run with --apply-reembed "
              "(recommended) or --apply-delete to fix the rows above.")
        return 0

    print("\n⚠  This will modify registry.db directly.")
    print("   Recommended: ensure OneDrive sync is paused and you have a recent "
          "backup before proceeding.\n")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted — no changes were made.")
        return 0

    if args.apply_reembed:
        count = reembed_corrupted(corrupted)
        print(f"\n✓ Re-embedded {count} row(s). They should now read correctly.")
    else:
        count = delete_corrupted(corrupted)
        print(f"\n✓ Deleted {count} corrupted row(s) from topic_index.")
        print("  These tags will re-appear automatically the next time a note "
              "with that tag is processed by the pipeline.")

    print("\nRe-run this script with no flags to confirm 0 corrupted rows remain "
          "before proceeding to retag_existing_notes.py / retrofit_related_notes.py.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
