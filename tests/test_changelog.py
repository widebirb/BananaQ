"""
Tests for the changelog pipeline — Phase 3.

Covers:
- LLM generates valid changelog entry from PR metadata
- Fallback to PR title when LLM fails
- Entry is prepended to existing changelog section (newest first)
- New ## Changelog section is created if none exists
- Entry inserted after existing entries in the section
- Closed-but-not-merged PR → dispatcher returns skip (no LLM call)
- Merged PR → dispatcher returns changelog (no LLM call, deterministic)
- _prepend_changelog_entry helper edge cases
- PRMeta model validation
- Date formatting
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.changelog_updater import (
    PRMeta,
    _clean_summary,
    _format_date,
    update_changelog,
)
from api.webhook import _prepend_changelog_entry
from config.settings import Settings
from orchestrator.dispatcher import DispatchDecision, dispatch


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


@pytest.fixture
def sample_pr():
    return PRMeta(
        number=12,
        title="Add retry logic to reviewer agent",
        body="Retries up to 2 times on parse failures before returning empty list.",
        merged_at="2026-06-17T14:00:00Z",
    )


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
# Tests: update_changelog (agent function)
# ---------------------------------------------------------------------------

class TestUpdateChangelog:

    @pytest.mark.asyncio
    async def test_valid_llm_response_produces_entry(self, settings, sample_pr):
        """LLM returns a good summary → entry formatted correctly."""
        mock_resp = _make_mock_response("Added retry logic to the reviewer agent on LLM parse failures")

        with patch("agents.changelog_updater.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat = MagicMock()
            instance.chat.completions = MagicMock()
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            entry = await update_changelog(sample_pr, settings)

        assert entry.startswith("- **#12**")
        assert "retry logic" in entry.lower()
        assert "2026-06-17" in entry

    @pytest.mark.asyncio
    async def test_entry_format_matches_spec(self, settings, sample_pr):
        """Entry must match: - **#N** — Summary (YYYY-MM-DD)"""
        mock_resp = _make_mock_response("Introduced retry logic for reviewer agent")

        with patch("agents.changelog_updater.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            entry = await update_changelog(sample_pr, settings)

        import re
        assert re.match(r"^- \*\*#\d+\*\* — .+ \(\d{4}-\d{2}-\d{2}\)$", entry), \
            f"Entry doesn't match format: {entry!r}"

    @pytest.mark.asyncio
    async def test_llm_bullet_prefix_stripped(self, settings, sample_pr):
        """LLM response with a leading bullet should be cleaned."""
        mock_resp = _make_mock_response("- Added retry logic to reviewer agent")

        with patch("agents.changelog_updater.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            entry = await update_changelog(sample_pr, settings)

        # Should not have double bullet
        assert "- - " not in entry
        assert "Added retry logic" in entry

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_pr_title(self, settings, sample_pr):
        """All LLM attempts fail → entry uses PR title as fallback."""
        mock_resp = _make_mock_response("")  # empty = failure

        with patch("agents.changelog_updater.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            entry = await update_changelog(sample_pr, settings)

        assert "**#12**" in entry
        assert sample_pr.title in entry
        assert "2026-06-17" in entry
        assert instance.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_exception_falls_back_to_pr_title(self, settings, sample_pr):
        """LLM raises exception → fallback to PR title."""
        with patch("agents.changelog_updater.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(
                side_effect=Exception("LLM unavailable")
            )

            entry = await update_changelog(sample_pr, settings)

        assert "**#12**" in entry
        assert sample_pr.title in entry

    @pytest.mark.asyncio
    async def test_no_body_pr_still_works(self, settings):
        """PR with no body → LLM call still made, entry produced."""
        pr = PRMeta(number=5, title="Fix null pointer", body=None, merged_at="2026-06-17T10:00:00Z")
        mock_resp = _make_mock_response("Fixed null pointer in parser module")

        with patch("agents.changelog_updater.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            entry = await update_changelog(pr, settings)

        assert "**#5**" in entry

    @pytest.mark.asyncio
    async def test_no_merged_at_falls_back_to_today(self, settings):
        """PR with no merged_at → date is today (format YYYY-MM-DD)."""
        pr = PRMeta(number=9, title="Test PR", merged_at=None)
        mock_resp = _make_mock_response("Test change summary")

        with patch("agents.changelog_updater.AsyncOpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create = AsyncMock(return_value=mock_resp)

            entry = await update_changelog(pr, settings)

        import re
        assert re.search(r"\(\d{4}-\d{2}-\d{2}\)", entry), "Date not found in entry"


# ---------------------------------------------------------------------------
# Tests: _prepend_changelog_entry (webhook helper)
# ---------------------------------------------------------------------------

class TestPrependChangelogEntry:

    def test_creates_section_if_absent(self):
        readme = "# My Project\n\nSome content.\n"
        entry = "- **#1** — First change (2026-06-17)"
        result = _prepend_changelog_entry(readme, entry)

        assert "## Changelog" in result
        assert entry in result

    def test_prepends_to_existing_section(self):
        readme = (
            "# My Project\n\n"
            "## Changelog\n"
            "- **#1** — Old entry (2026-06-16)\n"
        )
        entry = "- **#2** — New entry (2026-06-17)"
        result = _prepend_changelog_entry(readme, entry)

        # New entry should appear before the old one
        new_pos = result.index("**#2**")
        old_pos = result.index("**#1**")
        assert new_pos < old_pos, "New entry should be before old entry"

    def test_prepends_immediately_after_header(self):
        readme = "## Changelog\n- **#1** — Existing (2026-06-16)\n"
        entry = "- **#2** — Newest (2026-06-17)"
        result = _prepend_changelog_entry(readme, entry)

        lines = result.splitlines()
        header_idx = next(i for i, l in enumerate(lines) if l.strip() == "## Changelog")
        assert lines[header_idx + 1] == entry

    def test_empty_readme_gets_section(self):
        result = _prepend_changelog_entry("", "- **#1** — Entry (2026-06-17)")
        assert "## Changelog" in result
        assert "**#1**" in result

    def test_multiple_sections_targets_changelog(self):
        readme = (
            "# Project\n\n"
            "## Installation\n\nSome install steps.\n\n"
            "## Changelog\n"
            "- **#3** — Old entry (2026-06-15)\n\n"
            "## License\n\nMIT\n"
        )
        entry = "- **#4** — New entry (2026-06-17)"
        result = _prepend_changelog_entry(readme, entry)

        new_pos = result.index("**#4**")
        old_pos = result.index("**#3**")
        assert new_pos < old_pos
        assert "## Installation" in result
        assert "## License" in result

    def test_idempotent_on_repeated_prepend(self):
        readme = "## Changelog\n"
        entry1 = "- **#1** — First (2026-06-16)"
        entry2 = "- **#2** — Second (2026-06-17)"

        result = _prepend_changelog_entry(readme, entry1)
        result = _prepend_changelog_entry(result, entry2)

        assert result.index("**#2**") < result.index("**#1**")


# ---------------------------------------------------------------------------
# Tests: Dispatcher fast paths for merged/closed PRs
# ---------------------------------------------------------------------------

class TestDispatcherMergedFastPath:

    @pytest.mark.asyncio
    async def test_merged_pr_returns_changelog_without_llm(self, settings):
        """merged=True → action=changelog, no LLM call."""
        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            decision = await dispatch("", "", settings, merged=True)
            MockClient.return_value.chat.completions.create.assert_not_called()

        assert decision.action == "changelog"
        assert decision.agent == "changelog_updater"

    @pytest.mark.asyncio
    async def test_closed_not_merged_does_not_fast_path(self, settings):
        """merged=False (closed but not merged) → hits normal pre-filters, returns skip for empty diff."""
        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            decision = await dispatch("event", "", settings, merged=False)
            MockClient.return_value.chat.completions.create.assert_not_called()

        # Empty diff → deterministic skip, not changelog
        assert decision.action == "skip"
        assert decision.agent is None

    @pytest.mark.asyncio
    async def test_merged_fast_path_has_priority_over_empty_diff(self, settings):
        """merged=True with empty diff → still returns changelog (not skip)."""
        with patch("orchestrator.dispatcher.AsyncOpenAI") as MockClient:
            decision = await dispatch("", "", settings, merged=True)

        assert decision.action == "changelog"


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_format_date_parses_iso_timestamp(self):
        result = _format_date("2026-06-17T14:30:00Z")
        assert result == "2026-06-17"

    def test_format_date_handles_none(self):
        result = _format_date(None)
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)

    def test_format_date_handles_invalid(self):
        result = _format_date("not-a-date")
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)

    def test_clean_summary_strips_bullet(self):
        assert _clean_summary("- Some summary") == "Some summary"

    def test_clean_summary_strips_asterisk_bullet(self):
        assert _clean_summary("* Some summary") == "Some summary"

    def test_clean_summary_strips_whitespace(self):
        assert _clean_summary("  Some summary  ") == "Some summary"

    def test_clean_summary_collapses_newlines(self):
        assert _clean_summary("Line one\nLine two") == "Line one Line two"

    def test_clean_summary_empty(self):
        assert _clean_summary("") == ""


# ---------------------------------------------------------------------------
# Tests: PRMeta model
# ---------------------------------------------------------------------------

class TestPRMeta:

    def test_valid_pr_meta(self):
        pr = PRMeta(number=1, title="Fix bug", body="Details", merged_at="2026-06-17T10:00:00Z")
        assert pr.number == 1
        assert pr.title == "Fix bug"

    def test_optional_fields_default_to_none(self):
        pr = PRMeta(number=2, title="Minimal PR")
        assert pr.body is None
        assert pr.merged_at is None

    def test_empty_body_allowed(self):
        pr = PRMeta(number=3, title="Empty body", body="")
        assert pr.body == ""
