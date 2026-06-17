"""
Tests for the orchestrator dispatcher, Phase 2.

Covers:
- Empty diff → orchestrator returns skip
- Single-line typo fix → orchestrator returns skip
- Substantial code change → orchestrator dispatches reviewer
- Malformed/binary diff → orchestrator returns skip
- LLM JSON parse failure → graceful fallback to "review" (fail-open)
- Prompt construction sanity checks
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings
from orchestrator.dispatcher import (
    DispatchDecision,
    _build_user_prompt,
    _extract_changed_paths,
    _is_skip_by_extension,
    dispatch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings():
    return Settings(
        github_token="fake",
        github_webhook_secret="fake",
        github_repo="widebirb/BQTR",
        ollama_llm_model="qwen2.5-coder:1.5b",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(content: str):
    """Build a mock OpenAI ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


SAMPLE_EVENT_SUMMARY = (
    'Pull request #7: "Fix null pointer in parser" by widebirb.\n'
    'Action: opened.\n'
    'Stats: 2 file(s) changed, +15/-3 lines.'
)

SUBSTANTIAL_DIFF = """\
diff --git a/src/parser.py b/src/parser.py
index 0000000..1111111 100644
--- a/src/parser.py
+++ b/src/parser.py
@@ -10,6 +10,20 @@
 class Parser:
     def parse(self, data):
-        return data.split(",")
+        if data is None:
+            raise ValueError("data must not be None")
+        tokens = data.strip().split(",")
+        return [t.strip() for t in tokens if t]
+
+    def validate(self, tokens):
+        for t in tokens:
+            if not isinstance(t, str):
+                raise TypeError(f"Expected str, got {type(t).__name__}")
+        return True
"""

TYPO_FIX_DIFF = """\
diff --git a/README.md b/README.md
index 0000000..1111111 100644
--- a/README.md
+++ b/README.md
@@ -5,3 +5,3 @@
-This is a smple project.
+This is a simple project.
"""

EMPTY_DIFF = ""

BINARY_DIFF = """\
diff --git a/image.png b/image.png
new file mode 100644
index 0000000..1111111
Binary files /dev/null and b/image.png differ
"""


# ---------------------------------------------------------------------------
# Tests: LLM triage decisions
# ---------------------------------------------------------------------------

class TestDispatchDecisions:
    """Test that the dispatcher correctly interprets LLM responses."""

    @pytest.mark.asyncio
    async def test_substantial_code_change_dispatches_reviewer(self, settings):
        """Substantial code diff → action=review, agent=pr_reviewer."""
        llm_response = json.dumps({
            "action": "review",
            "agent": "pr_reviewer",
            "reason": "2 Python files changed with new logic. Dispatching pr_reviewer.",
        })
        mock_resp = _make_mock_response(llm_response)

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, SUBSTANTIAL_DIFF, settings)

        assert decision.action == "review"
        assert decision.agent == "pr_reviewer"
        assert decision.reason

    @pytest.mark.asyncio
    async def test_empty_diff_returns_skip(self, settings):
        """LLM sees empty diff → should return skip."""
        llm_response = json.dumps({
            "action": "skip",
            "agent": None,
            "reason": "Diff is empty. Nothing to review.",
        })
        mock_resp = _make_mock_response(llm_response)

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, EMPTY_DIFF, settings)

        assert decision.action == "skip"
        assert decision.agent is None
        assert "empty" in decision.reason.lower() or "nothing" in decision.reason.lower()

    @pytest.mark.asyncio
    async def test_typo_fix_returns_skip(self, settings):
        """Single-line typo fix in markdown → should return skip."""
        llm_response = json.dumps({
            "action": "skip",
            "agent": None,
            "reason": "Single-line whitespace fix. Not worth a review pass.",
        })
        mock_resp = _make_mock_response(llm_response)

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, TYPO_FIX_DIFF, settings)

        assert decision.action == "skip"
        assert decision.agent is None

    @pytest.mark.asyncio
    async def test_binary_diff_returns_skip(self, settings):
        """Binary/malformed diff → should return skip."""
        llm_response = json.dumps({
            "action": "skip",
            "agent": None,
            "reason": "Binary file diff. Not reviewable.",
        })
        mock_resp = _make_mock_response(llm_response)

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, BINARY_DIFF, settings)

        assert decision.action == "skip"
        assert decision.agent is None


