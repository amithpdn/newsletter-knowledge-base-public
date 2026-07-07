# =============================================================================
# config.example.py — Newsletter AI Pipeline v1.0
# Sanitised configuration TEMPLATE for the public repository.
#
# This file is a copy of config.py with all personal data removed
# (Gmail sender allowlists, local paths). The real config.py is excluded
# from the public sync — see sync-to-public.ps1.
#
# SETUP INSTRUCTIONS:
#   1. Copy this file to config.py in the same folder:
#        copy pipeline\config.example.py pipeline\config.py     (Windows)
#        cp pipeline/config.example.py pipeline/config.py       (Linux/macOS)
#   2. Copy your Anthropic API key into the .env file (never hardcode it here).
#   3. Add your Gmail account details to GMAIL_ACCOUNTS below.
#   4. Run `python pipeline/config.py` to validate the setup before running
#      the pipeline.
# =============================================================================

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# Load API keys from .env file located in the project root.
# The .env file is gitignored — never commit it. See .env.example.
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_env_path)

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Primary storage path
# ---------------------------------------------------------------------------

# All pipeline output is written here. By default PROJECT_ROOT is derived
# from this file's location (the repository root), so cloning the repo into
# an OneDrive-synced folder is enough — the OneDrive client handles cloud
# propagation automatically with no pipeline code needed for sync.
#
# To pin the pipeline to a fixed folder instead, uncomment the line below
# and replace <username> with your actual Windows account name.
# To find it: open PowerShell and run: $env:USERNAME
# PROJECT_ROOT = Path(r"C:\Users\<username>\OneDrive\Documents\newsletter-pipeline")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Derived paths — do not edit these directly; change PROJECT_ROOT above.
NOTES_DIR       = PROJECT_ROOT / "notes"
ASSETS_DIR      = NOTES_DIR / "assets"
DASHBOARD_DIR   = PROJECT_ROOT / "dashboard"
SECRETS_DIR     = PROJECT_ROOT / "secrets"
DB_PATH         = PROJECT_ROOT / "registry.db"
TOPICS_INDEX    = PROJECT_ROOT / "topics_index.json"
INDEX_MD        = PROJECT_ROOT / "INDEX.md"
LOGS_DIR        = PROJECT_ROOT / "logs"      # session-level run logs (e.g. link_ingestion fetch logs)

# ---------------------------------------------------------------------------
# Gmail account configuration
# ---------------------------------------------------------------------------
#
# Each entry represents one Gmail account the pipeline will ingest from.
#
# Fields:
#   alias             — short name used in note filenames and tags (e.g. "personal")
#   credentials_file  — path to the OAuth2 client secret JSON downloaded from
#                       Google Cloud Console (Desktop App type)
#   token_file        — path where the OAuth2 access/refresh token will be stored
#                       after first-run browser authentication; auto-created
#   newsletter_label  — Gmail label name to filter inbox (e.g. "newsletters")
#                       Only used if sender_allowlist is empty
#   sender_allowlist  — if non-empty, only these senders are fetched regardless
#                       of label; bypasses the classifier (treated as editorial)
#   sender_blocklist  — senders to always skip, even if they match the label
#
# Add a second account by duplicating the dict below with alias="work".
# Run `python main.py emails --account personal --dry-run` first to
# authenticate each account individually before a combined run.

GMAIL_ACCOUNTS: list[dict] = [
    {
        "alias":            "personal",
        "credentials_file": str(SECRETS_DIR / "credentials-personal.json"),
        "token_file":       str(SECRETS_DIR / "token-personal.json"),
        "newsletter_label": "newsletters",
        "sender_allowlist": [
            # Add the newsletter senders you subscribe to, e.g.:
            # "lenny@lennysnewsletter.com",
            # "newsletter@tldr.tech",
            # "author@substack.com",
        ],
        "sender_blocklist": [
            # Senders to always skip (e.g. transactional emails that got labelled):
            # "noreply@someservice.com",
        ],
    },
    # Uncomment and fill in once the first account is working:
    # {
    #     "alias":            "work",
    #     "credentials_file": str(SECRETS_DIR / "credentials-work.json"),
    #     "token_file":       str(SECRETS_DIR / "token-work.json"),
    #     "newsletter_label": "newsletters",
    #     "sender_allowlist": [],
    #     "sender_blocklist": [],
    # },
]

# ---------------------------------------------------------------------------
# Gmail OAuth scopes
# ---------------------------------------------------------------------------

# gmail.modify allows reading messages AND applying labels.
# If you only want to read (no labelling), use gmail.readonly instead —
# but note that Agent 8 (Gmail Label) will then fail silently.
GMAIL_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.modify"]

# ---------------------------------------------------------------------------
# Agent 1.5 — Content Classification
# ---------------------------------------------------------------------------

