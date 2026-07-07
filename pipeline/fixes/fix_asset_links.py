# =============================================================================
# pipeline/fixes/fix_asset_links.py — Newsletter AI Pipeline
#
# ONE-OFF repair script. Not part of the regular pipeline run.
#
# Repairs broken image links in existing notes caused by a slug mismatch
# between the image extractor and the note writer:
#
#   - image_extraction.extract_images() created asset folders as
#       assets/{date}-{alias}-{hash}/     e.g. 2026-07-07-personal-6a60b53c
#     using the email's Date header and account alias.
#
#   - local_writer._build_images_section() recomputed the slug from
#     message_id ONLY, producing links like
#       assets/{today}-{hash}/            e.g. 2026-07-07-6a60b53c
#     (alias segment missing; date = run date, not received date).
#
#   Result: every note image link pointed at a folder that does not exist.
#   A third variant: deduplicated images live in the FIRST message's folder
#   (canonical copy), which the recomputed slug can never reference.
#
# Resolution strategy, per broken link ![alt](assets/<slug>/<file>):
#   1. If assets/<slug>/<file> exists on disk        → link is fine, skip.
#   2. Match by hash: the trailing 8-hex-char segment of the slug is
#      md5(message_id)[:8] — identical in both the folder name and the
#      broken link. Find folders under assets/ ending in "-<hash>" that
#      contain <file>. Exactly one match                → rewrite.
#   3. Match by filename: search ALL asset folders for <file> (filenames
#      are content-derived and near-unique). Exactly one match → rewrite.
#      This covers the dedup case, where the file lives in another
#      message's canonical folder.
#   4. Zero or multiple matches → report as unresolved; never guess.
#
# The script is idempotent: already-correct links are skipped (rule 1),
# so it can be re-run safely after a partial repair.
#
# Usage:
#   cd pipeline
#   python fixes/fix_asset_links.py             # dry run — report only
#   python fixes/fix_asset_links.py --apply     # rewrite links in notes
#   python fixes/fix_asset_links.py --apply --verbose
# =============================================================================

import argparse
import re
import sys
from pathlib import Path

# This script lives in pipeline/fixes/ — add pipeline/ to sys.path so that
# config.py resolves when run standalone from anywhere.
_pipeline_dir = Path(__file__).resolve().parent.parent
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

from config import NOTES_DIR, ASSETS_DIR

# Matches markdown image embeds pointing into assets/:
#   ![alt](assets/<slug>/<file>)
# Group 1 = alt text, 2 = slug (folder), 3 = filename.
# Slug/filename characters are restricted to what the pipeline generates
# (slugify output + hex hashes + image extensions) so exotic links in
# hand-edited notes are left untouched.
_IMG_LINK = re.compile(
    r"!\[([^\]]*)\]\(assets/([A-Za-z0-9._\-]+)/([A-Za-z0-9._\-]+)\)"
)

# Trailing 8-hex-char hash segment of a slug, e.g. "...-6a60b53c"
_HASH_SUFFIX = re.compile(r"-([0-9a-f]{8})$")


def _index_asset_folders() -> dict:
    """
    Build lookup indexes over notes/assets/ in a single directory walk.

    Returns:
        {
          "by_hash":     {hash8: [folder_name, ...]},   # trailing-hash index
          "by_filename": {filename: [folder_name, ...]} # file location index
        }
    """
    by_hash: dict[str, list[str]] = {}
    by_filename: dict[str, list[str]] = {}

    if not ASSETS_DIR.exists():
        return {"by_hash": by_hash, "by_filename": by_filename}

    for folder in sorted(p for p in ASSETS_DIR.iterdir() if p.is_dir()):
        m = _HASH_SUFFIX.search(folder.name)
        if m:
            by_hash.setdefault(m.group(1), []).append(folder.name)
        for f in folder.iterdir():
            if f.is_file() and f.name != "manifest.json":
                by_filename.setdefault(f.name, []).append(folder.name)

    return {"by_hash": by_hash, "by_filename": by_filename}