# ---------------------------------------------------------------------------
# Tests: JSON parse failure → graceful fallback
# ---------------------------------------------------------------------------

class TestDispatchFallback:
    """Test that parse failures fall back to review (fail-open)."""

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back_to_review(self, settings):
        """LLM returns garbage → 2 retries → fallback to review."""
        mock_resp = _make_mock_response("this is not json at all")

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, SUBSTANTIAL_DIFF, settings)

        # Should fall back to review (fail-open)
        assert decision.action == "review"
        assert decision.agent == "pr_reviewer"
        # Should have retried twice
        assert instance.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_missing_fields_falls_back_to_review(self, settings):
        """LLM returns valid JSON but missing required fields → fallback."""
        mock_resp = _make_mock_response('{"action": "review"}')  # missing "reason"

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, SUBSTANTIAL_DIFF, settings)

        # Pydantic should reject this (missing 'reason' field) → fallback
        assert decision.action == "review"
        assert decision.agent == "pr_reviewer"

    @pytest.mark.asyncio
    async def test_empty_llm_content_falls_back_to_review(self, settings):
        """LLM returns empty string → fallback to review."""
        mock_resp = _make_mock_response("")

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, SUBSTANTIAL_DIFF, settings)

        assert decision.action == "review"
        assert decision.agent == "pr_reviewer"
        assert instance.chat.completions.create.call_count == 2


# ---------------------------------------------------------------------------
# Tests: Agent normalization
# ---------------------------------------------------------------------------

class TestAgentNormalization:
    """Test that agent field is normalized correctly."""

    @pytest.mark.asyncio
    async def test_review_action_sets_agent_to_pr_reviewer(self, settings):
        """Even if LLM forgets to set agent, review action normalizes it."""
        llm_response = json.dumps({
            "action": "review",
            "agent": None,  # LLM forgot
            "reason": "Code changed. Reviewing.",
        })
        mock_resp = _make_mock_response(llm_response)

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, SUBSTANTIAL_DIFF, settings)

        assert decision.action == "review"
        assert decision.agent == "pr_reviewer"

    @pytest.mark.asyncio
    async def test_skip_action_sets_agent_to_none(self, settings):
        """Skip action always normalizes agent to None."""
        llm_response = json.dumps({
            "action": "skip",
            "agent": "pr_reviewer",  # LLM set it wrong
            "reason": "Markdown only.",
        })
        mock_resp = _make_mock_response(llm_response)

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, TYPO_FIX_DIFF, settings)

        assert decision.action == "skip"
        assert decision.agent is None


# ---------------------------------------------------------------------------
# Tests: Prompt construction
# ---------------------------------------------------------------------------

class TestBuildUserPrompt:
    """Test the user prompt construction helper."""

    def test_includes_event_summary(self):
        prompt = _build_user_prompt("PR #5 opened", "some diff")
        assert "PR #5 opened" in prompt

    def test_includes_diff(self):
        prompt = _build_user_prompt("event", "def foo(): pass")
        assert "def foo(): pass" in prompt

    def test_truncates_long_diff(self):
        long_diff = "x" * 20000
        prompt = _build_user_prompt("event", long_diff)
        assert "truncated" in prompt
        # Should not include the full 20k chars
        assert len(prompt) < 15000

    def test_includes_json_schema_instruction(self):
        prompt = _build_user_prompt("event", "diff")
        assert '"action"' in prompt
        assert '"review"' in prompt
        assert '"skip"' in prompt


# ---------------------------------------------------------------------------
# Tests: DispatchDecision model
# ---------------------------------------------------------------------------

class TestDispatchDecisionModel:
    """Test the Pydantic model directly."""

    def test_valid_review_decision(self):
        d = DispatchDecision(action="review", agent="pr_reviewer", reason="Code changed.")
        assert d.action == "review"
        assert d.agent == "pr_reviewer"

    def test_valid_skip_decision(self):
        d = DispatchDecision(action="skip", agent=None, reason="Empty diff.")
        assert d.action == "skip"
        assert d.agent is None

    def test_invalid_action_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DispatchDecision(action="invalid", agent=None, reason="Bad.")

    def test_missing_reason_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DispatchDecision(action="review", agent="pr_reviewer")


