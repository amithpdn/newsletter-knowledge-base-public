# agents/summarisation.py — Newsletter AI Pipeline v4.0
# Agent 2: Summarisation
#
# Responsibilities:
#   - Process each email body through Claude Haiku 4.5
#   - Extract: 3-sentence summary, 5–8 topic tags, key takeaways,
#     and mentions of tools / papers / people
#   - Return structured data (dict) plus token usage for cost logging
#   - Handle malformed or partial LLM responses gracefully
#
# Model:    Claude Haiku 4.5 (cost-optimised)
# API mode: Standard messages API (Batch API can be layered in later
#           for the scheduled nightly run; the interface is identical)
# Caching:  System prompt is eligible for prompt caching — the fixed
#           system prompt is written once and served from cache on
#           repeated runs, cutting input token costs by up to 90%
#
# Changes from v4.0:
#   - _SYSTEM_PROMPT: tags now explicitly hyphenated (no spaces), with
#     updated examples. summary and key_takeaways fields now explicitly
#     forbid [[wikilinks]] and bracket syntax in prose — those are added
#     by Agent 7's post-processing step, not the LLM.
#   - _normalise(): added _sanitise_tag() call as a safety net for any
#     tag that still contains spaces despite the prompt instruction.
#
# Usage (standalone test):
#   cd pipeline && python agents/summarisation.py
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
from config import ANTHROPIC_API_KEY, SUMMARISATION_MODEL, SUMMARISATION_BODY_LIMIT

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
# Written as a single fixed string to enable prompt caching.
# When this exact text is sent in subsequent API calls, Anthropic serves it
# from cache at ~10% of the normal input token cost.
#
# The prompt is intentionally strict about JSON output format to make
# response parsing reliable without regex fallbacks.
#
# Key constraints added vs original:
#   1. Tags must use hyphens between words, never spaces — required for
#      valid Obsidian tag syntax (spaces break tag parsing in Obsidian).
#   2. summary and key_takeaways must be plain prose — no [[wikilinks]],
#      no Markdown bracket syntax. Wikilinks are injected by Agent 7
#      (local_writer.py) as a post-processing step; if the LLM adds them
#      too, they fragment multi-word phrases and produce invalid links.

