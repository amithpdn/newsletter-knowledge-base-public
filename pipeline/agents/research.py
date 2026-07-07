# agents/research.py — Newsletter AI Pipeline v4.0
# Agent 5: Research
#
# Responsibilities:
#   - Accept a list of newly-detected topics (tags not yet in the topic index)
#   - For each topic, perform a targeted web search via the Anthropic web_search tool
#   - Produce a concise 3–5 sentence contextual summary
#   - Return summaries and token usage for inclusion in the note and cost log
#
# Model:   Claude Sonnet 4.6 (higher quality reasoning for research synthesis)
# Tools:   web_search_20250305 (Anthropic native web search tool)
# Trigger: Only fires when Agent 3 detects at least one tag absent from topic_index
#          Capped at MAX_RESEARCH_TOPICS_PER_RUN per pipeline run (config.py)
#
# Cost profile:
#   ~8 new topics/month × ~2.5K input + 0.5K output tokens @ Sonnet rates
#   Estimated ~$0.05/month — refer to cost analysis in design doc Section 8
#
# Usage (standalone test):
#   cd pipeline && python agents/research.py
# =============================================================================

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
from config import ANTHROPIC_API_KEY, RESEARCH_MODEL, MAX_RESEARCH_TOPICS_PER_RUN

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a research assistant helping a Business Analyst and product professional \
understand new concepts encountered in their technology and AI newsletters.

When given a topic, search for current, accurate information and produce a \
concise contextual summary. Focus on:
  1. What the concept is (clear, jargon-free definition)
  2. Why it matters in the context of AI, technology, or business
  3. One concrete real-world application or example
  4. Any notable recent development if relevant

