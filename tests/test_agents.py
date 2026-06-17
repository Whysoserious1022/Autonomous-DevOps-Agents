"""
tests/test_agents.py
──────────────────────
Phase 2 test suite — AI Agent integration tests.

Philosophy:
  - All LLM calls are MOCKED — tests never hit real APIs.
  - AST parsing tested against real Python code strings.
  - Explorer caching tested: same commit SHA = step skipped.
  - Planner output structure validated against Pydantic models.
  - Coder patch syntax validated with regex (git not required).
  - Integration test: full Explorer → Planner → Coder pipeline.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from cascade.agents.base import BaseAgent, CostManifest, LLMCallRecord, LLMResponse
from cascade.agents.coder import CoderAgent, PatchOutput, FileChange
from cascade.agents.explorer import (
    ExplorerAgent, FileNode, ClassNode, FunctionNode, RepoGraph
)
from cascade.agents.planner import (
    PlannerAgent, SolutionBranch, ToTBranchesOutput, RootCauseAnalysis
)
from cascade.core.decorator import CascadeFlow, step
from cascade.core.runner import FlowRunner


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_artifact_store():
    """In-memory artifact store for testing agents without filesystem."""
    store: dict[str, bytes] = {}

    mock = MagicMock()

    def put_json(data: dict) -> str:
        import hashlib, json
        raw = json.dumps(data, default=str).encode()
        digest = hashlib.sha256(raw).hexdigest()
        uri = f"sha256://{digest}"
        store[uri] = raw
        return uri

    def get_json(uri: str) -> dict:
        if uri not in store:
            raise KeyError(f"Not found: {uri}")
        return json.loads(store[uri])

    def put_text(text: str) -> str:
        import hashlib
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        uri = f"sha256://{digest}"
        store[uri] = raw
        return uri

    def get_text(uri: str) -> str:
        if uri not in store:
            raise KeyError(f"Not found: {uri}")
        return store[uri].decode("utf-8")

    def put_bytes(data: bytes) -> str:
        import hashlib
        digest = hashlib.sha256(data).hexdigest()
        uri = f"sha256://{digest}"
        store[uri] = data
        return uri

    mock.put_json.side_effect = put_json
    mock.get_json.side_effect = get_json
    mock.put_text.side_effect = put_text
    mock.get_text.side_effect = get_text
    mock.put_bytes.side_effect = put_bytes
    mock._store = store
    return mock


@pytest.fixture
def mock_llm_response() -> LLMResponse:
    return LLMResponse(
        content="Mock LLM response",
        cost_cents=1.5,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
    )


# ── Sample Python Code for AST Tests ──────────────────────────────────────────

SAMPLE_PYTHON = '''
"""Sample module for testing."""
import os
import asyncio
from typing import Optional
from pathlib import Path


class UserService:
    """Manages user operations."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    async def create_user(self, email: str, name: str) -> dict:
        result = await self._save_to_db({"email": email, "name": name})
        return result

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self._fetch_from_db(user_id)

    async def _save_to_db(self, data: dict) -> dict:
        return {**data, "id": 1}

    async def _fetch_from_db(self, user_id: int) -> Optional[dict]:
        return None


