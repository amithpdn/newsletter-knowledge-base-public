# =============================================================================
# pipeline/fixes/retrofit_related_notes.py — Newsletter AI Pipeline
#
# ONE-OFF maintenance script. Not part of the regular pipeline run.
#
# Re-runs find_related_notes() against each existing note's tags (read
# from its YAML frontmatter) and REPLACES that note's ## Related Notes
# section with the result under the current RELATED_NOTES_MIN_SHARED_TAGS
# / RELATED_NOTES_MAX_RESULTS settings.
#
# Use this once, after applying patch_05_topic_linking_related_notes_cap.py,
# to retroactively trim already-bloated Related Notes sections in your
# existing notes/ folder.
#
# Safe to re-run — it fully replaces the section each time rather than
# appending, so running it twice produces the same result as running it
# once (idempotent).
#
# Usage:
#   cd pipeline
#   python fixes/retrofit_related_notes.py --dry-run
#   python fixes/retrofit_related_notes.py --apply
# =============================================================================

import argparse
import re
import sys
import yaml  # already a transitive dependency via other tooling; if not
             # installed, `pip install pyyaml --break-system-packages`
from pathlib import Path

# This script lives in pipeline/fixes/ — add pipeline/ to sys.path so that
# config.py, db.py, and agents/ resolve when run standalone from anywhere.
_pipeline_dir = Path(__file__).resolve().parent.parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import NOTES_DIR
from agents.topic_linking import find_related_notes


_RELATED_SECTION_PATTERN = re.compile(
    r"\n## Related Notes\n(?:- .+\n?)*",
    re.MULTILINE,
)


def _extract_frontmatter_tags(content: str) -> list[str]:
    """
    Parse the YAML frontmatter block and return the `tags` list.
    Returns [] if no frontmatter or no tags key is present.
    """
    if not content.startswith("---"):
        return []
    end = content.find("\n---\n", 3)
    if end == -1:
        return []
    raw_yaml = content[3:end]
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        print(f"  ⚠ Could not parse frontmatter: {exc}")
        return []
    return data.get("tags", []) or []


def _build_related_section(related_notes: list[dict]) -> str:
    """
    Identical rendering logic to agents/local_writer.py's
    _build_related_section(), duplicated here so this script has no
    dependency on local_writer's internals beyond what's needed.
    """
    if not related_notes:
        return ""

    lines = ["\n## Related Notes"]
    for note in related_notes:
        note_stem   = Path(note["file"]).stem
        shared_tags = ", ".join(note.get("shared_tags", []))
        account_tag = f" *(from: {note['account_alias']})*" if note.get("account_alias") else ""
        lines.append(f"- [[{note_stem}]] — shared tags: {shared_tags}{account_tag}")

    return "\n".join(lines) + "\n"


def retrofit_note(note_path: Path, dry_run: bool) -> tuple[bool, int, int]:
    """
    Re-run topic linking for one note and replace its Related Notes section.

    Returns:
        (was_modified, old_count, new_count)
    """
    content = note_path.read_text(encoding="utf-8")
    tags    = _extract_frontmatter_tags(content)

    if not tags:
        return False, 0, 0

    # Count existing related notes (for reporting) by counting bullet
    # lines under the current ## Related Notes section, if present.
    existing_match = _RELATED_SECTION_PATTERN.search(content)
    old_count = existing_match.group(0).count("\n- ") if existing_match else 0

    related_notes = find_related_notes(tags, current_note_file=note_path.name)
    new_section    = _build_related_section(related_notes)
    new_count      = len(related_notes)

    if existing_match:
        new_content = content[:existing_match.start()] + new_section + content[existing_match.end():]
    elif new_section:
        # No existing section but new results found — append at end.
        new_content = content.rstrip("\n") + "\n" + new_section
    else:
        new_content = content  # nothing to change

    if new_content == content:
        return False, old_count, new_count

    if not dry_run:
        note_path.write_text(new_content, encoding="utf-8")

    return True, old_count, new_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retroactively re-trim ## Related Notes sections in "
                     "existing notes under the current threshold/cap settings."
    )
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would change without writing files")
    parser.add_argument("--apply", action="store_true",
                         help="Actually write the trimmed sections")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        args.dry_run = True
        print("No mode specified — defaulting to --dry-run.\n")

    note_files = sorted(NOTES_DIR.glob("*.md"))
    print(f"[retrofit] Found {len(note_files)} note(s) in {NOTES_DIR}\n")

    total_modified  = 0
    total_links_before = 0
    total_links_after  = 0

    for note_path in note_files:
        modified, old_count, new_count = retrofit_note(note_path, dry_run=args.dry_run)
        if modified:
            total_modified += 1
            total_links_before += old_count
            total_links_after  += new_count
            delta = new_count - old_count
            sign  = "+" if delta >= 0 else ""
            print(f"  {note_path.name}: {old_count} → {new_count} related notes ({sign}{delta})")

    print(f"\n{'─' * 60}")
    print(f"  Notes modified:        {total_modified} / {len(note_files)}")
    print(f"  Related links before:  {total_links_before}")
    print(f"  Related links after:   {total_links_after}")
    if total_links_before:
        reduction = 100 * (1 - total_links_after / max(total_links_before, 1))
        print(f"  Reduction:             {reduction:.1f}%")
    print(f"{'─' * 60}")

    if args.dry_run:
        print("\nDRY RUN — no files were modified. Re-run with --apply to write changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