_SYSTEM_PROMPT = """\
You are a knowledge extraction assistant specialising in technology, AI, data science, \
and business analysis newsletters.

Given a newsletter email, extract structured information and return ONLY a valid JSON object. \
No preamble, no explanation, no markdown code fences — raw JSON only.

Required JSON schema:
{
  "summary": "string — exactly 3 sentences capturing the core message and why it matters",
  "tags": ["string", ...],
  "key_takeaways": ["string", ...],
  "mentions": {
    "tools":  ["string", ...],
    "papers": ["string", ...],
    "people": ["string", ...]
  }
}

Field rules:
  summary        — 3 sentences. First: what the newsletter is about. \
Second: the main insight or finding. Third: why it matters or what to do with it. \
Plain prose only — do NOT add [[wikilinks]], Markdown links, or any bracket syntax. \
Wikilinks are added by a separate post-processing step.
  tags           — 5 to 8 short topic labels (2–4 words each, all lowercase, \
words separated by hyphens — no spaces). \
Cover the main themes, technologies, and domains discussed. \
Examples: "rag-pipelines", "product-management", "llm-fine-tuning", "causal-inference", \
"agent-architecture", "open-source-llms"
  key_takeaways  — 3 to 6 concise bullet-point strings. Each starts with a verb or noun. \
Practical, specific, and actionable where possible. \
Plain prose only — do NOT add [[wikilinks]], Markdown links, or any bracket syntax.
  tools          — Software tools, libraries, frameworks, platforms mentioned by name. \
Empty array if none.
  papers         — Academic papers, research reports, or studies mentioned by title or nickname. \
Empty array if none.
  people         — Named individuals mentioned (researchers, executives, authors). \
Empty array if none.

If a field has no relevant content, use an empty array [] or an empty string "".
Never invent content not present in the email."""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def summarise(email: dict) -> dict:
    """
    Summarise a single newsletter email and return structured note data.

    Args:
        email: Email dict from Agent 1 (or Agent 1.5 if classification ran).
               Relevant keys: subject, sender, account_alias, body_text, body_html.

    Returns:
        dict with keys:
          structured  — parsed dict matching the JSON schema above
          usage       — token usage dict for cost logging:
                        {input_tokens, output_tokens,
                         cache_creation_tokens, cache_read_tokens}

    Never raises — returns a safe fallback structured dict on any failure,
    so a single bad email doesn't halt the pipeline run.
    """
    body = _prepare_body(email)

    try:
        response = _client.messages.create(
            model=SUMMARISATION_MODEL,
            max_tokens=1_000,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": _build_user_message(email, body),
            }],
        )

        usage      = _extract_usage(response)
        structured = _parse_response(response.content[0].text, email)

    except anthropic.APIError as exc:
        print(f"    [summarise] API error for '{email.get('subject', '?')}': {exc}")
        structured = _fallback_structured(email)
        usage      = {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_tokens": 0, "cache_read_tokens": 0}

    return {"structured": structured, "usage": usage}


# ---------------------------------------------------------------------------
# Body preparation
# ---------------------------------------------------------------------------

def _prepare_body(email: dict) -> str:
    """
    Select and clean the best available body text for summarisation.

    Preference order:
      1. body_text (plain text) — cleanest signal, no HTML noise
      2. body_html stripped of tags — fallback if plain text is absent

    Truncates to SUMMARISATION_BODY_LIMIT to stay within token budget.
    Leading/trailing whitespace and excessive blank lines are normalised.
    """
    raw = email.get("body_text", "").strip()

    if not raw:
        html = email.get("body_html", "")
        # Strip HTML tags and collapse whitespace
        raw = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = re.sub(r"[ \t]{2,}", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = raw.strip()

    # Truncate to configured limit
    if len(raw) > SUMMARISATION_BODY_LIMIT:
        raw = raw[:SUMMARISATION_BODY_LIMIT]
        # Don't cut mid-word
        last_space = raw.rfind(" ")
        if last_space > SUMMARISATION_BODY_LIMIT - 200:
            raw = raw[:last_space]
        raw += "\n\n[... truncated ...]"

    return raw


def _build_user_message(email: dict, body: str) -> str:
    """
    Construct the user-turn message sent to the model.
    Including subject and sender helps the model orient quickly
    and produces more accurate tags.
    """
    return (
        f"Newsletter: {email.get('subject', '(no subject)')}\n"
        f"From: {email.get('sender', '(unknown sender)')}\n"
        f"Account: {email.get('account_alias', 'unknown')}\n\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw_text: str, email: dict) -> dict:
    """
    Parse the model's raw text response into a structured dict.

    Handles three cases:
      1. Clean JSON — direct parse
      2. JSON wrapped in markdown code fences — strip fences, then parse
      3. Anything else — extract with lenient regex, fall back to safe defaults

    Args:
        raw_text: The model's response string.
        email:    Original email dict (used for fallback subject extraction).

    Returns:
        Validated and normalised structured dict.
    """
    text = raw_text.strip()

    # Case 1: Direct JSON parse
    try:
        data = json.loads(text)
        return _normalise(data)
    except json.JSONDecodeError:
        pass

    # Case 2: Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$",          "", cleaned, flags=re.MULTILINE)
    try:
        data = json.loads(cleaned.strip())
        return _normalise(data)
    except json.JSONDecodeError:
        pass

    # Case 3: Attempt to extract first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return _normalise(data)
        except json.JSONDecodeError:
            pass

    # All parsing failed — return a safe fallback so the pipeline continues
    print(
        f"    [summarise] WARNING: Could not parse LLM response for "
        f"'{email.get('subject', '?')}' — using fallback structure"
    )
    return _fallback_structured(email)


def _sanitise_tag(tag: str) -> str:
    """
    Convert a raw LLM-generated tag string into a valid Obsidian tag.

    Obsidian tags may only contain letters, numbers, underscores, and
    hyphens — no spaces, no punctuation. This function is a safety net
    for any tag that still contains spaces despite the prompt instruction
    explicitly requesting hyphens. It runs on every tag unconditionally.

    Steps:
      1. Lowercase and strip surrounding whitespace
      2. Replace any run of whitespace with a single hyphen
      3. Strip characters that are not alphanumeric, hyphen, or underscore
      4. Collapse multiple consecutive hyphens into one
      5. Strip leading/trailing hyphens

    Examples:
        "rag pipelines"      -> "rag-pipelines"
        "LLM Fine-Tuning"    -> "llm-fine-tuning"
        "RAG / vector search" -> "rag-vector-search"
        "open-source-llms"   -> "open-source-llms"  (already valid, unchanged)

    Returns:
        Sanitised tag string, or empty string if nothing valid remains
        (caller filters empties out).
    """
    tag = tag.lower().strip()
    tag = re.sub(r"\s+", "-", tag)           # spaces -> hyphen
    tag = re.sub(r"[^a-z0-9_-]", "", tag)    # strip invalid characters
    tag = re.sub(r"-{2,}", "-", tag)          # collapse multiple hyphens
    tag = tag.strip("-")                       # trim leading/trailing hyphens
    return tag


def _normalise(data: dict) -> dict:
    """
    Ensure all expected keys are present and values are the right types.
    Coerces wrong types rather than raising, so partial responses are usable.

    Tag sanitisation runs here as a safety net — even if the LLM ignores
    the hyphen instruction in the prompt, _sanitise_tag() converts any
    space-containing tag to a valid Obsidian-compatible hyphenated string.
    De-duplication is also applied post-sanitisation, since two originally
    distinct tags can collide after sanitisation (e.g. "RAG pipelines" and
    "rag-pipelines" both become "rag-pipelines").
    """
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        summary = str(summary)

    tags = data.get("tags", [])
    if not isinstance(tags, list):
        tags = [str(tags)] if tags else []

    # Step 1: lowercase + strip
    tags = [t.lower().strip() for t in tags if isinstance(t, str) and t.strip()]
    # Step 2: sanitise to valid Obsidian tag syntax (safety net for spaces/punctuation)
    tags = [_sanitise_tag(t) for t in tags]
    # Step 3: drop any tag that became empty after sanitisation
    tags = [t for t in tags if t]
    # Step 4: de-duplicate while preserving order
    seen       = set()
    deduped    = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    tags = deduped

    takeaways = data.get("key_takeaways", [])
    if not isinstance(takeaways, list):
        takeaways = [str(takeaways)] if takeaways else []
    takeaways = [t.strip() for t in takeaways if isinstance(t, str) and t.strip()]

    raw_mentions = data.get("mentions", {})
    if not isinstance(raw_mentions, dict):
        raw_mentions = {}

    def _clean_list(val) -> list[str]:
        if isinstance(val, list):
            return [str(v).strip() for v in val if str(v).strip()]
        if isinstance(val, str) and val.strip():
            return [val.strip()]
        return []

    mentions = {
        "tools":  _clean_list(raw_mentions.get("tools",  [])),
        "papers": _clean_list(raw_mentions.get("papers", [])),
        "people": _clean_list(raw_mentions.get("people", [])),
    }

    return {
        "summary":       summary,
        "tags":          tags,
        "key_takeaways": takeaways,
        "mentions":      mentions,
    }


def _fallback_structured(email: dict) -> dict:
    """
    Return a minimal valid structure when parsing completely fails.
    Ensures Agent 7 (file writer) always has something to write.
    """
    return {
        "summary":       (
            f"Newsletter from {email.get('sender', 'unknown sender')}. "
            f"Automated extraction failed — review original email. "
            f"Subject: {email.get('subject', '(no subject)')}."
        ),
        "tags":          [],
        "key_takeaways": ["[Extraction failed — see original email]"],
        "mentions":      {"tools": [], "papers": [], "people": []},
    }


# ---------------------------------------------------------------------------
# Token usage extraction
# ---------------------------------------------------------------------------

def _extract_usage(response) -> dict:
    """
    Extract token usage from an Anthropic API response object.
    Handles both standard and cached token fields safely.
    """
    usage = response.usage
    return {
        "input_tokens":          getattr(usage, "input_tokens",                0),
        "output_tokens":         getattr(usage, "output_tokens",               0),
        "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        "cache_read_tokens":     getattr(usage, "cache_read_input_tokens",     0),
    }


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Test with a synthetic newsletter email. Does not require Gmail.
    Run: python agents/summarisation.py
    """
    print("=== Summarisation Agent — standalone test ===\n")

    sample_email = {
        "message_id":    "<test-summ-001@example.com>",
        "account_alias": "personal",
        "sender":        "lenny@lennysnewsletter.com",
        "subject":       "How the best PMs use AI in their workflow",
        "body_text": """\
This week I spoke with 12 senior PMs at companies including Notion, Linear, and Figma \
about how they've actually changed their day-to-day workflows with AI tools.

The most common shift: they're using LLMs not to write PRDs from scratch, but as a \
"thinking partner" to pressure-test assumptions before writing. One PM at Figma told me \
she pastes her problem statement into Claude and asks it to steelman the three strongest \
objections before she writes a single spec line.

Key tools mentioned: Claude, ChatGPT, Cursor, Notion AI, and Linear's new AI triage.

Research worth reading: "LLMs as Thought Partners" (Stanford HAI, May 2026) found that \
PMs who used AI for pre-writing reasoning produced specs with 40% fewer revision cycles.

People: Lenny Rachitsky, Shreyas Doshi (featured in sidebar), Adam Fishman.

Three things I took away:
1. The best PMs use AI before writing, not during.
2. Structured prompting (persona + constraint + format) beats open-ended prompting.
3. AI-assisted user interview synthesis is the sleeper use case — faster than Dovetail.
        """,
        "body_html": "",
    }

    print(f"Subject : {sample_email['subject']}")
    print(f"Sender  : {sample_email['sender']}\n")

    result = summarise(sample_email)

    s = result["structured"]
    u = result["usage"]

    print(f"{'─' * 60}")
    print(f"Summary:\n  {s['summary']}\n")
    print(f"Tags:  {', '.join(s['tags'])}\n")
    print(f"Takeaways:")
    for t in s["key_takeaways"]:
        print(f"  - {t}")
    print(f"\nMentions:")
    print(f"  Tools:  {', '.join(s['mentions']['tools'])  or '—'}")
    print(f"  Papers: {', '.join(s['mentions']['papers']) or '—'}")
    print(f"  People: {', '.join(s['mentions']['people']) or '—'}")
    print(f"\nToken usage:")
    print(f"  Input:          {u['input_tokens']}")
    print(f"  Output:         {u['output_tokens']}")
    print(f"  Cache creation: {u['cache_creation_tokens']}")
    print(f"  Cache read:     {u['cache_read_tokens']}")
    print(f"{'─' * 60}")
    print("\nTest complete.")
