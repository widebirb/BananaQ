"""
Tests for the webhook endpoint.

Covers:
- HMAC signature verification (valid, invalid, missing)
- Event filtering (non-PR events, non-reviewable actions)
- Happy-path PR event → accepted response
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_SECRET = "test-webhook-secret"
TEST_PAYLOAD = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "head": {"sha": "abc123def456"},
    },
    "repository": {
        "name": "BQTR",
        "owner": {"login": "widebirb"},
    },
}


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.HMAC(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post_webhook(
    client: TestClient,
    payload: dict,
    secret: str = TEST_SECRET,
    event: str = "pull_request",
    tamper: bool = False,
) -> object:
    body = json.dumps(payload).encode()
    sig = _sign(secret, body)
    if tamper:
        sig = sig[:-4] + "XXXX"  # corrupt the signature

    return client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": event,
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    """Patch settings so tests don't need a real .env file."""
    from config import settings as settings_module
    import api.webhook as webhook_module

    # Grab the real cached function BEFORE patching so teardown can clear it
    original_get_settings = settings_module.get_settings
    original_get_settings.cache_clear()

    fake_settings = settings_module.Settings(
        github_token="fake-token",
        github_webhook_secret=TEST_SECRET,
        github_repo="widebirb/BQTR",
    )

    # Patch BOTH the canonical location and the local reference in webhook.py
    # (webhook.py does `from config.settings import get_settings`, so it holds
    # its own binding that is unaffected by patching config.settings directly)
    monkeypatch.setattr(settings_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(webhook_module, "get_settings", lambda: fake_settings)

    yield
    # Clear the real lru_cache so it doesn't leak state to subsequent tests
    original_get_settings.cache_clear()



@pytest.fixture
def client(patch_settings):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSignatureVerification:
    def test_missing_signature_returns_401(self, client):
        body = json.dumps(TEST_PAYLOAD).encode()
        resp = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request"},
        )
        assert resp.status_code == 401

    def test_invalid_signature_returns_401(self, client):
        resp = _post_webhook(client, TEST_PAYLOAD, tamper=True)
        assert resp.status_code == 401

    def test_valid_signature_passes(self, client):
        resp = _post_webhook(client, TEST_PAYLOAD)
        # 202 Accepted or 200 OK — either is fine for an accepted PR event
        assert resp.status_code in (200, 202)


class TestEventFiltering:
    def test_non_pr_event_is_ignored(self, client):
        resp = _post_webhook(client, {"action": "created"}, event="push")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_pr_closed_action_is_ignored(self, client):
        payload = {**TEST_PAYLOAD, "action": "closed"}
        resp = _post_webhook(client, payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_pr_opened_is_accepted(self, client):
        resp = _post_webhook(client, TEST_PAYLOAD)
        data = resp.json()
        assert resp.status_code == 200
        assert data["status"] == "accepted"
        assert data["pr"] == 42

    def test_pr_synchronize_is_accepted(self, client):
        payload = {**TEST_PAYLOAD, "action": "synchronize"}
        resp = _post_webhook(client, payload)
        assert resp.json()["status"] == "accepted"


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
