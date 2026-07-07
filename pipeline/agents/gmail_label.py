# agents/gmail_label.py — Newsletter AI Pipeline v4.0
# Agent 8: Gmail Label
#
# Responsibilities:
#   - Apply "AI Processed" label after Agent 7 confirms a successful note write
#   - Apply "AI Review" label to marketing/uncertain emails sent to review queue
#   - Create labels in Gmail if they don't exist yet (first run)
#   - Operate per-account using the Gmail service object from Agent 1
#
# Trigger rules (from design doc):
#   Successfully written to OneDrive folder  → "AI Processed"
#   Skipped — marketing or low confidence   → "AI Review"
#   Skipped — cross-account duplicate       → No label applied
#   Failed (exception during processing)    → No label (retried next run)
#
# The Gmail service object is passed in from Agent 1 (ingestion) so that
# authentication is handled once per account per run, not once per email.
#
# Labels are created lazily on first use and cached in-memory for the
# duration of the run — one list() call per account per run maximum.
#
# Usage (standalone test):
#   cd pipeline && python agents/gmail_label.py
#   (Requires a working OAuth token from a previous ingestion test run)
# =============================================================================

import sys
from pathlib import Path

from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ---------------------------------------------------------------------------
# Label name constants
# ---------------------------------------------------------------------------

LABEL_PROCESSED = "AI Processed"
LABEL_REVIEW    = "AI Review"

# ---------------------------------------------------------------------------
# In-memory label cache
# ---------------------------------------------------------------------------
# Maps: (account_alias, label_name) → label_id
# Populated lazily on first use per account per run.
# Avoids repeated labels.list() API calls for the same account.

_label_cache: dict[tuple[str, str], str] = {}


def clear_label_cache() -> None:
    """
    Clear the in-memory label ID cache.
    Call at the start of each pipeline run to force a fresh label lookup.
    Useful if labels are manually renamed or deleted between runs.
    """
    _label_cache.clear()


# ---------------------------------------------------------------------------
# Core label application
# ---------------------------------------------------------------------------

def apply_processed_label(service, gmail_id: str, account_alias: str) -> bool:
    """
    Apply the "AI Processed" label to a Gmail message.

    Call this after Agent 7 returns a successful note write path.
    The label signals to the Gmail inbox that this email has been processed
    and should not be picked up again on the next pipeline run.

    Args:
        service:       Authenticated Gmail API service from Agent 1.
        gmail_id:      Gmail internal message ID (not the Message-ID header).
        account_alias: Account alias string, used for cache keying and logging.

    Returns:
        True if the label was applied successfully, False otherwise.
        Failures are logged as warnings — a label failure does not invalidate
        the note that was already written.
    """
    return _apply_label(service, gmail_id, account_alias, LABEL_PROCESSED)


def apply_review_label(service, gmail_id: str, account_alias: str) -> bool:
    """
    Apply the "AI Review" label to a Gmail message.

    Call this for emails that were classified as marketing/uncertain and
    sent to the review queue rather than processed. Allows manual inspection
    to catch false positives from the classification agent.

    Args:
        service:       Authenticated Gmail API service from Agent 1.
        gmail_id:      Gmail internal message ID.
        account_alias: Account alias for cache keying and logging.

    Returns:
        True on success, False on failure (logged as warning).
    """
    return _apply_label(service, gmail_id, account_alias, LABEL_REVIEW)


def apply_label_by_status(
    service,
    gmail_id:      str,
    account_alias: str,
    status:        str,
) -> bool:
    """
    Apply the appropriate label based on processing status.

    Convenience function for the orchestrator — translates a status string
    to the correct label and applies it.

    Status → Label mapping:
        "success"            → "AI Processed"
        "skipped_marketing"  → "AI Review"
        "skipped_blocklist"  → "AI Review"
        "failed"             → no label (returns False without API call)
        "skipped_duplicate"  → no label (returns False without API call)

    Args:
        service:       Authenticated Gmail API service.
        gmail_id:      Gmail internal message ID.
        account_alias: Account alias string.
        status:        Processing status from the orchestrator.

    Returns:
        True if a label was applied, False if no label was needed or call failed.
    """
    if status == "success":
        return apply_processed_label(service, gmail_id, account_alias)

    if status in ("skipped_marketing", "skipped_blocklist"):
        return apply_review_label(service, gmail_id, account_alias)

    # "failed" and "skipped_duplicate" intentionally receive no label
    return False