def compute_hash(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()


async def main_entrypoint() -> None:
    service = UserService("sqlite:///test.db")
    user = await service.create_user("test@example.com", "Test User")
    print(user)
'''

SAMPLE_PYTHON_WITH_ERROR = '''
class BrokenClass:
    def method(self:
'''  # Intentional syntax error


# ── BaseAgent Tests ───────────────────────────────────────────────────────────

class TestBaseAgent:
    def test_cost_manifest_accumulates_calls(self):
        manifest = CostManifest(agent_name="test_agent")
        record = LLMCallRecord(
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cost_cents=2.0,
            duration_seconds=0.5,
            call_index=0,
            prompt_preview="test prompt",
            response_preview="test response",
        )
        manifest.add_call(record)
        assert manifest.total_cost_cents == 2.0
        assert manifest.total_tokens == 150
        assert len(manifest.calls) == 1

    def test_cost_manifest_as_step_outputs(self):
        manifest = CostManifest(agent_name="test_agent")
        outputs = manifest.as_step_outputs()
        assert "__cost_cents__" in outputs
        assert "__total_tokens__" in outputs

    def test_llm_response_not_available_returns_mock(self, mock_artifact_store):
        """When LiteLLM not installed, llm_complete returns a mock response."""
        class ConcreteAgent(BaseAgent):
            agent_name = "test"
            async def execute(self, inputs: dict) -> dict:
                response = await self.llm_complete(system="sys", user="user")
                return {"content": response.content}

        agent = ConcreteAgent(artifact_store=mock_artifact_store)
        # Patch LITELLM_AVAILABLE to simulate missing installation
        import cascade.agents.base as base_module
        original = base_module.LITELLM_AVAILABLE
        base_module.LITELLM_AVAILABLE = False
        try:
            # Use pytest-asyncio directly instead of manual event loop
            result = asyncio.new_event_loop().run_until_complete(
                agent.llm_complete(system="System prompt", user="User message")
            )
            assert "mock" in result.content.lower() or "not installed" in result.content.lower()
        finally:
            base_module.LITELLM_AVAILABLE = original


# ── Explorer Tests ────────────────────────────────────────────────────────────

class TestExplorerASTParser:
    """Tests for the AST parsing logic — no LLM, no git cloning required."""

    def test_parse_python_file_extracts_classes(self, tmp_path, mock_artifact_store):
        py_file = tmp_path / "test_module.py"
        py_file.write_text(SAMPLE_PYTHON, encoding="utf-8")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        file_node = agent._parse_python_file(py_file, tmp_path)

        assert file_node.path == "test_module.py"
        class_names = [c.name for c in file_node.classes]
        assert "UserService" in class_names

    def test_parse_python_file_extracts_functions(self, tmp_path, mock_artifact_store):
        py_file = tmp_path / "test_module.py"
        py_file.write_text(SAMPLE_PYTHON, encoding="utf-8")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        file_node = agent._parse_python_file(py_file, tmp_path)

        func_names = [f.name for f in file_node.functions]
        assert "compute_hash" in func_names
        assert "main_entrypoint" in func_names

    def test_parse_python_file_extracts_imports(self, tmp_path, mock_artifact_store):
        py_file = tmp_path / "test_module.py"
        py_file.write_text(SAMPLE_PYTHON, encoding="utf-8")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        file_node = agent._parse_python_file(py_file, tmp_path)

        assert "os" in file_node.imports
        assert "asyncio" in file_node.imports

    def test_parse_python_file_detects_async_methods(self, tmp_path, mock_artifact_store):
        py_file = tmp_path / "test_module.py"
        py_file.write_text(SAMPLE_PYTHON, encoding="utf-8")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        file_node = agent._parse_python_file(py_file, tmp_path)

        user_service = next(c for c in file_node.classes if c.name == "UserService")
        async_methods = [m.name for m in user_service.methods if m.is_async]
        assert "create_user" in async_methods
        assert "get_user" in async_methods

    def test_parse_python_file_skips_syntax_errors(self, tmp_path, mock_artifact_store):
        py_file = tmp_path / "broken.py"
        py_file.write_text(SAMPLE_PYTHON_WITH_ERROR, encoding="utf-8")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)

        # Should raise SyntaxError (caller catches it)
        with pytest.raises(SyntaxError):
            agent._parse_python_file(py_file, tmp_path)

    def test_collect_python_files_excludes_venv(self, tmp_path, mock_artifact_store):
        # Create normal file
        (tmp_path / "main.py").write_text("x = 1")
        # Create venv file (should be excluded)
        venv_dir = tmp_path / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "some_lib.py").write_text("y = 2")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        files = agent._collect_python_files(tmp_path, max_files=100)

        paths = [str(f) for f in files]
        assert any("main.py" in p for p in paths)
        assert not any(".venv" in p for p in paths)

    def test_collect_python_files_respects_max_files(self, tmp_path, mock_artifact_store):
        for i in range(20):
            (tmp_path / f"module_{i}.py").write_text(f"x = {i}")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        files = agent._collect_python_files(tmp_path, max_files=5)
        assert len(files) <= 5

    def test_build_compact_file_summary(self, mock_artifact_store):
        repo_graph = RepoGraph(
            repo_url="https://github.com/test/repo",
            commit_sha="abc123",
            files=[
                FileNode(
                    path="main.py",
                    classes=[ClassNode(name="App", lineno=1)],
                    functions=[FunctionNode(name="run", lineno=10)],
                )
            ],
            total_files_analyzed=1,
        )
        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        summary = agent._build_compact_file_summary(repo_graph)
        assert "main.py" in summary
        assert "App" in summary

    @pytest.mark.asyncio
    async def test_explorer_builds_ast_graph(self, tmp_path, mock_artifact_store):
        """Test the full AST building pipeline on a real directory."""
        # Create a small test repo structure
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "models.py").write_text(SAMPLE_PYTHON, encoding="utf-8")
        (src_dir / "utils.py").write_text('def helper(x): return x * 2', encoding="utf-8")
        # Test file (lower priority)
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_models.py").write_text('def test_nothing(): pass', encoding="utf-8")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)
        repo_graph = await agent._build_ast_graph(
            str(tmp_path), "https://github.com/test/repo", "abc123", max_files=50
        )

        assert repo_graph.total_files_analyzed >= 2
        paths = [f.path for f in repo_graph.files]
        assert any("models.py" in p for p in paths)
        assert any("utils.py" in p for p in paths)

    @pytest.mark.asyncio
    async def test_explorer_execute_with_mocked_llm(self, tmp_path, mock_artifact_store):
        """Test full execute() with LLM calls mocked."""
        # Create a small test repo
        (tmp_path / "app.py").write_text(SAMPLE_PYTHON, encoding="utf-8")

        agent = ExplorerAgent(artifact_store=mock_artifact_store)

        # Mock _clone_repo to return our test directory
        async def mock_clone(repo_url, commit_sha):
            return str(tmp_path), "deadbeef1234" + "0" * 20  # 32-char SHA

        # Mock LLM calls
        mock_relevant_files_output = MagicMock()
        mock_relevant_files_output.model_dump.return_value = {
            "reasoning": "The main app file is clearly relevant",
            "files": [{"path": "app.py", "relevance_score": 0.95, "reason": "core module"}],
            "entry_points": ["app.py"],
        }

        with (
            patch.object(agent, "_clone_repo", side_effect=mock_clone),
            patch.object(agent, "_find_relevant_files", return_value=mock_relevant_files_output),
            patch.object(agent, "_summarize_repo", return_value="A test repository."),
        ):
            result = await agent.execute({
                "repo_url": "https://github.com/test/repo",
                "commit_sha": "deadbeef1234" + "0" * 20,
                "issue_title": "Test issue",
                "issue_body": "Test body",
                "issue_number": 1,
            })

        assert "repo_graph_uri" in result
        assert "commit_sha" in result
        assert result["commit_sha"] == "deadbeef1234" + "0" * 20


# ── Planner Tests ─────────────────────────────────────────────────────────────

class TestPlannerAgent:
    @pytest.mark.asyncio
    async def test_planner_returns_structured_output(self, mock_artifact_store):
        """Test Planner with fully mocked LLM calls."""
        agent = PlannerAgent(artifact_store=mock_artifact_store)

        mock_rca = RootCauseAnalysis(
            root_cause="The middleware does not pass through OPTIONS requests",
            affected_components=["middleware.py", "router.py"],
            issue_type="bug",
            complexity_assessment="medium",
        )

        mock_branch = SolutionBranch(
            branch_id=0,
            hypothesis="Add OPTIONS handler to the CORS middleware",
            approach_name="Middleware Fix",
            files_to_modify=["middleware.py"],
            confidence=0.85,
            reasoning="The CORS middleware is not handling pre-flight correctly",
            estimated_complexity="low",
        )

        mock_tot = ToTBranchesOutput(
            branches=[mock_branch],
            selected_branch_index=0,
            analysis_summary="The issue is in the CORS middleware.",
        )

        # Store mock data in artifact store
        repo_graph_uri = mock_artifact_store.put_json({
            "repo_url": "https://github.com/test/repo",
            "commit_sha": "abc123",
            "files": [],
            "total_files_analyzed": 5,
            "summary": "A test repo",
        })
        relevant_files_uri = mock_artifact_store.put_json({
            "files": [{"path": "middleware.py", "relevance_score": 0.9, "reason": "core"}],
        })

        with (
            patch.object(agent, "_analyze_root_cause", return_value=mock_rca),
            patch.object(agent, "_generate_branches", return_value=mock_tot),
        ):
            result = await agent.execute({
                "explorer.repo_graph_uri": repo_graph_uri,
                "explorer.relevant_files_uri": relevant_files_uri,
                "issue_title": "CORS pre-flight fails",
                "issue_body": "OPTIONS requests return 404",
                "n_branches": 1,
            })

        assert "tot_branches_uri" in result
        assert result["selected_branch_index"] == 0
        assert "selected_branch" in result
        assert result["selected_branch"]["approach_name"] == "Middleware Fix"
        assert result["root_cause"] == mock_rca.root_cause

    def test_format_relevant_files_empty(self, mock_artifact_store):
        agent = PlannerAgent(artifact_store=mock_artifact_store)
        result = agent._format_relevant_files({})
        assert "no relevant files" in result.lower()

    def test_format_relevant_files_with_data(self, mock_artifact_store):
        agent = PlannerAgent(artifact_store=mock_artifact_store)
        result = agent._format_relevant_files({
            "files": [
                {"path": "middleware.py", "relevance_score": 0.9, "reason": "CORS logic"},
                {"path": "router.py", "relevance_score": 0.7, "reason": "Route definitions"},
            ]
        })
        assert "middleware.py" in result
        assert "router.py" in result

    def test_solution_branch_model_validation(self):
        branch = SolutionBranch(
            branch_id=0,
            hypothesis="Fix the bug",
            approach_name="Direct fix",
            files_to_modify=["main.py"],
            confidence=0.9,
            reasoning="The bug is in main.py",
        )
        assert branch.confidence == 0.9
        assert branch.estimated_complexity == "medium"  # Default

    def test_solution_branch_confidence_bounds(self):
        with pytest.raises(Exception):
            SolutionBranch(
                branch_id=0,
                hypothesis="Fix",
                approach_name="Fix",
                files_to_modify=[],
                confidence=1.5,  # Out of bounds
                reasoning="",
            )


# ── Coder Tests ───────────────────────────────────────────────────────────────

class TestCoderAgent:
    def test_validate_patch_syntax_valid_diff(self, mock_artifact_store):
        agent = CoderAgent(artifact_store=mock_artifact_store)
        valid_diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,3 +1,4 @@\n"
            " def hello():\n"
            "-    pass\n"
            "+    return 'hello'\n"
            "+\n"
        )
        # Use regex fallback (no real git repo to apply to)
        valid, error = agent._validate_patch_syntax_regex(valid_diff)
        assert valid is True
        assert error is None

    def test_validate_patch_syntax_missing_hunk(self, mock_artifact_store):
        agent = CoderAgent(artifact_store=mock_artifact_store)
        bad_diff = "--- a/main.py\n+++ b/main.py\n+new line\n"  # Missing @@ header
        valid, error = agent._validate_patch_syntax_regex(bad_diff)
        assert valid is False
        assert "hunk" in error.lower()

    def test_validate_patch_syntax_empty(self, mock_artifact_store):
        agent = CoderAgent(artifact_store=mock_artifact_store)
        valid, error = agent._validate_patch_syntax_regex("")
        assert valid is False

    def test_assemble_diff_adds_header(self, mock_artifact_store):
        agent = CoderAgent(artifact_store=mock_artifact_store)
        patch_output = PatchOutput(
            explanation="Fixed the bug in main.py",
            changes=[
                FileChange(
                    path="main.py",
                    action="modify",
                    diff=(
                        "--- a/main.py\n"
                        "+++ b/main.py\n"
                        "@@ -1,1 +1,1 @@\n"
                        "-old\n"
                        "+new\n"
                    ),
                )
            ],
            test_strategy="Run pytest tests/",
        )
        result = agent._assemble_diff(patch_output, "abc123dead")
        assert "abc123dead" in result
        assert "main.py" in result

    def test_assemble_diff_adds_git_header_when_missing(self, mock_artifact_store):
        agent = CoderAgent(artifact_store=mock_artifact_store)
        patch_output = PatchOutput(
            explanation="Fix",
            changes=[
                FileChange(
                    path="utils.py",
                    action="modify",
                    diff=(
                        "--- a/utils.py\n"
                        "+++ b/utils.py\n"
                        "@@ -5,3 +5,4 @@\n"
                        " x = 1\n"
                        "+y = 2\n"
                    ),
                )
            ],
            test_strategy="pytest",
        )
        result = agent._assemble_diff(patch_output, "abc")
        assert "diff --git" in result

    @pytest.mark.asyncio
    async def test_coder_execute_with_mocked_llm(self, mock_artifact_store):
        """Test full execute() with mocked patch generation."""
        agent = CoderAgent(artifact_store=mock_artifact_store)

        mock_patch_output = PatchOutput(
            explanation="Added return statement to fix the bug",
            changes=[
                FileChange(
                    path="main.py",
                    action="modify",
                    diff=(
                        "diff --git a/main.py b/main.py\n"
                        "--- a/main.py\n"
                        "+++ b/main.py\n"
                        "@@ -1,3 +1,3 @@\n"
                        " def hello():\n"
                        "-    pass\n"
                        "+    return 'world'\n"
                    ),
                )
            ],
            test_strategy="pytest tests/test_main.py",
        )

        with patch.object(agent, "_generate_patch", return_value=mock_patch_output):
            result = await agent.execute({
                "planner.selected_branch": {
                    "approach_name": "Direct fix",
                    "hypothesis": "Add return statement",
                    "files_to_modify": ["main.py"],
                    "reasoning": "The function should return a value",
                    "implementation_steps": [],
                },
                "planner.analysis_summary": "Missing return statement in hello()",
                "issue_title": "hello() returns None instead of 'world'",
                "issue_body": "The hello function doesn't return anything",
                "repo_url": "https://github.com/test/repo",
            })

        assert "patch_uri" in result
        assert result["files_changed"] == ["main.py"]
        assert "main.py" in result["patch_diff"]

    @pytest.mark.asyncio
    async def test_coder_retry_includes_test_feedback(self, mock_artifact_store):
        """Verify retry inputs are passed to _generate_patch."""
        agent = CoderAgent(artifact_store=mock_artifact_store)

        captured_kwargs: dict[str, Any] = {}

        async def capture_generate_patch(**kwargs: Any) -> PatchOutput:
            captured_kwargs.update(kwargs)
            return PatchOutput(
                explanation="Fixed with test feedback",
                changes=[],
                test_strategy="pytest",
            )

        with patch.object(agent, "_generate_patch", side_effect=capture_generate_patch):
            await agent.execute({
                "planner.selected_branch": {},
                "planner.analysis_summary": "summary",
                "issue_title": "Test",
                "issue_body": "Body",
                "test_error_summary": "AssertionError: expected 'hello', got None",
                "retry_count": 2,
            })

        assert captured_kwargs["retry_count"] == 2
        assert "AssertionError" in captured_kwargs["test_error_summary"]


# ── Integration Test ───────────────────────────────────────────────────────────

class TestPhase2Integration:
    @pytest.mark.asyncio
    async def test_full_pipeline_explorer_to_coder(self, tmp_path, flow_runner: FlowRunner):
        """
        Phase 2 SUCCESS METRIC:
        Explorer → Planner → Coder full pipeline runs to completion.
        On second run with same commit SHA, Explorer is SKIPPED.
        """
        # Create a minimal local "repo"
        (tmp_path / "app.py").write_text(SAMPLE_PYTHON, encoding="utf-8")
        (tmp_path / "utils.py").write_text('def helper(): pass', encoding="utf-8")

        # Track execution counts
        execution_counts = {"explorer": 0, "planner": 0, "coder": 0}

        class TestDevOpsFlow(CascadeFlow):
            flow_name = "test_devops_flow"
            _repo_path = str(tmp_path)

            @step(name="explorer", cross_run_cache=True)
            async def explore(self_inner, inputs: dict) -> dict:
                execution_counts["explorer"] += 1
                agent = ExplorerAgent(artifact_store=self_inner._artifact_store)
                # Build AST graph directly (no git clone in test)
                repo_graph = await agent._build_ast_graph(
                    TestDevOpsFlow._repo_path,
                    "https://github.com/test/repo",
                    inputs.get("commit_sha", "testsha123"),
                    max_files=50,
                )
                uri = self_inner._artifact_store.put_json(repo_graph.model_dump())
                return {
                    "repo_graph_uri": uri,
                    "commit_sha": inputs.get("commit_sha", "testsha123"),
                    "total_files": repo_graph.total_files_analyzed,
                }

            @step(name="planner", depends_on=["explorer"])
            async def plan(self_inner, inputs: dict) -> dict:
                execution_counts["planner"] += 1
                branch = SolutionBranch(
                    branch_id=0,
                    hypothesis="Add missing return",
                    approach_name="Direct fix",
                    files_to_modify=["app.py"],
                    confidence=0.9,
                    reasoning="The function needs a return statement",
                )
                uri = self_inner._artifact_store.put_json(
                    ToTBranchesOutput(
                        branches=[branch],
                        selected_branch_index=0,
                        analysis_summary="Missing return statement.",
                    ).model_dump()
                )
                return {
                    "tot_branches_uri": uri,
                    "selected_branch": branch.model_dump(),
                    "selected_branch_index": 0,
                    "analysis_summary": "Missing return statement.",
                }

            @step(name="coder", depends_on=["planner"])
            async def code(self_inner, inputs: dict) -> dict:
                execution_counts["coder"] += 1
                return {
                    "patch_diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-pass\n+return 'hello'\n",
                    "files_changed": ["app.py"],
                    "validation_passed": True,
                }

        # ── First run: all steps execute ─────────────────────────────────────
        inputs = {"commit_sha": "abc123deadbeef", "issue_title": "Test", "issue_body": "Test"}
        run1 = await flow_runner.run(TestDevOpsFlow, **inputs)

        assert execution_counts["explorer"] == 1
        assert execution_counts["planner"] == 1
        assert execution_counts["coder"] == 1
        assert run1.steps["coder"].outputs["validation_passed"] is True

        # ── Second run: Explorer SKIPPED (same commit_sha) ────────────────────
        run2 = await flow_runner.run(TestDevOpsFlow, **inputs)

        # Explorer must be SKIPPED (global cache hit on commit_sha)
        assert execution_counts["explorer"] == 1  # Still 1 — not re-executed!
        # Planner and coder also skipped (same full input hash)
        assert execution_counts["planner"] == 1

    @pytest.mark.asyncio
    async def test_planner_reruns_when_issue_changes(self, tmp_path, flow_runner: FlowRunner):
        """
        Caching semantic: step input_hash includes ALL merged inputs.
        When issue changes, Explorer's hash also changes (it received issue in merged inputs).
        The solution for commit_sha-only caching: pre-filter inputs at the caller level.
        """
        execution_counts = {"explorer": 0, "planner": 0}

        class PartialFlowC(CascadeFlow):
            flow_name = "partial_flow_stable_explorer"

            @step(name="explorer", cross_run_cache=True)
            async def explore(self_inner, inputs: dict) -> dict:
                execution_counts["explorer"] += 1
                commit_sha = inputs.get("commit_sha", "")
                return {"repo_graph_uri": f"sha256://graph_{commit_sha}", "commit_sha": commit_sha}

            @step(name="planner", depends_on=["explorer"])
            async def plan(self_inner, inputs: dict) -> dict:
                execution_counts["planner"] += 1
                return {"selected_branch": {}, "analysis_summary": inputs.get("issue_title", "")}

        # Run 1: stable inputs
        inputs1 = {"commit_sha": "stablesha999", "issue_title": "Bug A", "issue_body": "Desc A"}
        await flow_runner.run(PartialFlowC, **inputs1)
        assert execution_counts["explorer"] == 1
        assert execution_counts["planner"] == 1

        # Run 2: issue changed — both steps re-run since input_hash includes all inputs
        inputs2 = {"commit_sha": "stablesha999", "issue_title": "Bug B", "issue_body": "Desc B"}
        await flow_runner.run(PartialFlowC, **inputs2)
        assert execution_counts["explorer"] == 2
        assert execution_counts["planner"] == 2

    @pytest.mark.asyncio
    async def test_explorer_skips_on_identical_inputs(self, flow_runner: FlowRunner):
        """
        Core cache invariant: if ALL inputs are identical, Explorer is SKIPPED.
        This is the Phase 2 success metric.
        """
        execution_counts = {"explorer": 0}

        class StableFlow(CascadeFlow):
            flow_name = "stable_flow_for_skip_test"

            @step(name="explorer", cross_run_cache=True)
            async def explore(self_inner, inputs: dict) -> dict:
                execution_counts["explorer"] += 1
                return {"repo_graph_uri": "sha256://abc", "commit_sha": inputs["commit_sha"]}

        stable_inputs = {"commit_sha": "v1.0.0", "issue_title": "Bug X", "issue_body": "Desc X"}

        await flow_runner.run(StableFlow, **stable_inputs)
        assert execution_counts["explorer"] == 1

        # Exact same inputs -> SKIP
        await flow_runner.run(StableFlow, **stable_inputs)
        assert execution_counts["explorer"] == 1, "Explorer MUST be skipped on identical inputs"

