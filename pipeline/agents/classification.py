# agents/classification.py — Newsletter AI Pipeline v4.0
# Agent 1.5: Content Classification
#
# Responsibilities:
#   - Gate emails before they reach the Summarisation Agent
#   - Three-stage pipeline: heuristic → LLM classify → LLM editorial extract
#   - Respect per-account sender allowlist and blocklist from config.py
#   - Return an enriched email dict with classification metadata and an
#     'action' field: 'process' | 'skip'
#
# Stage breakdown:
#   Stage 0 — Allowlist/Blocklist  (free, instant)
#   Stage 1 — Heuristic scoring    (free, regex-based)
#   Stage 2 — LLM classification   (Haiku 4.5, conditional on score 3–7)
#   Stage 3 — Editorial extraction (Haiku 4.5, mixed emails only)
#
# Cost profile:
#   ~80% of emails are resolved at Stage 0 or 1 (no API cost)
#   Only ambiguous emails (heuristic score 3–7) hit the LLM
#   Mixed emails trigger a second LLM call to strip marketing content
#
# Usage (standalone test):
#   cd pipeline && python agents/classification.py
# =============================================================================

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_agents_dir   = Path(__file__).resolve().parent
_pipeline_dir = _agents_dir.parent
for _p in (_pipeline_dir, _agents_dir):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import anthropic
from config import (
    ANTHROPIC_API_KEY,
    CLASSIFIER_CONFIDENCE_THRESHOLD,
    HEURISTIC_SKIP_THRESHOLD,
    HEURISTIC_PASS_THRESHOLD,
)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Stage 1 — Heuristic signal definitions
# ---------------------------------------------------------------------------
# Each entry: (signal_name, regex_pattern_or_None, weight, target_field)
# target_field: 'subject' | 'body' | 'headers' | 'links'
# Signals without a regex pattern are computed programmatically below.

_HEURISTIC_SIGNALS: list[tuple[str, str | None, int, str]] = [
    # Subject line patterns — high signal weight
    (
        "subject_sales_language",
        r"(?i)\b(sale|% off|\$\d+\s*off|discount|promo|coupon|voucher|deal|"
        r"offer|free shipping|limited time|act now|hurry|expires|last chance|"
        r"flash sale|clearance|saving|win|giveaway|prize)\b",
        3,
        "subject",
    ),
    (
        "subject_urgency",
        r"(?i)\b(urgent|don't miss|ends (today|tonight|soon)|only \d+ (left|remaining)|"
        r"selling fast|almost gone|now or never)\b",
        2,
        "subject",
    ),
    # Body patterns
    (
        "unsubscribe_footer",
        r"(?i)(unsubscribe|opt.?out|manage.*preferences|email preferences|"
        r"update.*subscription|remove.*from.*list)",
        2,
        "body",
    ),
    (
        "cta_buttons",
        r"(?i)\b(buy now|shop now|order now|get started|sign up today|"
        r"claim (your|my|this)|grab (your|this)|download now|try (it )?free|"
        r"start (your )?free trial)\b",
        2,
        "body",
    ),
    (
        "promotional_language",
        r"(?i)\b(exclusive (offer|deal|discount)|members? only|"
        r"loyalty (reward|point|discount)|referral (code|bonus)|"
        r"affiliate|sponsored|advertisement|paid partnership)\b",
        2,
        "body",
    ),
    (
        "price_patterns",
        r"(?i)(\$\d+\.?\d*\s*(off|discount|saving)|use code\s+\w+|promo code|"
        r"coupon code|discount code)",
        2,
        "body",
    ),
    # Header-based signals
    (
        "bulk_mail_headers",
        r"(?i)(list-unsubscribe|x-campaign|x-mailer|x-bulk|"
        r"precedence:\s*(bulk|list)|x-mc-|x-mailchimp|x-sendgrid)",
        1,
        "headers",
    ),
]

# ---------------------------------------------------------------------------
# Stage 1 — Heuristic scoring
# ---------------------------------------------------------------------------

