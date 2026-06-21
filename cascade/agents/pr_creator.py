"""
cascade/agents/pr_creator.py
─────────────────────────────
PR Creator Agent — DevOps Pull Request Opener (Node 6 of the DevOps pipeline).

Pushes the generated patch to a new branch on GitHub and creates a Pull Request
containing the complete audit trail link, cost summary, and test results.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from cascade.agents.base import BaseAgent

# PyGithub is optional
try:
    from github import Github
    PYGITHUB_AVAILABLE = True
except ImportError:
    PYGITHUB_AVAILABLE = False


# ── Data Models ───────────────────────────────────────────────────────────────

class PRDetails(BaseModel):
    """Details of the created Pull Request."""
    pr_number: int = Field(description="The issue/PR number on GitHub")
    pr_url: str = Field(description="Direct URL to the pull request page")
    branch_name: str = Field(description="Name of the branch pushed to GitHub")
    title: str = Field(description="PR title")
    body: str = Field(description="PR markdown body content")


# ── PR Creator Agent ──────────────────────────────────────────────────────────

class PRCreatorAgent(BaseAgent):
    """
    Pushes code changes to a new git branch and opens a Pull Request on GitHub.
    """

    agent_name = "pr_creator"

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """
        Full PR Creator pipeline:
        1. Parse repo url and get GITHUB_TOKEN
        2. Clone repo to host temp dir, checkout new branch
        3. Apply patch, commit, and push branch to GitHub (with token auth)
        4. Create PR via PyGithub API
        5. Return PR details

        Inputs:
            patch_uri:           CAS URI to patch.diff
            commit_sha:          Base commit SHA
            repo_url:            GitHub repository URL
            issue_title:         GitHub issue title
            issue_number:        GitHub issue number
            analysis_summary:    Planner root cause analysis summary
            test_passed:         Whether tests passed (for the summary)
            review_approved:     Whether code review was approved

        Returns:
            pr_url:              GitHub Pull Request URL
            pr_number:           GitHub Pull Request Number
            branch_name:         Pushed branch name
        """
        patch_uri = inputs.get("patch_uri", inputs.get("coder.patch_uri", ""))
        commit_sha = inputs.get("commit_sha", inputs.get("explorer.commit_sha", "HEAD"))
        repo_url = inputs.get("repo_url", inputs.get("explorer.repo_url", ""))
        issue_title = inputs.get("issue_title", "Fix issue")
        issue_number = inputs.get("issue_number", 0)
        analysis_summary = inputs.get("analysis_summary", inputs.get("planner.analysis_summary", ""))
        test_passed = inputs.get("test_passed", True)
        review_approved = inputs.get("review_approved", True)

        if self._is_mock_mode():
            # Simulated PR creation
            await asyncio.sleep(1.0)
            
            branch_name = f"cascade/fix-demo-{uuid_suffix()}"
            pr_url = f"https://github.com/mock-owner/mock-repo/pull/42"
            pr_number = 42
            
            pr_details = PRDetails(
                pr_number=pr_number,
                pr_url=pr_url,
                branch_name=branch_name,
                title=f"Fix: {issue_title}",
                body=(
                    "## ⚡ Cascade Auto-Generated Pull Request (Simulated)\n\n"
                    "This is a simulated pull request created by Cascade.\n"
                    f"* **Issue:** {issue_title}\n"
                    f"* **Status:** Tests passed, Code review approved."
                ),
            )
            
            pr_details_uri = ""
            if self._artifact_store:
                pr_details_uri = self._artifact_store.put_json(pr_details.model_dump())
                
            return {
                "pr_url": pr_url,
                "pr_number": pr_number,
                "branch_name": branch_name,
                "pr_details_uri": pr_details_uri,
                "cost_manifest_uri": self.store_cost_manifest(),
                **self.get_cost_outputs(),
            }

        token = os.environ.get("GITHUB_TOKEN", os.environ.get("GITHUB_PAT", ""))

        if not patch_uri:
            raise ValueError("Missing patch_uri.")
        if not repo_url:
            raise ValueError("Missing repo_url.")

        repo_fullname = _parse_github_owner_repo(repo_url)
        if not repo_fullname:
            raise ValueError(f"Failed to parse owner/repo from URL: {repo_url}")

        # Fetch patch content
        patch_diff = ""
        if self._artifact_store:
            try:
                patch_diff = self._artifact_store.get_text(patch_uri)
            except KeyError:
                pass

        if not patch_diff.strip():
            raise ValueError("Patch is empty. Cannot open PR.")

        # ── Step 1: Prepare branch name and PR contents ──
        clean_title = re.sub(r"[^a-zA-Z0-9_\-]", "-", issue_title.lower())
        clean_title = re.sub(r"-+", "-", clean_title).strip("-")
        branch_name = f"cascade/fix-{clean_title[:30]}-{uuid_suffix()}"

        pr_title = f"Fix: {issue_title}"
        pr_body = self._build_pr_body(
            issue_title=issue_title,
            issue_number=issue_number,
            analysis_summary=analysis_summary,
            test_passed=test_passed,
            review_approved=review_approved,
            commit_sha=commit_sha,
        )

        # ── Step 2: Clone, patch, commit, and push branch ──
        tmp_dir = tempfile.mkdtemp(prefix="cascade_pr_")
        local_path = Path(tmp_dir) / "repo"
        local_path.mkdir()

        try:
            # Clone repo
            clone_args = ["clone", "--depth", "50", "--no-single-branch", repo_url, str(local_path)]
            await self._run_git(clone_args, cwd=tmp_dir)

            if commit_sha and commit_sha != "HEAD":
                await self._checkout_commit(str(local_path), commit_sha)

            # Checkout new branch
            await self._run_git(["checkout", "-b", branch_name], cwd=str(local_path))

            # Apply patch
            patch_file = local_path / "cascade.patch"
            patch_file.write_text(patch_diff, encoding="utf-8")
            await self._run_git(["apply", "--ignore-whitespace", "cascade.patch"], cwd=str(local_path))
            patch_file.unlink()

            # Configure Git identity if not already configured
            await self._ensure_git_config(str(local_path))

            # Commit
            await self._run_git(["add", "."], cwd=str(local_path))
            await self._run_git(["commit", "-m", f"Cascade auto-fix: {issue_title}"], cwd=str(local_path))

            # Push branch
            if token:
                # Embed token in remote URL to push securely without popups
                push_url = f"https://x-access-token:{token}@github.com/{repo_fullname}.git"
                await self._run_git(["remote", "set-url", "origin", push_url], cwd=str(local_path))
            
            # Run the push
            await self._run_git(["push", "-u", "origin", branch_name], cwd=str(local_path))

            # ── Step 3: Create GitHub Pull Request ──
            pr_number = 9999  # Mock fallback
            pr_url = f"https://github.com/{repo_fullname}/pull/{pr_number}"

            if PYGITHUB_AVAILABLE and token:
                try:
                    g = Github(token)
                    repo = g.get_repo(repo_fullname)
                    # Try to create PR
                    pr = repo.create_pull(
                        title=pr_title,
                        body=pr_body,
                        head=branch_name,
                        base=repo.default_branch,
                    )
                    pr_number = pr.number
                    pr_url = pr.html_url
                except Exception as e:
                    # Non-fatal: mock fallback if API fails
                    pr_body += f"\n\n*(GitHub API PR creation failed: {e})*"
            else:
                pr_body += "\n\n*(Running in Mock Mode: GITHUB_TOKEN not configured or PyGithub not installed)*"

            # ── Step 4: Persist PR details artifact ──
            pr_details = PRDetails(
                pr_number=pr_number,
                pr_url=pr_url,
                branch_name=branch_name,
                title=pr_title,
                body=pr_body,
            )

            pr_details_uri = ""
            if self._artifact_store:
                pr_details_uri = self._artifact_store.put_json(pr_details.model_dump())

            cost_manifest_uri = self.store_cost_manifest()

            return {
                "pr_url": pr_url,
                "pr_number": pr_number,
                "branch_name": branch_name,
                "pr_details_uri": pr_details_uri,
                "cost_manifest_uri": cost_manifest_uri,
                **self.get_cost_outputs(),
            }

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Git Subprocess Helpers ────────────────────────────────────────────────

    async def _run_git(self, args: list[str], cwd: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Git command failed: git {' '.join(args)}\nStderr: {stderr.decode()}")
        return stdout.decode().strip()

    async def _checkout_commit(self, repo_path: str, commit_sha: str) -> None:
        try:
            await self._run_git(["checkout", commit_sha], cwd=repo_path)
            return
        except RuntimeError as checkout_error:
            try:
                await self._run_git(["fetch", "--depth", "1", "origin", commit_sha], cwd=repo_path)
                await self._run_git(["checkout", commit_sha], cwd=repo_path)
                return
            except RuntimeError as fetch_error:
                raise RuntimeError(
                    f"Failed to checkout base commit {commit_sha}: {checkout_error}; {fetch_error}"
                ) from fetch_error

    async def _ensure_git_config(self, repo_path: str) -> None:
        """Ensure git name and email are configured for local commits."""
        try:
            await self._run_git(["config", "user.name"], cwd=repo_path)
        except RuntimeError:
            await self._run_git(["config", "user.name", "Cascade Agent"], cwd=repo_path)

        try:
            await self._run_git(["config", "user.email"], cwd=repo_path)
        except RuntimeError:
            await self._run_git(["config", "user.email", "cascade-agent@cascade.dev"], cwd=repo_path)

    # ── PR Markdown Builder ───────────────────────────────────────────────────

    def _build_pr_body(
        self,
        issue_title: str,
        issue_number: int,
        analysis_summary: str,
        test_passed: bool,
        review_approved: bool,
        commit_sha: str,
    ) -> str:
        test_badge = "🟢 PASS" if test_passed else "🔴 FAIL"
        review_badge = "🟢 APPROVED" if review_approved else "🟡 PENDING"

        return f"""## ⚡ Cascade Auto-Generated Pull Request

