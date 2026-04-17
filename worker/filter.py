"""LLM relevance scoring using Claude Haiku."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Iterator

import anthropic

from worker.fetch import Paper

MODEL = "claude-haiku-4-5"
BATCH_SIZE = 10

SYSTEM_PROMPT = """\
You are a scientific paper relevance assistant. Your job is to evaluate whether a list of papers is relevant to a researcher's interests.

Output ONLY a JSON array — no markdown, no explanation, no preamble. Each element corresponds to one input paper in the same order.

Schema for each element:
{
  "score": <integer 0-10>,
  "reason": "<one sentence max, explaining why this is or isn't relevant>"
}

Scoring guide:
10 — directly addresses the researcher's core topic
7-9 — closely related, likely interesting
4-6 — tangentially related, might be worth skimming
1-3 — only loosely connected, probably not worth reading
0   — unrelated

Be strict. The researcher's time is valuable. Score 0-3 liberally for papers that merely share vocabulary but not substance.\
"""

USER_PROMPT_TEMPLATE = """\
Researcher interests: {keyword_profile}

Papers to evaluate:
{papers_block}

Return a JSON array with {n} elements, one per paper, in the same order.\
"""


@dataclass
class ScoredPaper:
    paper: Paper
    llm_score: float
    llm_reason: str


def _chunked(lst: list, n: int) -> Iterator[list]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _format_papers_block(papers: list[Paper]) -> str:
    lines = []
    for i, p in enumerate(papers, 1):
        authors = ", ".join(p.authors[:3])
        if len(p.authors) > 3:
            authors += " et al."
        abstract = p.abstract[:400]
        if len(p.abstract) > 400:
            abstract += "..."
        lines.append(f"[{i}] {p.title}\nAuthors: {authors}\nAbstract: {abstract}\n")
    return "\n".join(lines)


def _score_batch(
    batch: list[Paper],
    profile: str,
    client: anthropic.Anthropic,
    retries: int = 3,
) -> list[dict]:
    prompt = USER_PROMPT_TEMPLATE.format(
        keyword_profile=profile,
        papers_block=_format_papers_block(batch),
        n=len(batch),
    )

    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=1500,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            # Strip accidental markdown fences
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
            text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
            scores = json.loads(text)
            if len(scores) != len(batch):
                print(
                    f"[filter] Warning: expected {len(batch)} scores, got {len(scores)}. "
                    "Skipping batch."
                )
                return []
            return scores
        except (json.JSONDecodeError, anthropic.APIError) as exc:
            wait = 2 ** attempt
            print(f"[filter] Attempt {attempt + 1}/{retries} failed: {exc}. Retrying in {wait}s…")
            time.sleep(wait)

    print("[filter] All retries exhausted for batch. Skipping.")
    return []


def score_papers(
    papers: list[Paper],
    profile: str,
    min_score: float = 6.0,
) -> list[ScoredPaper]:
    """Score papers against a user profile, return those above min_score."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    results: list[ScoredPaper] = []

    for batch in _chunked(papers, BATCH_SIZE):
        scores = _score_batch(batch, profile, client)
        if not scores:
            continue
        for paper, s in zip(batch, scores):
            score_val = float(s.get("score", 0))
            reason = s.get("reason", "")
            if score_val >= min_score:
                results.append(ScoredPaper(paper=paper, llm_score=score_val, llm_reason=reason))

    print(
        f"[filter] Scored {len(papers)} papers; {len(results)} passed "
        f"min_score={min_score}"
    )
    return results
