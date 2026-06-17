"""
tests/test_state.py
────────────────────
Tests for Pydantic state models and hash utilities.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from cascade.core.state import (
    StepState,
    StepStatus,
    RunState,
    RunStatus,
    compute_input_hash,
    compute_code_hash,
)


class TestStepState:
    def test_default_status_is_pending(self):
        state = StepState(run_id=uuid4(), name="test_step")
        assert state.status == StepStatus.PENDING

    def test_name_is_slugified(self):
        state = StepState(run_id=uuid4(), name="My-Step")
        assert state.name == "my_step"

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="must only contain"):
            StepState(run_id=uuid4(), name="step with spaces!")

    def test_mark_running_sets_started_at(self):
        state = StepState(run_id=uuid4(), name="test")
        running = state.mark_running()
        assert running.status == StepStatus.RUNNING
        assert running.started_at is not None

    def test_mark_completed_sets_outputs(self):
        state = StepState(run_id=uuid4(), name="test").mark_running()
        time.sleep(0.01)
        completed = state.mark_completed(
            outputs={"result": "done"},
            llm_cost_cents=5.0,
            total_tokens=100,
        )
        assert completed.status == StepStatus.COMPLETED
        assert completed.outputs == {"result": "done"}
        assert completed.llm_cost_cents == 5.0
        assert completed.duration_seconds is not None
        assert completed.duration_seconds >= 0

    def test_mark_failed_increments_retry(self):
        state = StepState(run_id=uuid4(), name="test", max_retries=3).mark_running()
        failed = state.mark_failed("something broke")
        assert failed.status == StepStatus.FAILED
        assert failed.retry_count == 1
        assert failed.error_message == "something broke"

    def test_mark_failed_permanently_when_retries_exhausted(self):
        state = StepState(run_id=uuid4(), name="test", max_retries=2, retry_count=1).mark_running()
        failed = state.mark_failed("final failure")
        assert failed.status == StepStatus.PERMANENTLY_FAILED
        assert not failed.can_retry

    def test_mark_skipped(self):
        state = StepState(run_id=uuid4(), name="test")
        skipped = state.mark_skipped(outputs={"cached": True})
        assert skipped.status == StepStatus.SKIPPED
        assert skipped.outputs == {"cached": True}

    def test_can_retry_true_when_retries_remain(self):
        state = StepState(run_id=uuid4(), name="test", max_retries=3, retry_count=1)
        assert state.can_retry is True

    def test_can_retry_false_when_exhausted(self):
        state = StepState(run_id=uuid4(), name="test", max_retries=3, retry_count=3)
        assert state.can_retry is False

    def test_is_terminal(self):
        for status in (StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.PERMANENTLY_FAILED):
            state = StepState(run_id=uuid4(), name="test", status=status)
            assert state.is_terminal

        for status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.FAILED):
            state = StepState(run_id=uuid4(), name="test", status=status)
            assert not state.is_terminal


class TestRunState:
    def test_default_status_is_pending(self):
        run = RunState(flow_name="my_flow")
        assert run.status == RunStatus.PENDING

    def test_cost_summary(self):
        run = RunState(flow_name="my_flow")
        run.steps["explorer"] = StepState(run_id=run.id, name="explorer", llm_cost_cents=10.0)
        run.steps["planner"] = StepState(run_id=run.id, name="planner", llm_cost_cents=25.5)
        summary = run.cost_summary()
        assert summary["explorer"] == 10.0
        assert summary["planner"] == 25.5


class TestHashUtilities:
    def test_same_inputs_same_hash(self):
        inputs = {"repo": "fastapi", "issue": 42}
        source = "def my_step(): pass"
        h1 = compute_input_hash(inputs, source)
        h2 = compute_input_hash(inputs, source)
        assert h1 == h2

    def test_different_inputs_different_hash(self):
        source = "def my_step(): pass"
        h1 = compute_input_hash({"repo": "fastapi"}, source)
        h2 = compute_input_hash({"repo": "django"}, source)
        assert h1 != h2

    def test_different_source_code_invalidates_cache(self):
        inputs = {"repo": "fastapi"}
        h1 = compute_input_hash(inputs, "def step_v1(): return 1")
        h2 = compute_input_hash(inputs, "def step_v2(): return 2")
        assert h1 != h2

    def test_hash_is_hex_string(self):
        h = compute_input_hash({}, "")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_non_serializable_inputs_handled(self):
        """Non-JSON-serializable inputs should fall back to repr without error."""
        class CustomObj:
            def __repr__(self): return "CustomObj()"

        h = compute_input_hash({"obj": CustomObj()}, "")
        assert len(h) == 64

    def test_compute_code_hash_returns_short_hash(self):
        def my_func():
            return 42
        h = compute_code_hash(my_func)
        assert len(h) == 16
