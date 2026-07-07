# =============================================================================
# pipeline/relink_notes.py — Newsletter AI Pipeline v4.0
# Batch Wikilink Injector
#
# Scans every .md file in the notes/ folder and injects [[wikilinks]] for any
# topic tag found in topics_index.json that appears as a plain phrase in the
# note body. Sections already containing wikilinks are left unchanged.
#
# Safe to run repeatedly — already-linked phrases are never double-wrapped.
#
# Run manually whenever you want to refresh links across all notes:
#   python pipeline\relink_notes.py
#
# Or with a dry-run to preview changes without writing files:
#   python pipeline\relink_notes.py --dry-run
#
# Flags:
#   --dry-run     Show what would change without writing any files
#   --note FILE   Process a single note file only (relative to notes/)
#   --verbose     Print every substitution made
# =============================================================================

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_pipeline_dir = Path(__file__).resolve().parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import NOTES_DIR, TOPICS_INDEX


# ---------------------------------------------------------------------------
# Wikilink injection logic (shared with local_writer)
# ---------------------------------------------------------------------------

def build_tag_pattern(tags: list[str]) -> list[tuple[re.Pattern, str]]:
    """
    Build a list of (compiled_pattern, tag) pairs for all known tags,
    sorted longest-first so multi-word phrases match before their substrings.

    e.g. "llm fine-tuning" matches before "llm" so we don't get [[llm]] fine-tuning.

    Args:
        tags: List of tag strings from topics_index.json keys.

    Returns:
        List of (pattern, original_tag) tuples, longest tag first.
    """
    # Sort by length descending — longest phrases first
    sorted_tags = sorted(tags, key=len, reverse=True)

    patterns = []
    for tag in sorted_tags:
        if not tag.strip():
            continue
        # Match whole words only (word boundary on both sides)
        # re.escape handles hyphens and other special chars in tag names
        escaped = re.escape(tag)
        pattern = re.compile(
            r"(?<!\[)"          # not already preceded by [
            r"\b"               # word boundary
            r"(" + escaped + r")"
            r"\b"               # word boundary
            r"(?!\])",          # not already followed by ]
            re.IGNORECASE,
        )
        patterns.append((pattern, tag))

    return patterns


def inject_wikilinks(text: str, patterns: list[tuple[re.Pattern, str]]) -> str:
    """
    Scan a block of text and wrap matching tag phrases with [[wikilinks]].

    Rules:
      - Only replaces the FIRST occurrence of each tag per text block.
        Obsidian only needs one link per note to build the graph — repeated
        wikilinks for the same tag add visual noise without graph benefit.
      - Never wraps text already inside [[...]] (guards against double-linking).
      - Never wraps text inside YAML frontmatter (handled by caller).
      - Never wraps text inside code fences (``` blocks).
      - Never wraps markdown link syntax [text](url).
      - Case-insensitive match, but preserves the original casing in the
        display text: [[llm-fine-tuning|LLM-Fine-Tuning]] is not used —
        we use [[tag]] directly since Obsidian resolves case-insensitively.

    Args:
        text:     The text block to process (body only, not frontmatter).
        patterns: Output of build_tag_pattern().

    Returns:
        Text with wikilinks injected.
    """
    already_linked: set[str] = set()

    for pattern, tag in patterns:
        tag_lower = tag.lower()

        # Skip if this tag was already linked earlier in this text
        if tag_lower in already_linked:
            continue

        def _replace(match: re.Match) -> str:
            # Check we're not inside an existing [[...]] by scanning context
            start = match.start()
            preceding = text[:start]
            # Count unmatched [[ before this position
            if preceding.count("[[") > preceding.count("]]"):
                return match.group(0)  # already inside a wikilink
            return f"[[{match.group(1)}]]"

        new_text, n = pattern.subn(_replace, text, count=1)

        if n > 0:
            text = new_text
            already_linked.add(tag_lower)

    return text


def split_frontmatter(content: str) -> tuple[str, str]:
    """
    Split a note into (frontmatter, body).

    Frontmatter is the YAML block between the opening and closing --- lines.
    If no frontmatter is present, returns ("", content).
    """
    if content.startswith("---"):
        end = content.find("\n---\n", 3)
        if end != -1:
            frontmatter = content[: end + 5]   # include closing ---\n
            body        = content[end + 5:]
            return frontmatter, body
    return "", content


def split_code_fences(body: str) -> list[tuple[str, bool]]:
    """
    Split body text into segments, marking code fence blocks.

    Returns a list of (text_segment, is_code) tuples.
    Wikilink injection is only applied to segments where is_code=False.
    """
    segments = []
    fence_pattern = re.compile(r"^```", re.MULTILINE)
    parts = fence_pattern.split(body)

    for i, part in enumerate(parts):
        is_code = (i % 2 == 1)  # odd-indexed parts are inside fences
        segments.append((part, is_code))

    return segments


def process_note_content(content: str, patterns: list[tuple[re.Pattern, str]]) -> str:
    """
    Inject wikilinks into a full note's content string.

    Protects frontmatter and code fences from modification.

    Args:
        content:  Full .md file content.
        patterns: Output of build_tag_pattern().

    Returns:
        Modified content string with wikilinks injected in body text.
    """
    frontmatter, body = split_frontmatter(content)

    # Process body segment by segment, skipping code fences
    segments      = split_code_fences(body)
    new_segments  = []

    for segment_text, is_code in segments:
        if is_code:
            new_segments.append(segment_text)
        else:
            new_segments.append(inject_wikilinks(segment_text, patterns))

    new_body = "```".join(new_segments)
    return frontmatter + new_body


