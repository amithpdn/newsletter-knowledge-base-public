# =============================================================================
# pipeline/fixes/retag_existing_notes.py — Newsletter AI Pipeline
#
# ONE-OFF maintenance script. Not part of the regular pipeline run.
#
# Retroactively sanitises tags in already-written notes so they become
# valid Obsidian tags (no spaces/punctuation), and rebuilds topic_index so
# Agent 3's topic linking and Related Notes continue working correctly
# afterward.
#
# This touches FOUR things per note, all of which must move together or
# the note ends up internally inconsistent:
#   1. YAML frontmatter `tags:` list
#   2. The `## Tags` section's [[wikilinks]]
#   3. Any inline [[wikilinks]] in the Summary/Takeaways prose that match
#      an old (space-containing) tag
#   4. The shared topic_index table in registry.db + topics_index.json
#
# Run this ONCE after applying patch_06_tag_sanitization.py to
# summarisation.py. Safe to re-run (idempotent) — already-sanitised tags
# pass through _sanitise_tag() unchanged.
#
# Usage:
#   cd pipeline
#   python fixes/retag_existing_notes.py --dry-run
#   python fixes/retag_existing_notes.py --apply
# =============================================================================

import argparse
import json
import re
import sys
from pathlib import Path

# This script lives in pipeline/fixes/ — add pipeline/ to sys.path so that
# config.py, db.py, and agents/ resolve when run standalone from anywhere.
_pipeline_dir = Path(__file__).resolve().parent.parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

import yaml

from config import NOTES_DIR, PROJECT_ROOT
from db import get_connection


# Reuse the exact same sanitisation logic as the live pipeline, so the
# retroactive fix and the going-forward fix can never drift apart.
def _sanitise_tag(tag: str) -> str:
    tag = tag.lower().strip()
    tag = re.sub(r"\s+", "-", tag)
    tag = re.sub(r"[^a-z0-9_-]", "", tag)
    tag = re.sub(r"-{2,}", "-", tag)
    tag = tag.strip("-")
    return tag


_FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TAGS_SECTION_PATTERN = re.compile(r"## Tags\n(.+?)(?=\n##|\n---|\Z)", re.DOTALL)


def _build_tag_map(old_tags: list[str]) -> dict[str, str]:
    """Map each old tag string to its sanitised replacement."""
    return {t: _sanitise_tag(t) for t in old_tags if t.strip()}