Keep the summary to 3–5 sentences. Be specific and substantive — avoid vague generalities. \
Write for someone with a technical background who has not encountered this specific term before."""


# ---------------------------------------------------------------------------
# Single topic research
# ---------------------------------------------------------------------------

def research_topic(topic: str) -> dict:
    """
    Research a single topic using web search and return a contextual summary.

    The web_search tool is passed to the model, which decides when to call it
    based on the topic. For well-known concepts the model may answer from
    knowledge; for recent or niche topics it will issue a search call first.

    Args:
        topic: A short topic label, e.g. "causal ML", "RLHF", "Mamba architecture"

    Returns:
        dict with keys:
          topic    str  — the input topic (echoed back for clarity)
          summary  str  — 3–5 sentence contextual summary
          usage    dict — token counts: {input_tokens, output_tokens,
                          cache_creation_tokens, cache_read_tokens}

    Never raises — returns a safe fallback summary on any API failure so
    one bad topic doesn't abort the note being written.
    """
    print(f"    [research] Researching: '{topic}'")

    try:
        response = _client.messages.create(
            model=RESEARCH_MODEL,
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Provide a concise contextual summary of this topic "
                    f"as it relates to technology, AI, data science, or business:\n\n"
                    f"**{topic}**"
                ),
            }],
        )

        summary = _extract_summary(response)
        usage   = _extract_usage(response)

        print(
            f"    [research] Done: {usage['input_tokens']} in / "
            f"{usage['output_tokens']} out tokens"
        )

        return {
            "topic":   topic,
            "summary": summary,
            "usage":   usage,
        }

    except anthropic.APIError as exc:
        print(f"    [research] API error for '{topic}': {exc}")
        return {
            "topic":   topic,
            "summary": f"Research unavailable for '{topic}' — API error during lookup.",
            "usage":   {"input_tokens": 0, "output_tokens": 0,
                        "cache_creation_tokens": 0, "cache_read_tokens": 0},
            "error":   str(exc),
        }


# ---------------------------------------------------------------------------
# Batch research (called by orchestrator)
# ---------------------------------------------------------------------------

def research_new_topics(new_topics: list[str]) -> dict[str, dict]:
    """
    Research a list of new topics, respecting the per-run cap.

    Called by the orchestrator after Agent 3 identifies topics not yet in
    the topic index. Results are passed to Agent 7 (local_writer) to append
    Context sections to the note.

    Args:
        new_topics: List of topic strings from get_new_topics() in topic_linking.py.
                    May contain 0 items (no research triggered), or many items
                    (capped at MAX_RESEARCH_TOPICS_PER_RUN).

    Returns:
        Dict keyed by topic string, each value a research result dict:
          {topic, summary, usage}
        Empty dict if new_topics is empty.

    The per-run cap (MAX_RESEARCH_TOPICS_PER_RUN) protects against cost
    blowout when a newsletter introduces an unusually large number of
    unfamiliar concepts in a single email.
    """
    if not new_topics:
        return {}

    topics_to_research = new_topics[:MAX_RESEARCH_TOPICS_PER_RUN]

    if len(new_topics) > MAX_RESEARCH_TOPICS_PER_RUN:
        skipped = new_topics[MAX_RESEARCH_TOPICS_PER_RUN:]
        print(
            f"    [research] Cap applied: researching {MAX_RESEARCH_TOPICS_PER_RUN} of "
            f"{len(new_topics)} new topics. Skipped (will be researched next run): "
            f"{', '.join(skipped)}"
        )

    results: dict[str, dict] = {}

    for topic in topics_to_research:
        result         = research_topic(topic)
        results[topic] = result

    return results


# ---------------------------------------------------------------------------
# Usage aggregation helper (used by orchestrator for cost logging)
# ---------------------------------------------------------------------------

def aggregate_usage(research_results: dict[str, dict]) -> dict:
    """
    Sum token usage across all research results in a batch.

    Args:
        research_results: The dict returned by research_new_topics().

    Returns:
        Aggregated usage dict:
          {input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens}
    """
    totals = {
        "input_tokens":          0,
        "output_tokens":         0,
        "cache_creation_tokens": 0,
        "cache_read_tokens":     0,
    }
    for result in research_results.values():
        for key in totals:
            totals[key] += result.get("usage", {}).get(key, 0)
    return totals


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------

def _extract_summary(response) -> str:
    """
    Extract the final text summary from the model response.

    The response may contain a mix of content block types when web search
    is used: tool_use blocks (search calls), tool_result blocks (search
    results), and text blocks (the final synthesised summary).

    We collect all text blocks and join them. The last text block is
    typically the synthesised summary; earlier text blocks (if any) are
    preamble that won't appear in practice for this prompt design.
    """
    text_parts = []

    for block in response.content:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            text = getattr(block, "text", "").strip()
            if text:
                text_parts.append(text)

    summary = " ".join(text_parts).strip()

    if not summary:
        summary = "Research summary unavailable — no text content in response."

    return summary


def _extract_usage(response) -> dict:
    """
    Extract token usage from the API response.
    Handles both standard and prompt-cached token field names safely.
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
    Test the research agent with 2 topics — one well-known, one niche.
    Requires ANTHROPIC_API_KEY in .env and internet access.

    Run: python agents/research.py
    """
    print("=== Research Agent — standalone test ===\n")

    test_topics = [
        "Mamba architecture",        # recent, specific — likely triggers web search
        "retrieval augmented generation",  # well-known — model may answer from knowledge
    ]

    print(f"Testing with {len(test_topics)} topic(s):\n")

    results = research_new_topics(test_topics)

    total_input  = 0
    total_output = 0

    for topic, result in results.items():
        print(f"{'─' * 60}")
        print(f"Topic:   {topic}")
        print(f"Summary: {result['summary']}\n")
        u = result["usage"]
        print(
            f"Tokens:  {u['input_tokens']} in / "
            f"{u['output_tokens']} out / "
            f"{u['cache_read_tokens']} cached"
        )
        total_input  += u["input_tokens"]
        total_output += u["output_tokens"]
        print()

    print(f"{'─' * 60}")
    print(f"Total tokens — input: {total_input}, output: {total_output}")

    # Estimate cost (Sonnet 4.6, no batch discount for standalone test)
    cost = (total_input / 1_000_000) * 3.00 + (total_output / 1_000_000) * 15.00
    print(f"Estimated cost (standard rate): ${cost:.5f}")

    print("\nTest complete.")
