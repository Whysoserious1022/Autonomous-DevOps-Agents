"""
tests/test_graph.py
───────────────────
Unit tests for LangGraph integration, checkpointer, and retry loop.
"""

from __future__ import annotations

import uuid
from typing import Any
import pytest
from unittest.mock import patch, AsyncMock

from cascade.core.decorator import CascadeFlow, step
from cascade.core.graph import build_devops_graph, DevOpsState, tester_router as graph_tester_router
from cascade.core.state import StepState, StepStatus
from cascade.storage.metadata import MetadataStore
from cascade.storage.artifact_store import LocalArtifactStore

@pytest.mark.asyncio
async def test_checkpointer_persistence(metadata_store, artifact_store):
    """Verify CascadeCheckpointer writes checkpoints to artifact store and reads them back."""
    from cascade.core.graph import CascadeCheckpointer
    from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

    checkpointer = CascadeCheckpointer(metadata_store, artifact_store)
    run_id = str(uuid.uuid4())

    checkpoint: Checkpoint = {
        "v": 1,
        "id": "chk-123",
        "ts": "2026-06-17T00:00:00Z",
        "channel_values": {"my_key": "my_val"},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }
    metadata: CheckpointMetadata = {
        "source": "input",
        "step": 1,
        "writes": {},
    }

    config = {"configurable": {"thread_id": run_id}}

    # Persist the checkpoint
    await checkpointer.aput(config, checkpoint, metadata, {})

    # Clear checkpointer cache to force DB lookup
    checkpointer._cache.clear()

    # Retrieve the checkpoint
    config_lookup = {"configurable": {"thread_id": run_id, "checkpoint_id": "chk-123"}}
    checkpoint_tuple = await checkpointer.aget_tuple(config_lookup)

    assert checkpoint_tuple is not None
    assert checkpoint_tuple.checkpoint["id"] == "chk-123"
    assert checkpoint_tuple.checkpoint["channel_values"]["my_key"] == "my_val"

@pytest.mark.asyncio
async def test_devops_workflow_retry_loop(metadata_store, artifact_store):
    """
    Test the Coder-Tester retry loop:
    1. Explorer & Planner run once.
    2. Coder runs, patch is generated.
    3. Tester fails.
    4. Coder runs again with test feedback.
    5. Tester passes.
    """
    run_id = str(uuid.uuid4())

    # Create dummy flow class with mock step methods
    class MockDevOpsFlow(CascadeFlow):
        flow_name = "mock_devops_flow"

        # Mock spy attributes
        explorer_calls = 0
        planner_calls = 0
        coder_calls = []
        tester_calls = 0

        @step(name="explorer")
        async def explore(self, inputs: dict) -> dict:
            MockDevOpsFlow.explorer_calls += 1
            return {"commit_sha": "sha123", "repo_graph_uri": "sha256://graph"}

        @step(name="planner", depends_on=["explorer"])
        async def plan(self, inputs: dict) -> dict:
            MockDevOpsFlow.planner_calls += 1
            return {
                "tot_branches_uri": "sha256://branches",
                "selected_branch": {"hypothesis": "fix bug"},
                "analysis_summary": "missing return",
            }

        @step(name="coder", depends_on=["planner"])
        async def code(self, inputs: dict) -> dict:
            MockDevOpsFlow.coder_calls.append(dict(inputs))
            return {"patch_uri": f"sha256://patch_{len(MockDevOpsFlow.coder_calls)}"}

        @step(name="tester", depends_on=["coder"])
        async def test(self, inputs: dict) -> dict:
            MockDevOpsFlow.tester_calls += 1
            # First execution fails, second passes
            passed = (MockDevOpsFlow.tester_calls > 1)
            return {
                "test_passed": passed,
                "test_results_uri": "sha256://xml" if not passed else "",
                "test_results_xml": "<failed/>" if not passed else "",
                "test_error_summary": "AssertionError: expected 42" if not passed else "",
            }

    # Build and compile graph
    graph = build_devops_graph(
        flow_class=MockDevOpsFlow,
        store=metadata_store,
        artifact_store=artifact_store,
        run_id=run_id,
    )

    # Initial state
    initial_state: DevOpsState = {
        "repo_url": "https://github.com/org/repo",
        "commit_sha": "sha123",
        "issue_title": "broken test",
        "issue_body": "test is broken",
        "n_branches": 3,
    }

    # Run the graph
    final_state = await graph.run(initial_state)

    # Verify nodes ran the correct number of times
    assert MockDevOpsFlow.explorer_calls == 1
    assert MockDevOpsFlow.planner_calls == 1
    assert len(MockDevOpsFlow.coder_calls) == 2  # Coder ran twice!
    assert MockDevOpsFlow.tester_calls == 2    # Tester ran twice!

    # Verify correct routing output state
    assert final_state["test_passed"] is True
    assert final_state["retry_count"] == 1

    # Verify Coder call 1 inputs (first attempt, retry_count=0)
    assert MockDevOpsFlow.coder_calls[0]["retry_count"] == 0
    assert MockDevOpsFlow.coder_calls[0]["test_results_xml"] == ""

    # Verify Coder call 2 inputs (second attempt, retry_count=1 with feedback)
    assert MockDevOpsFlow.coder_calls[1]["retry_count"] == 1
    assert MockDevOpsFlow.coder_calls[1]["test_results_xml"] == "<failed/>"
    assert MockDevOpsFlow.coder_calls[1]["test_error_summary"] == "AssertionError: expected 42"
    assert MockDevOpsFlow.coder_calls[1]["previous_patch_uri"] == "sha256://patch_1"

def test_tester_router_logic():
    """Verify tester_router routes based on test_passed and retry budget."""
    # Test passed -> reviewer (represented by END in Phase 3)
    state_pass: DevOpsState = {"test_passed": True}
    assert graph_tester_router(state_pass) == "__end__"

    # Test failed, retries remaining -> coder
    state_fail: DevOpsState = {"test_passed": False, "retry_count": 1}
    assert graph_tester_router(state_fail) == "coder"

    # Test failed, retries exhausted -> __end__
    state_exhausted: DevOpsState = {"test_passed": False, "retry_count": 3}
    assert graph_tester_router(state_exhausted) == "__end__"
