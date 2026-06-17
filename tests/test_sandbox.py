"""
tests/test_sandbox.py
──────────────────────
Integration tests for TesterAgent using real Docker execution.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
import pytest

from cascade.agents.tester import TesterAgent
from cascade.storage.artifact_store import LocalArtifactStore

@pytest.fixture
def local_repo_setup(tmp_path: Path):
    """Create a minimal git repo with a python file and tests."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Create app.py
    app_py = repo_dir / "app.py"
    app_py.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    # Create test_app.py
    test_app_py = repo_dir / "test_app.py"
    test_app_py.write_text(
        "import app\n"
        "def test_add():\n"
        "    assert app.add(2, 3) == 5\n",
        encoding="utf-8"
    )

    # Initialize git and commit files
    subprocess.run(["git", "init", str(repo_dir)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), capture_output=True, check=True)

    # Get the commit SHA
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, check=True)
    commit_sha = res.stdout.strip()

    return {
        "repo_url": str(repo_dir),
        "commit_sha": commit_sha,
        "repo_path": repo_dir,
    }

@pytest.mark.asyncio
async def test_tester_agent_passing_tests(local_repo_setup, tmp_path):
    """Verify TesterAgent runs and passes tests successfully."""
    artifact_store = LocalArtifactStore(root=tmp_path / "artifacts")
    agent = TesterAgent(artifact_store=artifact_store)

    # Create a dummy/empty patch (should pass since tests are already passing)
    patch_diff = ""
    patch_uri = artifact_store.put_text(patch_diff)

    inputs = {
        "patch_uri": patch_uri,
        "commit_sha": local_repo_setup["commit_sha"],
        "repo_url": local_repo_setup["repo_url"],
    }

    result = await agent.execute(inputs)

    assert result["test_passed"] is True
    assert result["exit_code"] == 0
    assert "test_results_uri" in result
    assert "docker_logs_uri" in result
    assert result["test_error_summary"] == ""

@pytest.mark.asyncio
async def test_tester_agent_failing_patch(local_repo_setup, tmp_path):
    """Verify TesterAgent reports failures when a patch breaks the tests."""
    artifact_store = LocalArtifactStore(root=tmp_path / "artifacts")
    agent = TesterAgent(artifact_store=artifact_store)

    # Create a breaking patch (making add() return wrong result)
    patch_diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b + 10\n"
    )
    patch_uri = artifact_store.put_text(patch_diff)

    inputs = {
        "patch_uri": patch_uri,
        "commit_sha": local_repo_setup["commit_sha"],
        "repo_url": local_repo_setup["repo_url"],
    }

    result = await agent.execute(inputs)

    assert result["test_passed"] is False
    assert result["exit_code"] != 0
    assert "test_app" in result["test_error_summary"] or "assert 15 == 5" in result["test_error_summary"]
    # Check that logs were stored
    assert result["docker_logs_uri"] != ""