# ---------------------------------------------------------------------------
# Tests: Deterministic pre-filters (no LLM call)
# ---------------------------------------------------------------------------

class TestDeterministicSkip:
    """Test that obvious skip cases are handled without an LLM call."""

    @pytest.mark.asyncio
    async def test_markdown_only_diff_skipped_deterministically(self, settings):
        """PR that only changes .md files → skip without calling LLM."""
        md_diff = (
            "diff --git a/README.md b/README.md\n"
            "index 0000000..1111111 100644\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,3 +1,3 @@\n"
            "-Old line\n"
            "+New line\n"
        )

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            decision = await dispatch(SAMPLE_EVENT_SUMMARY, md_diff, settings)
            # LLM should NOT have been called
            MockClient.return_value.chat.completions.create.assert_not_called()

        assert decision.action == "skip"
        assert decision.agent is None
        assert "md" in decision.reason.lower()

    @pytest.mark.asyncio
    async def test_empty_diff_skipped_deterministically(self, settings):
        """Empty diff → skip without calling LLM."""
        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            decision = await dispatch(SAMPLE_EVENT_SUMMARY, "", settings)
            MockClient.return_value.chat.completions.create.assert_not_called()

        assert decision.action == "skip"
        assert "empty" in decision.reason.lower()

    @pytest.mark.asyncio
    async def test_whitespace_only_diff_skipped_deterministically(self, settings):
        """Whitespace-only diff → skip without calling LLM."""
        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            decision = await dispatch(SAMPLE_EVENT_SUMMARY, "   \n\n  ", settings)
            MockClient.return_value.chat.completions.create.assert_not_called()

        assert decision.action == "skip"

    @pytest.mark.asyncio
    async def test_mixed_files_still_calls_llm(self, settings):
        """PR with .md AND .py files → should call LLM (not deterministic skip)."""
        mixed_diff = (
            "diff --git a/README.md b/README.md\n"
            "+++ b/README.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/src/app.py b/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1,2 @@\n"
            " existing\n"
            "+new_code()\n"
        )
        llm_response = json.dumps({
            "action": "review",
            "agent": "pr_reviewer",
            "reason": "Python file changed. Dispatching pr_reviewer.",
        })
        mock_resp = _make_mock_response(llm_response)

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            decision = await dispatch(SAMPLE_EVENT_SUMMARY, mixed_diff, settings)
            # LLM SHOULD have been called
            instance.chat.completions.create.assert_called_once()

        assert decision.action == "review"

    @pytest.mark.asyncio
    async def test_json_only_diff_skipped_deterministically(self, settings):
        """PR that only changes .json files → skip without calling LLM."""
        json_diff = (
            "diff --git a/package.json b/package.json\n"
            "+++ b/package.json\n"
            "@@ -1 +1 @@\n"
            '-  "version": "1.0.0"\n'
            '+  "version": "1.1.0"\n'
        )

        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            decision = await dispatch(SAMPLE_EVENT_SUMMARY, json_diff, settings)
            MockClient.return_value.chat.completions.create.assert_not_called()

        assert decision.action == "skip"


class TestHelperFunctions:
    """Test the deterministic helper functions directly."""

    def test_extract_changed_paths(self):
        paths = _extract_changed_paths(SUBSTANTIAL_DIFF)
        assert paths == ["src/parser.py"]

    def test_extract_changed_paths_multiple(self):
        diff = (
            "+++ b/README.md\n"
            "+++ b/src/app.py\n"
        )
        paths = _extract_changed_paths(diff)
        assert paths == ["README.md", "src/app.py"]

    def test_is_skip_by_extension_markdown(self):
        assert _is_skip_by_extension(["README.md"]) is True

    def test_is_skip_by_extension_python(self):
        assert _is_skip_by_extension(["src/app.py"]) is False

    def test_is_skip_by_extension_mixed(self):
        assert _is_skip_by_extension(["README.md", "src/app.py"]) is False

    def test_is_skip_by_extension_empty(self):
        assert _is_skip_by_extension([]) is False

    def test_is_skip_by_extension_multiple_docs(self):
        assert _is_skip_by_extension(["README.md", "CHANGELOG.txt", "notes.rst"]) is True

    def test_is_skip_by_extension_lockfile(self):
        assert _is_skip_by_extension(["package-lock.json"]) is True