# LLM confidence threshold below which an email is sent to "AI Review"
# regardless of its classification result.
# Range: 0.0–1.0. Default 0.75 is a safe starting point; lower it (e.g. 0.65)
# once you've reviewed a few weeks of "AI Review" emails and the classifier
# is performing well.
CLASSIFIER_CONFIDENCE_THRESHOLD: float = 0.75

# What to do with emails classified as marketing or below the confidence threshold.
# Options: "review" (apply "AI Review" label) | "archive" | "delete"
# "review" is strongly recommended during the tuning period.
MARKETING_DISPOSITION: str = "review"

# Heuristic score thresholds (Agent 1.5, Stage 1):
#   score >= HEURISTIC_SKIP_THRESHOLD  → skip immediately (no LLM call)
#   score < HEURISTIC_PASS_THRESHOLD   → pass directly to summarisation
#   between the two                    → escalate to LLM classifier
HEURISTIC_SKIP_THRESHOLD: int = 8
HEURISTIC_PASS_THRESHOLD: int = 3

# ---------------------------------------------------------------------------
# Agent 2 — Summarisation
# ---------------------------------------------------------------------------

# Model used for summarisation. Haiku 4.5 is the cost-optimal choice.
SUMMARISATION_MODEL: str = "claude-haiku-4-5"

# Maximum body length (characters) passed to the summarisation prompt.
# Truncates at this point to stay within token limits for long newsletters.
SUMMARISATION_BODY_LIMIT: int = 8_000

# ---------------------------------------------------------------------------
# Agent 3 — Topic Linking
# ---------------------------------------------------------------------------

# sentence-transformers model for local embedding generation.
# all-MiniLM-L6-v2 is ~80MB, fast, and well-suited for short tag phrases.
# No API cost — runs entirely on-device.
EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"

# Cosine similarity threshold for linking two notes as "related".
# 0.75 keeps links meaningful; lower values produce more (noisier) links.
SIMILARITY_THRESHOLD: float = 0.75

# Minimum number of shared tags required before a note is considered
# "related" enough to render in the ## Related Notes section.
#
# 1 means a single coincidental tag match (e.g. both notes tagged "ai")
# is enough to link two otherwise-unrelated notes. As the topic index
# grows, broad tags accumulate hundreds of notes, and a threshold of 1
# is what causes Related Notes sections to balloon to 100+ entries.
#
# Raising this to 2 means two notes must share at least two specific
# topics to be considered related — a much stronger relevance signal.
# Notes with very few tags (1-2 total) may never reach this bar against
# any other note; this is an intentional trade-off favouring precision
# over recall.
RELATED_NOTES_MIN_SHARED_TAGS: int = 2

# Hard cap on the number of related notes rendered per note, regardless
# of how many qualify under RELATED_NOTES_MIN_SHARED_TAGS. Notes are
# sorted by shared-tag count (descending) before truncation, so the cap
# always keeps the strongest matches.
#
# This is the safety net: even if a topic is shared strongly across many
# notes, the rendered section never grows unbounded.
RELATED_NOTES_MAX_RESULTS: int = 10

# ---------------------------------------------------------------------------
# Agent 4 — Image Extraction
# ---------------------------------------------------------------------------

# Images are extracted by default. Disable for a run with --no-images flag.
IMAGE_EXTRACTION_ENABLED: bool = True

# Filter thresholds — images failing any of these are not saved.
IMAGE_MIN_SIZE_BYTES:  int = 10_000      # Skip files smaller than 10KB (tracking pixels)
IMAGE_MAX_SIZE_BYTES:  int = 5_242_880   # Skip files larger than 5MB
IMAGE_MIN_DIMENSION:   int = 100         # Skip images narrower/shorter than 100px

# Enable cross-message image deduplication. When True, an image whose
# *content* (not just URL) has already been saved by a previous run is
# not re-downloaded or re-saved — the existing file is reused and only
# referenced in the new note's manifest.
#
# Newsletters commonly reuse the same header logo, footer banner, sponsor
# graphic, or author headshot across every issue. Without dedup, each of
# these gets re-downloaded and re-saved into a new per-message asset
# folder on every single run, since the existing folder layout is keyed
# by message_id, not by image content. This is the #1 cause of notes/
# folder bloat at scale.
IMAGE_DEDUP_ENABLED: bool = True

# Where the dedup index is stored. A lightweight SQLite table
# (image_dedup_index) tracks: content_hash -> canonical saved path.
# Lives in the same registry.db as everything else — no separate file.
#
# This setting exists for documentation/clarity only; the table name
# itself is fixed in db.py and not meant to be changed at runtime.
IMAGE_DEDUP_TABLE: str = "image_dedup_index"

# ---------------------------------------------------------------------------
# Agent 5 — Research
# ---------------------------------------------------------------------------

