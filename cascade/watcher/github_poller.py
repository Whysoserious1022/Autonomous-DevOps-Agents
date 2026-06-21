"""
cascade/watcher/github_poller.py
─────────────────────────────────
Autonomous GitHub Issue Poller.

Continuously polls GitHub repos for issues labeled "agent-task" and
automatically triggers the Cascade DevOps pipeline for each new issue.

Features:
  - Polling loop with configurable interval (default: 60s)
  - Deduplication: tracks seen issue IDs to avoid re-triggering
  - Supports multiple repos simultaneously (asyncio task per repo)
  - Publishes events to the API EventBus for real-time dashboard updates
  - Graceful shutdown on SIGINT / SIGTERM
  - Persists seen-issue state to JSON file to survive restarts

Usage (standalone):
    python -m cascade.watcher.github_poller --repo https://github.com/owner/repo

Usage (from API, managed lifecycle):
    from cascade.watcher.github_poller import GithubPoller
    poller = GithubPoller(trigger_callback=api_trigger_function)
    await poller.watch(repo_url="https://github.com/owner/repo")
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# GitHub API — uses PyGithub (already installed)
try:
    from github import Github, GithubException, Auth
    PYGITHUB_AVAILABLE = True
except ImportError:
    PYGITHUB_AVAILABLE = False
    Github = None  # type: ignore[assignment, misc]


# ── Constants ────────────────────────────────────────────────────────────────

AGENT_TASK_LABEL = "agent-task"
DEFAULT_POLL_INTERVAL_SECONDS = 60
SEEN_ISSUES_FILE = Path.home() / ".cascade" / "seen_issues.json"


# ── Seen-Issue Persistence ────────────────────────────────────────────────────

def _load_seen_issues() -> dict[str, set[int]]:
    """Load previously seen issue numbers from disk (survives restarts)."""
    SEEN_ISSUES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SEEN_ISSUES_FILE.exists():
        try:
            raw = json.loads(SEEN_ISSUES_FILE.read_text(encoding="utf-8"))
            return {repo: set(nums) for repo, nums in raw.items()}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_seen_issues(seen: dict[str, set[int]]) -> None:
    """Persist seen issue numbers to disk."""
    SEEN_ISSUES_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = {repo: sorted(nums) for repo, nums in seen.items()}
    SEEN_ISSUES_FILE.write_text(json.dumps(raw, indent=2), encoding="utf-8")


# ── Poller Class ─────────────────────────────────────────────────────────────

class GithubPoller:
    """
    Autonomous GitHub Issue Poller.

    Watches one or more GitHub repositories for issues labeled "agent-task"
    and fires a callback for each newly discovered issue.

    Args:
        trigger_callback:    Async callable(repo_url, issue) → None
                             Called once per new issue. The caller (API layer)
                             is responsible for creating a new run from it.
        poll_interval:       Seconds between API polls per repo.
        github_token:        GitHub PAT. Defaults to GITHUB_TOKEN env var.
        label:               Label to filter on. Defaults to "agent-task".
    """

    def __init__(
        self,
        trigger_callback: Callable[[str, Any], Awaitable[None]],
        poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
        github_token: Optional[str] = None,
        label: str = AGENT_TASK_LABEL,
    ) -> None:
        if not PYGITHUB_AVAILABLE:
            raise ImportError(
                "PyGithub is required for the GitHub Poller. "
                "Install with: pip install PyGithub"
            )

        self._token = github_token or os.getenv("GITHUB_TOKEN", "")
        self._label = label
        self._poll_interval = poll_interval
        self._trigger_callback = trigger_callback

        # Initialize GitHub client
        if self._token:
            auth = Auth.Token(self._token)
            self._gh = Github(auth=auth)
        else:
            self._gh = Github()  # Unauthenticated (rate limit: 60 req/hr)

        # Tracked repos: dict[repo_url → asyncio.Task]
        self._watched_repos: dict[str, asyncio.Task] = {}
        # Deduplication state
        self._seen_issues: dict[str, set[int]] = _load_seen_issues()
        # Shutdown flag
        self._shutdown = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def watch(self, repo_url: str) -> None:
        """
        Begin watching a new repository.
        Creates a new background asyncio Task for the poll loop.
        Safe to call multiple times with the same repo (idempotent).
        """
        if repo_url in self._watched_repos:
            return  # Already watching

        task = asyncio.create_task(
            self._poll_loop(repo_url),
            name=f"poller:{repo_url}",
        )
        task.add_done_callback(lambda t: self._on_task_done(repo_url, t))
        self._watched_repos[repo_url] = task
        print(f"[Poller] Started watching: {repo_url} (label={self._label!r}, interval={self._poll_interval}s)")

    def unwatch(self, repo_url: str) -> bool:
        """Stop watching a repository. Returns True if it was being watched."""
        task = self._watched_repos.pop(repo_url, None)
        if task and not task.done():
            task.cancel()
            print(f"[Poller] Stopped watching: {repo_url}")
            return True
        return False

    def list_watched(self) -> list[str]:
        """Return all currently watched repository URLs."""
        return list(self._watched_repos.keys())

    def is_watching(self, repo_url: str) -> bool:
        return repo_url in self._watched_repos

    async def shutdown(self) -> None:
        """Gracefully stop all polling tasks."""
        self._shutdown.set()
        for repo_url, task in list(self._watched_repos.items()):
            task.cancel()
        await asyncio.gather(*self._watched_repos.values(), return_exceptions=True)
        self._watched_repos.clear()
        _save_seen_issues(self._seen_issues)
        print("[Poller] All watchers stopped and state saved.")

    # ── Internal Poll Loop ────────────────────────────────────────────────────

    async def _poll_loop(self, repo_url: str) -> None:
        """Main polling loop for a single repository."""
        if repo_url not in self._seen_issues:
            self._seen_issues[repo_url] = set()

        while not self._shutdown.is_set():
            try:
                await self._poll_once(repo_url)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[Poller] Error polling {repo_url}: {exc}")

            # Wait for poll interval or shutdown
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown.wait()),
                    timeout=self._poll_interval,
                )
                break  # Shutdown was signalled
            except asyncio.TimeoutError:
                pass  # Normal: continue polling

    async def _poll_once(self, repo_url: str) -> None:
        """Perform a single poll of the repository for new agent-task issues."""
        # Parse owner/repo from URL
        owner_repo = self._parse_owner_repo(repo_url)
        if not owner_repo:
            print(f"[Poller] Cannot parse repo URL: {repo_url}")
            return

        try:
            repo = await asyncio.to_thread(self._gh.get_repo, owner_repo)
            issues = await asyncio.to_thread(
                repo.get_issues,
                state="open",
                labels=[self._label],
            )
            new_issues = []
            for issue in issues:
                if issue.number not in self._seen_issues[repo_url]:
                    new_issues.append(issue)

        except GithubException as exc:
            print(f"[Poller] GitHub API error for {repo_url}: {exc}")
            return

        if not new_issues:
            print(f"[Poller] {owner_repo}: no new '{self._label}' issues ({datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC)")
            return

        for issue in new_issues:
            print(f"[Poller] NEW issue #{issue.number}: {issue.title!r} in {owner_repo}")
            self._seen_issues[repo_url].add(issue.number)
            _save_seen_issues(self._seen_issues)

            # Fire the trigger callback (creates a new pipeline run)
            try:
                await self._trigger_callback(repo_url, issue)
            except Exception as exc:
                print(f"[Poller] Trigger callback failed for #{issue.number}: {exc}")

    def _on_task_done(self, repo_url: str, task: asyncio.Task) -> None:
        """Called when a poll task completes/errors."""
        self._watched_repos.pop(repo_url, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[Poller] Task for {repo_url} failed: {exc}")

    @staticmethod
    def _parse_owner_repo(repo_url: str) -> str | None:
        """Extract 'owner/repo' from a GitHub URL."""
        cleaned = repo_url.strip().removesuffix(".git").rstrip("/")
        match = re.search(r"github\.com[:/]([^/]+)/([^/]+)$", cleaned)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        return None


# ── Standalone CLI Entrypoint ─────────────────────────────────────────────────

async def _standalone_main() -> None:
    """Run the poller as a standalone daemon process."""
    import argparse

    parser = argparse.ArgumentParser(description="Cascade GitHub Issue Poller Daemon")
    parser.add_argument("--repo", required=True, action="append", dest="repos",
                        help="GitHub repository URL to watch (can be repeated)")
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--label", default=AGENT_TASK_LABEL,
                        help="GitHub label to filter on (default: agent-task)")
    parser.add_argument("--api-url", default="http://localhost:8000",
                        help="Cascade API URL to trigger runs on (default: http://localhost:8000)")
    args = parser.parse_args()

    import httpx

    async def http_trigger_callback(repo_url: str, issue: Any) -> None:
        """Trigger a Cascade run via the REST API."""
        payload = {
            "repo_url": repo_url,
            "issue_title": issue.title,
            "issue_body": issue.body or "No description provided.",
            "commit_sha": "",
            "test_command": "",
            "n_branches": 3,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{args.api_url}/api/runs", json=payload)
            if resp.status_code == 202:
                data = resp.json()
                print(f"[Poller] Run created: {data['run_id']} for issue #{issue.number}")
            else:
                print(f"[Poller] API trigger failed ({resp.status_code}): {resp.text}")

    poller = GithubPoller(
        trigger_callback=http_trigger_callback,
        poll_interval=args.interval,
        label=args.label,
    )

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(poller.shutdown()))
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for all signals

    print(f"\n{'='*60}")
    print(f"  CASCADE GITHUB POLLER DAEMON")
    print(f"  Label:    {args.label}")
    print(f"  Interval: {args.interval}s")
    print(f"  API:      {args.api_url}")
    print(f"{'='*60}\n")

    for repo_url in args.repos:
        poller.watch(repo_url)

    # Run until all tasks complete (i.e., shutdown is called)
    await asyncio.gather(*poller._watched_repos.values(), return_exceptions=True)
    print("[Poller] Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(_standalone_main())