def heuristic_score(email: dict) -> tuple[int, list[str]]:
    """
    Score an email against marketing signal patterns.

    Returns:
        (score, signals) where:
          score   — integer; higher = more likely marketing
          signals — list of signal names that fired

    Scoring interpretation (configurable via config.py):
        score >= HEURISTIC_SKIP_THRESHOLD (8) → skip immediately
        score <  HEURISTIC_PASS_THRESHOLD (3) → pass directly to summarisation
        between the two                       → escalate to LLM (Stage 2)
    """
    subject = email.get("subject", "")
    body    = email.get("body_text", "") or email.get("body_html", "")
    # Reconstruct a minimal header string from available email metadata
    # Full raw headers aren't available via the Gmail API fields we use,
    # so we use the sender field as a proxy for header-based signals.
    headers = email.get("sender", "")

    score   = 0
    signals = []

    for signal_name, pattern, weight, target in _HEURISTIC_SIGNALS:
        if pattern is None:
            continue
        target_text = {"subject": subject, "body": body, "headers": headers}.get(
            target, body
        )
        if re.search(pattern, target_text):
            score += weight
            signals.append(signal_name)

    # Programmatic signal: high link density in HTML body
    # More than 1 link per 20 words is characteristic of promotional emails
    html_body = email.get("body_html", "")
    if html_body:
        link_count = len(re.findall(r"https?://", html_body))
        word_count = max(len(re.sub(r"<[^>]+>", " ", html_body).split()), 1)
        density    = link_count / (word_count / 20)
        if density > 3:
            score += 2
            signals.append("high_link_density")

    # Programmatic signal: image-heavy with very little text (image-only promo)
    if html_body:
        img_count  = len(re.findall(r"<img\b", html_body, re.IGNORECASE))
        text_only  = re.sub(r"<[^>]+>", " ", html_body)
        text_words = len(text_only.split())
        if img_count > 3 and text_words < 50:
            score += 1
            signals.append("image_heavy_low_text")

    return score, signals


# ---------------------------------------------------------------------------
# Stage 2 — LLM classification
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = """\
You are an email content classifier specialising in newsletter analysis.

Classify the email as exactly one of:
  editorial  — Primarily informational, educational, analytical, or opinion content.
               The reader gains knowledge or insight. Minimal or no promotional content.
  marketing  — Primarily promotional. Pushes a product, service, sale, or event.
               Editorial value is negligible relative to the promotional content.
  mixed      — Contains meaningful editorial sections AND meaningful promotional sections.
               Extracting just the editorial sections would leave valuable content.

Respond ONLY with a JSON object. No preamble, no markdown, no explanation.
Schema: {"classification": "editorial|marketing|mixed", "confidence": 0.0}
confidence must be a float between 0.0 and 1.0."""


def llm_classify(email: dict) -> tuple[str, float, dict]:
    """
    Call Haiku 4.5 to classify an email as editorial, marketing, or mixed.

    The body is truncated to 3000 characters to keep token costs low.
    Subject and sender are included as they are strong classification signals.

    Returns:
        (classification, confidence, usage_dict)
        classification: "editorial" | "marketing" | "mixed"
        confidence:     float 0.0–1.0
        usage_dict:     token counts for cost logging
    """
    # Prefer plain text for classification — less noise than HTML
    body_snippet = (
        email.get("body_text") or
        re.sub(r"<[^>]+>", " ", email.get("body_html", ""))
    )[:3_000].strip()

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=100,
        system=_CLASSIFY_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Subject: {email.get('subject', '')}\n"
                f"From: {email.get('sender', '')}\n\n"
                f"{body_snippet}"
            ),
        }],
    )

    usage = {
        "input_tokens":          response.usage.input_tokens,
        "output_tokens":         response.usage.output_tokens,
        "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read_tokens":     getattr(response.usage, "cache_read_input_tokens", 0),
    }

    raw = response.content[0].text.strip()
    try:
        result         = json.loads(raw)
        classification = result.get("classification", "marketing").lower()
        confidence     = float(result.get("confidence", 0.5))

        # Normalise unexpected values
        if classification not in ("editorial", "marketing", "mixed"):
            classification = "marketing"
            confidence     = 0.5

    except (json.JSONDecodeError, ValueError, KeyError):
        # If the model returns garbage, treat as marketing with low confidence
        # so the email goes to "AI Review" rather than being silently skipped
        classification = "marketing"
        confidence     = 0.4

    return classification, confidence, usage


