"""
Orchestrator Dispatcher.

Sits between the webhook pre-filter and the reviewer agent.
Loads the playbook persona as a system prompt, sends event context
to the LLM, and returns a structured dispatch decision:
  - "review"    -> hand off to pr_reviewer agent
  - "skip"      -> post a personality-driven skip comment
  - "changelog" -> deterministic fast path for merged PRs (no LLM call)

The LLM is called with JSON mode and validated against a Pydantic model.
Up to 2 retry attempts on malformed output, then falls back to "review"
(fail-open: if the orchestrator can't decide, let the reviewer run).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from config.settings import Settings, load_playbook

logger = logging.getLogger(__name__)

# File extensions that are never worth a code review
_SKIP_EXTENSIONS = frozenset({
    ".md", ".txt", ".rst", ".log", ".csv",          # docs / data
    ".json", ".yaml", ".yml", ".toml", ".ini",       # config (pure data)
    ".lock", ".sum",                                  # lock files
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",  # images
    ".woff", ".woff2", ".ttf", ".eot",                # fonts
})


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class DispatchDecision(BaseModel):
    """Structured output from the orchestrator LLM."""

    action: Literal["review", "skip", "changelog"] = Field(
        description="Whether to dispatch the reviewer, skip, or run the changelog agent."
    )
    agent: str | None = Field(
        default=None,
        description="Agent to dispatch. 'pr_reviewer', 'changelog_updater', or null.",
    )
    reason: str = Field(
        description="One-sentence plain-language explanation of the decision."
    )


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

def _extract_changed_paths(diff: str) -> list[str]:
    """Extract file paths from a unified diff's +++ headers."""
    paths = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:])
    return paths


def _is_skip_by_extension(paths: list[str]) -> bool:
    """Return True if every changed file has a non-code extension."""
    if not paths:
        return False  # no paths means we can't tell — let the LLM decide
    return all(
        any(p.lower().endswith(ext) for ext in _SKIP_EXTENSIONS)
        for p in paths
    )


# User prompt builder
def _build_user_prompt(event_summary: str, diff: str) -> str:
    """Build the user message sent to the orchestrator LLM."""
    # Truncate very large diffs to avoid blowing the context window.
    # The orchestrator only needs enough to decide review vs. skip
    # the full diff goes to the reviewer agent separately.
    max_diff_chars = 8000
    truncated = diff[:max_diff_chars]
    if len(diff) > max_diff_chars:
        truncated += f"\n\n... [diff truncated — {len(diff):,} chars total]"

    return (
        f"## Event Summary\n{event_summary}\n\n"
        f"## Diff\n```diff\n{truncated}\n```\n\n"
        "Respond with a JSON object: "
        '{\"action\": \"review\" | \"skip\", \"agent\": \"pr_reviewer\" | null, \"reason\": \"...\"}'
    )


# Dispatcher function
async def dispatch(
    event_summary: str,
    diff: str,
    settings: Settings,
    *,
    merged: bool = False,
) -> DispatchDecision:
    """Route a PR event through the orchestrator.

    Order of evaluation:
    1. merged=True → changelog fast path (no LLM call, deterministic)
    2. Empty/whitespace diff → skip (no LLM call, deterministic)
    3. Docs/config-only file extensions → skip (no LLM call, deterministic)
    4. LLM path → review or skip (uses playbook.md persona)

    Returns a DispatchDecision. On LLM parse failures, falls back to
    action="review" (fail-open).
    """
    # 1. Merged PR → changelog fast path (no LLM call)
    if merged:
        logger.info("dispatch: PR merged — returning changelog (deterministic).")
        return DispatchDecision(
            action="changelog",
            agent="changelog_updater",
            reason="PR merged. Changelog agent will update README.md.",
        )

    # 2. Empty diff → skip (no LLM call)
    if not diff or not diff.strip():
        logger.info("dispatch: empty diff — skipping (deterministic).")
        return DispatchDecision(
            action="skip", agent=None, reason="Diff is empty. Nothing to review."
        )

    changed_paths = _extract_changed_paths(diff)
    if _is_skip_by_extension(changed_paths):
        exts = ", ".join(sorted({p.rsplit(".", 1)[-1] for p in changed_paths if "." in p}))
        logger.info("dispatch: docs/config-only diff (%s) — skipping (deterministic).", exts)
        return DispatchDecision(
            action="skip",
            agent=None,
            reason=f"Only non-code files changed ({exts}). Not a code review task.",
        )

    # 4. LLM path

    playbook = load_playbook()

    client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
    )

    user_message = _build_user_prompt(event_summary, diff)

    for attempt in range(1, 3):  # up to 2 attempts
        try:
            logger.info("dispatch: LLM attempt %d/2 …", attempt)
            response = await client.chat.completions.create(
                model=settings.ollama_llm_model,
                messages=[
                    {"role": "system", "content": playbook},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,  # very low — we want deterministic triage
            )

            raw = response.choices[0].message.content or ""
            data = json.loads(raw)
            decision = DispatchDecision.model_validate(data)

            # Normalize: action="review" must have agent="pr_reviewer"
            if decision.action == "review":
                decision.agent = "pr_reviewer"
            elif decision.action == "skip":
                decision.agent = None

            logger.info(
                "dispatch: decision=%s agent=%s reason=%r",
                decision.action,
                decision.agent,
                decision.reason,
            )
            return decision

        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            logger.warning(
                "dispatch: attempt %d failed to parse LLM output: %s", attempt, exc
            )
            if attempt == 2:
                logger.error(
                    "dispatch: all attempts failed — falling back to 'review' (fail-open)."
                )
                return DispatchDecision(
                    action="review",
                    agent="pr_reviewer",
                    reason="Orchestrator failed to produce valid JSON. Falling back to review.",
                )

    # Unreachable, satisfies type checker
    return DispatchDecision(
        action="review",
        agent="pr_reviewer",
        reason="Fallback.",
    )
