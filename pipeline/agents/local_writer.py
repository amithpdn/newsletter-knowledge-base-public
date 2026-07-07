# agents/local_writer.py — Newsletter AI Pipeline v4.0
# Agent 7: Local File Writer
#
# Responsibilities:
#   - Assemble the final Markdown note from all upstream agent outputs
#   - Write the .md file to {PROJECT_ROOT}/notes/
#   - Append a row to INDEX.md (the chronological note listing)
#   - Update topics_index.json (a lightweight JSON mirror of the SQLite
#     topic_index, for Obsidian Dataview and Git-visible tag browsing)
#   - Return the written file path so Agent 8 knows the write succeeded
#
# This agent has NO API calls and NO external dependencies beyond the
# local filesystem. It is the point of truth for "did the note land on disk".
# Agent 8 (Gmail labelling) only fires after this agent returns successfully.
#
# OneDrive sync is automatic — once files are written here, the Windows
# OneDrive client detects changes and syncs to cloud within seconds.
# No pipeline code handles sync; it is transparent to this agent.
#
# Note format follows the template from the v4.0 design doc Section 5 / Agent 2.
#
# Changes from v4.0:
#   - _inject_wikilinks_in_scope(): new helper that restricts the inline
#     wikilink injection pass to ## Summary and ## Key Takeaways only.
#     Previously the pass ran over the entire assembled note, causing
#     multi-word tag phrases in ## Related Notes to be fragmented
#     (e.g. "agent architecture" -> "agent [[architecture]]") and tool
#     names in ## Mentions to acquire unwanted wikilinks.
#   - _build_related_section(): docstring updated to explain why shared
#     tags are rendered as plain text rather than [[wikilinks]] — this
#     is intentional, not an oversight.
#
# Usage (standalone test):
#   cd pipeline && python agents/local_writer.py
# =============================================================================

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config import PROJECT_ROOT, NOTES_DIR, INDEX_MD, TOPICS_INDEX

# Wikilink injection — imported from the batch relink script so the logic
# lives in one place and is shared between new note writes and batch relinks.
# Import is deferred inside functions to avoid circular imports at module load.
def _get_wikilink_helpers():
    import sys as _sys
    if str(_pipeline_dir) not in _sys.path:
        _sys.path.insert(0, str(_pipeline_dir))
    from relink_notes import load_tags_from_index, build_tag_pattern, process_note_content
    return load_tags_from_index, build_tag_pattern, process_note_content


# ---------------------------------------------------------------------------
# Injection scope helper
# ---------------------------------------------------------------------------

def _inject_wikilinks_in_scope(
    content:  str,
    patterns: object,
    inject:   callable,
) -> str:
    """
    Run the wikilink injection pass over ONLY the ## Summary and
    ## Key Takeaways sections of the note, leaving all other sections
    completely untouched.

    Previously the injection pass ran over the entire assembled note
    content string. This caused two specific problems:

    1. Multi-word tag phrases in ## Related Notes were fragmented:
       e.g. "agent architecture" → "agent [[architecture]]"
       because the injection pass matched "architecture" as a partial
       token of a longer tag pattern and wrapped only that word.

    2. Tool names in ## Mentions, image paths in ## Images, and
       research prose in ## Context acquired unwanted wikilinks.

    The ## Tags section already guarantees every tag is wikilinked
    unconditionally via _build_tags_section() — the injection pass
    over prose is a bonus for in-sentence occurrences only and should
    never touch structural sections.

    Args:
        content:  Full assembled note Markdown string.
        patterns: Tag patterns from build_patterns() in relink_notes.py.
        inject:   process_note_content callable from relink_notes.py.

    Returns:
        Note content with wikilinks injected only in ## Summary and
        ## Key Takeaways; all other content is returned unchanged.
    """
    # Section headers where injection IS permitted
    _INJECTABLE = {"## Summary", "## Key Takeaways"}

    lines     = content.split("\n")
    result    = []
    in_scope  = False
    scope_buf = []

    def _flush():
        """Inject into buffered lines and extend result."""
        if scope_buf:
            injected = inject("\n".join(scope_buf), patterns)
            result.extend(injected.split("\n"))
        scope_buf.clear()

    for line in lines:
        # Entering an injectable section?
        if any(line.startswith(h) for h in _INJECTABLE):
            _flush()
            in_scope = True
            # BUGFIX: the heading line itself was previously appended to
            # scope_buf and therefore passed through the injector — a tag
            # named e.g. "summary" would rewrite the heading to
            # "## [[Summary]]". Headings delimit the scope; they are never
            # part of the injected text.
            result.append(line)
            continue

        # Leaving an injectable section (any ## heading)?
        if in_scope and line.startswith("## "):
            _flush()
            in_scope = False
            result.append(line)
            continue

        if in_scope:
            scope_buf.append(line)
        else:
            result.append(line)

    _flush()  # flush any remaining buffer (note ending inside a section)

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Public interface — write note
# ---------------------------------------------------------------------------

