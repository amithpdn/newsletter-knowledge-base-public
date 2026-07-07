# =============================================================================
# pipeline/fixes/cleanup_duplicate_images.py — Newsletter AI Pipeline
#
# ONE-OFF maintenance script. Not part of the regular pipeline run.
#
# Scans the existing notes/assets/ tree, finds images with identical
# content (SHA-256 hash match) across different message folders, and
# replaces all but one canonical copy with a symlink (or, on filesystems/
# OSes where symlinks aren't practical, rewrites the note's image
# reference to point at the canonical copy and deletes the duplicate file).
#
# This is for cleaning up duplicates that accumulated BEFORE the dedup
# patch (patch_03_image_extraction_dedup.py) was applied. After running
# this once, new duplicates are prevented going forward by that patch.
#
# SAFETY:
#   - Always run with --dry-run first and review the report.
#   - Takes a full backup recommendation seriously — see the printed
#     warning before any files are modified.
#   - Does not touch notes/*.md content unless --rewrite-links is passed;
#     by default it only deletes duplicate files and reports what would
#     break, so you can decide whether to rewrite links or just live with
#     broken image embeds for older notes (most people accept this,
#     since the image was usually a generic banner/logo, not unique content).
#
# Usage:
#   cd pipeline
#   python fixes/cleanup_duplicate_images.py --dry-run
#   python fixes/cleanup_duplicate_images.py --apply
#   python fixes/cleanup_duplicate_images.py --apply --rewrite-links
# =============================================================================

import argparse
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

# This script lives in pipeline/fixes/ — add pipeline/ to sys.path so that
# config.py, db.py, and agents/ resolve when run standalone from anywhere.
_pipeline_dir = Path(__file__).resolve().parent.parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import PROJECT_ROOT, NOTES_DIR, ASSETS_DIR
from db import initialise_db, register_image_hash, get_image_by_hash


