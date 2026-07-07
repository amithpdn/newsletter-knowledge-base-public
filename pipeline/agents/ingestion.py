# agents/ingestion.py — Newsletter AI Pipeline v4.0
# Agent 1: Multi-Account Gmail Ingestion
#
# Responsibilities:
#   - Authenticate with each configured Gmail account via OAuth2
#   - Retrieve newsletter emails using label filter or sender allowlist
#   - Decode email bodies (HTML and plain text)
#   - Tag every email with its source account alias
#   - Deduplicate across accounts using the Message-ID header
#   - Return a unified, sorted, pipeline-ready email list
#
# On first run:  a browser window opens for each account to complete OAuth consent.
#               Token files are written to secrets/ and reused on subsequent runs.
# On later runs: tokens are refreshed silently — no browser interaction needed.
#
# Usage (standalone test):
#   cd pipeline && python agents/ingestion.py
# =============================================================================

import base64
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allows this file to be run directly or imported from orchestrator
# ---------------------------------------------------------------------------
_agents_dir  = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from config import GMAIL_ACCOUNTS, GMAIL_SCOPES

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(account: dict) -> Credentials:
    """
    Authenticate a single Gmail account using OAuth2.

    Flow:
      1. If a token file already exists, load it.
      2. If the token is expired but has a refresh token, silently refresh it.
      3. If no valid token exists, open a local browser flow to prompt the user.
         This only happens on the very first run per account.
      4. Write the (new or refreshed) token back to disk for future runs.

    Args:
        account: A single entry from GMAIL_ACCOUNTS in config.py.

    Returns:
        A valid google.oauth2.credentials.Credentials object.

    Raises:
        FileNotFoundError: If credentials_file does not exist.
        google.auth.exceptions.TransportError: On network failures during refresh.
    """
    cred_path  = Path(account["credentials_file"])
    token_path = Path(account["token_file"])

    if not cred_path.exists():
        raise FileNotFoundError(
            f"OAuth2 credentials file not found for account '{account['alias']}':\n"
            f"  {cred_path}\n"
            f"Download it from: Google Cloud Console → APIs & Services → Credentials\n"
            f"Application type: Desktop App"
        )

    creds = None

    # Load existing token if available
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    # Refresh or re-authenticate if needed
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print(f"  [auth:{account['alias']}] Refreshing expired token...")
            creds.refresh(Request())
        else:
            print(
                f"  [auth:{account['alias']}] No valid token found. "
                f"Opening browser for OAuth consent..."
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(cred_path), GMAIL_SCOPES
            )
            # port=0 lets the OS pick a free port for the local redirect server
            creds = flow.run_local_server(port=0)

        # Persist the token so subsequent runs don't require browser interaction
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        print(f"  [auth:{account['alias']}] Token saved to {token_path.name}")

    return creds


# ---------------------------------------------------------------------------
# Email fetching
# ---------------------------------------------------------------------------

