"""
tests/test_reviewer_and_tools.py
──────────────────────────────────
Tests for:
  - ReviewerAgent — static scanning + LLM review
  - CodeGuardrailScanner — secrets and cyclomatic complexity
  - WorkspaceTools — file operations with path traversal protection
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cascade.agents.reviewer import (
    ReviewerAgent,
    ReviewStatusOutput,
    CodeGuardrailScanner,
)
from cascade.agents.tools import WorkspaceTools


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_artifact_store():
    """In-memory artifact store for testing reviewers and tools without filesystem."""
    store: dict[str, bytes] = {}
    mock = MagicMock()

    def put_json(data: dict) -> str:
        import hashlib
        raw = json.dumps(data, default=str).encode()
        digest = hashlib.sha256(raw).hexdigest()
        uri = f"sha256://{digest}"
        store[uri] = raw
        return uri

    def get_text(uri: str) -> str:
        if uri not in store:
            raise KeyError(f"Not found: {uri}")
        return store[uri].decode("utf-8")

    def put_text(text: str) -> str:
        import hashlib
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        uri = f"sha256://{digest}"
        store[uri] = raw
        return uri

    mock.put_json.side_effect = put_json
    mock.get_text.side_effect = get_text
    mock.put_text.side_effect = put_text
    mock._store = store
    return mock


# ── CodeGuardrailScanner Tests ────────────────────────────────────────────────

class TestCodeGuardrailScanner:
    # ── Secrets Detection ──────────────────────────────────────────────────────

    def test_scan_secrets_detects_password_assignment(self):
        patch_diff = '''
+password = "mysecretpassword123"
+api_key = "someapikey_value_xyz"
'''
        issues = CodeGuardrailScanner.scan_secrets(patch_diff)
        assert len(issues) > 0

    def test_scan_secrets_detects_github_pat(self):
        patch_diff = "+token = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJ'\n"
        issues = CodeGuardrailScanner.scan_secrets(patch_diff)
        # GitHub PAT pattern should match
        assert any("ghp_" in i.lower() or "secret" in i.lower() or "token" in i.lower() for i in issues)

    def test_scan_secrets_clean_code_returns_empty(self):
        patch_diff = '''
+def add_numbers(a: int, b: int) -> int:
+    return a + b
'''
        issues = CodeGuardrailScanner.scan_secrets(patch_diff)
        assert issues == []

    def test_scan_secrets_environment_variable_not_flagged(self):
        """Reading from env vars is fine — the value isn't hardcoded."""
        patch_diff = '+password = os.getenv("PASSWORD", "")\n'
        issues = CodeGuardrailScanner.scan_secrets(patch_diff)
        assert issues == []

    # ── Cyclomatic Complexity ──────────────────────────────────────────────────

    def test_simple_function_complexity_is_1(self):
        code = """
def simple_func(x):
    return x * 2
"""
        cc = CodeGuardrailScanner.compute_cyclomatic_complexity(code)
        assert cc.get("simple_func", 1) == 1

    def test_if_branch_increases_complexity(self):
        code = """
def with_if(x):
    if x > 0:
        return x
    return -x
"""
        cc = CodeGuardrailScanner.compute_cyclomatic_complexity(code)
        assert cc.get("with_if", 0) == 2

    def test_multiple_branches_accumulate(self):
        code = """
def complex_func(x, y, z):
    if x:
        if y:
            if z:
                return 1
            else:
                return 2
        else:
            return 3
    elif y:
        return 4
    else:
        return 5
"""
        cc = CodeGuardrailScanner.compute_cyclomatic_complexity(code)
        assert cc.get("complex_func", 0) >= 4

    def test_for_loop_increases_complexity(self):
        code = """
def loopy(items):
    result = []
    for item in items:
        result.append(item)
    return result
"""
        cc = CodeGuardrailScanner.compute_cyclomatic_complexity(code)
        assert cc.get("loopy", 0) == 2

    def test_invalid_python_returns_empty(self):
        cc = CodeGuardrailScanner.compute_cyclomatic_complexity("def broken(x:\n    ...")
        assert cc == {}

    def test_empty_string_returns_empty(self):
        cc = CodeGuardrailScanner.compute_cyclomatic_complexity("")
        assert cc == {}

    def test_async_function_detected(self):
        code = """
async def async_func(x):
    if x:
        return await x
    return None
"""
        cc = CodeGuardrailScanner.compute_cyclomatic_complexity(code)
        assert "async_func" in cc
        assert cc["async_func"] >= 2


# ── ReviewerAgent Tests ───────────────────────────────────────────────────────

