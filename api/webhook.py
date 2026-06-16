"""
Webhook endpoint — Phase 1.

Receives GitHub pull_request webhook events, verifies the HMAC-SHA256
signature, fetches the PR diff, runs the reviewer agent, and posts
line-level comments back to GitHub.

Phase 2 will insert the orchestrator LLM dispatcher between the
pre-filter and the reviewer agent call.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from agents.reviewer import review_diff
from config.settings import get_settings
from github_client.client import get_pr_diff, post_pr_comment, post_review_comments

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


# ---------------------------------------------------------------------------
# Background task: the actual review pipeline
# ---------------------------------------------------------------------------

async def _run_review_pipeline(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
) -> None:
    """Fetch diff → review → post comments. Runs as a FastAPI background task."""
    settings = get_settings()

    try:
        # 1. Fetch the PR diff from GitHub
        logger.info("Fetching diff for %s/%s#%d …", owner, repo, pr_number)
        diff = await get_pr_diff(settings, owner, repo, pr_number)

        # 2. Pre-filter: skip empty / useless diffs
        if not _is_diff_reviewable(diff):
            logger.info("Diff is empty or has no added lines — skipping review.")
            return

        # 3. Call the reviewer agent (pure function: diff in → comments out)
        logger.info("Running reviewer agent …")
        comments = await review_diff(diff, settings)

        if not comments:
            logger.info("Reviewer produced no comments — nothing to post.")
            await post_pr_comment(
                settings, owner, repo, pr_number,
                "🍌 BananaQ reviewed this PR and found nothing noteworthy. Looks clean!"
            )
            return

        # 4. Post line-level review comments back to GitHub
        comment_dicts = [c.model_dump() for c in comments]
        await post_review_comments(settings, owner, repo, pr_number, commit_sha, comment_dicts)

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

    logger.info(
        "Accepted %s event for %s/%s#%d (sha=%s).",
        action, owner, repo, pr_number, commit_sha[:7],
    )

    # --- 5. Kick off review in the background (don't block GitHub's 10s timeout) ---
    background_tasks.add_task(_run_review_pipeline, owner, repo, pr_number, commit_sha)

    return JSONResponse({"status": "accepted", "pr": pr_number})