def fetch_emails(
    account: dict,
    processed_ids: set[str],
    limit: int | None = None,
    bootstrap: bool = False,
    date_buffer_days: int = 3,
) -> tuple[list[dict], object]:
    """
    Fetch unprocessed newsletter emails from a single Gmail account.

    Filtering strategy:
      - If sender_allowlist is populated in the account config, builds a Gmail
        search query using `from:` filters — only those senders are fetched.
        This is the most precise method and bypasses label dependency.
      - Otherwise, filters by newsletter_label (e.g. "newsletters").
      - sender_blocklist senders are always excluded via `-from:` query terms.

    Incremental runs:
      - Emails whose Message-ID is already in processed_ids are skipped.
        This is the primary deduplication mechanism for reprocessing safety.
      - Bootstrap runs (first run, or --bootstrap explicitly) pass an empty
        processed_ids set so all historical emails are fetched and processed.

    Performance — date-bounded search (added v5.4):
      On every incremental run, Agent 1 previously asked Gmail for ALL
      messages matching the allowlist/label query with no date bound, then
      fetched each one's FULL body via messages.get(format="full") just to
      check its Message-ID against processed_ids and discard it if already
      seen. As the knowledge base grows, this means re-downloading and
      re-decoding every email ever processed, on every single run — O(total
      history) instead of O(new since last run).

      Fixed in two parts:
        1. The Gmail search query itself is now bounded with `after:` to the
           most recent processed_at date (minus a safety buffer) on
           incremental runs, so Gmail's search excludes most of the
           historical backlog before any per-message fetch happens at all.
        2. Each candidate message's Message-ID is now checked via a cheap
           metadata-only fetch (format="metadata", headers only — no body)
           BEFORE the expensive full-body fetch. Only genuinely new messages
           pay the cost of a full body download and decode.

      This optimisation is skipped entirely when bootstrap=True (no prior
      run exists, or --bootstrap was explicitly passed) — a bootstrap run
      is supposed to consider full history, so no date bound is applied.

    Args:
        account:          A single entry from GMAIL_ACCOUNTS.
        processed_ids:    Set of Message-IDs already in processing_log.
                          Pass an empty set for a bootstrap (full history) run.
        limit:            Optional cap on emails returned. Useful for testing.
                          Set via --limit N CLI flag.
        bootstrap:        If True, skip the after: date bound entirely and
                          search full history. Set via --bootstrap CLI flag,
                          or automatically True on a brand-new database
                          (db.get_latest_processed_date() returns None).
        date_buffer_days: Safety margin subtracted from the most recent
                          processed_at date before building the after: bound.
                          Protects against: emails that arrived but weren't
                          yet visible in a Gmail search at the time of the
                          last run, timezone differences between Gmail's
                          Date header and the pipeline's processed_at
                          timestamp, and the gap between an email's Date
                          header and when it actually lands in the mailbox
                          (forwarded mail, delayed delivery, etc).

    Returns:
        A tuple of:
          - list of email dicts (see schema below)
          - the authenticated Gmail API service object (reused by Agent 8)

    Email dict schema:
        gmail_id        str   — Gmail internal message ID (for label operations)
        message_id      str   — RFC 2822 Message-ID header (dedup key)
        account_alias   str   — from account config (e.g. "personal")
        sender          str   — From header
        subject         str   — Subject header
        received_date   str   — Date header (raw string)
        body_html       str   — Decoded text/html body part (may be empty string)
        body_text       str   — Decoded text/plain body part (may be empty string)
        service         obj   — Gmail API service, passed through for labelling
    """
    print(f"  [ingestion:{account['alias']}] Authenticating...")
    creds   = authenticate(account)
    service = build("gmail", "v1", credentials=creds)

    # Determine the date bound for incremental runs only.
    after_date = None
    if not bootstrap:
        from db import get_latest_processed_date  # local import avoids a
                                                     # circular import at module
                                                     # load time (db imports
                                                     # nothing from agents/)
        latest = get_latest_processed_date(account_alias=account["alias"])
        if latest:
            after_date = _subtract_days(latest, date_buffer_days)
            print(
                f"  [ingestion:{account['alias']}] "
                f"Bounding search to after:{after_date} "
                f"(last processed: {latest}, buffer: {date_buffer_days}d)"
            )
        else:
            print(
                f"  [ingestion:{account['alias']}] "
                f"No prior processed emails found — searching full history "
                f"(this is expected on first run for this account)"
            )

    # Build the Gmail search query
    query = _build_search_query(account, after_date=after_date)
    print(f"  [ingestion:{account['alias']}] Query: {query}")

    emails     = []
    page_token = None
    page_num   = 0

    while True:
        page_num += 1
        try:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token

            result   = service.users().messages().list(**kwargs).execute()
            messages = result.get("messages", [])

            print(
                f"  [ingestion:{account['alias']}] "
                f"Page {page_num}: {len(messages)} message refs"
            )

            for msg_ref in messages:
                # Respect the per-account limit
                if limit is not None and len(emails) >= limit:
                    break

                email = _fetch_single_message(
                    service, msg_ref["id"], account["alias"], processed_ids
                )
                if email is not None:
                    email["service"] = service
                    emails.append(email)

        except HttpError as exc:
            print(
                f"  [ingestion:{account['alias']}] "
                f"Gmail API error on page {page_num}: {exc}"
            )
            break

        # Check pagination
        page_token = result.get("nextPageToken")
        if not page_token:
            break
        if limit is not None and len(emails) >= limit:
            break

    print(
        f"  [ingestion:{account['alias']}] "
        f"Fetched {len(emails)} new emails "
        f"(limit={limit if limit else 'none'})"
    )
    return emails, service