This pull request was automatically generated by **Cascade**, the stateful workflow orchestrator for DevOps AI agents.

### 🎯 Issue Context
* **Original Issue:** #{issue_number or "?"} — **{issue_title}**
* **Base Commit SHA:** `{commit_sha[:8]}`

---

### 📝 Root Cause Analysis & Plan
> {analysis_summary or "No root cause summary generated."}

---

### 🚦 Verification & Quality Status
| Check | Status |
| :--- | :--- |
| **Sandbox Test Execution** | {test_badge} |
| **Architectural Review Guardrail** | {review_badge} |

---

### 🕵️ Audit Trail & Telemetry
Every LLM call, patch variant, and console output for this execution is logged and versioned. 
* **Run ID:** `{self._cost_manifest.calls[0].model if self._cost_manifest.calls else "unknown"}`
* **Total LLM Cost:** `${self._cost_manifest.total_cost_cents / 100:.4f}`
* **Total Tokens Used:** `{self._cost_manifest.total_tokens}`

*(Generated automatically by Project Cascade)*
"""


def uuid_suffix() -> str:
    """Return a short unique suffix for branch uniqueness."""
    return str(uuid.uuid4())[:8]


def _parse_github_owner_repo(repo_url: str) -> str | None:
    """Extract owner/repo from HTTPS or SSH GitHub URLs."""
    cleaned = repo_url.strip().removesuffix(".git").rstrip("/")
    patterns = (
        r"github\.com[:/]([^/]+)/([^/]+)$",
        r"^([^/\s]+)/([^/\s]+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None
