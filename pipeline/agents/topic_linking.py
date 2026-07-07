# agents/topic_linking.py — Newsletter AI Pipeline v4.0
# Agent 3: Topic Linking
#
# Responsibilities:
#   - Compare newly extracted tags against the unified topic index
#   - Identify semantically related notes using local embeddings
#   - Surface connections across accounts (personal ↔ work)
#   - Update the topic index with new tags after a note is written
#   - Identify brand-new topics that should trigger the Research Agent
#
# Method:
#   sentence-transformers model 'all-MiniLM-L6-v2'
#   Cosine similarity threshold: configurable via SIMILARITY_THRESHOLD in config.py
#   Embeddings stored as pickled numpy arrays in the topic_index SQLite table
#   Zero API cost — runs entirely on-device
#
# Model download:
#   The 'all-MiniLM-L6-v2' model (~80MB) is downloaded automatically from
#   HuggingFace on first use and cached locally in ~/.cache/huggingface/.
#   Subsequent runs load from the local cache — no internet required.
#
# Usage (standalone test):
#   cd pipeline && python agents/topic_linking.py
# =============================================================================

import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config import EMBEDDING_MODEL, SIMILARITY_THRESHOLD, RELATED_NOTES_MIN_SHARED_TAGS, RELATED_NOTES_MAX_RESULTS
from db import get_connection

# ---------------------------------------------------------------------------
# Model — lazy-loaded singleton
# ---------------------------------------------------------------------------
# The sentence-transformers model is ~80MB and takes ~1–2 seconds to load.
# We load it once on first use and reuse across all calls in a pipeline run.

_model = None