def _subtract_days(date_str: str, days: int) -> str:
    """
    Subtract a number of days from a "YYYY-MM-DD" string.

    Used to build the after: search bound with a safety buffer — see
    fetch_emails()'s date_buffer_days parameter for the full rationale.

    Args:
        date_str: "YYYY-MM-DD" string.
        days:     Number of days to subtract.

    Returns:
        "YYYY-MM-DD" string, `days` earlier than the input. Falls back to
        returning date_str unchanged if it can't be parsed (defensive —
        get_latest_processed_date() always returns a valid format or None,
        but this guards against unexpected input regardless).
    """
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return (dt - timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return date_str


def _build_search_query(account: dict, after_date: str | None = None) -> str:
    """
    Construct the Gmail search query string for an account.

    Priority:
      1. sender_allowlist → explicit from: filters (most precise)
      2. newsletter_label → label-based filter (catch-all)

    Blocklisted senders are always appended as exclusions.

    Args:
        account:    Account config dict.
        after_date: Optional "YYYY-MM-DD" string. If provided, appended as
                    Gmail's `after:` search operator (uses YYYY/MM/DD syntax,
                    converted here) so the API itself excludes most of the
                    historical backlog instead of returning it for the
                    pipeline to discard after a full per-message fetch.
                    Pass None on first-ever run (no processed emails yet,
                    full historical bootstrap needed) or when --bootstrap
                    is explicitly requested.
    """
    if account.get("sender_allowlist"):
        parts   = [f"from:{s}" for s in account["sender_allowlist"]]
        query   = "(" + " OR ".join(parts) + ")"
    else:
        label   = account.get("newsletter_label", "newsletters")
        query   = f"label:{label}"

    for blocked in account.get("sender_blocklist", []):
        query += f" -from:{blocked}"

    if after_date:
        # Gmail's after: operator uses YYYY/MM/DD, not YYYY-MM-DD
        gmail_date = after_date.replace("-", "/")
        query += f" after:{gmail_date}"

    return query


def _fetch_single_message(
    service,
    gmail_id: str,
    account_alias: str,
    processed_ids: set[str],
) -> dict | None:
    """
    Fetch and decode a single Gmail message.

    Performance (added v5.4): this now does a cheap metadata-only fetch
    first (format="metadata", headers only — no body) to extract the
    Message-ID and check it against processed_ids BEFORE paying for the
    expensive full-body fetch (format="full", which downloads and the
    caller decodes the entire HTML/text body). Combined with the date-bound
    search query in fetch_emails(), this means duplicate/already-processed
    messages that still happen to match the search query are discarded
    cheaply instead of fully downloaded and decoded just to be thrown away.

    Returns None if:
      - The Message-ID header is missing (malformed email)
      - The Message-ID is already in processed_ids (already processed)
      - A Gmail API error occurs fetching this specific message

    Args:
        service:       Authenticated Gmail API service.
        gmail_id:      Gmail internal message ID string.
        account_alias: Account alias for tagging.
        processed_ids: Set of already-processed Message-IDs.

    Returns:
        Email dict or None.
    """
    # --- Cheap pass: headers only, no body ---
    try:
        meta = service.users().messages().get(
            userId="me", id=gmail_id, format="metadata",
            metadataHeaders=["Message-ID"],
        ).execute()
    except HttpError as exc:
        print(f"    [ingestion] Could not fetch metadata for {gmail_id}: {exc}")
        return None

    message_id = _extract_header(meta, "Message-ID")

    if not message_id:
        print(f"    [ingestion] Skipping message {gmail_id}: no Message-ID header")
        return None

    if message_id in processed_ids:
        # Already processed — discarded here, before any body download.
        return None

    # --- Expensive pass: only reached for genuinely new messages ---
    try:
        msg = service.users().messages().get(
            userId="me", id=gmail_id, format="full"
        ).execute()
    except HttpError as exc:
        print(f"    [ingestion] Could not fetch message {gmail_id}: {exc}")
        return None

    return {
        "gmail_id":      gmail_id,
        "message_id":    message_id,
        "account_alias": account_alias,
        "sender":        _extract_header(msg, "From"),
        "subject":       _extract_header(msg, "Subject"),
        "received_date": _extract_header(msg, "Date"),
        "body_html":     _decode_body(msg, "text/html"),
        "body_text":     _decode_body(msg, "text/plain"),
    }


# ---------------------------------------------------------------------------
# Multi-account ingestion with deduplication
# ---------------------------------------------------------------------------

def ingest_all_accounts(
    accounts: list[dict],
    processed_ids: set[str],
    limit: int | None = None,
    bootstrap: bool = False,
) -> list[dict]:
    """
    Run Agent 1 across all configured Gmail accounts.

    Steps:
      1. Fetch emails from each account independently.
      2. Merge all emails into a single list.
      3. Deduplicate by Message-ID — if the same email appears in multiple
         accounts (e.g. a newsletter forwarded to both), the first-seen copy
         is kept and the rest are dropped. The first-seen copy retains the
         account_alias of the account it was first retrieved from.
      4. Sort the merged list by received_date ascending so the pipeline
         processes emails in chronological order.

    Args:
        accounts:      List of account dicts from GMAIL_ACCOUNTS.
        processed_ids: Already-processed Message-IDs from the database.
        limit:         Optional per-account cap (passed to fetch_emails).
        bootstrap:     If True, skip the after: date-bound search optimisation
                       and search full history for every account. Passed
                       through from the orchestrator's --bootstrap flag.

    Returns:
        Unified, deduplicated, sorted list of email dicts.
        Each email has a 'service' key for use by Agent 8 (labelling).
    """
    all_emails: list[dict] = []

    for account in accounts:
        emails, _ = fetch_emails(account, processed_ids, limit=limit, bootstrap=bootstrap)
        all_emails.extend(emails)

    # Deduplicate by Message-ID across accounts, keeping first occurrence
    seen_ids: set[str]  = set()
    deduped:  list[dict] = []
    duplicates_dropped  = 0

    for email in all_emails:
        mid = email["message_id"]
        if mid not in seen_ids:
            seen_ids.add(mid)
            deduped.append(email)
        else:
            duplicates_dropped += 1

    if duplicates_dropped:
        print(
            f"  [ingestion] Dropped {duplicates_dropped} cross-account duplicate(s)"
        )

    # Sort chronologically — Gmail Date headers are RFC 2822 strings which sort
    # lexicographically in a useful order for the common formats; this is a
    # best-effort sort since Date header formats vary across senders.
    deduped.sort(key=lambda e: e.get("received_date", ""))

    total = len(deduped)
    print(
        f"  [ingestion] Total after deduplication: {total} email(s) ready for processing"
    )
    return deduped


# ---------------------------------------------------------------------------
# Body decoding helpers
# ---------------------------------------------------------------------------

def _extract_header(msg: dict, name: str) -> str:
    """
    Extract a single header value from a Gmail message object.
    Returns empty string if the header is not present.
    Header name matching is case-insensitive.
    """
    headers = msg.get("payload", {}).get("headers", [])
    name_lower = name.lower()
    return next(
        (h["value"] for h in headers if h["name"].lower() == name_lower),
        ""
    )


def _decode_body(msg: dict, target_mime: str) -> str:
    """
    Recursively walk the Gmail message payload tree and return the decoded
    body of the first part matching target_mime.

    Gmail message payloads can be:
      - Flat (singlepart): payload.body.data contains the body directly.
      - Nested (multipart): payload.parts is a list of sub-parts, each of
        which may itself be multipart (e.g. multipart/related containing
        multipart/alternative containing text/plain and text/html).

    This function handles arbitrary nesting depth.

    Args:
        msg:         Full Gmail message object (format="full").
        target_mime: MIME type to find, e.g. "text/html" or "text/plain".

    Returns:
        Decoded UTF-8 string of the body, or empty string if not found.
    """
    def _walk(part: dict) -> str:
        mime = part.get("mimeType", "")

        # Direct match on a leaf part
        if mime == target_mime:
            data = part.get("body", {}).get("data", "")
            if data:
                try:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    return ""

        # Recurse into multipart sub-parts
        for sub in part.get("parts", []):
            result = _walk(sub)
            if result:
                return result

        return ""

    return _walk(msg.get("payload", {}))


# ---------------------------------------------------------------------------
# Standalone test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Run this file directly to verify OAuth and email fetching for the first
    configured account before wiring up the full pipeline.

    Usage:
        cd C:\\...\\newsletter-pipeline
        .venv\\Scripts\\python.exe pipeline\\agents\\ingestion.py

    Expected output:
        - Browser window for OAuth on first run (token saved after)
        - List of 3 email subjects printed to stdout
        - No exceptions
    """
    print("=== Ingestion Agent — standalone test ===\n")

    if not GMAIL_ACCOUNTS:
        print("ERROR: No accounts configured in config.py")
        sys.exit(1)

    test_account = GMAIL_ACCOUNTS[0]
    print(f"Testing account: {test_account['alias']}\n")

    try:
        emails, _ = fetch_emails(
            account=test_account,
            processed_ids=set(),   # empty set → fetch everything
            limit=3,               # fetch at most 3 for the test
        )
    except FileNotFoundError as exc:
        print(f"\nSetup error:\n{exc}")
        sys.exit(1)

    if not emails:
        print(
            "\nNo emails returned. Check that:\n"
            f"  1. The Gmail label '{test_account.get('newsletter_label')}' exists "
            f"and has emails, OR\n"
            f"  2. sender_allowlist in config.py contains at least one active sender."
        )
        sys.exit(0)

    print(f"\n{'─' * 60}")
    print(f"  {'#':<4} {'Account':<12} {'Subject'}")
    print(f"{'─' * 60}")
    for i, email in enumerate(emails, 1):
        subject = (email["subject"] or "(no subject)")[:55]
        print(f"  {i:<4} {email['account_alias']:<12} {subject}")
    print(f"{'─' * 60}")
    print(f"\nTest passed — {len(emails)} email(s) fetched successfully.")
    print("body_html available:", bool(emails[0].get("body_html")))
    print("body_text available:", bool(emails[0].get("body_text")))