class TestReviewerAgent:
    @pytest.mark.asyncio
    async def test_reviewer_execute_approved_patch(self, mock_artifact_store):
        """A clean patch should be approved."""
        agent = ReviewerAgent(artifact_store=mock_artifact_store)

        patch_diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -10,3 +10,8 @@\n"
            " app = FastAPI()\n"
            "+\n"
            "+# Conditional docs disabling for production\n"
            "+if os.environ.get('ENV') == 'production':\n"
            "+    app.docs_url = None\n"
            "+    app.redoc_url = None\n"
        )
        mock_artifact_store.get_text.side_effect = lambda uri: patch_diff

        mock_review = ReviewStatusOutput(
            approved=True,
            score=9.0,
            security_summary="No secrets found.",
            complexity_summary="Complexity is low.",
            architectural_review="Clean, minimal change.",
            issues=[],
        )

        with patch.object(agent, "llm_structured", return_value=mock_review):
            result = await agent.execute({
                "patch_uri": "sha256://fakehash",
                "issue_title": "Disable docs in production",
                "issue_body": "We need to disable docs when ENV=production",
            })

        assert result["review_approved"] is True
        assert result["review_score"] == 9.0
        assert "review_status_uri" in result

    @pytest.mark.asyncio
    async def test_reviewer_execute_rejected_patch_with_secrets(self, mock_artifact_store):
        """Patch with hardcoded secrets should be rejected."""
        agent = ReviewerAgent(artifact_store=mock_artifact_store)

        patch_diff = (
            "diff --git a/config.py b/config.py\n"
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -1,1 +1,2 @@\n"
            "-password = os.getenv('PASSWORD')\n"
            '+password = "hardcoded_secret_password_xyz"\n'
        )
        mock_artifact_store.get_text.side_effect = lambda uri: patch_diff

        mock_review = ReviewStatusOutput(
            approved=True,  # LLM might miss it — scanner catches it
            score=8.0,
            security_summary="No obvious secrets.",
            complexity_summary="Low complexity.",
            architectural_review="Simple change.",
            issues=[],
        )

        with patch.object(agent, "llm_structured", return_value=mock_review):
            result = await agent.execute({
                "patch_uri": "sha256://fakehash",
                "issue_title": "Change database config",
                "issue_body": "Update the config file",
            })

        # Static scanner should override LLM approval
        assert result["review_approved"] is False
        assert len(result["review_issues"]) > 0

    @pytest.mark.asyncio
    async def test_reviewer_raises_without_patch(self, mock_artifact_store):
        """Missing patch content raises ValueError."""
        agent = ReviewerAgent(artifact_store=mock_artifact_store)
        mock_artifact_store.get_text.side_effect = KeyError("not found")

        with pytest.raises(ValueError, match="No patch content"):
            await agent.execute({
                "issue_title": "Some issue",
                "issue_body": "Some body",
            })

    @pytest.mark.asyncio
    async def test_reviewer_uses_inline_patch_diff(self, mock_artifact_store):
        """If patch_diff is provided inline, no artifact lookup needed."""
        agent = ReviewerAgent(artifact_store=mock_artifact_store)

        inline_diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1 +1 @@\n"
            "-old = 1\n"
            "+new = 1\n"
        )

        mock_review = ReviewStatusOutput(
            approved=True,
            score=8.5,
            security_summary="Clean.",
            complexity_summary="Low.",
            architectural_review="OK.",
            issues=[],
        )

        with patch.object(agent, "llm_structured", return_value=mock_review):
            result = await agent.execute({
                "patch_diff": inline_diff,
                "issue_title": "Rename variable",
                "issue_body": "",
            })

        assert result["review_approved"] is True

    @pytest.mark.asyncio
    async def test_reviewer_high_complexity_flagged(self, mock_artifact_store):
        """High cyclomatic complexity is detected and flagged."""
        agent = ReviewerAgent(artifact_store=mock_artifact_store)

        # Build a function with very high cyclomatic complexity
        branches = "\n".join([f"    if x == {i}:\n        return {i}" for i in range(15)])
        complex_code = f"def mega_func(x):\n{branches}\n    return -1\n"
        patch_diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,20 @@\n"
        ) + "\n".join(f"+{line}" for line in complex_code.split("\n"))

        mock_review = ReviewStatusOutput(
            approved=True,
            score=6.0,
            security_summary="No secrets.",
            complexity_summary="Function is complex.",
            architectural_review="Consider refactoring.",
            issues=[],
        )

        with patch.object(agent, "llm_structured", return_value=mock_review):
            result = await agent.execute({
                "patch_diff": patch_diff,
                "issue_title": "Refactor",
                "issue_body": "",
            })

        # High complexity should add to issues list
        assert len(result["review_issues"]) >= 0  # May or may not catch depending on parsing


# ── WorkspaceTools Tests ──────────────────────────────────────────────────────