def _resolve(slug: str, filename: str, index: dict) -> tuple[str, str]:
    """
    Resolve the real asset folder for a broken (slug, filename) pair.

    Returns:
        (resolved_folder_name, reason) — resolved_folder_name is "" when
        the link could not be resolved unambiguously; reason explains why.
    """
    # Rule 2 — hash match: same message, folder name carries the alias/date
    m = _HASH_SUFFIX.search(slug)
    if m:
        candidates = [
            f for f in index["by_hash"].get(m.group(1), [])
            if (ASSETS_DIR / f / filename).exists()
        ]
        if len(candidates) == 1:
            return candidates[0], "hash"
        if len(candidates) > 1:
            return "", f"ambiguous hash match ({len(candidates)} folders)"

    # Rule 3 — filename match: covers dedup canonical copies in other folders
    candidates = index["by_filename"].get(filename, [])
    if len(candidates) == 1:
        return candidates[0], "filename"
    if len(candidates) > 1:
        return "", f"ambiguous filename match ({len(candidates)} folders)"
    return "", "file not found in any asset folder"


def repair_note(note_path: Path, index: dict, apply: bool, verbose: bool) -> dict:
    """
    Scan one note and rewrite broken asset links.

    Returns a stats dict: {"ok": n, "fixed": n, "unresolved": [(link, reason)]}
    """
    text = note_path.read_text(encoding="utf-8")
    stats = {"ok": 0, "fixed": 0, "unresolved": []}

    def _sub(match: re.Match) -> str:
        alt, slug, filename = match.group(1), match.group(2), match.group(3)

        # Rule 1 — link already points at a real file: leave untouched
        if (ASSETS_DIR / slug / filename).exists():
            stats["ok"] += 1
            return match.group(0)

        folder, reason = _resolve(slug, filename, index)
        if not folder:
            stats["unresolved"].append((match.group(0), reason))
            return match.group(0)

        stats["fixed"] += 1
        if verbose:
            print(f"    {slug}/{filename}  →  {folder}/{filename}  [{reason}]")
        return f"![{alt}](assets/{folder}/{filename})"

    new_text = _IMG_LINK.sub(_sub, text)

    if apply and new_text != text:
        note_path.write_text(new_text, encoding="utf-8")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair broken assets/<slug>/ image links in existing notes."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write repaired links back to the note files "
                             "(default is a dry run that only reports).")
    parser.add_argument("--verbose", action="store_true",
                        help="Print each individual link rewrite.")
    args = parser.parse_args()

    if not NOTES_DIR.exists():
        print(f"[fix_asset_links] NOTES_DIR not found: {NOTES_DIR}")
        return 1

    index = _index_asset_folders()
    notes = sorted(NOTES_DIR.glob("*.md"))
    mode  = "APPLY" if args.apply else "DRY RUN"

    print(f"[fix_asset_links] {mode} — scanning {len(notes)} note(s) in {NOTES_DIR}")
    print(f"[fix_asset_links] indexed {sum(len(v) for v in index['by_hash'].values())} "
          f"asset folder(s) under {ASSETS_DIR}\n")

    total_ok = total_fixed = 0
    total_unresolved: list[tuple[str, str, str]] = []  # (note, link, reason)

    for note in notes:
        s = repair_note(note, index, apply=args.apply, verbose=args.verbose)
        if s["fixed"] or s["unresolved"]:
            print(f"  {note.name}: {s['fixed']} fixed, "
                  f"{s['ok']} already OK, {len(s['unresolved'])} unresolved")
        total_ok    += s["ok"]
        total_fixed += s["fixed"]
        total_unresolved.extend((note.name, link, why) for link, why in s["unresolved"])

    print("\n" + "=" * 70)
    print(f"  Links already correct : {total_ok}")
    print(f"  Links {'repaired' if args.apply else 'repairable'}        : {total_fixed}")
    print(f"  Unresolved            : {len(total_unresolved)}")
    if total_unresolved:
        print("\n  Unresolved links (left untouched — investigate manually):")
        for note_name, link, why in total_unresolved:
            print(f"    {note_name}: {link}\n      reason: {why}")
    if not args.apply and total_fixed:
        print("\n  Re-run with --apply to write these repairs.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