# ---------------------------------------------------------------------------
# Stage 3 — Editorial extraction (mixed emails)
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """\
You are an editorial content extractor for newsletter emails.

The email you receive contains both editorial/informational content and promotional/marketing content.

Your task: extract and return ONLY the editorial, informational, educational, or analytical sections.

Remove completely:
  - Promotional offers, discounts, sales announcements
  - "Buy now" / "Shop now" / "Sign up" calls to action
  - Coupon codes, referral codes, affiliate links
  - Event ticket sales or registration pitches
  - Footer boilerplate (unsubscribe links, legal disclaimers, address blocks)
  - Social media follow prompts

Preserve:
  - News summaries, analysis, commentary
  - How-to guides, tutorials, explainers
  - Research findings, data insights
  - Industry trends, opinion pieces
  - Tool and resource recommendations (but not sales pitches for them)
  - People, paper, and project mentions

Return only the extracted text. No preamble, no commentary, no markdown formatting."""


def extract_editorial(email: dict) -> tuple[str, dict]:
    """
    For mixed emails, strip marketing sections and return clean editorial text.

    The cleaned body replaces email['body_text'] before passing to Agent 2,
    so the Summarisation Agent only sees editorial content.

    Returns:
        (clean_body_text, usage_dict)
    """
    # Use up to 6000 chars — mixed emails are often longer
    body = (
        email.get("body_text") or
        re.sub(r"<[^>]+>", " ", email.get("body_html", ""))
    )[:6_000].strip()

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4_000,
        system=_EXTRACT_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Newsletter: {email.get('subject', '')}\n"
                f"From: {email.get('sender', '')}\n\n"
                f"{body}"
            ),
        }],
    )

    usage = {
        "input_tokens":          response.usage.input_tokens,
        "output_tokens":         response.usage.output_tokens,
        "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read_tokens":     getattr(response.usage, "cache_read_input_tokens", 0),
    }

    clean_body = response.content[0].text.strip()
    return clean_body, usage


# ---------------------------------------------------------------------------
# Public interface — full classification pipeline
# ---------------------------------------------------------------------------