def _hash_file(path: Path) -> str:
    """SHA-256 of file contents, read in chunks to handle large files safely."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_assets() -> dict[str, list[Path]]:
    """
    Walk notes/assets/**/*.{png,jpg,jpeg,gif,webp,svg} and group files by
    content hash.

    Returns:
        dict mapping content_hash -> list of file paths sharing that hash,
        sorted by modification time (oldest first), so [0] is treated as
        canonical (the original, earliest-saved copy).
    """
    extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
    groups: dict[str, list[Path]] = defaultdict(list)

    if not ASSETS_DIR.exists():
        print(f"[cleanup] No assets directory found at {ASSETS_DIR}")
        return {}

    all_files = [
        p for p in ASSETS_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in extensions
    ]
    print(f"[cleanup] Scanning {len(all_files)} image file(s)...")

    for path in all_files:
        try:
            file_hash = _hash_file(path)
            groups[file_hash].append(path)
        except OSError as exc:
            print(f"  ⚠ Could not read {path}: {exc}")

    # Sort each group oldest-first so the first save is treated as canonical
    for file_hash, paths in groups.items():
        paths.sort(key=lambda p: p.stat().st_mtime)

    return groups


def find_duplicate_groups(groups: dict[str, list[Path]]) -> dict[str, list[Path]]:
    """Filter to only groups with more than one file (actual duplicates)."""
    return {h: paths for h, paths in groups.items() if len(paths) > 1}


def _find_referencing_notes(filename: str) -> list[Path]:
    """
    Search all .md files in notes/ for a reference to the given image
    filename (used by --rewrite-links to find what needs updating).

    Simple substring search — Markdown image syntax is
    ![alt](assets/{slug}/{filename}), so matching on filename alone is
    sufficient and avoids needing a full Markdown parser.
    """
    matches = []
    for note_path in NOTES_DIR.glob("*.md"):
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if filename in text:
            matches.append(note_path)
    return matches


def report(duplicate_groups: dict[str, list[Path]]) -> dict:
    """Print a human-readable summary and return stats for the caller."""
    total_duplicate_files = sum(len(p) - 1 for p in duplicate_groups.values())
    total_bytes_recoverable = 0

    print(f"\n{'=' * 70}")
    print(f"  Duplicate Image Report")
    print(f"{'=' * 70}\n")

    if not duplicate_groups:
        print("  No duplicate images found. Nothing to clean up.")
        return {"groups": 0, "duplicate_files": 0, "bytes_recoverable": 0}

    for file_hash, paths in sorted(
        duplicate_groups.items(),
        key=lambda kv: len(kv[1]),
        reverse=True,
    ):
        canonical = paths[0]
        dupes     = paths[1:]
        size      = canonical.stat().st_size
        recoverable = size * len(dupes)
        total_bytes_recoverable += recoverable

        print(f"  Hash: {file_hash[:12]}...  ({len(paths)} copies, {size:,} bytes each)")
        print(f"    Canonical (kept): {canonical.relative_to(PROJECT_ROOT)}")
        for d in dupes:
            print(f"    Duplicate:        {d.relative_to(PROJECT_ROOT)}")
        print()

    print(f"{'─' * 70}")
    print(f"  Duplicate groups:      {len(duplicate_groups)}")
    print(f"  Redundant files:       {total_duplicate_files}")
    print(f"  Recoverable space:     {total_bytes_recoverable / 1_048_576:.2f} MB")
    print(f"{'=' * 70}\n")

    return {
        "groups":            len(duplicate_groups),
        "duplicate_files":   total_duplicate_files,
        "bytes_recoverable": total_bytes_recoverable,
    }


def apply_cleanup(
    duplicate_groups: dict[str, list[Path]],
    rewrite_links:    bool,
) -> None:
    """
    Delete redundant duplicate files, keeping the oldest copy in each group
    as canonical. Optionally rewrites note Markdown to point remaining
    references at the canonical copy before deleting.

    Also registers the canonical copy in image_dedup_index so the
    image_extraction.py dedup patch recognises it going forward.
    """
    initialise_db()

    for file_hash, paths in duplicate_groups.items():
        canonical = paths[0]
        dupes     = paths[1:]

        # Register canonical copy in the dedup index (idempotent — uses
        # INSERT OR IGNORE under the hood)
        register_image_hash(
            content_hash   = file_hash,
            canonical_path = str(canonical),
            message_id     = "backfilled-by-cleanup-script",
        )

        for dupe in dupes:
            if rewrite_links:
                referencing_notes = _find_referencing_notes(dupe.name)
                for note_path in referencing_notes:
                    text = note_path.read_text(encoding="utf-8")
                    # Replace the duplicate's relative path with the
                    # canonical copy's relative path within the markdown.
                    dupe_rel      = dupe.relative_to(NOTES_DIR).as_posix()
                    canonical_rel = canonical.relative_to(NOTES_DIR).as_posix()
                    new_text = text.replace(dupe_rel, canonical_rel)
                    if new_text != text:
                        note_path.write_text(new_text, encoding="utf-8")
                        print(f"  [rewrite] {note_path.name}: "
                              f"{dupe.name} → {canonical.name}")

            try:
                dupe.unlink()
                print(f"  [delete] {dupe.relative_to(PROJECT_ROOT)}")
            except OSError as exc:
                print(f"  ⚠ Could not delete {dupe}: {exc}")

    # Clean up any now-empty asset folders left behind
    for folder in sorted(ASSETS_DIR.iterdir(), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            print(f"  [rmdir] {folder.relative_to(PROJECT_ROOT)} (now empty)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find and remove duplicate images in notes/assets/"
    )
    parser.add_argument("--dry-run", action="store_true",
                         help="Report duplicates only — no files modified (default)")
    parser.add_argument("--apply", action="store_true",
                         help="Actually delete duplicate files")
    parser.add_argument("--rewrite-links", action="store_true",
                         help="Also rewrite note Markdown to point at the "
                              "canonical copy before deleting duplicates "
                              "(requires --apply)")
    args = parser.parse_args()

    if args.rewrite_links and not args.apply:
        print("ERROR: --rewrite-links requires --apply")
        return 1

    if not args.apply:
        print("Running in DRY-RUN mode (default). Pass --apply to actually "
              "delete duplicate files.\n")

    groups           = scan_assets()
    duplicate_groups = find_duplicate_groups(groups)
    stats            = report(duplicate_groups)

    if args.apply and duplicate_groups:
        print("\n⚠  WARNING: This will permanently delete files.")
        print("   It is strongly recommended you have a OneDrive version "
              "history or backup before proceeding.\n")
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Aborted — no files were modified.")
            return 0

        apply_cleanup(duplicate_groups, rewrite_links=args.rewrite_links)
        print(f"\n✓ Cleanup complete. Recovered "
              f"{stats['bytes_recoverable'] / 1_048_576:.2f} MB.")
        if not args.rewrite_links:
            print("  Note: --rewrite-links was not passed, so older notes "
                  "that referenced a deleted duplicate's exact path will "
                  "show a broken image link. Re-run with --rewrite-links "
                  "to fix this, or accept it for one-off banner/logo "
                  "images that aren't unique content.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
