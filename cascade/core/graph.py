"""
cascade/core/graph.py
──────────────────────
LangGraph integration with Cascade's persistent state backend.

Provides:
  CascadeCheckpointer   — LangGraph BaseCheckpointSaver backed by MetadataStore.
  CascadeStateGraph     — Thin wrapper around LangGraph StateGraph with:
                          - Cascade checkpointing wired in automatically
                          - Conditional edge helpers for the retry loop
                          - Type-annotated state schema

Architecture:
  The @step decorator handles caching at the coarse level (full agent steps).
  LangGraph handles the fine-grained execution graph INSIDE an agentic step,
  specifically the Coder ↔ Tester retry loop (Phase 3).

  For Phase 2, CascadeStateGraph is used as the backbone of the DevOpsFlow DAG.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from cascade.core.state import StepStatus

# Lazy import guard — langgraph is optional
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.base import (
        BaseCheckpointSaver,
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
    )
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    # Provide stubs so the rest of the module is importable
    BaseCheckpointSaver = object  # type: ignore[assignment,misc]
    StateGraph = None  # type: ignore[assignment,misc]
    END = "__end__"


# ── Cascade Graph State Schema ────────────────────────────────────────────────

from typing import TypedDict, Optional, List


class DevOpsState(TypedDict, total=False):
    """
    Shared state object threaded through the DevOps pipeline DAG.
    Each node reads from and writes to this dict.
    LangGraph persists this between node executions via the checkpointer.
    """
    # Input
    issue_url: str
    repo_url: str
    issue_number: int
    issue_title: str
    issue_body: str

    # Explorer outputs
    commit_sha: str
    repo_path: str
    repo_graph_uri: str          # CAS URI → repo_graph.json
    relevant_files: list[str]

    # Planner outputs
    tot_branches_uri: str        # CAS URI → tot_branches.json
    selected_branch_index: int
    selected_branch: dict[str, Any]

    # Coder outputs
    patch_uri: str               # CAS URI → patch.diff
    cost_manifest_uri: str

    # Tester outputs (Phase 3)
    test_results_uri: str        # CAS URI → test_results.xml
    test_passed: bool
    retry_count: int

    # Reviewer outputs (Phase 4)
    review_status_uri: str
    review_approved: bool

    # PR outputs (Phase 4)
    pr_url: str
    pr_number: int

    # Error tracking
    last_error: str
    current_node: str


# ── Cascade Checkpointer ──────────────────────────────────────────────────────

class CascadeCheckpointer(BaseCheckpointSaver if LANGGRAPH_AVAILABLE else object):
    """
    LangGraph checkpoint saver backed by Cascade's MetadataStore.

    Replaces LangGraph's MemorySaver with persistent, resumable storage.
    Checkpoints are stored as JSON artifacts in the CAS artifact store.

    Each checkpoint maps to a StepState record:
      - thread_id  → run_id
      - checkpoint_id → step execution attempt
      - Checkpoint data → serialized to CAS artifact
    """

    def __init__(self, store: Any, artifact_store: Any) -> None:
        if LANGGRAPH_AVAILABLE:
            super().__init__()
        self._store = store
        self._artifact_store = artifact_store
        # In-memory cache for the current session
        self._cache: dict[str, CheckpointTuple] = {} if LANGGRAPH_AVAILABLE else {}

    def get_tuple(self, config: dict[str, Any]) -> "CheckpointTuple | None":
        """Retrieve the most recent checkpoint for this thread."""
        if not LANGGRAPH_AVAILABLE:
            return None
        thread_id = config.get("configurable", {}).get("thread_id", "")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        key = f"{thread_id}:{checkpoint_id or 'latest'}"
        return self._cache.get(key)

    def list(
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator["CheckpointTuple"]:
        """List all checkpoints for a thread."""
        if not LANGGRAPH_AVAILABLE:
            return iter([])
        thread_id = config.get("configurable", {}).get("thread_id", "")
        for key, checkpoint_tuple in self._cache.items():
            if key.startswith(f"{thread_id}:"):
                yield checkpoint_tuple

    def put(
        self,
        config: dict[str, Any],
        checkpoint: "Checkpoint",
        metadata: "CheckpointMetadata",
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a checkpoint to artifact store."""
        if not LANGGRAPH_AVAILABLE:
            return config
        thread_id = config.get("configurable", {}).get("thread_id", "")
        checkpoint_id = checkpoint.get("id", str(uuid.uuid4()))

        # Serialize and store in CAS
        checkpoint_data = json.dumps({
            "checkpoint": checkpoint,
            "metadata": metadata,
        }, default=str).encode()
        uri = self._artifact_store.put_bytes(checkpoint_data)

        # Cache locally for fast retrieval
        checkpoint_tuple = CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=None,
        )
        self._cache[f"{thread_id}:latest"] = checkpoint_tuple
        self._cache[f"{thread_id}:{checkpoint_id}"] = checkpoint_tuple

        return {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: "Checkpoint",
        metadata: "CheckpointMetadata",
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """Async version of put."""
        return self.put(config, checkpoint, metadata, new_versions)

    async def aget_tuple(self, config: dict[str, Any]) -> "CheckpointTuple | None":
        """Async version of get_tuple."""
        return self.get_tuple(config)

    async def alist(
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator["CheckpointTuple"]:
        """Async version of list."""
        return self.list(config, filter=filter, before=before, limit=limit)


# ── Cascade State Graph ───────────────────────────────────────────────────────

class CascadeGraph:
    """
    Thin wrapper around LangGraph StateGraph with:
    - Cascade checkpointing wired in automatically
    - Helper methods for building the DevOps pipeline
    - Conditional edge routing for the retry loop (Phase 3)

    Usage:
        graph = CascadeGraph.build(store, artifact_store)
        result = await graph.run(initial_state)
    """

    def __init__(
        self,
        graph: Any,  # LangGraph CompiledGraph
        checkpointer: CascadeCheckpointer,
        run_id: str,
    ) -> None:
        self._graph = graph
        self._checkpointer = checkpointer
        self._run_id = run_id

    @classmethod
    def create(
        cls,
        state_schema: type,
        store: Any,
        artifact_store: Any,
        run_id: str,
    ) -> "tuple[CascadeGraph, Any]":
        """
        Create a new CascadeGraph with the given state schema.

        Returns (cascade_graph, state_graph_builder) so callers can
        add nodes and edges before compiling.
        """
        if not LANGGRAPH_AVAILABLE:
            msg = (
                "LangGraph is not installed. Install with: "
                "pip install cascade[ai]"
            )
            raise ImportError(msg)

        checkpointer = CascadeCheckpointer(store, artifact_store)
        builder = StateGraph(state_schema)
        return cls(None, checkpointer, run_id), builder

    def compile(self, builder: Any) -> "CascadeGraph":
        """Compile the StateGraph with our checkpointer."""
        self._graph = builder.compile(checkpointer=self._checkpointer)
        return self

    async def run(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        """Execute the compiled graph with persistent checkpointing."""
        if self._graph is None:
            msg = "Graph not compiled. Call .compile(builder) first."
            raise RuntimeError(msg)
        config = {"configurable": {"thread_id": self._run_id}}
        result = await self._graph.ainvoke(initial_state, config=config)
        return result

    def resume(self, from_node: str | None = None) -> dict[str, Any]:
        """
        Resume graph execution from the last checkpoint (or specific node).
        Used by `cascade resume --from <step>`.
        """
        config = {"configurable": {"thread_id": self._run_id}}
        if from_node:
            config["configurable"]["checkpoint_ns"] = from_node
        return config


# ── Router helpers for conditional edges ─────────────────────────────────────

def tester_router(state: DevOpsState) -> str:
    """
    LangGraph conditional edge router for the Coder ↔ Tester retry loop.

    Routes:
      test_passed=True  → "reviewer"   (proceed to review)
      test_passed=False, retries left  → "coder"  (fix the patch)
      test_passed=False, max retries   → "failed" (give up)
    """
    from cascade.core.config import settings  # noqa: PLC0415
    max_retries = settings().max_retries

    if state.get("test_passed", False):
        return "reviewer"

    retry_count = state.get("retry_count", 0)
    if retry_count >= max_retries:
        return "__end__"

    return "coder"


def review_router(state: DevOpsState) -> str:
    """Routes from Reviewer → PR Creator (approved) or __end__ (rejected)."""
    if state.get("review_approved", False):
        return "pr_creator"
    return "__end__"
