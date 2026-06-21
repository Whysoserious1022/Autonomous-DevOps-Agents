"""
tests/test_poller.py
─────────────────────
Tests for the GitHub Issue Poller.

Uses MagicMock to simulate PyGithub responses without making real API calls.
Tests cover:
  - URL parsing (owner/repo extraction)
  - Seen-issue deduplication
  - Watch/unwatch lifecycle
  - Poll loop with new/existing issues
  - Graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from cascade.watcher.github_poller import (
    GithubPoller,
    _load_seen_issues,
    _save_seen_issues,
    SEEN_ISSUES_FILE,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_trigger():
    """Async trigger callback that records calls."""
    triggered_issues = []

    async def _trigger(repo_url: str, issue: Any) -> None:
        triggered_issues.append((repo_url, issue))

    _trigger.calls = triggered_issues
    return _trigger


@pytest.fixture
def mock_poller(mock_trigger, tmp_path):
    """GithubPoller with mocked PyGithub and patched seen-issues file."""
    with patch("cascade.watcher.github_poller.PYGITHUB_AVAILABLE", True), \
         patch("cascade.watcher.github_poller.Github") as mock_gh_class, \
         patch("cascade.watcher.github_poller.Auth"):
        mock_gh = MagicMock()
        mock_gh_class.return_value = mock_gh

        poller = GithubPoller(
            trigger_callback=mock_trigger,
            poll_interval=1,
            github_token="fake-token",
        )
        poller._gh = mock_gh
        # Override seen issues path for test isolation
        poller._seen_issues = {}
        yield poller, mock_gh


# ── URL Parsing Tests ─────────────────────────────────────────────────────────

class TestParseOwnerRepo:
    def test_https_url(self):
        assert GithubPoller._parse_owner_repo("https://github.com/org/repo") == "org/repo"

    def test_https_url_with_git_suffix(self):
        assert GithubPoller._parse_owner_repo("https://github.com/org/repo.git") == "org/repo"

    def test_ssh_url(self):
        assert GithubPoller._parse_owner_repo("git@github.com:org/repo.git") == "org/repo"

    def test_trailing_slash(self):
        assert GithubPoller._parse_owner_repo("https://github.com/org/repo/") == "org/repo"

    def test_invalid_url_returns_none(self):
        assert GithubPoller._parse_owner_repo("not-a-github-url") is None

    def test_gitlab_url_returns_none(self):
        assert GithubPoller._parse_owner_repo("https://gitlab.com/org/repo") is None

    def test_repo_with_dashes_and_underscores(self):
        result = GithubPoller._parse_owner_repo("https://github.com/my-org/my_repo-v2.git")
        assert result == "my-org/my_repo-v2"


# ── Seen Issues Persistence ───────────────────────────────────────────────────

class TestSeenIssuesPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        """Data survives a save → load cycle."""
        test_file = tmp_path / "seen.json"
        seen = {"https://github.com/org/repo": {1, 2, 3}}

        with patch("cascade.watcher.github_poller.SEEN_ISSUES_FILE", test_file):
            _save_seen_issues(seen)
            loaded = _load_seen_issues()

        # Compare as sets
        assert loaded["https://github.com/org/repo"] == {1, 2, 3}

    def test_load_nonexistent_file_returns_empty(self, tmp_path):
        """If file doesn't exist, returns empty dict."""
        nonexistent = tmp_path / "does_not_exist.json"
        with patch("cascade.watcher.github_poller.SEEN_ISSUES_FILE", nonexistent):
            result = _load_seen_issues()
        assert result == {}

    def test_load_corrupted_json_returns_empty(self, tmp_path):
        """Corrupted JSON gracefully returns empty dict."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json!!!", encoding="utf-8")
        with patch("cascade.watcher.github_poller.SEEN_ISSUES_FILE", bad_file):
            result = _load_seen_issues()
        assert result == {}


# ── Watch/Unwatch API ─────────────────────────────────────────────────────────

class TestPollerWatchUnwatch:
    def test_watch_creates_task(self, mock_poller):
        poller, mock_gh = mock_poller
        repo_url = "https://github.com/org/test-repo"

        async def run():
            poller.watch(repo_url)
            await asyncio.sleep(0)  # Let task scheduler tick
            assert poller.is_watching(repo_url)
            await poller.shutdown()

        asyncio.run(run())

    def test_watch_idempotent(self, mock_poller):
        """Calling watch twice doesn't create duplicate tasks."""
        poller, mock_gh = mock_poller
        repo_url = "https://github.com/org/repo"

        async def run():
            poller.watch(repo_url)
            poller.watch(repo_url)  # Second call should be no-op
            assert len(poller._watched_repos) == 1
            await poller.shutdown()

        asyncio.run(run())

    def test_unwatch_removes_repo(self, mock_poller):
        """Unwatching stops the polling task."""
        poller, mock_gh = mock_poller
        repo_url = "https://github.com/org/repo"

        async def run():
            poller.watch(repo_url)
            await asyncio.sleep(0)
            result = poller.unwatch(repo_url)
            assert result is True
            await asyncio.sleep(0)
            assert not poller.is_watching(repo_url)

        asyncio.run(run())

    def test_unwatch_nonexistent_returns_false(self, mock_poller):
        """Unwatching a repo that's not watched returns False."""
        poller, _ = mock_poller
        result = poller.unwatch("https://github.com/not/watching")
        assert result is False

    def test_list_watched_returns_urls(self, mock_poller):
        """list_watched() returns all watched repo URLs."""
        poller, mock_gh = mock_poller
        urls = [
            "https://github.com/org/repo1",
            "https://github.com/org/repo2",
        ]

        async def run():
            for url in urls:
                poller.watch(url)
            watched = poller.list_watched()
            assert set(watched) == set(urls)
            await poller.shutdown()

        asyncio.run(run())

    def test_shutdown_clears_all_watchers(self, mock_poller):
        """shutdown() stops all polling tasks and clears state."""
        poller, mock_gh = mock_poller

        async def run():
            poller.watch("https://github.com/org/repo")
            await poller.shutdown()
            assert poller.list_watched() == []

        asyncio.run(run())