# ---------------------------------------------------------------------------
# Internal: label resolution and application
# ---------------------------------------------------------------------------

def _apply_label(
    service,
    gmail_id:      str,
    account_alias: str,
    label_name:    str,
) -> bool:
    """
    Internal: resolve label ID, then call messages.modify to apply it.

    Args:
        service:       Gmail API service object.
        gmail_id:      Gmail message ID.
        account_alias: Used for cache key and log messages.
        label_name:    Human-readable label name (e.g. "AI Processed").

    Returns:
        True on success, False on any HttpError.
    """
    try:
        label_id = _get_or_create_label(service, account_alias, label_name)

        service.users().messages().modify(
            userId="me",
            id=gmail_id,
            body={"addLabelIds": [label_id]},
        ).execute()

        print(f"    [label:{account_alias}] Applied '{label_name}' to {gmail_id[:12]}...")
        return True

    except HttpError as exc:
        print(
            f"    [label:{account_alias}] WARNING: Could not apply '{label_name}' "
            f"to {gmail_id[:12]}...: {exc}"
        )
        return False


def _get_or_create_label(service, account_alias: str, label_name: str) -> str:
    """
    Return the Gmail label ID for label_name, creating it if it doesn't exist.

    Uses the in-memory cache to avoid repeated API calls within a run.

    Args:
        service:       Gmail API service object.
        account_alias: Used as part of the cache key.
        label_name:    The label name to look up or create.

    Returns:
        Gmail label ID string (e.g. "Label_1234567890123456789").

    Raises:
        googleapiclient.errors.HttpError: If API calls fail.
    """
    cache_key = (account_alias, label_name)

    # Return from cache if already resolved this run
    if cache_key in _label_cache:
        return _label_cache[cache_key]

    # Fetch all existing labels for this account
    response = service.users().labels().list(userId="me").execute()
    existing_labels = response.get("labels", [])

    for label in existing_labels:
        if label["name"] == label_name:
            _label_cache[cache_key] = label["id"]
            return label["id"]

    # Label doesn't exist — create it
    print(f"    [label:{account_alias}] Creating new label: '{label_name}'")
    created = service.users().labels().create(
        userId="me",
        body={
            "name":                  label_name,
            "labelListVisibility":   "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()

    label_id = created["id"]
    _label_cache[cache_key] = label_id
    print(f"    [label:{account_alias}] Created '{label_name}' with ID: {label_id}")
    return label_id


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Minimal connectivity test — authenticates to Gmail and lists existing labels.
    Does NOT apply any labels to real emails (read-only verification).

    Requires a valid token file from a prior ingestion test run.
    Run: python agents/gmail_label.py
    """
    from googleapiclient.discovery import build
    from agents.ingestion import authenticate
    from config import GMAIL_ACCOUNTS

    print("=== Gmail Label Agent — standalone test ===\n")

    if not GMAIL_ACCOUNTS:
        print("ERROR: No accounts configured in config.py")
        sys.exit(1)

    account = GMAIL_ACCOUNTS[0]
    print(f"Testing with account: {account['alias']}\n")

    creds   = authenticate(account)
    service = build("gmail", "v1", credentials=creds)

    # List existing labels — read-only, no modifications
    response       = service.users().labels().list(userId="me").execute()
    existing_names = [lb["name"] for lb in response.get("labels", [])]

    print(f"Existing labels in account ({len(existing_names)} total):")
    pipeline_labels = [n for n in existing_names if n.startswith("AI ")]
    other_labels    = [n for n in existing_names if not n.startswith("AI ")]

    if pipeline_labels:
        print(f"\n  Pipeline labels already present:")
        for name in sorted(pipeline_labels):
            print(f"    ✓ {name}")
    else:
        print(f"\n  No pipeline labels found yet.")
        print(f"  '{LABEL_PROCESSED}' and '{LABEL_REVIEW}' will be created on first run.")

    print(f"\n  Other labels (first 10): {sorted(other_labels)[:10]}")
    print(f"\nTest complete — no labels were applied or modified.")
