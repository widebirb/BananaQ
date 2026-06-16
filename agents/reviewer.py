"""
PR Reviewer Agent — pure function.

Takes a unified diff string, calls the LLM, and returns a list of
structured review comments. No side effects; no GitHub API calls here.

Input:  diff (str)
Output: list[ReviewComment]
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from config.settings import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class ReviewComment(BaseModel):
    """A single line-level review comment."""

    path: str = Field(description="File path relative to repo root, e.g. 'src/main.py'")
    line: int = Field(description="Line number in the diff (1-indexed, on the new/right side)")
    body: str = Field(description="The review comment text")


class ReviewOutput(BaseModel):
    """Structured output from the reviewer LLM."""

    comments: list[ReviewComment] = Field(
        description="List of review comments. Empty list if no issues found."
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert code reviewer. You will be given a unified diff of a pull request.
Your job is to identify real issues: bugs, security flaws, logic errors, missing error handling,
performance problems, and violations of best practices.

Rules:
- Only comment on lines that are ADDED (prefixed with +) in the diff.
- Be specific and actionable. Explain WHY it is an issue and HOW to fix it.
- Skip cosmetic/style issues unless they are likely to cause bugs.
- Do not invent issues. If the code looks fine, return an empty comments list.
- Keep each comment concise (2-4 sentences max).

You MUST respond with valid JSON matching this exact schema:
{
  "comments": [
    {
      "path": "<file path>",
      "line": <line number on new side>,
      "body": "<your review comment>"
    }
  ]
}

If there are no issues, respond with: {"comments": []}
"""


# ---------------------------------------------------------------------------
# Agent function
# ---------------------------------------------------------------------------

async def review_diff(
    diff: str,
    settings: Settings,
    max_comments: int | None = None,
) -> list[ReviewComment]:
    """Pure function: unified diff in → list of ReviewComment out.

    Makes up to 2 LLM attempts before falling back to an empty list.
    Uses Ollama's JSON mode to improve output reliability.
    """
    if not diff or not diff.strip():
        logger.info("review_diff: empty diff — returning no comments.")
        return []

    cap = max_comments or settings.max_review_comments

    client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
    )

    user_message = f"Please review the following pull request diff:\n\n```diff\n{diff}\n```"

    for attempt in range(1, 3):  # up to 2 attempts
        try:
            logger.info("review_diff: LLM attempt %d/%d …", attempt, 2)
            response = await client.chat.completions.create(
                model=settings.ollama_llm_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},  # Ollama JSON mode
                temperature=0.2,  # low temp for consistent, factual output
            )

            raw = response.choices[0].message.content or ""
            data: Any = json.loads(raw)
            output = ReviewOutput.model_validate(data)

            # Cap the number of comments to avoid overwhelming the PR
            comments = output.comments[:cap]
            logger.info("review_diff: produced %d comment(s).", len(comments))
            return comments

        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            logger.warning("review_diff: attempt %d failed to parse LLM output: %s", attempt, exc)
            if attempt == 2:
                logger.error("review_diff: all attempts failed — returning empty list.")
                return []

    return []  # unreachable, satisfies type checker
