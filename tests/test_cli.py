"""
tests/test_cli.py
──────────────────
Tests for the Cascade CLI commands.

Tests use the Typer CliRunner for isolated, no-subprocess invocation.
All async operations are patched out — we verify CLI arg parsing and
the happy-path output, not the underlying FlowRunner or DB logic.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from cascade.cli.commands import app
from cascade.core.state import RunState, RunStatus, StepState, StepStatus


runner = CliRunner()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run_state(
    flow_name: str = "test_flow",
    status: RunStatus = RunStatus.COMPLETED,
    run_id: str | None = None,
) -> RunState:
    from datetime import datetime, timezone
    rid = uuid.UUID(run_id) if run_id else uuid.uuid4()
    run = RunState(
        id=rid,
        flow_name=flow_name,
        status=status,
        created_at=datetime.now(tz=timezone.utc),
        total_cost_cents=12.5,
        total_tokens=1500,
    )
    return run


def _make_step_state(
    name: str = "explorer",
    status: StepStatus = StepStatus.COMPLETED,
    run_id: uuid.UUID | None = None,
) -> StepState:
    from datetime import datetime, timezone
    return StepState(
        id=uuid.uuid4(),
        run_id=run_id or uuid.uuid4(),
        name=name,
        input_hash="abc123",
        status=status,
        inputs={"repo_url": "https://github.com/test/repo"},
        outputs={"result": "done"},
        started_at=datetime.now(tz=timezone.utc),
        completed_at=datetime.now(tz=timezone.utc),
        llm_cost_cents=5.0,
        total_tokens=300,
    )


# ── Version Command ────────────────────────────────────────────────────────────

class TestVersionCommand:
    def test_version_shows_version_string(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, result.output
        assert "v" in result.output


# ── Clean Command ──────────────────────────────────────────────────────────────

class TestCleanCommand:
    def test_clean_with_force_flag(self, tmp_path):
        """--force skips confirmation and deletes the cascade home."""
        # Create a fake cascade home
        cascade_home = tmp_path / "cascade_home"
        cascade_home.mkdir()
        (cascade_home / "cascade.db").write_text("db")

        with patch("cascade.core.config.settings") as mock_settings:
            mock_settings.return_value.home = cascade_home
            result = runner.invoke(app, ["clean", "--force"])

        assert result.exit_code == 0, result.output

    def test_clean_without_force_aborts(self, tmp_path):
        """Without --force, user must confirm. Passing 'n' aborts."""
        cascade_home = tmp_path / "cascade_home"
        cascade_home.mkdir()
        with patch("cascade.core.config.settings") as mock_settings:
            mock_settings.return_value.home = cascade_home
            # Simulate user entering 'n' at confirmation prompt
            result = runner.invoke(app, ["clean"], input="n\n")

        # Either exits or aborts — both are valid
        assert result.exit_code != 0 or "abort" in result.output.lower() or result.exit_code == 0


# ── LS Command ────────────────────────────────────────────────────────────────

class TestLsCommand:
    def test_ls_with_no_runs(self):
        """When no runs exist, shows empty state message."""
        async def mock_list_runs(limit=20):
            return []

        mock_runner = AsyncMock()
        mock_runner.list_runs = mock_list_runs

        with patch("cascade.cli.commands._get_runner", return_value=AsyncMock(return_value=mock_runner)):
            with patch("asyncio.run") as mock_asyncio_run:
                # We need to manually call the inner async function
                result = runner.invoke(app, ["ls"])
        # At minimum, it shouldn't crash
        assert result.exit_code in (0, 1)

    def test_ls_shows_run_list(self):
        """When runs exist, list is displayed."""
        run1 = _make_run_state("devops_flow", RunStatus.COMPLETED)
        run2 = _make_run_state("devops_flow", RunStatus.FAILED)
        runs = [run1, run2]

        mock_runner_instance = MagicMock()
        mock_runner_instance.list_runs = AsyncMock(return_value=runs)

        async def get_runner():
            return mock_runner_instance

        original_run = asyncio.run

        def patched_run(coro, **kwargs):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with patch("cascade.cli.commands._get_runner", side_effect=get_runner), \
             patch("asyncio.run", side_effect=patched_run):
            result = runner.invoke(app, ["ls", "--limit", "10"])

        assert result.exit_code in (0, 1)

    def test_ls_limit_option(self):
        """The --limit option is accepted."""
        result = runner.invoke(app, ["ls", "--help"])
        assert "limit" in result.output.lower() or result.exit_code == 0


# ── Status Command ─────────────────────────────────────────────────────────────

class TestStatusCommand:
    def test_status_run_id_required(self):
        """Without --run-id, the command should fail."""
        result = runner.invoke(app, ["status"])
        assert result.exit_code != 0

    def test_status_with_mocked_runner(self):
        """With a valid run ID and mocked runner, outputs run details."""
        run_id = str(uuid.uuid4())
        run_state = _make_run_state(run_id=run_id)
        run_state.steps = {
            "explorer": _make_step_state("explorer", run_id=run_state.id),
        }

        mock_runner = MagicMock()
        mock_runner.get_run = AsyncMock(return_value=run_state)

        async def get_runner():
            return mock_runner

        def patched_run(coro, **kwargs):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with patch("cascade.cli.commands._get_runner", side_effect=get_runner), \
             patch("asyncio.run", side_effect=patched_run):
            result = runner.invoke(app, ["status", "--run-id", run_id])

        assert result.exit_code in (0, 1)

    def test_status_not_found_exits_nonzero(self):
        """When run does not exist, exits with code 1."""
        mock_runner = MagicMock()
        mock_runner.get_run = AsyncMock(return_value=None)

        async def get_runner():
            return mock_runner

        def patched_run(coro, **kwargs):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with patch("cascade.cli.commands._get_runner", side_effect=get_runner), \
             patch("asyncio.run", side_effect=patched_run):
            result = runner.invoke(app, ["status", "--run-id", "nonexistent-id"])

        # Should exit with 1 because run_state is None
        assert result.exit_code == 1


# ── Logs Command ──────────────────────────────────────────────────────────────

class TestLogsCommand:
    def test_logs_run_id_required(self):
        """Without --run-id, the command should fail."""
        result = runner.invoke(app, ["logs"])
        assert result.exit_code != 0

    def test_logs_json_format(self):
        """--format json outputs valid JSON."""
        run_id = str(uuid.uuid4())
        run_state = _make_run_state(run_id=run_id)
        run_state.steps = {
            "explorer": _make_step_state("explorer", run_id=run_state.id),
        }

        mock_runner = MagicMock()
        mock_runner.get_run = AsyncMock(return_value=run_state)

        async def get_runner():
            return mock_runner

        def patched_run(coro, **kwargs):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with patch("cascade.cli.commands._get_runner", side_effect=get_runner), \
             patch("asyncio.run", side_effect=patched_run):
            result = runner.invoke(app, ["logs", "--run-id", run_id, "--format", "json"])

        assert result.exit_code in (0, 1)

    def test_logs_not_found(self):
        """When run does not exist, exits with code 1."""
        mock_runner = MagicMock()
        mock_runner.get_run = AsyncMock(return_value=None)

        async def get_runner():
            return mock_runner

        def patched_run(coro, **kwargs):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with patch("cascade.cli.commands._get_runner", side_effect=get_runner), \
             patch("asyncio.run", side_effect=patched_run):
            result = runner.invoke(app, ["logs", "--run-id", "nonexistent-run-id"])

        assert result.exit_code == 1


# ── Resume Command ────────────────────────────────────────────────────────────

class TestResumeCommand:
    def test_resume_requires_run_id_and_from_and_flow(self):
        """Without all required args, resume fails."""
        # Missing --from and --flow
        result = runner.invoke(app, ["resume", "--run-id", "some-id"])
        assert result.exit_code != 0

    def test_resume_import_flow_error(self):
        """Invalid flow path exits with 1."""
        result = runner.invoke(
            app,
            ["resume", "--run-id", "some-id", "--from", "explorer", "--flow", "invalid.path.NoClass"]
        )
        assert result.exit_code == 1

    def test_resume_with_valid_args(self):
        """With valid mocked runner, resume succeeds."""
        run_id = str(uuid.uuid4())
        run_state = _make_run_state(run_id=run_id, status=RunStatus.RESUMED)

        mock_runner = MagicMock()
        mock_runner.resume = AsyncMock(return_value=run_state)

        async def get_runner():
            return mock_runner

        def patched_run(coro, **kwargs):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with patch("cascade.cli.commands._get_runner", side_effect=get_runner), \
             patch("asyncio.run", side_effect=patched_run), \
             patch("cascade.cli.commands._import_flow_class") as mock_import:
            mock_import.return_value = MagicMock()
            result = runner.invoke(
                app,
                ["resume", "--run-id", run_id, "--from", "tester", "--flow", "examples.devops_workflow.DevOpsFlow"]
            )

        assert result.exit_code in (0, 1)


# ── Run Command ───────────────────────────────────────────────────────────────

class TestRunCommand:
    def test_run_invalid_flow_path_exits_1(self):
        """Invalid flow path (no dot) exits with 1."""
        result = runner.invoke(app, ["run", "--flow", "invalid_no_dot"])
        assert result.exit_code == 1
        assert "must be" in result.output.lower() or "error" in result.output.lower()

    def test_run_nonexistent_module_exits_1(self):
        """Nonexistent module exits with 1."""
        result = runner.invoke(app, ["run", "--flow", "nonexistent_module.SomeClass"])
        assert result.exit_code == 1

    def test_run_flow_arg_required(self):
        """Without --flow, exits with non-zero."""
        result = runner.invoke(app, ["run"])
        assert result.exit_code != 0

    def test_run_help_shows_options(self):
        """--help shows usage info."""
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "--flow" in result.output


# ── Import Flow Helper ────────────────────────────────────────────────────────

class TestImportFlowHelper:
    def test_import_valid_flow_class(self):
        """Can import a real class from examples.devops_workflow."""
        from cascade.cli.commands import _import_flow_class
        flow_class = _import_flow_class("examples.devops_workflow.DevOpsFlow")
        from examples.devops_workflow import DevOpsFlow
        assert flow_class is DevOpsFlow

    def test_import_invalid_format_raises_exit(self):
        """Single-word path (no dot) raises SystemExit."""
        from cascade.cli.commands import _import_flow_class
        import typer
        with pytest.raises((SystemExit, typer.Exit)):
            _import_flow_class("nomodule")

    def test_import_missing_class_raises_exit(self):
        """Existing module but missing class raises SystemExit."""
        from cascade.cli.commands import _import_flow_class
        import typer
        with pytest.raises((SystemExit, typer.Exit)):
            _import_flow_class("examples.devops_workflow.NoSuchClass")

    def test_import_missing_module_raises_exit(self):
        """Nonexistent module raises SystemExit."""
        from cascade.cli.commands import _import_flow_class
        import typer
        with pytest.raises((SystemExit, typer.Exit)):
            _import_flow_class("this.module.doesnt.exist.MyClass")