# Model used for web-search-powered topic research.
# Sonnet is used here for higher-quality research summaries.
RESEARCH_MODEL: str = "claude-sonnet-4-6"

# Maximum number of new topics researched per pipeline run.
# Prevents runaway API costs if a newsletter introduces many new tags at once.
# 0 disables auto-research entirely — new topics go to the manual research
# queue instead (dashboard/research_queue.html).
MAX_RESEARCH_TOPICS_PER_RUN: int = 0

# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

# Pricing reference (June 2026). Update if Anthropic changes pricing.
# batch_multiplier: Batch API gives 50% discount on input and output tokens.
# cache_read rate is not discounted by the Batch API.
PRICING: dict = {
    "claude-haiku-4-5": {
        "input":             1.00,   # USD per 1M tokens
        "output":            5.00,
        "cache_read":        0.10,
        "batch_multiplier":  0.50,
    },
    "claude-sonnet-4-6": {
        "input":             3.00,
        "output":           15.00,
        "cache_read":        0.30,
        "batch_multiplier":  0.50,
    },
}


def calculate_cost(model: str, usage: dict, batch: bool = True) -> float:
    """
    Calculate API cost for a single completion call.

    Args:
        model:  Model identifier matching a key in PRICING.
        usage:  Dict with keys: input_tokens, output_tokens,
                cache_read_tokens (optional), cache_creation_tokens (optional).
        batch:  Whether the Batch API discount applies.

    Returns:
        Cost in USD as a float.

    Example:
        cost = calculate_cost("claude-haiku-4-5",
                              {"input_tokens": 800, "output_tokens": 200},
                              batch=True)
    """
    p = PRICING.get(model, PRICING["claude-haiku-4-5"])
    m = p["batch_multiplier"] if batch else 1.0
    return (
        (usage.get("input_tokens", 0)          / 1_000_000) * p["input"]      * m +
        (usage.get("output_tokens", 0)         / 1_000_000) * p["output"]     * m +
        (usage.get("cache_read_tokens", 0)     / 1_000_000) * p["cache_read"]
        # cache_creation tokens are billed at standard input rate on first write
    )

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate():
    """
    Run basic configuration checks. Called automatically when this module
    is executed directly: `python config.py`
    Raises SystemExit with a descriptive message if a critical check fails.
    """
    errors = []
    warnings = []

    # API key
    if not ANTHROPIC_API_KEY:
        errors.append(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to your .env file: ANTHROPIC_API_KEY=sk-ant-..."
        )

    # Project root placeholder check
    if "<username>" in str(PROJECT_ROOT):
        errors.append(
            f"PROJECT_ROOT still contains '<username>'. "
            f"Replace it with your actual Windows username in config.py.\n"
            f"  Current value: {PROJECT_ROOT}"
        )

    # Project root existence
    if not PROJECT_ROOT.exists():
        warnings.append(
            f"PROJECT_ROOT does not exist yet: {PROJECT_ROOT}\n"
            f"  Run the folder creation script or create it manually."
        )

    # Secrets directory
    if PROJECT_ROOT.exists() and not SECRETS_DIR.exists():
        warnings.append(
            f"secrets/ directory not found at {SECRETS_DIR}.\n"
            f"  Create it and place your OAuth2 credentials JSON files inside."
        )

    # Gmail credentials files
    for account in GMAIL_ACCOUNTS:
        cred_path = Path(account["credentials_file"])
        if not cred_path.exists():
            warnings.append(
                f"credentials file not found for account '{account['alias']}': {cred_path}\n"
                f"  Download it from Google Cloud Console → APIs & Services → Credentials."
            )

    # Account alias uniqueness
    aliases = [a["alias"] for a in GMAIL_ACCOUNTS]
    if len(aliases) != len(set(aliases)):
        errors.append("Duplicate account aliases detected in GMAIL_ACCOUNTS. Each alias must be unique.")

    # Empty account list — nothing to ingest
    if not GMAIL_ACCOUNTS:
        warnings.append(
            "GMAIL_ACCOUNTS is empty — the email pipeline has nothing to fetch. "
            "The link pipeline (python main.py links) still works without it."
        )

    # Print results
    if errors:
        print("\n[config] ERRORS — pipeline will not run correctly:\n")
        for e in errors:
            print(f"  ✗ {e}\n")
    if warnings:
        print("\n[config] WARNINGS — review before first run:\n")
        for w in warnings:
            print(f"  ⚠ {w}\n")
    if not errors and not warnings:
        print("[config] ✓ Configuration looks good.")
        print(f"  PROJECT_ROOT : {PROJECT_ROOT}")
        print(f"  Accounts     : {[a['alias'] for a in GMAIL_ACCOUNTS]}")
        print(f"  API key      : sk-ant-...{ANTHROPIC_API_KEY[-6:] if ANTHROPIC_API_KEY else 'NOT SET'}")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    validate()