def write_note(
    email:         dict,
    structured:    dict,
    related_notes: list[dict],
    research:      dict[str, dict],
    saved_assets:  list[dict],
) -> Path:
    """
    Assemble and write the Markdown note for a single newsletter email.

    This is the primary function called by the orchestrator. It combines
    output from all upstream agents into a single well-formatted note and
    writes it to the notes directory.

    Args:
        email:         Email dict from Agent 1/1.5. Used for: subject, sender,
                       account_alias, received_date, message_id.
        structured:    Output from Agent 2 (summarisation): summary, tags,
                       key_takeaways, mentions.
        related_notes: Output from Agent 3 (topic linking): list of dicts
                       with file, account_alias, shared_tags.
        research:      Output from Agent 5 (research): dict keyed by topic,
                       each value has a 'summary' string.
                       Pass {} if no research was triggered.
        saved_assets:  Output from Agent 4 (image extraction): list of dicts
                       with filename, alt_text, and the message slug.
                       Pass [] if images are disabled or none were saved.

    Returns:
        Path object pointing to the written .md file.

    Raises:
        OSError: If the file cannot be written (disk full, permissions, etc.)
                 The orchestrator wraps this in a try/except.
    """
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    filename  = _make_filename(email)
    note_path = NOTES_DIR / filename

    content = _assemble_note(email, structured, related_notes, research, saved_assets)

    # Best-effort inline wikilink injection — scans Summary and Key Takeaways
    # prose ONLY for exact tag phrases and wraps verbatim matches in [[...]].
    #
    # Scope is intentionally restricted to these two sections by
    # _inject_wikilinks_in_scope(). Other sections (## Tags, ## Related Notes,
    # ## Mentions, ## Images, ## Context, YAML frontmatter) are left untouched:
    #   - ## Tags already wikilinks every tag unconditionally via _build_tags_section()
    #   - ## Related Notes: injection fragments multi-word shared tags
    #   - ## Mentions: tool names should not become wikilinks
    #   - ## Images: paths must not be modified
    #   - ## Context: research prose should remain unlinked
    #
    # This pass frequently finds ZERO matches in Summary/Takeaways because
    # LLM-generated prose rarely repeats an exact tag string. That is expected
    # and not a bug — ## Tags is the reliable linking mechanism.
    try:
        load_tags, build_patterns, inject = _get_wikilink_helpers()
        tags_for_linking = load_tags()
        if tags_for_linking:
            patterns = build_patterns(tags_for_linking)
            content  = _inject_wikilinks_in_scope(content, patterns, inject)
    except Exception as exc:
        print(f"    [writer] WARNING: wikilink injection failed: {exc}")

    note_path.write_text(content, encoding="utf-8")
    print(f"    [writer] Note written: {filename}")

    return note_path


# ---------------------------------------------------------------------------
# Public interface — update index files
# ---------------------------------------------------------------------------

def update_index(email: dict, structured: dict, note_path: Path) -> None:
    """
    Append a row to INDEX.md for the newly written note.

    INDEX.md is a simple Markdown table providing a chronological listing
    of all processed notes. It is readable both in Obsidian and on GitHub.

    The header row and table separator are written only on first creation.
    Subsequent calls append a single data row.

    Args:
        email:      Email dict (for account_alias, subject, received_date).
        structured: Summarisation output (for tags).
        note_path:  Path of the written note (for the relative link).
    """
    if not INDEX_MD.exists():
        INDEX_MD.write_text(
            "# Newsletter Pipeline — Note Index\n\n"
            "| Date | Subject | Account | Tags |\n"
            "|------|---------|---------|------|\n",
            encoding="utf-8",
        )

    date_str = _parse_date(email.get("received_date", ""))
    subject  = (email.get("subject") or "(no subject)").replace("|", "—")
    account  = email.get("account_alias", "unknown")
    tags_str = ", ".join(structured.get("tags", []))

    # Use a relative path from the project root for the link
    rel_path = note_path.relative_to(PROJECT_ROOT).as_posix()

    row = f"| {date_str} | [{subject}]({rel_path}) | {account} | {tags_str} |\n"

    with open(INDEX_MD, "a", encoding="utf-8") as f:
        f.write(row)


