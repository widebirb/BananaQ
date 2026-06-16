"""
Tests for the reviewer agent.

Uses a mocked LLM client so no Ollama instance is needed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.reviewer import ReviewComment, review_diff
from config.settings import Settings

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


SAMPLE_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 0000000..1111111 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,5 +1,8 @@
 def process(data):
-    return data
+    result = eval(data)  # dangerous!
+    return result
"""

VALID_LLM_RESPONSE = """\
{
  "comments": [
    {
      "path": "src/app.py",
      "line": 3,
      "body": "Using `eval()` is a critical security risk. It executes arbitrary code."
    }
  ]
}
"""

EMPTY_LLM_RESPONSE = '{"comments": []}'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(content: str):
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReviewDiff:
    @pytest.mark.asyncio
    async def test_empty_diff_returns_no_comments(self, settings):
        result = await review_diff("", settings)
        assert result == []

    @pytest.mark.asyncio
    async def test_whitespace_diff_returns_no_comments(self, settings):
        result = await review_diff("   \n\n  ", settings)
        assert result == []

    @pytest.mark.asyncio
    async def test_valid_llm_response_parsed_correctly(self, settings):
        mock_response = _make_mock_response(VALID_LLM_RESPONSE)

        with patch("agents.reviewer.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_response)

            result = await review_diff(SAMPLE_DIFF, settings)

        assert len(result) == 1
        assert isinstance(result[0], ReviewComment)
        assert result[0].path == "src/app.py"
        assert result[0].line == 3
        assert "eval" in result[0].body

    @pytest.mark.asyncio
    async def test_empty_comments_from_llm(self, settings):
        mock_response = _make_mock_response(EMPTY_LLM_RESPONSE)

        with patch("agents.reviewer.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_response)

            result = await review_diff(SAMPLE_DIFF, settings)

        assert result == []

    @pytest.mark.asyncio
    async def test_invalid_json_retries_and_returns_empty(self, settings):
        mock_response = _make_mock_response("this is not json at all")

        with patch("agents.reviewer.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_response)

            result = await review_diff(SAMPLE_DIFF, settings)

        assert result == []
        # Should have retried twice
        assert instance.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_max_comments_cap_applied(self, settings):
        # Build LLM response with 5 comments
        many_comments = {
            "comments": [
                {"path": "file.py", "line": i, "body": f"Issue {i}"}
                for i in range(1, 6)
            ]
        }
        import json
        mock_response = _make_mock_response(json.dumps(many_comments))

        with patch("agents.reviewer.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_response)

            result = await review_diff(SAMPLE_DIFF, settings, max_comments=3)

        assert len(result) == 3
