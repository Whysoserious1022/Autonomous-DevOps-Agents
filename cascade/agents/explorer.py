"""
cascade/agents/explorer.py
───────────────────────────
Explorer Agent — The Context Builder (Node 1 of the DevOps pipeline).

Responsibilities:
  1. Clone the target repository at the specific commit SHA.
  2. Build an Abstract Syntax Tree (AST) graph of the relevant Python files.
  3. Map the dependency graph: Class → methods → called_functions → imports.
  4. Use LiteLLM to identify which files are most relevant to the GitHub issue.
  5. Produce a RepoGraph JSON artifact (stored in CAS by SHA → deduplicated).

The Cascade Cache Superpower:
  Cache key = SHA-256(commit_sha + issue_body_hash + source_code_of_step).
  If the same commit_sha is encountered in ANY future run (even for a different
  issue on the same repo), the EXPLORER STEP IS SKIPPED ENTIRELY, saving
  ~200K tokens of repo analysis.

Output artifacts:
  repo_graph.json  — JSON knowledge graph of the codebase
  relevant_files.json — LLM-ranked list of files to modify
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from cascade.agents.base import BaseAgent

# GitPython is optional
try:
    import git
    GITPYTHON_AVAILABLE = True
except ImportError:
    GITPYTHON_AVAILABLE = False


# ── Data Models ───────────────────────────────────────────────────────────────

class FunctionNode(BaseModel):
    """A single function or method in the AST graph."""
    name: str
    lineno: int
    is_async: bool = False
    calls: list[str] = Field(default_factory=list)      # Functions this calls
    decorators: list[str] = Field(default_factory=list)


class ClassNode(BaseModel):
    """A class in the AST graph with its methods."""
    name: str
    lineno: int
    bases: list[str] = Field(default_factory=list)      # Superclasses
    methods: list[FunctionNode] = Field(default_factory=list)
    decorators: list[str] = Field(default_factory=list)


class FileNode(BaseModel):
    """AST analysis of a single Python file."""
    path: str                                            # Relative to repo root
    language: str = "python"
    imports: list[str] = Field(default_factory=list)    # Imported modules
    classes: list[ClassNode] = Field(default_factory=list)
    functions: list[FunctionNode] = Field(default_factory=list)
    line_count: int = 0


class RepoGraph(BaseModel):
    """The full knowledge graph of a repository at a specific commit."""
    repo_url: str
    commit_sha: str
    files: list[FileNode] = Field(default_factory=list)
    total_files_analyzed: int = 0
    languages_detected: list[str] = Field(default_factory=list)
    summary: str = ""  # LLM-generated 1-paragraph repo summary


class RelevantFilesOutput(BaseModel):
    """LLM-structured output: which files are most relevant to the issue."""
    reasoning: str = Field(description="Why these files are relevant")
    files: list[dict[str, Any]] = Field(
        description="List of {path, relevance_score, reason} dicts"
    )
    entry_points: list[str] = Field(
        default_factory=list,
        description="Likely entry points for the fix (e.g., main function files)"
    )


# ── Explorer Agent ────────────────────────────────────────────────────────────

class ExplorerAgent(BaseAgent):
    """
    Builds a comprehensive knowledge graph of a repository for a given issue.

    The output RepoGraph is the primary input to the Planner agent.
    """

    agent_name = "explorer"

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """
        Full explorer pipeline:
        1. Clone repo → 2. Build AST → 3. LLM relevance ranking → 4. Store artifacts

        Inputs (from @step decorator):
            repo_url:     GitHub repo URL (e.g., https://github.com/fastapi/fastapi)
            commit_sha:   Specific commit to analyze (ensures cache key is stable)
            issue_title:  GitHub issue title
            issue_body:   GitHub issue description
            issue_number: GitHub issue number

        Returns:
            repo_graph_uri:      CAS URI → repo_graph.json
            relevant_files_uri:  CAS URI → relevant_files.json
            commit_sha:          Confirmed commit SHA
            total_files:         Number of Python files analyzed
        """
        repo_url = inputs.get("repo_url", "")
        commit_sha = inputs.get("commit_sha", "")
        issue_title = inputs.get("issue_title", "No title")
        issue_body = inputs.get("issue_body", "No description")
        issue_number = inputs.get("issue_number", 0)
        max_files = inputs.get("max_files", 150)  # Analysis cap

        # ── Mock Mode: skip real cloning and AST analysis ─────────────────────
        if self._is_mock_mode():
            import asyncio as _asyncio
            await _asyncio.sleep(1.5)  # Simulate realistic execution time

            simulated_commit_sha = commit_sha or "abc123def456789"
            mock_repo_graph = RepoGraph(
                repo_url=repo_url,
                commit_sha=simulated_commit_sha,
                files=[
                    FileNode(
                        path="app.py",
                        language="python",
                        imports=["fastapi", "os", "uvicorn"],
                        classes=[
                            ClassNode(
                                name="AppFactory",
                                lineno=10,
                                bases=[],
                                methods=[
                                    FunctionNode(
                                        name="create_app",
                                        lineno=12,
                                        is_async=False,
                                        calls=["FastAPI"],
                                    )
                                ],
                            )
                        ],
                        functions=[
                            FunctionNode(name="get_app", lineno=5, is_async=False, calls=["create_app"])
                        ],
                        line_count=80,
                    ),
                    FileNode(
                        path="utils.py",
                        language="python",
                        imports=["os"],
                        classes=[],
                        functions=[FunctionNode(name="get_env", lineno=3, is_async=False, calls=[])],
                        line_count=25,
                    ),
                ],
                total_files_analyzed=2,
                languages_detected=["python"],
                summary=(
                    "A FastAPI web application with standard routing, middleware, "
                    "and a utility module. The app.py creates the FastAPI instance and "
                    "mounts all routes. The docs UI (Swagger/ReDoc) is currently "
                    "always enabled regardless of environment."
                ),
            )
            mock_relevant = RelevantFilesOutput(
                reasoning="app.py contains the FastAPI app instantiation and is the primary file to change.",
                files=[
                    {"path": "app.py", "relevance_score": 0.95, "reason": "Contains FastAPI app definition"},
                    {"path": "utils.py", "relevance_score": 0.65, "reason": "Helper utilities used by app"},
                ],
                entry_points=["app.py"],
            )

            repo_graph_uri = ""
            relevant_files_uri = ""
            if self._artifact_store:
                repo_graph_uri = self._artifact_store.put_json(mock_repo_graph.model_dump())
                relevant_files_uri = self._artifact_store.put_json(mock_relevant.model_dump())

            cost_manifest_uri = self.store_cost_manifest()
            return {
                "repo_graph_uri": repo_graph_uri,
                "relevant_files_uri": relevant_files_uri,
                "cost_manifest_uri": cost_manifest_uri,
                "commit_sha": simulated_commit_sha,
                "total_files": mock_repo_graph.total_files_analyzed,
                "languages": mock_repo_graph.languages_detected,
                "repo_summary": mock_repo_graph.summary[:500],
                "relevant_files": [f["path"] for f in mock_relevant.files],
                **self.get_cost_outputs(),
            }

        # ── Step 1: Clone or fetch repo ───────────────────────────────────────
        repo_path, actual_commit_sha = await self._clone_repo(repo_url, commit_sha)

        try:
            # ── Step 2: Build AST graph ───────────────────────────────────────
            repo_graph = await self._build_ast_graph(
                repo_path, repo_url, actual_commit_sha, max_files=max_files
            )

            # ── Step 3: LLM relevance ranking ─────────────────────────────────
            relevant_files = await self._find_relevant_files(
                repo_graph, issue_title, issue_body
            )

            # ── Step 4: LLM repo summary ──────────────────────────────────────
            repo_graph.summary = await self._summarize_repo(repo_graph, issue_title)

            # ── Step 5: Store artifacts in CAS ────────────────────────────────
            repo_graph_uri = self._artifact_store.put_json(repo_graph.model_dump())
            relevant_files_uri = self._artifact_store.put_json(relevant_files.model_dump())
            cost_manifest_uri = self.store_cost_manifest()

            return {
                "repo_graph_uri": repo_graph_uri,
                "relevant_files_uri": relevant_files_uri,
                "cost_manifest_uri": cost_manifest_uri,
                "commit_sha": actual_commit_sha,
                "total_files": repo_graph.total_files_analyzed,
                "languages": repo_graph.languages_detected,
                "repo_summary": repo_graph.summary[:500],
                **self.get_cost_outputs(),
            }

        finally:
            # Always clean up the cloned repo
            if repo_path and Path(repo_path).exists():
                shutil.rmtree(repo_path, ignore_errors=True)

    # ── Cloning ───────────────────────────────────────────────────────────────

    async def _clone_repo(
        self, repo_url: str, target_sha: str
    ) -> tuple[str, str]:
        """
        Clone the repository at the given commit SHA.

        Uses a shallow clone (depth=1) for speed, then fetches the specific
        commit if needed. Falls back to full clone for private repos.

        Returns:
            (local_path, actual_commit_sha)
        """
        tmp_dir = tempfile.mkdtemp(prefix="cascade_explorer_")

        if not GITPYTHON_AVAILABLE:
            # Fallback: use subprocess git
            return await self._clone_with_subprocess(repo_url, target_sha, tmp_dir)

        try:
            import asyncio
            # Run blocking git operations in thread pool
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._clone_sync(repo_url, target_sha, tmp_dir)
            )
            return result
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to clone {repo_url}: {e}") from e

    def _clone_sync(self, repo_url: str, target_sha: str, tmp_dir: str) -> tuple[str, str]:
        """Synchronous git clone (runs in thread pool)."""
        repo_path = Path(tmp_dir) / "repo"

        if target_sha:
            # Shallow clone then checkout specific SHA
            repo = git.Repo.clone_from(
                repo_url,
                str(repo_path),
                depth=50,  # Fetch enough history to find the commit
                no_single_branch=True,
            )
            try:
                repo.git.checkout(target_sha)
                actual_sha = repo.head.commit.hexsha
            except Exception:
                actual_sha = repo.head.commit.hexsha
        else:
            repo = git.Repo.clone_from(repo_url, str(repo_path), depth=1)
            actual_sha = repo.head.commit.hexsha

        return str(repo_path), actual_sha

    async def _clone_with_subprocess(
        self, repo_url: str, target_sha: str, tmp_dir: str
    ) -> tuple[str, str]:
        """Subprocess-based git clone fallback."""
        import asyncio
        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()

        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "50", repo_url, str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        if target_sha:
            proc2 = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_path), "checkout", target_sha,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc2.communicate()

        # Get actual SHA
        proc3 = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_path), "rev-parse", "HEAD",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc3.communicate()
        actual_sha = stdout.decode().strip() or target_sha

        return str(repo_path), actual_sha

    # ── AST Analysis ──────────────────────────────────────────────────────────

    async def _build_ast_graph(
        self,
        repo_path: str,
        repo_url: str,
        commit_sha: str,
        max_files: int = 150,
    ) -> RepoGraph:
        """
        Walk the repository and build a knowledge graph via Python's ast module.

        Skips: test files, __pycache__, .venv, node_modules, migrations.
        Prioritizes: core source files, models, routes, services.
        """
        import asyncio
        loop = asyncio.get_event_loop()

        # Run in thread pool (ast parsing is CPU-bound)
        repo_graph = await loop.run_in_executor(
            None,
            lambda: self._parse_repo_sync(repo_path, repo_url, commit_sha, max_files)
        )
        return repo_graph

    def _parse_repo_sync(
        self,
        repo_path: str,
        repo_url: str,
        commit_sha: str,
        max_files: int,
    ) -> RepoGraph:
        """Synchronous AST parsing (CPU-bound, runs in thread pool)."""
        root = Path(repo_path)
        file_nodes: list[FileNode] = []
        languages: set[str] = set()

        # Collect Python files, sorted by priority
        python_files = self._collect_python_files(root, max_files)

        for py_file in python_files:
            try:
                file_node = self._parse_python_file(py_file, root)
                file_nodes.append(file_node)
                languages.add("python")
            except (SyntaxError, UnicodeDecodeError, OSError):
                # Skip unparseable files
                pass

        return RepoGraph(
            repo_url=repo_url,
            commit_sha=commit_sha,
            files=file_nodes,
            total_files_analyzed=len(file_nodes),
            languages_detected=sorted(languages),
        )

    def _collect_python_files(self, root: Path, max_files: int) -> list[Path]:
        """Collect Python files, prioritizing core source over tests/migrations."""
        SKIP_DIRS = {
            "__pycache__", ".venv", "venv", "env", "node_modules",
            ".git", "migrations", "alembic", "dist", "build", ".eggs",
        }
        SKIP_PATTERNS = {"test_", "_test.py", "conftest.py", "setup.py"}

        priority: list[Path] = []   # Core source files
        secondary: list[Path] = []  # Test/support files

        for py_file in root.rglob("*.py"):
            # Skip excluded directories
            if any(part in SKIP_DIRS for part in py_file.parts):
                continue
            name = py_file.name
            is_test = any(p in name for p in SKIP_PATTERNS)
            if is_test:
                secondary.append(py_file)
            else:
                priority.append(py_file)

        # Sort: shorter paths first (core files tend to be at root level)
        priority.sort(key=lambda p: (len(p.parts), p.name))
        secondary.sort(key=lambda p: (len(p.parts), p.name))

        combined = priority + secondary
        return combined[:max_files]

    def _parse_python_file(self, py_file: Path, root: Path) -> FileNode:
        """Parse a single Python file into a FileNode using the ast module."""
        source = py_file.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(py_file))
        rel_path = str(py_file.relative_to(root))

        imports: list[str] = []
        classes: list[ClassNode] = []
        functions: list[FunctionNode] = []

        for node in ast.walk(tree):
            # Imports
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.append(module)

        # Top-level classes and functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(self._parse_class(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(self._parse_function(node))

        return FileNode(
            path=rel_path.replace("\\", "/"),
            imports=list(dict.fromkeys(imports))[:30],  # Deduplicate, limit
            classes=classes,
            functions=functions,
            line_count=len(source.splitlines()),
        )

    def _parse_class(self, node: ast.ClassDef) -> ClassNode:
        """Parse a class definition into a ClassNode."""
        methods: list[FunctionNode] = []
        for item in ast.iter_child_nodes(node):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(self._parse_function(item))

        return ClassNode(
            name=node.name,
            lineno=node.lineno,
            bases=[ast.unparse(b) for b in node.bases],
            methods=methods,
            decorators=[ast.unparse(d) for d in node.decorator_list],
        )

    def _parse_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> FunctionNode:
        """Parse a function/method into a FunctionNode, extracting call graph."""
        calls: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                try:
                    call_name = ast.unparse(child.func)
                    # Only include short names (avoid long chained calls)
                    if len(call_name) < 60:
                        calls.append(call_name)
                except Exception:  # noqa: BLE001
                    pass

        return FunctionNode(
            name=node.name,
            lineno=node.lineno,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            calls=list(dict.fromkeys(calls))[:20],
            decorators=[ast.unparse(d) for d in node.decorator_list],
        )

    # ── LLM Steps ─────────────────────────────────────────────────────────────

    async def _find_relevant_files(
        self,
        repo_graph: RepoGraph,
        issue_title: str,
        issue_body: str,
    ) -> RelevantFilesOutput:
        """
        Use LLM to rank files by relevance to the GitHub issue.
        This dramatically narrows the context window for the Planner.
        """
        # Build a compact representation of the file graph
        file_summary = self._build_compact_file_summary(repo_graph)

        system = (
            "You are an expert software engineer performing root cause analysis. "
            "Given a list of source files from a repository and a GitHub issue, "
            "identify which files are most likely to need modification to fix the issue. "
            "Be precise and conservative — prefer fewer, more relevant files."
        )
        user = (
            f"GitHub Issue #{repo_graph.commit_sha[:8]}\n"
            f"Title: {issue_title}\n\n"
            f"Description:\n{issue_body[:2000]}\n\n"
            f"Repository Files (path: key classes/functions):\n{file_summary}"
        )

        return await self.llm_structured(
            system=system,
            user=user,
            output_model=RelevantFilesOutput,
        )

    async def _summarize_repo(self, repo_graph: RepoGraph, issue_title: str) -> str:
        """Generate a 1-paragraph LLM summary of the repository."""
        file_summary = self._build_compact_file_summary(repo_graph, max_files=30)
        response = await self.llm_complete(
            system=(
                "You are a technical writer. Summarize this repository in 2-3 sentences. "
                "Focus on: what it does, its main components, and the technology stack."
            ),
            user=(
                f"Repository: {repo_graph.repo_url}\n"
                f"Files analyzed: {repo_graph.total_files_analyzed}\n"
                f"Issue context: {issue_title}\n\n"
                f"Top files:\n{file_summary}"
            ),
            max_tokens=300,
        )
        return response.content

    def _build_compact_file_summary(self, repo_graph: RepoGraph, max_files: int = 80) -> str:
        """Build a compact text representation of the repo for LLM context."""
        lines: list[str] = []
        for file_node in repo_graph.files[:max_files]:
            parts = [file_node.path]
            if file_node.classes:
                class_names = [c.name for c in file_node.classes[:5]]
                parts.append(f"classes: {', '.join(class_names)}")
            if file_node.functions:
                func_names = [f.name for f in file_node.functions[:8]]
                parts.append(f"funcs: {', '.join(func_names)}")
            lines.append("  " + " | ".join(parts))
        return "\n".join(lines)
