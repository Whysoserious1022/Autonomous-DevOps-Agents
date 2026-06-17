"""
cascade/agents/tester.py
─────────────────────────
Tester Agent — Sandbox Tester (Node 4 of the DevOps pipeline).

Clones the repository, applies the patch, runs tests inside an isolated
ephemeral Docker container with resource limits and no network access.

Output artifacts:
  test_results.xml  — JUnit test results XML
  docker_logs.log  — Execution logs from the test container
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from pydantic import BaseModel, Field

from cascade.agents.base import BaseAgent

# Docker is optional but required for sandbox mode
try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


# ── Data Models ───────────────────────────────────────────────────────────────

class TestResultManifest(BaseModel):
    """Structured representation of sandbox test results."""
    test_passed: bool = Field(description="True if all tests passed")
    exit_code: int = Field(description="Exit code of the test command")
    total_tests: int = Field(default=0, description="Total number of tests run")
    failures: int = Field(default=0, description="Number of test failures")
    errors: int = Field(default=0, description="Number of test errors")
    error_summary: str = Field(default="", description="Human-readable summary of failures")
    results_xml: str = Field(default="", description="Raw JUnit XML contents")
    logs: str = Field(default="", description="Raw container stdout/stderr logs")


# ── Tester Agent ──────────────────────────────────────────────────────────────

class TesterAgent(BaseAgent):
    """
    Runs the test suite for a patched repository inside an isolated Docker sandbox.
    """
    __test__ = False

    agent_name = "tester"
    DEFAULT_PYTHON_IMAGE = "cascade-tester-python:latest"

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """
        Full tester pipeline:
        1. Fetch patch and commit SHA
        2. Clone repo and apply patch
        3. Build or get testing Docker image
        4. Run test command inside isolated container
        5. Extract test results and logs
        6. Clean up resources

        Inputs:
            patch_uri:           CAS URI to patch.diff
            commit_sha:          Base commit SHA (or explorer.commit_sha)
            repo_url:            GitHub repository URL (or explorer.repo_url)
            test_command:        Optional test command (defaults to auto-detect)
            image:               Optional custom Docker image
            retry_count:         Current retry count (for tracking)

        Returns:
            test_passed:         bool
            test_results_uri:    CAS URI → test_results.xml
            docker_logs_uri:     CAS URI → docker_logs.log
            test_error_summary:  Summary of failures
            exit_code:           Process exit code
            retry_count:         Forwarded retry count
        """
        patch_uri = inputs.get("patch_uri", inputs.get("coder.patch_uri", ""))
        commit_sha = inputs.get("commit_sha", inputs.get("explorer.commit_sha", "HEAD"))
        repo_url = inputs.get("repo_url", inputs.get("explorer.repo_url", ""))
        test_command = inputs.get("test_command", "")
        custom_image = inputs.get("image", "")
        retry_count = inputs.get("retry_count", 0)

        if self._is_mock_mode():
            # Simulated test execution
            await asyncio.sleep(1.0)
            
            test_results_uri = ""
            docker_logs_uri = ""
            results_xml = "<testsuite name='pytest' tests='1' errors='0' failures='0' skipped='0' time='0.05'><testcase classname='tests.test_docs' name='test_disable_docs_in_production' time='0.05'/></testsuite>"
            logs = (
                "============================= test session starts =============================\n"
                "platform linux -- Python 3.12.3, pytest-8.1.1, pluggy-1.4.0\n"
                "rootdir: /app\n"
                "collected 1 item\n\n"
                "tests/test_docs.py .                                                     [100%]\n\n"
                "============================== 1 passed in 0.05s =============================="
            )
            
            if self._artifact_store:
                test_results_uri = self._artifact_store.put_text(results_xml)
                docker_logs_uri = self._artifact_store.put_text(logs)

            return {
                "test_passed": True,
                "test_results_uri": test_results_uri,
                "test_results_xml": results_xml,
                "docker_logs_uri": docker_logs_uri,
                "test_error_summary": "",
                "exit_code": 0,
                "retry_count": retry_count,
                "cost_manifest_uri": self.store_cost_manifest(),
                **self.get_cost_outputs(),
            }

        if not DOCKER_AVAILABLE:
            raise RuntimeError(
                "Docker Python SDK is not installed. "
                "Install with: pip install cascade[sandbox]"
            )

        if not patch_uri:
            raise ValueError("Missing patch_uri or coder.patch_uri in inputs.")
        if not repo_url:
            raise ValueError("Missing repo_url or explorer.repo_url in inputs.")

        # Load patch content from store
        patch_diff = ""
        if self._artifact_store:
            try:
                patch_diff = self._artifact_store.get_text(patch_uri)
            except KeyError as e:
                raise FileNotFoundError(f"Patch artifact not found at {patch_uri}") from e

        # ── Step 1: Clone repo and apply patch on host temporary directory ──
        tmp_dir = tempfile.mkdtemp(prefix="cascade_tester_")
        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()

        try:
            # Clone repo
            await self._clone_repo(repo_url, commit_sha, str(repo_path))

            # Apply patch
            if patch_diff.strip():
                self._apply_patch(str(repo_path), patch_diff)

            # Auto-detect language and command
            is_python = self._detect_python(repo_path)
            if not test_command:
                test_command = (
                    "pytest --tb=short -q --junit-xml=results.xml"
                    if is_python
                    else "npm test"
                )

            # ── Step 2: Ensure Docker image is ready ──
            client = docker.from_env()
            image_name = custom_image or (
                self.DEFAULT_PYTHON_IMAGE if is_python else "node:20-slim"
            )

            if is_python and not custom_image:
                self._ensure_python_image(client)

            # ── Step 3: Run container and execute tests ──
            manifest = self._run_sandbox_tests(
                client=client,
                image=image_name,
                test_command=test_command,
                repo_path=repo_path,
            )

            # ── Step 4: Persist outputs to CAS ──
            test_results_uri = ""
            docker_logs_uri = ""
            if self._artifact_store:
                if manifest.results_xml:
                    test_results_uri = self._artifact_store.put_text(manifest.results_xml)
                docker_logs_uri = self._artifact_store.put_text(manifest.logs)

            cost_manifest_uri = self.store_cost_manifest()

            return {
                "test_passed": manifest.test_passed,
                "test_results_uri": test_results_uri,
                "test_results_xml": manifest.results_xml,
                "docker_logs_uri": docker_logs_uri,
                "test_error_summary": manifest.error_summary,
                "exit_code": manifest.exit_code,
                "retry_count": retry_count,
                "cost_manifest_uri": cost_manifest_uri,
                **self.get_cost_outputs(),
            }

        finally:
            # Always clean up host temp files
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Git & Patch Helpers ───────────────────────────────────────────────────

    async def _clone_repo(self, repo_url: str, commit_sha: str, dest_path: str) -> None:
        """Clone repo using subprocess for simplicity and independence."""
        # Use shallow clone (depth 50)
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "50", repo_url, dest_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        if commit_sha and commit_sha != "HEAD":
            proc2 = await asyncio.create_subprocess_exec(
                "git", "-C", dest_path, "checkout", commit_sha,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc2.communicate()

    def _apply_patch(self, repo_path: str, patch_diff: str) -> None:
        """Apply patch.diff to the cloned repository."""
        patch_file = Path(repo_path) / "cascade.patch"
        patch_file.write_text(patch_diff, encoding="utf-8")

        result = subprocess.run(
            ["git", "apply", "--ignore-whitespace", "cascade.patch"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        # Clean up the patch file itself so it isn't part of the container
        if patch_file.exists():
            patch_file.unlink()

        if result.returncode != 0:
            raise RuntimeError(f"Failed to apply patch: {result.stderr}")

    def _detect_python(self, repo_path: Path) -> bool:
        """Return True if the repository seems to be a Python codebase."""
        if (repo_path / "pyproject.toml").exists() or (repo_path / "requirements.txt").exists():
            return True
        # Check if there are python files
        python_files = list(repo_path.rglob("*.py"))
        return len(python_files) > 0

    # ── Docker Helpers ────────────────────────────────────────────────────────

    def _ensure_python_image(self, client: docker.DockerClient) -> None:
        """Ensure the custom python test runner image exists; build if not."""
        try:
            client.images.get(self.DEFAULT_PYTHON_IMAGE)
        except docker.errors.ImageNotFound:
            # Build custom image containing pytest
            dockerfile = (
                "FROM python:3.12-slim\n"
                "RUN pip install --no-cache-dir pytest\n"
                "WORKDIR /app\n"
            )
            f = io.BytesIO(dockerfile.encode("utf-8"))
            client.images.build(fileobj=f, tag=self.DEFAULT_PYTHON_IMAGE)

    def _run_sandbox_tests(
        self,
        client: docker.DockerClient,
        image: str,
        test_command: str,
        repo_path: Path,
    ) -> TestResultManifest:
        """Run tests inside the ephemeral container and parse results."""
        # Create in-memory tarball of the workspace
        tar_bytes = self._create_tar_archive(repo_path)

        # Create container with resource limits, no network, and standard command
        container = client.containers.create(
            image=image,
            command=f"sh -c '{test_command}'",
            working_dir="/app",
            network_mode="none",
            mem_limit="1g",
            nano_cpus=1000000000,  # 1 CPU core
        )

        try:
            # Extract tarball into /app in container
            container.put_archive("/app", tar_bytes)

            # Run container
            container.start()

            # Wait for execution (timeout 120s)
            result = container.wait(timeout=120)
            exit_code = result.get("StatusCode", 0)

            # Get logs
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")

            # Try to get results.xml
            results_xml = ""
            try:
                bits, _ = container.get_archive("/app/results.xml")
                tar_stream = io.BytesIO()
                for chunk in bits:
                    tar_stream.write(chunk)
                tar_stream.seek(0)
                with tarfile.open(fileobj=tar_stream) as tar:
                    results_xml = tar.extractfile("results.xml").read().decode("utf-8", errors="replace")
            except Exception:
                # results.xml may not exist if tests crashed or used custom unittest
                pass

            # Parse results and format error summary
            return self._parse_test_outputs(exit_code, logs, results_xml)

        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass

    def _create_tar_archive(self, src_dir: Path) -> bytes:
        """Create deterministic in-memory tarball."""
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            for file_path in src_dir.rglob("*"):
                # Skip .git directory to keep tar archive minimal
                if ".git" in file_path.parts:
                    continue
                if file_path.is_file():
                    rel_path = file_path.relative_to(src_dir)
                    tar.add(str(file_path), arcname=str(rel_path).replace("\\", "/"))
        tar_stream.seek(0)
        return tar_stream.getvalue()

    def _parse_test_outputs(self, exit_code: int, logs: str, results_xml: str) -> TestResultManifest:
        """Parse test results from JUnit XML or fallback to container logs."""
        test_passed = (exit_code == 0)
        total_tests = 0
        failures = 0
        errors = 0
        error_summary = ""

        if results_xml:
            try:
                root = ET.fromstring(results_xml)
                # Parse suite attributes
                total_tests = int(root.get("tests", 0))
                failures = int(root.get("failures", 0))
                errors = int(root.get("errors", 0))

                failure_details = []
                for testcase in root.findall(".//testcase"):
                    name = testcase.get("name", "Unknown test")
                    classname = testcase.get("classname", "")
                    full_name = f"{classname}.{name}" if classname else name

                    for fail in testcase.findall("failure"):
                        msg = fail.get("message", "Test failed")
                        failure_details.append(f"FAIL: {full_name}\nDetail: {msg}")
                    for err in testcase.findall("error"):
                        msg = err.get("message", "Error during execution")
                        failure_details.append(f"ERROR: {full_name}\nDetail: {msg}")

                if failure_details:
                    error_summary = "\n\n".join(failure_details[:10])
                elif exit_code != 0:
                    error_summary = f"Exit code {exit_code} (non-zero), but no failure recorded in XML."
            except Exception as e:
                error_summary = f"Failed to parse JUnit XML: {e}"

        # Fallback to logs if JUnit XML not present or didn't contain errors
        if not error_summary and exit_code != 0:
            # Get last 20 lines of logs as summary
            log_lines = logs.splitlines()
            last_lines = log_lines[-20:]
            error_summary = "Test execution failed (exit code {}):\n\n{}".format(
                exit_code, "\n".join(last_lines)
            )

        return TestResultManifest(
            test_passed=test_passed,
            exit_code=exit_code,
            total_tests=total_tests,
            failures=failures,
            errors=errors,
            error_summary=error_summary,
            results_xml=results_xml,
            logs=logs,
        )