def _get_model():
    """
    Lazy-load and cache the sentence-transformers model.
    Import is also deferred here so that agents that don't use topic linking
    don't pay the import cost of loading sentence-transformers.
    """
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers is not installed.\n"
                "Run: pip install sentence-transformers"
            )
        print(f"  [topic_linking] Loading embedding model '{EMBEDDING_MODEL}'...")
        _model = SentenceTransformer(EMBEDDING_MODEL)
        print(f"  [topic_linking] Model loaded.")
    return _model


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _embed(texts: list[str]) -> np.ndarray:
    """
    Encode a list of text strings into embedding vectors.

    Returns:
        numpy array of shape (len(texts), embedding_dim)
        For all-MiniLM-L6-v2, embedding_dim = 384
    """
    model = _get_model()
    return model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Compute cosine similarity between two 1-D numpy vectors.
    Both vectors are assumed to be already L2-normalised (as produced by
    sentence-transformers with normalize_embeddings=True), so this reduces
    to a dot product — fast and numerically stable.
    """
    return float(np.dot(a, b))


def _serialise_embedding(vec: np.ndarray) -> bytes:
    """Serialise a numpy float32 embedding vector to bytes for SQLite BLOB storage."""
    return pickle.dumps(vec.astype(np.float32))


def _deserialise_embedding(blob: bytes) -> np.ndarray:
    """Deserialise a numpy embedding vector from SQLite BLOB bytes."""
    return pickle.loads(blob).astype(np.float32)


# ---------------------------------------------------------------------------
# Core: find related notes
# ---------------------------------------------------------------------------

def find_related_notes(
    new_tags:          list[str],
    current_note_file: str,
    min_shared_tags:   int = RELATED_NOTES_MIN_SHARED_TAGS,
    max_results:       int = RELATED_NOTES_MAX_RESULTS,
) -> list[dict]:
    """
    Compare new_tags against all existing tags in the topic index and return
    notes that share semantically similar topics.
 
    A note is included in results if at least one of its tags has cosine
    similarity >= SIMILARITY_THRESHOLD against at least one of new_tags,
    AND the note accumulates at least `min_shared_tags` distinct shared
    tags overall.
 
    Cross-account matches are included and annotated with the source alias,
    so the note writer can render them as:
      [[2026-05-12-work-some-newsletter]] — shared tags: RAG, embeddings *(from: work)*
 
    Args:
        new_tags:          List of tags extracted by Agent 2 for the current email.
        current_note_file: Filename of the note being written (excluded from results).
        min_shared_tags:   Minimum number of distinct shared tags required for
                            a note to be included. Defaults to
                            config.RELATED_NOTES_MIN_SHARED_TAGS. A value of 1
                            reproduces the original (pre-patch) behaviour —
                            any single coincidental tag match qualifies.
        max_results:       Maximum number of related notes returned, after
                            sorting by shared-tag count descending. Defaults
                            to config.RELATED_NOTES_MAX_RESULTS. Pass a large
                            number (or float('inf')) to effectively disable
                            the cap.
 
    Returns:
        List of dicts, each with:
          file          str  — note filename
          account_alias str  — source account alias
          shared_tags   list — tags from new_tags that matched this note
        Sorted by number of shared tags descending (most related first),
        truncated to at most max_results entries.
        Empty list if no related notes found or topic index is empty.
    """
    # --- everything from here down to the sort/return block below is
    #     UNCHANGED from the original function ---
    if not new_tags:
        return []
 
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT tag, note_files, embedding_vector FROM topic_index"
        ).fetchall()
    finally:
        conn.close()
 
    if not rows:
        return []
 
    new_embeddings = _embed(new_tags)
 
    related: dict[str, dict] = {}
 
    for row in rows:
        if row["embedding_vector"] is None:
            continue
 
        existing_embedding = _deserialise_embedding(row["embedding_vector"])
 
        # Parsed lazily, once per row — previously json.loads() ran inside
        # the inner loop for EVERY tag that cleared the similarity threshold,
        # re-parsing the same JSON repeatedly on every note write.
        note_files = None
 
        for new_tag, new_emb in zip(new_tags, new_embeddings):
            sim = _cosine_similarity(new_emb, existing_embedding)
 
            if sim >= SIMILARITY_THRESHOLD:
                if note_files is None:
                    note_files = json.loads(row["note_files"] or "[]")
                for nf in note_files:
                    note_file     = nf.get("file", "")
                    account_alias = nf.get("account_alias", "")
 
                    if note_file == current_note_file:
                        continue
 
                    if note_file not in related:
                        related[note_file] = {
                            "file":          note_file,
                            "account_alias": account_alias,
                            "shared_tags":   [],
                        }
                    if new_tag not in related[note_file]["shared_tags"]:
                        related[note_file]["shared_tags"].append(new_tag)
 
    # -----------------------------------------------------------------
    # CHANGED FROM HERE DOWN — apply the min-shared-tags filter, then
    # sort, then truncate to max_results.
    # -----------------------------------------------------------------
 
    # Quality filter: drop notes that only share a coincidental single
    # tag (or fewer than min_shared_tags), before sorting/truncating.
    qualifying = [
        r for r in related.values()
        if len(r["shared_tags"]) >= min_shared_tags
    ]
 
    # Sort by number of shared tags descending — most related notes first
    result = sorted(
        qualifying,
        key=lambda r: len(r["shared_tags"]),
        reverse=True,
    )
 
    # Hard cap — keep only the strongest max_results matches
    truncated_count = max(0, len(result) - max_results)
    result = result[:max_results]
 
    if result:
        suffix = f" (capped from {len(result) + truncated_count})" if truncated_count else ""
        print(
            f"    [topic_linking] Found {len(result)} related note(s){suffix} "
            f"for tags: {', '.join(new_tags[:4])}{'...' if len(new_tags) > 4 else ''}"
        )
    else:
        print(f"    [topic_linking] No related notes found "
              f"(min_shared_tags={min_shared_tags}).")
 
    return result


# ---------------------------------------------------------------------------
# Core: identify new topics
# ---------------------------------------------------------------------------

def get_new_topics(tags: list[str]) -> list[str]:
    """
    Return tags that do not yet exist in the topic index (exact match only).

    These are candidates for the Research Agent (Agent 5). The Research Agent
    will fetch web context for each new topic and append it to the note.

    Exact match is used here (not semantic similarity) because:
      - The goal is to find truly unseen concepts, not approximate matches
      - Semantic match would suppress research for genuinely new topics
        that happen to be similar to an existing tag
      - The Research Agent is cheap per call and should err on the side
        of running more often rather than less

    Args:
        tags: List of tags extracted by Agent 2.

    Returns:
        List of tags not present in topic_index.tag (case-insensitive).
    """
    if not tags:
        return []

    conn = get_connection()
    try:
        existing = {
            row["tag"].lower()
            for row in conn.execute("SELECT tag FROM topic_index").fetchall()
        }
    finally:
        conn.close()

    new = [t for t in tags if t.lower() not in existing]

    if new:
        print(f"    [topic_linking] New topics detected: {', '.join(new)}")
    else:
        print(f"    [topic_linking] All tags already in index.")

    return new


# ---------------------------------------------------------------------------
# Core: update topic index
# ---------------------------------------------------------------------------

def update_topic_index(
    tags:          list[str],
    note_file:     str,
    account_alias: str,
) -> None:
    """
    Add or update tags in the unified topic index after a note has been written.

    For each tag:
      - If the tag is new: insert a row with embedding vector and note reference
      - If the tag exists: append the new note reference to its note_files list
        (the embedding vector is not re-computed — the first embedding is kept,
        as MiniLM produces stable embeddings for the same text)

    Args:
        tags:          List of tags from the just-written note.
        note_file:     Filename of the written note (e.g. "2026-06-07-personal-slug.md").
        account_alias: Account alias for the note (e.g. "personal").

    This function is idempotent for a given (tag, note_file) pair — calling it
    twice will result in a duplicate note_file entry in the JSON array, but
    since processed emails are tracked in processing_log this should not occur
    in normal operation.
    """
    if not tags:
        return

    now         = datetime.now(timezone.utc).isoformat()
    embeddings  = _embed(tags)

    conn = get_connection()
    try:
        for tag, embedding in zip(tags, embeddings):
            tag_lower = tag.lower().strip()
            if not tag_lower:
                continue

            existing = conn.execute(
                "SELECT note_files FROM topic_index WHERE tag = ?",
                (tag_lower,)
            ).fetchone()

            note_entry = {"file": note_file, "account_alias": account_alias}

            if existing:
                # Tag exists — append this note to its reference list
                note_files: list = json.loads(existing["note_files"] or "[]")
                note_files.append(note_entry)
                conn.execute(
                    "UPDATE topic_index SET note_files = ? WHERE tag = ?",
                    (json.dumps(note_files), tag_lower)
                )
            else:
                # New tag — insert with embedding
                conn.execute(
                    """INSERT INTO topic_index
                       (tag, first_seen, first_seen_account, note_files, embedding_vector)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        tag_lower,
                        now,
                        account_alias,
                        json.dumps([note_entry]),
                        _serialise_embedding(embedding),
                    )
                )

        conn.commit()
        print(
            f"    [topic_linking] Topic index updated: "
            f"{len(tags)} tag(s) for '{note_file}'"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Utility: get all tags (for dashboard / inspection)
# ---------------------------------------------------------------------------

def get_all_tags() -> list[dict]:
    """
    Return all tags in the topic index with metadata.
    Used by the dashboard and for debugging.

    Returns:
        List of dicts: {tag, first_seen, first_seen_account, note_count}
        Sorted alphabetically by tag.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT tag, first_seen, first_seen_account, note_files FROM topic_index"
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        note_files = json.loads(row["note_files"] or "[]")
        result.append({
            "tag":               row["tag"],
            "first_seen":        row["first_seen"],
            "first_seen_account": row["first_seen_account"],
            "note_count":        len(note_files),
        })

    return sorted(result, key=lambda r: r["tag"])


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Test topic linking with synthetic data.
    Inserts test entries into topic_index, then finds related notes.
    Cleans up after itself.

    Run: python agents/topic_linking.py
    """
    print("=== Topic Linking Agent — standalone test ===\n")

    # Step 1: Seed the index with some existing tags
    print("Step 1: Seeding topic index with existing tags...")
    update_topic_index(
        tags=["rag pipelines", "llm fine-tuning", "vector databases", "embeddings"],
        note_file="2026-05-15-personal-ai-weekly.md",
        account_alias="personal",
    )
    update_topic_index(
        tags=["product management", "user research", "agile frameworks"],
        note_file="2026-05-20-work-pm-digest.md",
        account_alias="work",
    )
    print()

    # Step 2: Find related notes for new tags
    print("Step 2: Finding related notes for new tags...")
    new_tags = ["retrieval augmented generation", "semantic search", "product strategy"]
    related  = find_related_notes(new_tags, current_note_file="2026-06-07-personal-new.md")

    print(f"\nRelated notes found: {len(related)}")
    for r in related:
        print(
            f"  [{r['account_alias']}] {r['file']} "
            f"— shared tags: {', '.join(r['shared_tags'])}"
        )

    # Step 3: Identify new topics (not yet in index)
    print("\nStep 3: Identifying new topics...")
    new_topics = get_new_topics(new_tags)
    print(f"  New topics (would trigger Research Agent): {new_topics}")

    # Step 4: Show index state
    print("\nStep 4: Current topic index:")
    all_tags = get_all_tags()
    print(f"  {'Tag':<35} {'Account':<12} {'Notes'}")
    print(f"  {'─' * 55}")
    for t in all_tags:
        print(
            f"  {t['tag']:<35} {t['first_seen_account']:<12} {t['note_count']}"
        )

    # Step 5: Clean up test entries
    print("\nStep 5: Cleaning up test data...")
    conn = get_connection()
    conn.execute(
        "DELETE FROM topic_index WHERE tag IN (?, ?, ?, ?, ?, ?, ?)",
        (
            "rag pipelines", "llm fine-tuning", "vector databases", "embeddings",
            "product management", "user research", "agile frameworks",
        )
    )
    conn.commit()
    conn.close()
    print("  Cleanup done.")

    print("\nTest complete.")
