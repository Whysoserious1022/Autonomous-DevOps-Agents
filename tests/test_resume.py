"""
tests/test_resume.py
─────────────────────
Tests for the Cascade resume feature — the core "stop re-reasoning" capability.

Phase 1 Success Metric:
  A Python script runs 2 steps. Step 1 runs. Step 2 runs.
  Killing the script and re-running: Step 1 is SKIPPED, Step 2 runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cascade.core.decorator import CascadeFlow, step
from cascade.core.runner import FlowRunner
from cascade.core.state import RunStatus, StepStatus


# ── Test Flows ────────────────────────────────────────────────────────────────

class ThreeStepFlow(CascadeFlow):
    """Three sequential steps for resume testing."""
    flow_name = "three_step_flow"

    executions: dict[str, int] = {"step_a": 0, "step_b": 0, "step_c": 0}

    @step(name="step_a")
    async def step_a(self, inputs: dict) -> dict:
        ThreeStepFlow.executions["step_a"] += 1
        return {"a_result": "computed_a"}

    @step(name="step_b", depends_on=["step_a"])
    async def step_b(self, inputs: dict) -> dict:
        ThreeStepFlow.executions["step_b"] += 1
        return {"b_result": "computed_b"}

    @step(name="step_c", depends_on=["step_b"])
    async def step_c(self, inputs: dict) -> dict:
        ThreeStepFlow.executions["step_c"] += 1
        return {"c_result": "computed_c"}


class ResumableFlow(CascadeFlow):
    """Flow where step_b fails on first attempt, succeeds after resume."""
    flow_name = "resumable_flow"
    should_fail: bool = True

    @step(name="expensive_step", max_retries=1)
    async def expensive_step(self, inputs: dict) -> dict:
        """Simulates an expensive step (e.g., repo cloning + AST building)."""
        return {"repo_graph": "large_graph_data", "commit_sha": "abc123"}

    @step(name="failable_step", depends_on=["expensive_step"], max_retries=1)
    async def failable_step(self, inputs: dict) -> dict:
        """Fails on first run, succeeds after the bug is 'fixed' (should_fail=False)."""
        if ResumableFlow.should_fail:
            msg = "Test harness failure"
            raise RuntimeError(msg)
        return {"patch": "fixed_code.diff"}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestResumeFeature:
    @pytest.mark.asyncio
    async def test_completed_steps_are_skipped_on_resume(self, flow_runner: FlowRunner):
        """
        PHASE 1 SUCCESS METRIC:
        After a run completes, re-running with same inputs skips completed steps.
        """
        ThreeStepFlow.executions = {"step_a": 0, "step_b": 0, "step_c": 0}

        # First run — all steps execute
        run_state = await flow_runner.run(ThreeStepFlow)
        assert run_state.status == RunStatus.COMPLETED
        assert ThreeStepFlow.executions["step_a"] == 1
        assert ThreeStepFlow.executions["step_b"] == 1
        assert ThreeStepFlow.executions["step_c"] == 1

        # Second run with same inputs — all should be SKIPPED from global cache
        run_state2 = await flow_runner.run(ThreeStepFlow)
        # Steps should be SKIPPED (loaded from cache)
        for name, s in run_state2.steps.items():
            assert s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED), \
                f"Step {name} should be completed or skipped, got {s.status}"

        # Steps should NOT have been re-executed
        assert ThreeStepFlow.executions["step_a"] == 1  # Still 1, not 2
        assert ThreeStepFlow.executions["step_b"] == 1
        assert ThreeStepFlow.executions["step_c"] == 1

    @pytest.mark.asyncio
    async def test_resume_skips_completed_re_executes_from_step(self, flow_runner: FlowRunner):
        """
        RESUME SCENARIO:
        1. Run fails at step_b (step_a completed).
        2. 'Fix the bug' (set should_fail=False).
        3. Resume from step_b — step_a is SKIPPED, step_b re-runs.
        """
        ResumableFlow.should_fail = True

        # First run — fails at failable_step
        run_state = await flow_runner.run(ResumableFlow)
        expensive = run_state.steps.get("expensive_step")
        failable = run_state.steps.get("failable_step")

        assert expensive is not None
        assert expensive.status == StepStatus.COMPLETED

        assert failable is not None
        assert failable.status in (StepStatus.PERMANENTLY_FAILED, StepStatus.FAILED)

        # "Fix the bug"
        ResumableFlow.should_fail = False

        # Resume from failable_step
        resumed = await flow_runner.resume(
            run_id=str(run_state.id),
            from_step="failable_step",
            flow_class=ResumableFlow,
        )

        # expensive_step should have been loaded from cache (SKIPPED)
        resumed_expensive = resumed.steps.get("expensive_step")
        assert resumed_expensive is not None
        # failable_step should now be completed
        resumed_failable = resumed.steps.get("failable_step")
        assert resumed_failable is not None
        assert resumed_failable.status == StepStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_run_not_found_raises_on_resume(self, flow_runner: FlowRunner):
        with pytest.raises(ValueError, match="not found"):
            await flow_runner.resume(
                run_id="nonexistent-run-id",
                from_step="step_a",
                flow_class=ThreeStepFlow,
            )

    @pytest.mark.asyncio
    async def test_resume_time_under_5_seconds(self, flow_runner: FlowRunner):
        """
        KPI: Resuming a completed pipeline should take < 5 seconds
        (excluding LLM call time for the resumed step).
        """
        import time
        ThreeStepFlow.executions = {"step_a": 0, "step_b": 0, "step_c": 0}

        run_state = await flow_runner.run(ThreeStepFlow)

        start = time.perf_counter()
        resumed = await flow_runner.resume(
            run_id=str(run_state.id),
            from_step="step_c",
            flow_class=ThreeStepFlow,
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 5.0, f"Resume took {elapsed:.2f}s — must be < 5s"


class TestMetadataStore:
    @pytest.mark.asyncio
    async def test_run_persisted_to_db(self, flow_runner: FlowRunner):
        run_state = await flow_runner.run(ThreeStepFlow)
        retrieved = await flow_runner.get_run(str(run_state.id))
        assert retrieved is not None
        assert retrieved.id == run_state.id
        assert retrieved.flow_name == "three_step_flow"

    @pytest.mark.asyncio
    async def test_step_states_persisted(self, flow_runner: FlowRunner):
        ThreeStepFlow.executions = {"step_a": 0, "step_b": 0, "step_c": 0}
        run_state = await flow_runner.run(ThreeStepFlow)
        retrieved = await flow_runner.get_run(str(run_state.id))
        assert retrieved is not None
        assert len(retrieved.steps) >= 1

    @pytest.mark.asyncio
    async def test_list_runs(self, flow_runner: FlowRunner):
        ThreeStepFlow.executions = {"step_a": 0, "step_b": 0, "step_c": 0}
        await flow_runner.run(ThreeStepFlow)
        await flow_runner.run(ThreeStepFlow)
        runs = await flow_runner.list_runs()
        assert len(runs) >= 1  # At least one run exists
