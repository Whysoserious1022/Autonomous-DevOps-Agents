"""
cascade/core/graph.py
──────────────────────
LangGraph integration with Cascade's persistent state backend.

Provides:
  CascadeCheckpointer   — LangGraph BaseCheckpointSaver backed by MetadataStore.
  CascadeGraph          — Thin wrapper around LangGraph StateGraph with:
                          - Cascade checkpointing wired in automatically
                          - Conditional edge helpers for the retry loop
                          - Type-annotated state schema
  build_devops_graph    — Factory to build the Coder-Tester retry loop workflow.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence, TypedDict, Optional, List

from cascade.core.state import StepState, StepStatus

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

class DevOpsState(TypedDict, total=False):
    """
    Shared state object threaded through the DevOps pipeline DAG.
    Each node reads from and writes to this dict.
    LangGraph persists this between node executions via the checkpointer.
    """
    # Inputs
    issue_url: str
    repo_url: str
    issue_number: int
    issue_title: str
    issue_body: str
    n_branches: int
    test_command: str

    # Explorer outputs
    commit_sha: str
    repo_path: str
    repo_graph_uri: str          # CAS URI → repo_graph.json
    relevant_files_uri: str      # CAS URI → relevant_files.json
    relevant_files: list[str]

    # Planner outputs
    tot_branches_uri: str        # CAS URI → tot_branches.json
    selected_branch_index: int
    selected_branch: dict[str, Any]
    analysis_summary: str

    # Coder outputs
    patch_uri: str               # CAS URI → patch.diff
    cost_manifest_uri: str

    # Tester outputs (Phase 3)
    test_results_uri: str        # CAS URI → test_results.xml
    test_results_xml: str
    test_error_summary: str
    test_passed: bool
    retry_count: int
    previous_patch_uri: str

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

    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        """Retrieve the most recent checkpoint for this thread."""
        if not LANGGRAPH_AVAILABLE:
            return None
        thread_id = config.get("configurable", {}).get("thread_id", "")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
        
        key = f"{thread_id}:{checkpoint_id or 'latest'}"
        if key in self._cache:
            return self._cache[key]
            
        # Fallback to sync DB lookup using event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # In an async runtime, we use nest_asyncio to run synchronous wait
                try:
                    import nest_asyncio
                    nest_asyncio.apply()
                except ImportError:
                    pass
            return loop.run_until_complete(self.aget_tuple(config))
        except Exception:
            return None

    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        """Retrieve the most recent checkpoint for this thread asynchronously."""
        if not LANGGRAPH_AVAILABLE:
            return None
        thread_id = config.get("configurable", {}).get("thread_id", "")
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")

        key = f"{thread_id}:{checkpoint_id or 'latest'}"
        if key in self._cache:
            return self._cache[key]

        # Query metadata DB for the checkpoint step state
        steps = await self._store.list_steps(thread_id)
        checkpoint_steps = [s for s in steps if s.name == "__checkpoint__"]
        
        if not checkpoint_steps:
            return None
            
        target_step = None
        if checkpoint_id:
            for s in checkpoint_steps:
                if s.outputs.get("checkpoint_id") == checkpoint_id:
                    target_step = s
                    break
        else:
            checkpoint_steps.sort(key=lambda s: s.completed_at or datetime.min)
            target_step = checkpoint_steps[-1] if checkpoint_steps else None

        if not target_step:
            return None

        uri = target_step.outputs.get("checkpoint_uri", "")
        if not uri:
            return None

        try:
            data = self._artifact_store.get_bytes(uri)
            doc = json.loads(data.decode("utf-8"))
            checkpoint = doc["checkpoint"]
            metadata = doc["metadata"]
            
            config_out = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": target_step.outputs.get("checkpoint_id"),
                }
            }
            checkpoint_tuple = CheckpointTuple(
                config=config_out,
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=None,
            )
            self._cache[key] = checkpoint_tuple
            return checkpoint_tuple
        except Exception:
            return None

    def put(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a checkpoint to artifact store."""
        if not LANGGRAPH_AVAILABLE:
            return config
        
        loop = asyncio.get_event_loop()
        if loop.is_running():
            try:
                import nest_asyncio
                nest_asyncio.apply()
            except ImportError:
                pass
        return loop.run_until_complete(
            self.aput(config, checkpoint, metadata, new_versions)
        )

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """Async version of put. Writes to CAS and logs to metadata DB."""
        if not LANGGRAPH_AVAILABLE:
            return config
            
        thread_id = config.get("configurable", {}).get("thread_id", "")
        checkpoint_id = checkpoint.get("id", str(uuid.uuid4()))

        # 1. Serialize and store in CAS
        checkpoint_data = json.dumps({
            "checkpoint": checkpoint,
            "metadata": metadata,
        }, default=str).encode()
        uri = self._artifact_store.put_bytes(checkpoint_data)

        # 2. Persist checkpoint metadata in steps table of MetadataStore
        import uuid as uuid_pkg
        step_id = uuid_pkg.uuid4()
        step_state = StepState(
            id=step_id,
            run_id=uuid_pkg.UUID(thread_id),
            name="__checkpoint__",
            input_hash=checkpoint_id,
            status=StepStatus.COMPLETED,
            inputs={"checkpoint_id": checkpoint_id},
            outputs={"checkpoint_id": checkpoint_id, "checkpoint_uri": uri},
        )
        await self._store.upsert_step(step_state)

        # 3. Cache locally for fast retrieval
        config_out = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id,
            }
        }
        checkpoint_tuple = CheckpointTuple(
            config=config_out,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=None,
        )
        self._cache[f"{thread_id}:latest"] = checkpoint_tuple
        self._cache[f"{thread_id}:{checkpoint_id}"] = checkpoint_tuple

        return config_out

    def put_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        """Store intermediate writes (no-op for Cascade)."""
        pass

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        """Store intermediate writes asynchronously (no-op for Cascade)."""
        pass

    def list(
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List all checkpoints for a thread."""
        if not LANGGRAPH_AVAILABLE:
            return iter([])
        thread_id = config.get("configurable", {}).get("thread_id", "")
        for key, checkpoint_tuple in self._cache.items():
            if key.startswith(f"{thread_id}:"):
                yield checkpoint_tuple

    async def alist(
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
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
    ) -> tuple[CascadeGraph, Any]:
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

    def compile(self, builder: Any) -> CascadeGraph:
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
      test_passed=False, max retries   → "__end__" (give up)
    """
    from cascade.core.config import settings  # noqa: PLC0415
    max_retries = settings().max_retries

    if state.get("test_passed", False):
        return "reviewer"

    retry_count = state.get("retry_count", 0)
    if retry_count >= max_retries:
        return END

    return "coder"


def review_router(state: DevOpsState) -> str:
    """Routes from Reviewer → PR Creator (approved) or __end__ (rejected)."""
    if state.get("review_approved", False):
        return "pr_creator"
    return END


# ── DevOps LangGraph Factory ──────────────────────────────────────────────────

def build_devops_graph(
    flow_class: type,
    store: Any,
    artifact_store: Any,
    run_id: str,
) -> CascadeGraph:
    """
    Build and compile the complete DevOps agent pipeline LangGraph.

    Uses Flow steps dynamically to wrap execution. This guarantees
    coarse caching via @step decorator works in harmony with LangGraph checkpoints.
    """
    graph_wrapper, builder = CascadeGraph.create(DevOpsState, store, artifact_store, run_id)

    # ── Node Definitions ──────────────────────────────────────────────────────

    async def run_explorer(state: DevOpsState) -> dict[str, Any]:
        flow = flow_class()
        flow._store = store
        flow._artifact_store = artifact_store
        flow._run_id = uuid.UUID(run_id)

        step_inputs = {
            "repo_url": state.get("repo_url", ""),
            "commit_sha": state.get("commit_sha", ""),
            "issue_title": state.get("issue_title", ""),
            "issue_body": state.get("issue_body", ""),
            "issue_number": state.get("issue_number", 0),
        }
        step_state = await flow.explore(step_inputs)
        return {
            "commit_sha": step_state.outputs.get("commit_sha", ""),
            "repo_graph_uri": step_state.outputs.get("repo_graph_uri", ""),
            "relevant_files_uri": step_state.outputs.get("relevant_files_uri", ""),
            "relevant_files": step_state.outputs.get("relevant_files", []),
        }

    async def run_planner(state: DevOpsState) -> dict[str, Any]:
        flow = flow_class()
        flow._store = store
        flow._artifact_store = artifact_store
        flow._run_id = uuid.UUID(run_id)

        step_inputs = {
            "explorer.repo_graph_uri": state.get("repo_graph_uri", ""),
            "explorer.relevant_files_uri": state.get("relevant_files_uri", ""),
            "issue_title": state.get("issue_title", ""),
            "issue_body": state.get("issue_body", ""),
            "n_branches": state.get("n_branches", 3),
        }
        step_state = await flow.plan(step_inputs)
        return {
            "tot_branches_uri": step_state.outputs.get("tot_branches_uri", ""),
            "selected_branch_index": step_state.outputs.get("selected_branch_index", 0),
            "selected_branch": step_state.outputs.get("selected_branch", {}),
            "analysis_summary": step_state.outputs.get("analysis_summary", ""),
        }

    async def run_coder(state: DevOpsState) -> dict[str, Any]:
        flow = flow_class()
        flow._store = store
        flow._artifact_store = artifact_store
        flow._run_id = uuid.UUID(run_id)

        step_inputs = {
            "planner.selected_branch": state.get("selected_branch", {}),
            "planner.tot_branches_uri": state.get("tot_branches_uri", ""),
            "planner.analysis_summary": state.get("analysis_summary", ""),
            "issue_title": state.get("issue_title", ""),
            "issue_body": state.get("issue_body", ""),
            "repo_url": state.get("repo_url", ""),
            "commit_sha": state.get("commit_sha", ""),
            # Retry feedback
            "test_results_xml": state.get("test_results_xml", ""),
            "test_error_summary": state.get("test_error_summary", ""),
            "previous_patch_uri": state.get("previous_patch_uri", ""),
            "retry_count": state.get("retry_count", 0),
        }
        step_state = await flow.code(step_inputs)
        return {
            "patch_uri": step_state.outputs.get("patch_uri", ""),
            "cost_manifest_uri": step_state.outputs.get("cost_manifest_uri", ""),
        }

    async def run_tester(state: DevOpsState) -> dict[str, Any]:
        flow = flow_class()
        flow._store = store
        flow._artifact_store = artifact_store
        flow._run_id = uuid.UUID(run_id)

        step_inputs = {
            "patch_uri": state.get("patch_uri", ""),
            "commit_sha": state.get("commit_sha", ""),
            "repo_url": state.get("repo_url", ""),
            "test_command": state.get("test_command", ""),
            "retry_count": state.get("retry_count", 0),
        }
        step_state = await flow.test(step_inputs)
        
        test_passed = step_state.outputs.get("test_passed", False)
        retry_count = state.get("retry_count", 0)

        updates = {
            "test_passed": test_passed,
            "test_results_uri": step_state.outputs.get("test_results_uri", ""),
            "test_results_xml": step_state.outputs.get("test_results_xml", ""),
            "test_error_summary": step_state.outputs.get("test_error_summary", ""),
        }

        if not test_passed:
            # Increment retry count and feed back failed patch to coder
            updates["retry_count"] = retry_count + 1
            updates["previous_patch_uri"] = state.get("patch_uri", "")

        return updates

    async def run_reviewer(state: DevOpsState) -> dict[str, Any]:
        flow = flow_class()
        flow._store = store
        flow._artifact_store = artifact_store
        flow._run_id = uuid.UUID(run_id)

        step_inputs = {
            "patch_uri": state.get("patch_uri", ""),
            "issue_title": state.get("issue_title", ""),
            "issue_body": state.get("issue_body", ""),
        }
        step_state = await flow.review(step_inputs)
        return {
            "review_status_uri": step_state.outputs.get("review_status_uri", ""),
            "review_approved": step_state.outputs.get("review_approved", False),
        }

    async def run_pr_creator(state: DevOpsState) -> dict[str, Any]:
        flow = flow_class()
        flow._store = store
        flow._artifact_store = artifact_store
        flow._run_id = uuid.UUID(run_id)

        step_inputs = {
            "patch_uri": state.get("patch_uri", ""),
            "commit_sha": state.get("commit_sha", ""),
            "repo_url": state.get("repo_url", ""),
            "issue_title": state.get("issue_title", ""),
            "issue_number": state.get("issue_number", 0),
            "analysis_summary": state.get("analysis_summary", ""),
            "test_passed": state.get("test_passed", True),
            "review_approved": state.get("review_approved", True),
        }
        step_state = await flow.create_pr(step_inputs)
        return {
            "pr_url": step_state.outputs.get("pr_url", ""),
            "pr_number": step_state.outputs.get("pr_number", 0),
        }

    # ── Add Nodes and Edges ───────────────────────────────────────────────────

    builder.add_node("explorer", run_explorer)
    builder.add_node("planner", run_planner)
    builder.add_node("coder", run_coder)
    builder.add_node("tester", run_tester)
    builder.add_node("reviewer", run_reviewer)
    builder.add_node("pr_creator", run_pr_creator)

    builder.set_entry_point("explorer")
    builder.add_edge("explorer", "planner")
    builder.add_edge("planner", "coder")
    builder.add_edge("coder", "tester")

    builder.add_conditional_edges(
        "tester",
        tester_router,
        {
            "coder": "coder",
            "reviewer": "reviewer",
            END: END,
        }
    )

    builder.add_conditional_edges(
        "reviewer",
        review_router,
        {
            "pr_creator": "pr_creator",
            END: END,
        }
    )

    builder.add_edge("pr_creator", END)

    return graph_wrapper.compile(builder)
