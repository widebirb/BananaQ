"""
Changelog Updater Agent — Phase 3.

Pure async function: PR metadata in → formatted changelog entry string out.
Calls the LLM to generate a concise one-line summary, then returns
the entry formatted as:

    - **#N** — Summary (YYYY-MM-DD)

No GitHub API calls here. No side effects.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from config.settings import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------

class PRMeta(BaseModel):
    """Minimal PR metadata needed to generate a changelog entry."""

    number: int = Field(description="PR number, e.g. 12")
    title: str = Field(description="PR title")
    body: str | None = Field(default=None, description="PR description body (may be empty)")
    merged_at: str | None = Field(default=None, description="ISO 8601 merge timestamp")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a technical writer generating a changelog entry for a merged pull request.

You will receive a PR title and optional description. Your job is to write a single,
concise sentence summarising what changed. Write in the past tense. Be specific but brief.
No bullet points, no markdown, no preamble — just the one-line summary sentence.

Examples of good summaries:
- Added retry logic to the reviewer agent on LLM parse failures
- Fixed HMAC verification for empty webhook payloads
- Refactored GitHub client to use async httpx throughout
- Introduced orchestrator dispatcher with playbook-driven triage
"""


def _build_user_prompt(pr: PRMeta) -> str:
    body_section = f"\n\nDescription:\n{pr.body.strip()}" if pr.body and pr.body.strip() else ""
    return f"PR #{pr.number}: {pr.title}{body_section}\n\nWrite the one-line changelog summary:"


def _format_date(merged_at: str | None) -> str:
    """Parse merged_at ISO timestamp → YYYY-MM-DD. Falls back to today."""
    if merged_at:
        try:
            dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clean_summary(raw: str) -> str:
    """Strip leading bullets/dashes/whitespace the LLM might add."""
    cleaned = raw.strip()
    # Remove leading bullet characters the LLM might prepend
    cleaned = re.sub(r"^[-*•]\s*", "", cleaned)
    # Collapse internal newlines to a space (should be one line)
    cleaned = " ".join(cleaned.splitlines()).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Agent function
# ---------------------------------------------------------------------------

async def update_changelog(pr: PRMeta, settings: Settings) -> str:
    """Pure function: PR metadata in → formatted changelog entry string out.

    Calls the LLM to generate a one-line summary, then formats it as:
        - **#N** — Summary (YYYY-MM-DD)

    Makes up to 2 LLM attempts. Falls back to the PR title on failure.
    """
    client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
    )

    user_message = _build_user_prompt(pr)
    date_str = _format_date(pr.merged_at)
    fallback_summary = pr.title.strip()

    for attempt in range(1, 3):
        try:
            logger.info("update_changelog: LLM attempt %d/2 for PR #%d …", attempt, pr.number)
            response = await client.chat.completions.create(
                model=settings.ollama_llm_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                max_tokens=100,
            )

            raw = response.choices[0].message.content or ""
            summary = _clean_summary(raw)

            if not summary:
                raise ValueError("LLM returned empty summary")

            entry = f"- **#{pr.number}** — {summary} ({date_str})"
            logger.info("update_changelog: entry=%r", entry)
            return entry

        except Exception as exc:
            logger.warning(
                "update_changelog: attempt %d failed: %s", attempt, exc
            )
            if attempt == 2:
                logger.error(
                    "update_changelog: all attempts failed — falling back to PR title."
                )

    # Fallback: use the PR title as-is
    entry = f"- **#{pr.number}** — {fallback_summary} ({date_str})"
    logger.info("update_changelog: fallback entry=%r", entry)
    return entry