def update_topics_json(tags: list[str], note_filename: str, account_alias: str) -> None:
    """
    Update topics_index.json with the tags from the newly written note.

    topics_index.json is a lightweight JSON mirror of the SQLite topic_index
    table. It serves two purposes:
      1. Git-visible: committed to GitHub so the topic landscape is browsable
         without opening the DB.
      2. Obsidian Dataview: can be queried with Dataview JS blocks for
         custom tag dashboards inside Obsidian.

    The file maps each tag to a list of note filenames where it appears.
    Example:
      {
        "rag-pipelines": ["2026-06-07-personal-ai-weekly.md", ...],
        "llm-fine-tuning": ["2026-06-07-personal-ai-weekly.md", ...]
      }

    Args:
        tags:          List of tags for the current note.
        note_filename: Filename of the written note.
        account_alias: Account alias (stored alongside the note reference).
    """
    # Load existing index or start fresh
    if TOPICS_INDEX.exists():
        try:
            index = json.loads(TOPICS_INDEX.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            index = {}
    else:
        index = {}

    for tag in tags:
        tag_lower = tag.lower().strip()
        if not tag_lower:
            continue

        if tag_lower not in index:
            index[tag_lower] = []

        # Append note reference if not already present
        entry = {"file": note_filename, "account": account_alias}
        if entry not in index[tag_lower]:
            index[tag_lower].append(entry)

    # Sort keys alphabetically for stable diffs in Git
    sorted_index = dict(sorted(index.items()))

    TOPICS_INDEX.write_text(
        json.dumps(sorted_index, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Note assembly
# ---------------------------------------------------------------------------

def _assemble_note(
    email:         dict,
    structured:    dict,
    related_notes: list[dict],
    research:      dict[str, dict],
    saved_assets:  list[dict],
) -> str:
    """
    Build the full Markdown note string from all agent outputs.

    Section order matches the design doc template (Section 5, Agent 2):
      YAML frontmatter → Title → Summary → Key Takeaways → Mentions →
      Tags → Images (optional) → Related Notes (optional) →
      Context sections (optional)
    """
    date_str     = _parse_date(email.get("received_date", ""))
    account      = email.get("account_alias", "unknown")
    sender       = email.get("sender", "unknown")
    subject      = email.get("subject") or "(no subject)"
    processed_at = datetime.now(timezone.utc).isoformat()
    tags         = structured.get("tags", [])

    # -----------------------------------------------------------------------
    # YAML frontmatter
    # -----------------------------------------------------------------------
    frontmatter = (
        f"---\n"
        f"source_account: {account}\n"
        f"sender: {sender}\n"
        f"received: {date_str}\n"
        f"processed: {processed_at}\n"
        f"tags: {json.dumps(tags)}\n"
        f"---\n"
    )

    # -----------------------------------------------------------------------
    # Title and header line
    # -----------------------------------------------------------------------
    title  = f"\n# {subject} — {date_str}\n"
    header = f"**Account:** {account} | **Source:** {sender}\n"

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    summary_text    = structured.get("summary") or "_No summary generated._"
    summary_section = f"\n## Summary\n{summary_text}\n"

    # -----------------------------------------------------------------------
    # Key Takeaways
    # -----------------------------------------------------------------------
    takeaways = structured.get("key_takeaways", [])
    if takeaways:
        bullets = "\n".join(f"- {t}" for t in takeaways)
    else:
        bullets = "- _No takeaways identified._"
    takeaways_section = f"\n## Key Takeaways\n{bullets}\n"

    # -----------------------------------------------------------------------
    # Mentions
    # -----------------------------------------------------------------------
    mentions   = structured.get("mentions", {})
    tools_str  = ", ".join(mentions.get("tools",  [])) or "—"
    papers_str = ", ".join(mentions.get("papers", [])) or "—"
    people_str = ", ".join(mentions.get("people", [])) or "—"
    mentions_section = (
        f"\n## Mentions\n"
        f"- **Tools:** {tools_str}\n"
        f"- **Papers:** {papers_str}\n"
        f"- **People:** {people_str}\n"
    )

    # -----------------------------------------------------------------------
    # Tags — explicit wikilinks
    #
    # This section guarantees every tag becomes a real Obsidian [[wikilink]],
    # regardless of whether the tag phrase happens to appear verbatim
    # anywhere in the summary/takeaways prose. Inline injection
    # (_inject_wikilinks_in_scope) is a best-effort pass over Summary and
    # Key Takeaways prose only, and frequently finds zero matches because
    # LLM-generated summaries rarely repeat the exact tag string. This
    # section is the reliable mechanism — every tag here is guaranteed to
    # be wikilinked.
    # -----------------------------------------------------------------------
    tags_section = _build_tags_section(tags)

    # -----------------------------------------------------------------------
    # Images (only rendered if assets were saved)
    # -----------------------------------------------------------------------
    images_section = _build_images_section(email, saved_assets)

    # -----------------------------------------------------------------------
    # Related Notes (only rendered if Agent 3 found links)
    # -----------------------------------------------------------------------
    related_section = _build_related_section(related_notes)

    # -----------------------------------------------------------------------
    # Context / Research sections (one per new topic from Agent 5)
    # -----------------------------------------------------------------------
    research_sections = _build_research_sections(research)

    # -----------------------------------------------------------------------
    # Assemble
    # -----------------------------------------------------------------------
    return (
        frontmatter
        + title
        + header
        + summary_section
        + takeaways_section
        + mentions_section
        + tags_section
        + images_section
        + related_section
        + research_sections
    )


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_tags_section(tags: list[str]) -> str:
    """
    Render every tag as an explicit Obsidian wikilink in a dedicated section.

    This is the reliable linking mechanism — unlike the inline injection
    pass (which only links a tag if it happens to appear verbatim in the
    summary/takeaways prose), every tag listed here is guaranteed to become
    a real [[wikilink]] regardless of the note's prose content.

    Each tag becomes its own wikilink target. Two notes that share a tag
    will both link to the same [[tag-name]] page, and Obsidian's graph view
    will show them connected through that shared node — even if neither
    note's prose ever uses the tag phrase directly.

    Args:
        tags: List of sanitised, hyphenated tag strings from Agent 2.

    Returns:
        Markdown section string. Empty string if no tags exist.
    """
    if not tags:
        return ""

    links = " ".join(f"[[{tag}]]" for tag in tags if tag.strip())
    if not links:
        return ""

    return f"\n## Tags\n{links}\n"


def _build_images_section(email: dict, saved_assets: list[dict]) -> str:
    """
    Render the ## Images section with Obsidian-compatible image embeds.

    Links are derived from each asset's actual on-disk location
    (asset["local_path"], recorded by image_extraction at save time),
    expressed relative to NOTES_DIR — the folder the note itself lives in.

    BUGFIX: this function previously *recomputed* the slug via
    make_message_slug(message_id) — omitting received_date and
    account_alias. The extractor creates folders as
    {date}-{alias}-{hash} (e.g. 2026-07-07-personal-6a60b53c) using all
    three fields, so the recomputed link path {today}-{hash}
    (e.g. 2026-07-07-6a60b53c) never matched the real folder whenever an
    alias was set, and additionally drifted on date whenever the email's
    Date header differed from the run date. Deduplicated images were a
    third failure mode: their canonical file lives in the FIRST message's
    asset folder, which no recomputed slug for the current message can
    ever point to. Using local_path fixes all three cases at the source.

    Only included if at least one image was saved.
    """
    if not saved_assets:
        return ""

    lines = ["\n## Images"]

    for asset in saved_assets:
        alt      = asset.get("alt_text", "").strip() or asset.get("filename", "image")
        filename = asset.get("filename", "")
        if not filename:
            continue

        local_path = asset.get("local_path", "")
        rel_link   = ""
        if local_path:
            try:
                # as_posix(): markdown links need forward slashes; on Windows
                # relative_to() would otherwise yield backslashes.
                rel_link = Path(local_path).relative_to(NOTES_DIR).as_posix()
            except ValueError:
                rel_link = ""  # local_path outside NOTES_DIR — fall through

        if not rel_link:
            # Fallback for assets missing/foreign local_path (e.g. manifests
            # from older runs): recompute the slug with ALL the fields the
            # extractor uses, not just message_id.
            from agents.image_extraction import make_message_slug
            slug = make_message_slug(
                message_id    = email.get("message_id", "unknown"),
                received_date = email.get("received_date", ""),
                account_alias = email.get("account_alias", ""),
            )
            rel_link = f"assets/{slug}/{filename}"

        lines.append(f"![{alt}]({rel_link})")

    return "\n".join(lines) + "\n"


def _build_related_section(related_notes: list[dict]) -> str:
    """
    Render the ## Related Notes section with Obsidian wikilinks.

    Cross-account notes are annotated with *(from: {alias})* so it's
    immediately clear when a connection spans accounts.

    Shared tags are rendered as plain text (not as [[wikilinks]]). This is
    intentional: the inline injection pass (_inject_wikilinks_in_scope) is
    scoped to ## Summary and ## Key Takeaways only, so it no longer runs
    over this section. However, even before that scope restriction was added,
    rendering shared tags as [[...]] caused the injection pass to fragment
    multi-word phrases — e.g. "agent architecture" would become
    "agent [[architecture]]" because the pass matched only the last word.
    Plain text is immune to this and is correct here: the wikilink on the
    note stem (e.g. [[2026-06-21-links-...]]) is the navigation target;
    the shared tags list is supplementary label text, not a link destination.

    Only included if Agent 3 found at least one related note.
    """
    if not related_notes:
        return ""

    lines = ["\n## Related Notes"]

    for note in related_notes:
        note_stem   = Path(note["file"]).stem
        shared_tags = ", ".join(note.get("shared_tags", []))  # plain text, not wikilinks
        account_tag = f" *(from: {note['account_alias']})*" if note.get("account_alias") else ""
        lines.append(f"- [[{note_stem}]] — shared tags: {shared_tags}{account_tag}")

    return "\n".join(lines) + "\n"


def _build_research_sections(research: dict[str, dict]) -> str:
    """
    Render one ## Context section per researched topic.

    Each section is marked as auto-researched so readers know it was
    generated, not curated. Only included when the research dict is
    non-empty (Agent 5 fired for at least one topic).
    """
    if not research:
        return ""

    sections = []

    for topic, result in research.items():
        summary = result.get("summary", "_Research unavailable._")
        sections.append(
            f"\n## Context: {topic} *(new topic — auto-researched)*\n{summary}\n"
        )

    return "".join(sections)


# ---------------------------------------------------------------------------
# Filename and date helpers
# ---------------------------------------------------------------------------

def _make_filename(email: dict) -> str:
    """
    Generate the note filename in the format: YYYY-MM-DD-{account}-{slug}.md

    The slug is derived from the email subject — lowercased, spaces replaced
    with hyphens, non-alphanumeric characters stripped, truncated at 60 chars.

    Examples:
      "How LLMs Are Changing PM Work" → "2026-06-07-personal-how-llms-are-changing-pm-work.md"
      "AI Weekly #142"                → "2026-06-07-work-ai-weekly-142.md"
    """
    date_str = _parse_date(email.get("received_date", ""))
    account  = email.get("account_alias", "unknown")
    subject  = email.get("subject") or "untitled"
    slug     = _slugify(subject)
    return f"{date_str}-{account}-{slug}.md"


def _slugify(text: str) -> str:
    """
    Convert a string to a URL/filesystem-safe slug.

    Steps:
      1. Lowercase
      2. Replace runs of whitespace, underscores, or hyphens with a single hyphen
      3. Remove any character that is not alphanumeric or a hyphen
      4. Strip leading/trailing hyphens
      5. Truncate to 60 characters at a hyphen boundary where possible
    """
    text = text.lower()
    text = re.sub(r"[\s_\-]+", "-", text)
    text = re.sub(r"[^a-z0-9\-]", "", text)
    text = text.strip("-")

    if len(text) > 60:
        truncated   = text[:60]
        last_hyphen = truncated.rfind("-")
        text        = truncated[:last_hyphen] if last_hyphen > 40 else truncated

    return text or "untitled"


def _parse_date(received_date: str) -> str:
    """
    Extract YYYY-MM-DD from a raw RFC 2822 Date header string.

    Gmail Date headers have varying formats, e.g.:
      "Sat, 07 Jun 2026 14:32:00 +0800"
      "07 Jun 2026 14:32:00 GMT"

    We use a simple regex to find 4-digit years and look for month names,
    falling back to today's date if parsing fails.

    Returns:
        "YYYY-MM-DD" string.
    """
    _MONTHS = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }

    if received_date:
        # Pattern: DD Mon YYYY or Mon DD YYYY
        match = re.search(
            r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})",
            received_date
        )
        if match:
            day   = match.group(1).zfill(2)
            month = _MONTHS.get(match.group(2).lower(), "01")
            year  = match.group(3)
            return f"{year}-{month}-{day}"

    # Fallback to today in UTC
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Write a synthetic note to the notes directory and verify the output.
    Does not require Gmail or Anthropic API access.

    Run: python agents/local_writer.py
    """
    print("=== Local File Writer Agent — standalone test ===\n")

    # Synthetic inputs mimicking upstream agent outputs
    test_email = {
        "message_id":    "<CABtest1234@mail.gmail.com>",
        "account_alias": "personal",
        "sender":        "lenny@lennysnewsletter.com",
        "subject":       "How the Best PMs Use AI in Their Workflow",
        "received_date": "Sat, 07 Jun 2026 14:32:00 +0800",
    }

    test_structured = {
        "summary": (
            "Senior PMs at Notion, Linear, and Figma have shifted to using AI as a "
            "pre-writing reasoning partner rather than a drafting tool. "
            "The key insight is that LLMs excel at pressure-testing assumptions before "
            "a spec is written, reducing revision cycles by up to 40%. "
            "Teams that adopt structured prompting frameworks see the highest returns."
        ),
        "tags": ["product-management", "llm-workflows", "prompt-engineering",
                 "ai-productivity", "spec-writing"],
        "key_takeaways": [
            "Use AI before writing specs, not during — for assumption pressure-testing",
            "Structured prompting (persona + constraint + format) outperforms open-ended prompts",
            "AI-assisted user interview synthesis is an underrated use case",
            "Stanford HAI research: 40% fewer revision cycles with AI-assisted pre-writing",
        ],
        "mentions": {
            "tools":  ["Claude", "ChatGPT", "Notion AI", "Linear AI"],
            "papers": ["LLMs as Thought Partners (Stanford HAI, May 2026)"],
            "people": ["Lenny Rachitsky", "Shreyas Doshi", "Adam Fishman"],
        },
    }

    test_related = [
        {
            "file":          "2026-05-20-work-pm-digest.md",
            "account_alias": "work",
            "shared_tags":   ["product-management", "ai-productivity"],
        },
    ]

    test_research = {
        "llm-workflows": {
            "summary": (
                "LLM workflows refer to structured processes that integrate large language "
                "models into professional or automated tasks. They typically involve prompt "
                "design, chaining multiple model calls, and combining LLM output with "
                "deterministic logic. In product management, common patterns include "
                "user story generation, spec review, and synthesis of user research data. "
                "Recent tooling like LangChain and LlamaIndex has made these patterns "
                "more accessible to non-ML practitioners."
            ),
        },
    }

    test_assets = [
        {
            "filename":  "a3f9d12e01.png",
            "alt_text":  "AI workflow diagram",
        },
    ]

    print(f"Writing note for: {test_email['subject']}\n")

    # Write the note
    note_path = write_note(
        email         = test_email,
        structured    = test_structured,
        related_notes = test_related,
        research      = test_research,
        saved_assets  = test_assets,
    )

    print(f"\nNote written to: {note_path}")
    print(f"File size: {note_path.stat().st_size:,} bytes\n")

    # Update index files
    update_index(test_email, test_structured, note_path)
    update_topics_json(
        test_structured["tags"],
        note_path.name,
        test_email["account_alias"],
    )
    print("INDEX.md and topics_index.json updated.\n")

    # Print the note content for inspection
    print(f"{'─' * 60}")
    print("Note content preview:\n")
    content = note_path.read_text(encoding="utf-8")
    lines   = content.splitlines()
    for line in lines[:60]:
        print(f"  {line}")
    if len(lines) > 60:
        print(f"  ... ({len(lines) - 60} more lines)")
    print(f"{'─' * 60}")

    # Clean up test file
    note_path.unlink()
    print(f"\nTest note deleted: {note_path.name}")
    print("Test complete.")