def _rewrite_frontmatter(content: str, tag_map: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """
    Parse and rewrite the tags: list inside YAML frontmatter.

    Returns:
        (new_content, old_tags, new_tags)
    """
    match = _FRONTMATTER_PATTERN.match(content)
    if not match:
        return content, [], []

    raw_yaml = match.group(1)
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        return content, [], []

    old_tags = data.get("tags", []) or []
    if not old_tags:
        return content, [], []

    new_tags = []
    seen = set()
    for t in old_tags:
        clean = tag_map.get(t, _sanitise_tag(str(t)))
        if clean and clean not in seen:
            seen.add(clean)
            new_tags.append(clean)

    data["tags"] = new_tags
    new_yaml = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    new_frontmatter = f"---\n{new_yaml}---\n"

    new_content = _FRONTMATTER_PATTERN.sub(lambda m: new_frontmatter, content, count=1)
    return new_content, old_tags, new_tags


def _rewrite_tags_section(content: str, tag_map: dict[str, str]) -> str:
    """
    Rewrite the ## Tags section, replacing [[old tag]] wikilinks with
    [[sanitised-tag]] equivalents. Rebuilds the section entirely from the
    tag_map values rather than trying to surgically edit the existing
    [[...]] · [[...]] line, since that's more robust against formatting
    drift.
    """
    match = _TAGS_SECTION_PATTERN.search(content)
    if not match:
        return content

    new_tags = list(tag_map.values())
    new_tags = [t for t in new_tags if t]  # drop empties
    if not new_tags:
        return content

    new_line = " · ".join(f"[[{t}]]" for t in new_tags)
    new_section = f"## Tags\n{new_line}\n"

    return content[:match.start()] + new_section + content[match.end():]


def _rewrite_inline_wikilinks(content: str, tag_map: dict[str, str]) -> str:
    """
    Replace any inline [[old tag with spaces]] occurrences in prose
    (Summary, Key Takeaways, Context sections) with the sanitised form.
    Only touches exact [[...]] matches against known old tags — does not
    attempt fuzzy matching.
    """
    for old_tag, new_tag in tag_map.items():
        if not new_tag or old_tag == new_tag:
            continue
        old_link = f"[[{old_tag}]]"
        new_link = f"[[{new_tag}]]"
        content = content.replace(old_link, new_link)
    return content


def retag_note(note_path: Path, dry_run: bool) -> dict | None:
    """
    Sanitise tags throughout one note. Returns a summary dict if the note
    was (or would be) modified, else None.
    """
    content = note_path.read_text(encoding="utf-8")

    new_content, old_tags, new_tags = _rewrite_frontmatter(content, {})
    if not old_tags:
        return None  # no tags found, nothing to do

    tag_map = _build_tag_map(old_tags)

    # Re-run frontmatter rewrite now that we have the real tag_map
    new_content, _, new_tags = _rewrite_frontmatter(content, tag_map)
    new_content = _rewrite_tags_section(new_content, tag_map)
    new_content = _rewrite_inline_wikilinks(new_content, tag_map)

    changed_tags = {k: v for k, v in tag_map.items() if k != v}
    if not changed_tags:
        return None  # already-sanitised, nothing changed

    if not dry_run:
        note_path.write_text(new_content, encoding="utf-8")

    return {
        "note":         note_path.name,
        "changed_tags": changed_tags,
        "tag_count":    len(old_tags),
    }


def rebuild_topic_index(global_tag_map: dict[str, str], dry_run: bool) -> int:
    """
    Rebuild the topic_index table to use sanitised tag strings as primary
    keys, merging entries where two old tags sanitised to the same new tag
    (e.g. "RAG pipelines" and "rag-pipelines" both -> "rag-pipelines").

    Also rebuilds embeddings for any merged entries, since two previously
    distinct rows are now one row and the embedding should reflect the
    canonical sanitised tag text.

    IMPORTANT: embedding_vector is serialised with pickle.dumps(), matching
    agents/topic_linking.py's _serialise_embedding()/_deserialise_embedding()
    exactly. Do NOT switch to np.tobytes()/np.frombuffer() here — that
    produces a blob _deserialise_embedding() cannot read, and will corrupt
    every row it touches (manifesting as "pickle data was truncated" on
    next read, which looks identical to genuine file corruption).

    Returns the number of topic_index rows affected.
    """
    import pickle
    from sentence_transformers import SentenceTransformer

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT tag, first_seen, first_seen_account, note_files, embedding_vector FROM topic_index"
        ).fetchall()

        if not rows:
            return 0

        # Group old rows by their sanitised tag
        merged: dict[str, dict] = {}
        for row in rows:
            old_tag = row["tag"]
            new_tag = global_tag_map.get(old_tag, _sanitise_tag(old_tag))
            if not new_tag:
                continue

            old_note_files = json.loads(row["note_files"] or "[]")

            if new_tag not in merged:
                merged[new_tag] = {
                    "first_seen":         row["first_seen"],
                    "first_seen_account": row["first_seen_account"],
                    "note_files":         list(old_note_files),
                }
            else:
                # Merge: keep earliest first_seen, union note_files
                if row["first_seen"] < merged[new_tag]["first_seen"]:
                    merged[new_tag]["first_seen"] = row["first_seen"]
                    merged[new_tag]["first_seen_account"] = row["first_seen_account"]
                existing_files = {f["file"] for f in merged[new_tag]["note_files"]}
                for nf in old_note_files:
                    if nf["file"] not in existing_files:
                        merged[new_tag]["note_files"].append(nf)
                        existing_files.add(nf["file"])

        if dry_run:
            return len(merged)

        # Re-embed all merged (sanitised) tags fresh, since embeddings are
        # text-dependent and the canonical text has changed.
        model = SentenceTransformer("all-MiniLM-L6-v2")
        new_tags = list(merged.keys())
        embeddings = model.encode(new_tags)

        conn.execute("DELETE FROM topic_index")
        for tag, emb in zip(new_tags, embeddings):
            data = merged[tag]
            conn.execute(
                """INSERT INTO topic_index
                   (tag, first_seen, first_seen_account, note_files, embedding_vector)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    tag,
                    data["first_seen"],
                    data["first_seen_account"],
                    json.dumps(data["note_files"]),
                    pickle.dumps(np.asarray(emb, dtype=np.float32)),  # matches _serialise_embedding()
                ),
            )
        conn.commit()
        return len(merged)
    finally:
        conn.close()


def rebuild_topics_json(dry_run: bool) -> None:
    """Mirror the rebuilt topic_index table out to topics_index.json."""
    if dry_run:
        return

    conn = get_connection()
    try:
        rows = conn.execute("SELECT tag, note_files FROM topic_index").fetchall()
    finally:
        conn.close()

    output = {row["tag"]: json.loads(row["note_files"] or "[]") for row in rows}
    topics_json_path = PROJECT_ROOT / "topics_index.json"
    topics_json_path.write_text(json.dumps(output, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retroactively sanitise space-containing tags to valid "
                     "Obsidian tag syntax across all existing notes and the "
                     "shared topic index."
    )
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would change without writing files")
    parser.add_argument("--apply", action="store_true",
                         help="Actually rewrite notes and rebuild topic_index")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        args.dry_run = True
        print("No mode specified — defaulting to --dry-run.\n")

    note_files = sorted(NOTES_DIR.glob("*.md"))
    print(f"[retag] Found {len(note_files)} note(s) in {NOTES_DIR}\n")

    global_tag_map: dict[str, str] = {}
    modified_notes = []

    for note_path in note_files:
        result = retag_note(note_path, dry_run=args.dry_run)
        if result:
            modified_notes.append(result)
            global_tag_map.update(result["changed_tags"])
            print(f"  {result['note']}:")
            for old, new in result["changed_tags"].items():
                print(f"      \"{old}\" -> \"{new}\"")

    print(f"\n{'─' * 60}")
    print(f"  Notes modified:     {len(modified_notes)} / {len(note_files)}")
    print(f"  Unique tags fixed:  {len(global_tag_map)}")
    print(f"{'─' * 60}\n")

    if global_tag_map:
        print("[retag] Rebuilding topic_index and topics_index.json...")
        affected = rebuild_topic_index(global_tag_map, dry_run=args.dry_run)
        rebuild_topics_json(dry_run=args.dry_run)
        print(f"  topic_index rows after rebuild: {affected}")

    if args.dry_run:
        print("\nDRY RUN — no files were modified. Re-run with --apply to write changes.")
        print("Recommended: review the tag renames above, then run --apply.")
    else:
        print("\n✓ Retag complete. Re-open Obsidian and check the Tag pane — ")
        print("  multi-word tags should now appear as valid hyphenated tags.")
        print("  You may need to restart Obsidian or run 'Reload app without saving'")
        print("  for the tag index to refresh fully.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