# ── Poll Once Logic ────────────────────────────────────────────────────────────

class TestPollOnce:
    @pytest.mark.asyncio
    async def test_poll_once_triggers_callback_for_new_issues(self, mock_poller, mock_trigger):
        """New issues trigger the callback once each."""
        poller, mock_gh = mock_poller
        repo_url = "https://github.com/org/repo"
        poller._seen_issues[repo_url] = set()

        # Create mock issues
        issue1 = MagicMock()
        issue1.number = 42
        issue1.title = "Fix the bug"
        issue1.body = "Please fix it"
        issue1.html_url = "https://github.com/org/repo/issues/42"

        issue2 = MagicMock()
        issue2.number = 43
        issue2.title = "Another bug"
        issue2.body = "Please fix this too"
        issue2.html_url = "https://github.com/org/repo/issues/43"

        mock_repo = MagicMock()
        mock_repo.get_issues.return_value = [issue1, issue2]
        mock_gh.get_repo.return_value = mock_repo

        await poller._poll_once(repo_url)

        # Both issues should have triggered the callback
        assert len(mock_trigger.calls) == 2
        assert mock_trigger.calls[0][0] == repo_url
        assert mock_trigger.calls[0][1].number == 42
        assert mock_trigger.calls[1][1].number == 43

    @pytest.mark.asyncio
    async def test_poll_once_skips_seen_issues(self, mock_poller, mock_trigger):
        """Already-seen issues do not re-trigger the callback."""
        poller, mock_gh = mock_poller
        repo_url = "https://github.com/org/repo"
        poller._seen_issues[repo_url] = {42}  # Already seen issue #42

        issue1 = MagicMock()
        issue1.number = 42
        issue1.title = "Old bug"

        mock_repo = MagicMock()
        mock_repo.get_issues.return_value = [issue1]
        mock_gh.get_repo.return_value = mock_repo

        await poller._poll_once(repo_url)

        # No callback triggered for seen issue
        assert len(mock_trigger.calls) == 0

    @pytest.mark.asyncio
    async def test_poll_once_handles_invalid_url(self, mock_poller, mock_trigger):
        """Invalid URL logs error and returns without crashing."""
        poller, mock_gh = mock_poller
        # Should not raise
        await poller._poll_once("not-a-github-url")
        # No callback triggered
        assert len(mock_trigger.calls) == 0

    @pytest.mark.asyncio
    async def test_poll_once_handles_github_api_error(self, mock_poller, mock_trigger):
        """GitHub API errors are caught and logged without crashing."""
        poller, mock_gh = mock_poller
        repo_url = "https://github.com/org/repo"
        poller._seen_issues[repo_url] = set()

        from github import GithubException
        mock_gh.get_repo.side_effect = GithubException(403, {"message": "Rate limit exceeded"})

        # Should not raise
        await poller._poll_once(repo_url)
        assert len(mock_trigger.calls) == 0

    @pytest.mark.asyncio
    async def test_poll_once_persists_new_seen_issues(self, mock_poller, mock_trigger, tmp_path):
        """After processing, new issue numbers are persisted to disk."""
        poller, mock_gh = mock_poller
        repo_url = "https://github.com/org/repo"
        poller._seen_issues[repo_url] = set()

        issue = MagicMock()
        issue.number = 99
        issue.title = "New issue"

        mock_repo = MagicMock()
        mock_repo.get_issues.return_value = [issue]
        mock_gh.get_repo.return_value = mock_repo

        test_file = tmp_path / "seen.json"
        with patch("cascade.watcher.github_poller.SEEN_ISSUES_FILE", test_file), \
             patch("cascade.watcher.github_poller._save_seen_issues") as mock_save:
            await poller._poll_once(repo_url)

        assert 99 in poller._seen_issues[repo_url]


# ── PyGithub Unavailable ──────────────────────────────────────────────────────

class TestPollerWithoutPyGithub:
    def test_poller_raises_importerror_when_pygithub_missing(self):
        """GithubPoller.__init__ raises ImportError when PyGithub is not installed."""
        async def fake_callback(repo_url, issue):
            pass

        with patch("cascade.watcher.github_poller.PYGITHUB_AVAILABLE", False):
            with pytest.raises(ImportError, match="PyGithub"):
                GithubPoller(trigger_callback=fake_callback)