# ---------------------------------------------------------------------------
# Tag loading
# ---------------------------------------------------------------------------

def load_tags_from_index() -> list[str]:
    """
    Load all known tags from topics_index.json.

    Returns a deduplicated list of tag strings.
    Returns empty list if the index file doesn't exist yet.
    """
    if not TOPICS_INDEX.exists():
        print(f"[relink] topics_index.json not found at {TOPICS_INDEX}")
        print(f"  Run the pipeline at least once to build the topic index.")
        return []

    try:
        index = json.loads(TOPICS_INDEX.read_text(encoding="utf-8"))
        tags  = list(index.keys())
        print(f"[relink] Loaded {len(tags)} tags from topics_index.json")
        return tags
    except json.JSONDecodeError as exc:
        print(f"[relink] ERROR: Could not parse topics_index.json: {exc}")
        return []


# ---------------------------------------------------------------------------
# Per-note processing
# ---------------------------------------------------------------------------

def process_note(
    note_path: Path,
    patterns:  list[tuple[re.Pattern, str]],
    dry_run:   bool = False,
    verbose:   bool = False,
) -> tuple[bool, int]:
    """
    Inject wikilinks into a single note file.

    Args:
        note_path: Path to the .md file.
        patterns:  Compiled tag patterns from build_tag_pattern().
        dry_run:   If True, do not write changes to disk.
        verbose:   If True, print each substitution made.

    Returns:
        (was_modified, links_added) tuple.
    """
    try:
        original = note_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"  [relink] Could not read {note_path.name}: {exc}")
        return False, 0

    modified = process_note_content(original, patterns)

    if modified == original:
        return False, 0

    # Count how many new wikilinks were added
    original_links = len(re.findall(r"\[\[", original))
    modified_links = len(re.findall(r"\[\[", modified))
    links_added    = modified_links - original_links

    if verbose:
        # Show which tags were newly linked
        new_links = re.findall(r"\[\[([^\]]+)\]\]", modified)
        old_links = set(re.findall(r"\[\[([^\]]+)\]\]", original))
        new_only  = [l for l in new_links if l not in old_links]
        if new_only:
            print(f"  {note_path.name}: +{links_added} link(s) → {', '.join(new_only[:8])}"
                  + (" ..." if len(new_only) > 8 else ""))

    if not dry_run:
        note_path.write_text(modified, encoding="utf-8")

    return True, links_added


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def relink_all_notes(
    dry_run: bool = False,
    verbose: bool = False,
    single_note: str | None = None,
) -> dict:
    """
    Inject wikilinks into all notes (or a single note if specified).

    Args:
        dry_run:     If True, preview changes without writing files.
        verbose:     If True, print every substitution made.
        single_note: If set, process only this filename (relative to notes/).

    Returns:
        Summary dict: {notes_scanned, notes_modified, total_links_added}
    """
    tags     = load_tags_from_index()
    if not tags:
        return {"notes_scanned": 0, "notes_modified": 0, "total_links_added": 0}

    patterns = build_tag_pattern(tags)
    print(f"[relink] Built patterns for {len(patterns)} tags\n")

    # Determine which notes to process
    if single_note:
        note_files = [NOTES_DIR / single_note]
        if not note_files[0].exists():
            print(f"[relink] ERROR: Note not found: {note_files[0]}")
            return {"notes_scanned": 0, "notes_modified": 0, "total_links_added": 0}
    else:
        note_files = sorted(NOTES_DIR.glob("*.md"))

    if not note_files:
        print(f"[relink] No .md files found in {NOTES_DIR}")
        return {"notes_scanned": 0, "notes_modified": 0, "total_links_added": 0}

    print(f"[relink] {'DRY RUN — ' if dry_run else ''}Processing {len(note_files)} note(s)...\n")

    notes_scanned   = 0
    notes_modified  = 0
    total_links_added = 0

    for note_path in note_files:
        notes_scanned += 1
        modified, links_added = process_note(note_path, patterns, dry_run, verbose)
        if modified:
            notes_modified  += 1
            total_links_added += links_added
            if not verbose:
                action = "Would update" if dry_run else "Updated"
                print(f"  {action}: {note_path.name} (+{links_added} link(s))")

    return {
        "notes_scanned":    notes_scanned,
        "notes_modified":   notes_modified,
        "total_links_added": total_links_added,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inject Obsidian [[wikilinks]] into all newsletter notes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Dry run (preview):    python pipeline\\relink_notes.py --dry-run\n"
            "  Full relink:          python pipeline\\relink_notes.py\n"
            "  Verbose output:       python pipeline\\relink_notes.py --verbose\n"
            "  Single note only:     python pipeline\\relink_notes.py --note 2026-06-07-personal-ai-weekly.md\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing any files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every wikilink added.",
    )
    parser.add_argument(
        "--note",
        type=str,
        default=None,
        metavar="FILENAME",
        help="Process a single note file (filename only, relative to notes/).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args    = _parse_args()
    summary = relink_all_notes(
        dry_run     = args.dry_run,
        verbose     = args.verbose,
        single_note = args.note,
    )

    print(f"\n{'=' * 55}")
    print(f"  {'DRY RUN — ' if args.dry_run else ''}Relink complete")
    print(f"{'─' * 55}")
    print(f"  Notes scanned:     {summary['notes_scanned']}")
    print(f"  Notes modified:    {summary['notes_modified']}")
    print(f"  Wikilinks added:   {summary['total_links_added']}")
    if args.dry_run:
        print(f"\n  Run without --dry-run to apply changes.")
    print(f"{'=' * 55}\n")