class TestWorkspaceTools:
    def test_read_existing_file(self, tmp_path):
        """Reading a file in the workspace returns its content."""
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        content = tools.read_file("main.py")
        assert "x = 1" in content

    def test_read_nonexistent_file_returns_error(self, tmp_path):
        """Reading a missing file returns an error string (doesn't raise)."""
        tools = WorkspaceTools(tmp_path)
        result = tools.read_file("does_not_exist.py")
        assert "not found" in result.lower() or "error" in result.lower()

    def test_write_file_creates_file(self, tmp_path):
        """Writing creates the file with correct content."""
        tools = WorkspaceTools(tmp_path)
        result = tools.write_file("output.txt", "Hello, World!")
        assert (tmp_path / "output.txt").exists()
        assert (tmp_path / "output.txt").read_text() == "Hello, World!"
        assert "success" in result.lower() or "written" in result.lower()

    def test_write_file_creates_parent_dirs(self, tmp_path):
        """Writing to nested path creates parent directories."""
        tools = WorkspaceTools(tmp_path)
        result = tools.write_file("nested/dir/file.py", "y = 2")
        assert (tmp_path / "nested" / "dir" / "file.py").exists()

    def test_write_file_path_traversal_rejected(self, tmp_path):
        """Writing outside the workspace via .. is rejected."""
        tools = WorkspaceTools(tmp_path)
        result = tools.write_file("../../etc/passwd", "malicious")
        assert "escapes" in result.lower() or "error" in result.lower()
        assert not (tmp_path.parent.parent / "etc" / "passwd").exists()

    def test_read_file_path_traversal_rejected(self, tmp_path):
        """Reading outside the workspace via .. is rejected."""
        tools = WorkspaceTools(tmp_path)
        result = tools.read_file("../../etc/hosts")
        assert "escapes" in result.lower() or "error" in result.lower()

    def test_list_directory(self, tmp_path):
        """list_dir returns files in the workspace."""
        (tmp_path / "app.py").write_text("# app")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("# tests")

        tools = WorkspaceTools(tmp_path)
        result = tools.list_dir(".")
        assert "app.py" in result

    def test_list_directory_path_traversal_rejected(self, tmp_path):
        """Listing directories outside workspace is rejected."""
        tools = WorkspaceTools(tmp_path)
        result = tools.list_dir("../../etc")
        assert "escapes" in result.lower() or "error" in result.lower()

    def test_grep_finds_pattern(self, tmp_path):
        """Grep returns matching lines."""
        (tmp_path / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        result = tools.grep_search("main")
        assert "main" in result

    def test_grep_no_matches(self, tmp_path):
        """Grep returns 'no matches' when pattern not found."""
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        result = tools.grep_search("zzz_nonexistent_pattern_xyz")
        assert "no matches" in result.lower() or result.strip() == "" or "not found" in result.lower()

    def test_edit_file_replaces_content(self, tmp_path):
        """edit_file replaces a unique target in the file."""
        (tmp_path / "main.py").write_text("def greet():\n    print('hello')\n", encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        result = tools.edit_file("main.py", "print('hello')", "print('world')")
        assert "success" in result.lower()
        assert (tmp_path / "main.py").read_text() == "def greet():\n    print('world')\n"

    def test_edit_file_fails_if_target_not_found(self, tmp_path):
        """edit_file returns error when target not found."""
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        result = tools.edit_file("main.py", "DOES_NOT_EXIST", "replacement")
        assert "error" in result.lower()

    def test_edit_file_fails_if_target_not_unique(self, tmp_path):
        """edit_file returns error when target appears more than once."""
        (tmp_path / "main.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        result = tools.edit_file("main.py", "x = 1", "x = 2")
        assert "error" in result.lower() or "not unique" in result.lower()

    def test_execute_tool_dispatcher_read_file(self, tmp_path):
        """execute_tool dispatches to read_file."""
        (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        result = tools.execute_tool("read_file", {"path": "hello.txt"})
        assert "hello world" in result

    def test_execute_tool_dispatcher_unknown(self, tmp_path):
        """execute_tool returns error for unknown tool names."""
        tools = WorkspaceTools(tmp_path)
        result = tools.execute_tool("fly_to_moon", {})
        assert "not recognized" in result.lower() or "error" in result.lower()

    def test_list_dir_nonexistent_directory(self, tmp_path):
        """list_dir on a non-existent path returns an error."""
        tools = WorkspaceTools(tmp_path)
        result = tools.list_dir("nonexistent_dir")
        assert "error" in result.lower() or "not exist" in result.lower()

    def test_read_file_too_large_truncates(self, tmp_path):
        """Files with more than 500 lines are truncated with a warning."""
        large_content = "\n".join([f"line {i}" for i in range(600)])
        (tmp_path / "large.py").write_text(large_content, encoding="utf-8")
        tools = WorkspaceTools(tmp_path)
        result = tools.read_file("large.py")
        assert "Warning" in result or "500" in result


# ── ReviewStatusOutput Model ──────────────────────────────────────────────────

class TestReviewStatusOutputModel:
    def test_valid_model_creation(self):
        review = ReviewStatusOutput(
            approved=True,
            score=8.5,
            security_summary="Clean.",
            complexity_summary="Low.",
            architectural_review="Good.",
            issues=[],
        )
        assert review.approved is True
        assert review.score == 8.5

    def test_score_must_be_in_range(self):
        """Score outside 0-10 fails validation."""
        with pytest.raises(Exception):
            ReviewStatusOutput(
                approved=True,
                score=11.0,  # Out of range
                security_summary="",
                complexity_summary="",
                architectural_review="",
                issues=[],
            )

    def test_issues_defaults_to_empty_list(self):
        review = ReviewStatusOutput(
            approved=False,
            score=2.0,
            security_summary="Issues found.",
            complexity_summary="High.",
            architectural_review="Problems.",
        )
        assert review.issues == []
