"""
Webhook endpoint — Phase 2.

Receives GitHub pull_request webhook events, verifies the HMAC-SHA256
signature, fetches the PR diff, runs the orchestrator dispatcher to
decide review vs. skip, and either posts line-level review comments
or a personality-driven skip comment.
"""
# ruff: noqa: E501
from __future__ import annotations

import hashlib
import hmac
import logging
import re

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from agents.reviewer import review_diff
from config.settings import get_settings
from github_client.client import get_pr_diff, post_pr_comment, post_review_comments
from orchestrator.dispatcher import dispatch

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# HMAC signature verification
# ---------------------------------------------------------------------------

def _verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Return True if the X-Hub-Signature-256 header matches the expected HMAC."""
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.HMAC(
        secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


# ---------------------------------------------------------------------------
# Pre-filter helpers
# ---------------------------------------------------------------------------

def _is_reviewable_event(event_type: str, action: str) -> bool:
    """Return True only for PR events we care about."""
    return event_type == "pull_request" and action in ("opened", "synchronize", "reopened")


def _is_diff_reviewable(diff: str) -> bool:
    """Basic sanity check: diff must be non-empty and contain added lines."""
    if not diff or not diff.strip():
        return False
    # Must have at least one added line (not counting diff headers)
    added_lines = [
        line for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    return len(added_lines) > 0


def _parse_valid_diff_lines(diff: str) -> set[tuple[str, int]]:
    """Parse a unified diff and return a set of (path, line_number) tuples
    that are valid on the new (right) side of the diff.

    Only context lines (space-prefixed) and added lines (+prefixed) within
    hunk ranges are considered valid targets for GitHub PR review comments.
    """
    valid: set[tuple[str, int]] = set()
    current_path: str | None = None
    new_line_num = 0

    for line in diff.splitlines():
        # Detect file path from the +++ header
        if line.startswith("+++ b/"):
            current_path = line[6:]  # strip "+++ b/"
            continue

        # Detect hunk header: @@ -old_start,old_count +new_start,new_count @@
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk_match:
            new_line_num = int(hunk_match.group(1))
            continue

        if current_path is None:
            continue

        # Context line (unchanged) — valid target, increment new side
        if line.startswith(" "):
            valid.add((current_path, new_line_num))
            new_line_num += 1
        # Added line — valid target, increment new side
        elif line.startswith("+") and not line.startswith("+++"):
            valid.add((current_path, new_line_num))
            new_line_num += 1
        # Deleted line — only on old side, don't increment new side
        elif line.startswith("-") and not line.startswith("---"):
            pass  # old side only

    return valid


# ---------------------------------------------------------------------------
# Event summary builder (for orchestrator context)
# ---------------------------------------------------------------------------

def _build_event_summary(action: str, pr: dict) -> str:
    """Build a human-readable event summary for the orchestrator LLM."""
    pr_number = pr.get("number", "?")
    title = pr.get("title", "Untitled")
    user = pr.get("user", {}).get("login", "unknown")
    changed_files = pr.get("changed_files", "?")
    additions = pr.get("additions", "?")
    deletions = pr.get("deletions", "?")

    return (
        f"Pull request #{pr_number}: \"{title}\" by {user}.\n"
        f"Action: {action}.\n"
        f"Stats: {changed_files} file(s) changed, +{additions}/-{deletions} lines."
    )


# ---------------------------------------------------------------------------
# Background task: the review pipeline with orchestrator
# ---------------------------------------------------------------------------

async def _run_review_pipeline(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    event_summary: str,
) -> None:
    """Fetch diff → orchestrator triage → review or skip. Runs as a background task."""
    settings = get_settings()

    try:
        # 1. Fetch the PR diff from GitHub
        logger.info("Fetching diff for %s/%s#%d …", owner, repo, pr_number)
        diff = await get_pr_diff(settings, owner, repo, pr_number)

        # 2. Pre-filter: skip empty / useless diffs (fast path — no LLM call)
        if not _is_diff_reviewable(diff):
            logger.info("Diff is empty or has no added lines — skipping review.")
            return

        # 3. Orchestrator: ask the LLM whether to review or skip
        logger.info("Running orchestrator dispatcher …")
        decision = await dispatch(event_summary, diff, settings)

        if decision.action == "skip":
            # Post the orchestrator's skip reason as a PR comment (with personality)
            skip_msg = f"🍌 **BananaQ skipped this PR.** {decision.reason}"
            logger.info("Orchestrator decided to skip: %s", decision.reason)
            await post_pr_comment(settings, owner, repo, pr_number, skip_msg)
            return

        # 4. Orchestrator said "review" — call the reviewer agent
        logger.info("Orchestrator dispatched pr_reviewer. Running review …")
        comments = await review_diff(diff, settings)

        if not comments:
            logger.info("Reviewer produced no comments — nothing to post.")
            await post_pr_comment(
                settings, owner, repo, pr_number,
                "🍌 BananaQ reviewed this PR and found nothing noteworthy. Looks clean!"
            )
            return

        # 5. Validate comment line numbers against the actual diff
        valid_lines = _parse_valid_diff_lines(diff)
        valid_comments = [
            c for c in comments
            if (c.path, c.line) in valid_lines
        ]
        dropped = len(comments) - len(valid_comments)
        if dropped:
            logger.warning(
                "Dropped %d comment(s) with invalid line numbers (LLM hallucination).",
                dropped,
            )

        # 6. Post line-level review comments back to GitHub
        if valid_comments:
            comment_dicts = [c.model_dump() for c in valid_comments]
            await post_review_comments(settings, owner, repo, pr_number, commit_sha, comment_dicts)
        elif comments:
            # All comments had invalid lines — fall back to a plain issue comment
            fallback_body = "🍌 **BananaQ review findings** (posted as a comment because line numbers couldn't be resolved):\n\n"
            for c in comments:
                fallback_body += f"- **{c.path}:{c.line}** — {c.body}\n"
            await post_pr_comment(settings, owner, repo, pr_number, fallback_body)

    except Exception:
        logger.exception(
            "Unexpected error during review pipeline for %s/%s#%d.", owner, repo, pr_number
        )
        # Best-effort: try to notify the PR about the failure
        try:
            settings = get_settings()
            await post_pr_comment(
                settings, owner, repo, pr_number,
                "🍌 BananaQ encountered an error during review. Check the server logs."
            )
        except Exception:
            pass  # don't let the notification failure mask the original error


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> JSONResponse:
    """Receive and process GitHub webhook events."""
    settings = get_settings()
    body = await request.body()

    # --- 1. Verify HMAC signature ---
    if not _verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        logger.warning("Webhook signature verification failed.")
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    # --- 2. Parse JSON payload ---
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    action: str = payload.get("action", "")
    event_type: str = x_github_event or ""

    # --- 3. Pre-filter: only handle relevant PR events ---
    if not _is_reviewable_event(event_type, action):
        logger.info("Ignoring event: type=%r action=%r", event_type, action)
        return JSONResponse({"status": "ignored", "reason": "not a reviewable PR event"})

    # --- 4. Extract PR metadata ---
    pr = payload.get("pull_request", {})
    pr_number: int = pr.get("number")
    commit_sha: str = pr.get("head", {}).get("sha", "")
    repo_info = payload.get("repository", {})
    owner: str = repo_info.get("owner", {}).get("login", settings.repo_owner)
    repo: str = repo_info.get("name", settings.repo_name)

    if not pr_number or not commit_sha:
        raise HTTPException(status_code=422, detail="Missing pr_number or commit_sha in payload.")

    # --- 5. Build event summary for the orchestrator ---
    event_summary = _build_event_summary(action, pr)

    logger.info(
        "Accepted %s event for %s/%s#%d (sha=%s).",
        action, owner, repo, pr_number, commit_sha[:7],
    )

    # --- 6. Kick off review in the background (don't block GitHub's 10s timeout) ---
    background_tasks.add_task(
        _run_review_pipeline, owner, repo, pr_number, commit_sha, event_summary
    )

    return JSONResponse({"status": "accepted", "pr": pr_number})
