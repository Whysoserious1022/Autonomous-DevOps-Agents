"""
tests/test_decorator.py
────────────────────────
Tests for the @step decorator: cache skipping, retry logic, output threading.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from cascade.core.decorator import CascadeFlow, step
from cascade.core.state import StepStatus
from cascade.core.runner import FlowRunner


# ── Test Flows ────────────────────────────────────────────────────────────────

class SimpleFlow(CascadeFlow):
    flow_name = "simple_flow"
    call_count: int = 0

    @step(name="counter_step", max_retries=2)
    async def counter_step(self, inputs: dict) -> dict:
        SimpleFlow.call_count += 1
        return {"count": SimpleFlow.call_count, "input_echo": inputs.get("value")}


class TwoStepFlow(CascadeFlow):
    flow_name = "two_step_flow"

    @step(name="first")
    async def first(self, inputs: dict) -> dict:
        return {"msg": "from_first", "num": 10}

    @step(name="second", depends_on=["first"])
    async def second(self, inputs: dict) -> dict:
        upstream = inputs.get("first.msg", "")
        return {"final": f"second got: {upstream}"}


class FailingFlow(CascadeFlow):
    flow_name = "failing_flow"
    attempt: int = 0

    @step(name="flaky_step", max_retries=3)
    async def flaky_step(self, inputs: dict) -> dict:
        FailingFlow.attempt += 1
        if FailingFlow.attempt < 2:
            msg = "Simulated failure"
            raise RuntimeError(msg)
        return {"recovered": True}


class AlwaysFailFlow(CascadeFlow):
    flow_name = "always_fail_flow"

    @step(name="dead_step", max_retries=1)
    async def dead_step(self, inputs: dict) -> dict:
        msg = "I always fail"
        raise ValueError(msg)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestStepDecorator:
    @pytest.mark.asyncio
    async def test_step_executes_and_returns_state(self, flow_runner: FlowRunner):
        SimpleFlow.call_count = 0
        run_state = await flow_runner.run(SimpleFlow, value="test_input")
        assert run_state.status.value == "completed"
        assert "counter_step" in run_state.steps
        step_state = run_state.steps["counter_step"]
        assert step_state.status == StepStatus.COMPLETED
        assert step_state.outputs["count"] == 1

    @pytest.mark.asyncio
    async def test_step_skipped_on_identical_inputs(self, flow_runner: FlowRunner, tmp_path: Path):
        """
        THE KILLER FEATURE TEST:
        Run the same flow twice with identical inputs.
        Second run: step should be SKIPPED (global cache hit).
        """
        SimpleFlow.call_count = 0

        # First run
        await flow_runner.run(SimpleFlow, value="cached_input")
        assert SimpleFlow.call_count == 1

        # Second run — same inputs → cache hit
        await flow_runner.run(SimpleFlow, value="cached_input")
        # call_count should still be 1 (step was skipped)
        assert SimpleFlow.call_count == 1

    @pytest.mark.asyncio
    async def test_step_reruns_on_different_inputs(self, flow_runner: FlowRunner):
        SimpleFlow.call_count = 0

        await flow_runner.run(SimpleFlow, value="input_A")
        assert SimpleFlow.call_count == 1

        # Different input → cache miss → re-run
        await flow_runner.run(SimpleFlow, value="input_B")
        assert SimpleFlow.call_count == 2

    @pytest.mark.asyncio
    async def test_step_has_input_hash(self, flow_runner: FlowRunner):
        run_state = await flow_runner.run(SimpleFlow, value="hash_test")
        step_state = run_state.steps["counter_step"]
        assert len(step_state.input_hash) == 64
        assert all(c in "0123456789abcdef" for c in step_state.input_hash)

    @pytest.mark.asyncio
    async def test_step_stores_code_version(self, flow_runner: FlowRunner):
        run_state = await flow_runner.run(SimpleFlow, value="version_test")
        step_state = run_state.steps["counter_step"]
        assert len(step_state.step_version) == 16


class TestOutputThreading:
    @pytest.mark.asyncio
    async def test_upstream_outputs_passed_to_downstream(self, flow_runner: FlowRunner):
        run_state = await flow_runner.run(TwoStepFlow)
        assert "first" in run_state.steps
        assert "second" in run_state.steps

        second_out = run_state.steps["second"].outputs
        assert "final" in second_out
        assert "from_first" in second_out["final"]

    @pytest.mark.asyncio
    async def test_both_steps_completed(self, flow_runner: FlowRunner):
        run_state = await flow_runner.run(TwoStepFlow)
        assert run_state.steps["first"].status == StepStatus.COMPLETED
        assert run_state.steps["second"].status == StepStatus.COMPLETED


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_failed_step_marks_run_failed(self, flow_runner: FlowRunner):
        AlwaysFailFlow_instance = AlwaysFailFlow
        run_state = await flow_runner.run(AlwaysFailFlow_instance)
        # Should reach PERMANENTLY_FAILED since max_retries=1
        dead = run_state.steps.get("dead_step")
        assert dead is not None
        assert dead.status == StepStatus.PERMANENTLY_FAILED

    @pytest.mark.asyncio
    async def test_error_message_persisted(self, flow_runner: FlowRunner):
        run_state = await flow_runner.run(AlwaysFailFlow)
        dead = run_state.steps["dead_step"]
        assert dead.error_message == "I always fail"

    @pytest.mark.asyncio
    async def test_traceback_stored_as_artifact(self, flow_runner: FlowRunner):
        run_state = await flow_runner.run(AlwaysFailFlow)
        dead = run_state.steps["dead_step"]
        # Traceback should be stored as a CAS artifact
        assert len(dead.artifact_uris) > 0
        tb_uri = dead.artifact_uris[-1]
        tb_text = flow_runner._artifact_store.get_text(tb_uri)
        assert "I always fail" in tb_text


class TestStepRegistry:
    def test_decorated_step_registered(self):
        from cascade.core.decorator import _STEP_REGISTRY
        assert "counter_step" in _STEP_REGISTRY
        assert "first" in _STEP_REGISTRY
        assert "second" in _STEP_REGISTRY

    def test_step_meta_has_code_hash(self):
        from cascade.core.decorator import _STEP_REGISTRY
        meta = _STEP_REGISTRY["counter_step"]
        assert len(meta.code_hash) == 16

    def test_step_meta_depends_on(self):
        from cascade.core.decorator import _STEP_REGISTRY
        meta = _STEP_REGISTRY["second"]
        assert "first" in meta.depends_on