def classify(email: dict, account: dict) -> dict:
    """
    Run the full three-stage classification pipeline for a single email.

    Stages are executed in order and short-circuit as early as possible to
    minimise API calls and cost.

    Args:
        email:   Email dict from Agent 1 (ingestion).
        account: The matching account entry from GMAIL_ACCOUNTS (for
                 allowlist/blocklist lookup).

    Returns:
        Enriched email dict with added keys:
          classification        str   — "editorial"|"marketing"|"mixed"|
                                        "blocked"|"allowlisted"
          classification_stage  str   — which stage resolved the email
          confidence_score      float — LLM confidence (None for non-LLM stages)
          heuristic_score       int   — raw heuristic score (None if not reached)
          heuristic_signals     str   — JSON list of fired signal names
          marketing_sections    str   — "extracted" if Stage 3 ran, else None
          action                str   — "process" | "skip"
          _classification_usage dict  — token usage for cost logging (internal)

        If Stage 3 ran, body_text is replaced with the extracted editorial content.
    """
    sender       = email.get("sender", "").lower()
    total_usage: dict = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
    }

    # ------------------------------------------------------------------
    # Stage 0a — Sender allowlist (bypass classifier entirely)
    # ------------------------------------------------------------------
    for allowed in account.get("sender_allowlist", []):
        if allowed.lower() in sender:
            print(f"    [classify] Allowlisted sender → process")
            return {
                **email,
                "classification":        "editorial",
                "classification_stage":  "allowlist",
                "confidence_score":      1.0,
                "heuristic_score":       None,
                "heuristic_signals":     None,
                "marketing_sections":    None,
                "action":                "process",
                "_classification_usage": total_usage,
            }

    # ------------------------------------------------------------------
    # Stage 0b — Sender blocklist (skip without LLM)
    # ------------------------------------------------------------------
    for blocked in account.get("sender_blocklist", []):
        if blocked.lower() in sender:
            print(f"    [classify] Blocklisted sender → skip")
            return {
                **email,
                "classification":        "blocked",
                "classification_stage":  "blocklist",
                "confidence_score":      1.0,
                "heuristic_score":       None,
                "heuristic_signals":     None,
                "marketing_sections":    None,
                "action":                "skip",
                "_classification_usage": total_usage,
            }

    # ------------------------------------------------------------------
    # Stage 1 — Heuristic scoring
    # ------------------------------------------------------------------
    score, signals = heuristic_score(email)
    signals_json   = json.dumps(signals)

    print(
        f"    [classify] Heuristic score: {score} "
        f"(signals: {', '.join(signals) or 'none'})"
    )

    if score >= HEURISTIC_SKIP_THRESHOLD:
        print(f"    [classify] Score ≥ {HEURISTIC_SKIP_THRESHOLD} → skip (marketing)")
        return {
            **email,
            "classification":        "marketing",
            "classification_stage":  "heuristic",
            "confidence_score":      1.0,
            "heuristic_score":       score,
            "heuristic_signals":     signals_json,
            "marketing_sections":    None,
            "action":                "skip",
            "_classification_usage": total_usage,
        }

    if score < HEURISTIC_PASS_THRESHOLD:
        print(f"    [classify] Score < {HEURISTIC_PASS_THRESHOLD} → process (editorial)")
        return {
            **email,
            "classification":        "editorial",
            "classification_stage":  "heuristic",
            "confidence_score":      1.0,
            "heuristic_score":       score,
            "heuristic_signals":     signals_json,
            "marketing_sections":    None,
            "action":                "process",
            "_classification_usage": total_usage,
        }

    # ------------------------------------------------------------------
    # Stage 2 — LLM classification (score is in the ambiguous 3–7 range)
    # ------------------------------------------------------------------
    print(f"    [classify] Ambiguous score ({score}) → LLM classification...")
    classification, confidence, usage = llm_classify(email)
    _accumulate_usage(total_usage, usage)

    print(
        f"    [classify] LLM result: {classification} "
        f"(confidence: {confidence:.2f})"
    )

    # Low confidence → send to review regardless of classification
    if confidence < CLASSIFIER_CONFIDENCE_THRESHOLD:
        print(
            f"    [classify] Confidence {confidence:.2f} < threshold "
            f"{CLASSIFIER_CONFIDENCE_THRESHOLD} → skip (AI Review)"
        )
        return {
            **email,
            "classification":        classification,
            "classification_stage":  "llm",
            "confidence_score":      confidence,
            "heuristic_score":       score,
            "heuristic_signals":     signals_json,
            "marketing_sections":    None,
            "action":                "skip",
            "_classification_usage": total_usage,
        }

    if classification == "marketing":
        print(f"    [classify] LLM: marketing → skip")
        return {
            **email,
            "classification":        "marketing",
            "classification_stage":  "llm",
            "confidence_score":      confidence,
            "heuristic_score":       score,
            "heuristic_signals":     signals_json,
            "marketing_sections":    None,
            "action":                "skip",
            "_classification_usage": total_usage,
        }

    if classification == "editorial":
        print(f"    [classify] LLM: editorial → process")
        return {
            **email,
            "classification":        "editorial",
            "classification_stage":  "llm",
            "confidence_score":      confidence,
            "heuristic_score":       score,
            "heuristic_signals":     signals_json,
            "marketing_sections":    None,
            "action":                "process",
            "_classification_usage": total_usage,
        }

    # ------------------------------------------------------------------
    # Stage 3 — Editorial extraction (classification == "mixed")
    # ------------------------------------------------------------------
    print(f"    [classify] LLM: mixed → extracting editorial content...")
    clean_body, extract_usage = extract_editorial(email)
    _accumulate_usage(total_usage, extract_usage)

    print(
        f"    [classify] Extraction complete "
        f"({len(clean_body)} chars of editorial content retained)"
    )

    return {
        **email,
        "body_text":              clean_body,   # replace body with clean version
        "classification":         "mixed",
        "classification_stage":   "llm_extracted",
        "confidence_score":       confidence,
        "heuristic_score":        score,
        "heuristic_signals":      signals_json,
        "marketing_sections":     "extracted",
        "action":                 "process",
        "_classification_usage":  total_usage,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _accumulate_usage(total: dict, addition: dict):
    """Add token counts from addition into total in-place."""
    for key in ("input_tokens", "output_tokens",
                "cache_creation_tokens", "cache_read_tokens"):
        total[key] = total.get(key, 0) + addition.get(key, 0)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Smoke test with synthetic email samples.
    Does not require a live Gmail connection — tests the classification logic only.
    Run: python agents/classification.py
    """
    from config import GMAIL_ACCOUNTS

    print("=== Classification Agent — standalone test ===\n")

    test_account = GMAIL_ACCOUNTS[0] if GMAIL_ACCOUNTS else {
        "alias": "test", "sender_allowlist": [], "sender_blocklist": []
    }

    test_emails = [
        {
            "message_id":    "<test-editorial-001>",
            "account_alias": test_account["alias"],
            "sender":        "author@example-newsletter.com",
            "subject":       "How LLMs Are Changing Product Management",
            "body_text": (
                "This week we explore how large language models are being embedded "
                "into product workflows at companies like Notion and Linear. "
                "The trend is accelerating: 60% of PMs surveyed now use AI daily. "
                "Key insight: teams that combine AI tools with structured frameworks "
                "see 2x faster feature definition cycles."
            ),
            "body_html": "",
        },
        {
            "message_id":    "<test-marketing-001>",
            "account_alias": test_account["alias"],
            "sender":        "deals@someshop.com",
            "subject":       "🔥 FLASH SALE — 50% OFF today only! Shop now",
            "body_text": (
                "Don't miss our biggest sale of the year! "
                "Use code SAVE50 for 50% off everything. "
                "Offer expires tonight. Buy now before it's gone! "
                "Click here to shop. Unsubscribe from promotional emails."
            ),
            "body_html": "",
        },
        {
            "message_id":    "<test-mixed-001>",
            "account_alias": test_account["alias"],
            "sender":        "weekly@techdige.st",
            "subject":       "AI Weekly: GPT updates + our new course is live",
            "body_text": (
                "This week in AI: OpenAI released new fine-tuning capabilities "
                "allowing cheaper model customisation for enterprise use cases. "
                "Researchers at Stanford published findings on emergent reasoning "
                "in smaller models under 7B parameters. "
                "\n\n--- SPONSOR ---\n"
                "Enrol in our AI for PMs course — 30% off this week only! "
                "Use code AIPM30 at checkout. Limited spots available."
            ),
            "body_html": "",
        },
    ]

    for email in test_emails:
        print(f"{'─' * 60}")
        print(f"Subject : {email['subject'][:55]}")
        print(f"Sender  : {email['sender']}")
        result = classify(email, test_account)
        print(
            f"Result  : {result['classification']} "
            f"| stage={result['classification_stage']} "
            f"| action={result['action']}"
        )
        if result.get("confidence_score") is not None:
            print(f"          confidence={result['confidence_score']:.2f}")
        print()

    print("Test complete.")
