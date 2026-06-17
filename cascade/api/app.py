"""
cascade/api/app.py
──────────────────
FastAPI API server for Project Cascade.
Provides REST endpoints for inspecting runs/steps and WebSocket for real-time streaming.
"""

from __future__ import annotations

import asyncio
import os
import traceback
import uuid
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel

from fastapi import FastAPI, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from cascade.core.config import settings
from cascade.core.state import RunState, RunStatus, StepState, StepStatus
from cascade.storage.metadata import MetadataStore
from cascade.storage.artifact_store import create_artifact_store
from cascade.core.graph import build_devops_graph
from examples.devops_workflow import DevOpsFlow


app = FastAPI(
    title="Cascade API",
    description="REST & WebSocket API backend for the Cascade stateful AI orchestrator.",
    version="0.1.0",
)

# Enable CORS for frontend dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── WebSocket Subscription & Event Bus ────────────────────────────────────────

class EventBus:
    """Simple in-memory event bus to distribute status updates to WebSockets."""
    def __init__(self) -> None:
        self._listeners: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    def publish(self, run_id: str, data: dict[str, Any]) -> None:
        for q in self._listeners:
            q.put_nowait((run_id, data))


event_bus = EventBus()


# ── Observable Metadata Store ──────────────────────────────────────────────────

class ObservableMetadataStore(MetadataStore):
    """Subclass of MetadataStore that publishes write updates to the EventBus."""
    
    async def upsert_step(self, state: StepState) -> StepState:
        res = await super().upsert_step(state)
        event_bus.publish(
            str(state.run_id),
            {
                "type": "step_update",
                "run_id": str(state.run_id),
                "step": {
                    "id": str(state.id),
                    "name": state.name,
                    "status": state.status.value,
                    "retry_count": state.retry_count,
                    "duration_seconds": state.duration_seconds,
                    "llm_cost_cents": state.llm_cost_cents,
                    "total_tokens": state.total_tokens,
                    "error_message": state.error_message,
                    "outputs": state.outputs,
                },
            },
        )
        return res

    async def update_run_status(self, run_id: str, status: RunStatus) -> None:
        await super().update_run_status(run_id, status)
        event_bus.publish(
            run_id,
            {
                "type": "run_update",
                "run_id": run_id,
                "status": status.value,
            },
        )


# ── Lifespan Store Setup ───────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event() -> None:
    cfg = settings()
    # Initialize the observable store and artifact store
    store = ObservableMetadataStore(cfg.resolved_database_url)
    await store.initialize()
    
    artifact_kwargs: dict = {}
    if cfg.artifact_backend == "local":
        artifact_kwargs["root"] = cfg.resolved_artifact_local_root
    elif cfg.artifact_backend == "s3":
        artifact_kwargs["bucket"] = cfg.artifact_s3_bucket
        if cfg.artifact_s3_endpoint_url:
            artifact_kwargs["endpoint_url"] = cfg.artifact_s3_endpoint_url
    artifact_store = create_artifact_store(cfg.artifact_backend, **artifact_kwargs)

    app.state.store = store
    app.state.artifact_store = artifact_store
    print(f"[Cascade] Startup complete. DB={cfg.resolved_database_url!r}, Artifacts={cfg.artifact_backend!r}, LLM={cfg.llm_model!r}")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if hasattr(app.state, "store"):
        await app.state.store.close()


# ── API Schemas ───────────────────────────────────────────────────────────────

class RunTriggerPayload(BaseModel):
    repo_url: str
    issue_title: str
    issue_body: Optional[str] = "No description provided."
    commit_sha: Optional[str] = ""
    test_command: Optional[str] = ""
    n_branches: Optional[int] = 3


class ResumePayload(BaseModel):
    from_step: str


# ── Background Task Runner ────────────────────────────────────────────────────

async def run_workflow_graph_in_background(
    run_id: str,
    repo_url: str,
    issue_title: str,
    issue_body: str,
    commit_sha: str,
    test_command: str,
    n_branches: int,
) -> None:
    """Executes the full compiled DevOps pipeline graph as a background task."""
    try:
        # Build state graph
        graph = build_devops_graph(
            flow_class=DevOpsFlow,
            store=app.state.store,
            artifact_store=app.state.artifact_store,
            run_id=run_id,
        )

        initial_state = {
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "issue_title": issue_title,
            "issue_body": issue_body,
            "n_branches": n_branches,
            "test_command": test_command,
            "retry_count": 0,
        }

        # Update run status in DB to RUNNING
        await app.state.store.update_run_status(run_id, RunStatus.RUNNING)
        
        # Execute
        await graph.run(initial_state)
        
        # Mark completed in DB
        await app.state.store.update_run_status(run_id, RunStatus.COMPLETED)
    except Exception as e:
        print(f"Error running DevOps pipeline: {e}")
        traceback.print_exc()
        await app.state.store.update_run_status(run_id, RunStatus.FAILED)


async def resume_workflow_graph_in_background(
    run_id: str,
    from_step: str,
) -> None:
    """Resumes a workflow graph from a specific step."""
    try:
        # Update run status to RESUMED
        await app.state.store.update_run_status(run_id, RunStatus.RESUMED)
        
        # Compile graph
        graph = build_devops_graph(
            flow_class=DevOpsFlow,
            store=app.state.store,
            artifact_store=app.state.artifact_store,
            run_id=run_id,
        )

        # Build resume config and execute
        config = graph.resume(from_node=from_step)
        
        # Run graph from the checkpoint
        await graph._graph.ainvoke(None, config=config)
        
        # Mark completed
        await app.state.store.update_run_status(run_id, RunStatus.COMPLETED)
    except Exception as e:
        print(f"Error resuming DevOps pipeline: {e}")
        traceback.print_exc()
        await app.state.store.update_run_status(run_id, RunStatus.FAILED)


# ── REST Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/runs")
async def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    """List all pipeline runs, most recent first."""
    runs = await app.state.store.list_runs(limit=limit)
    return [
        {
            "id": str(r.id),
            "flow_name": r.flow_name,
            "status": r.status.value,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "total_cost_cents": r.total_cost_cents,
            "total_tokens": r.total_tokens,
            "repo_url": r.repo_url,
            "issue_url": r.issue_url,
            "tags": r.tags,
        }
        for r in runs
    ]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    """Retrieve details of a specific run and its step history."""
    run = await app.state.store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
        
    return {
        "id": str(run.id),
        "flow_name": run.flow_name,
        "status": run.status.value,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "total_cost_cents": run.total_cost_cents,
        "total_tokens": run.total_tokens,
        "repo_url": run.repo_url,
        "issue_url": run.issue_url,
        "tags": run.tags,
        "steps": {
            name: {
                "id": str(s.id),
                "name": s.name,
                "status": s.status.value,
                "retry_count": s.retry_count,
                "max_retries": s.max_retries,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                "llm_cost_cents": s.llm_cost_cents,
                "total_tokens": s.total_tokens,
                "error_message": s.error_message,
                "artifact_uris": s.artifact_uris,
                "inputs": s.inputs,
                "outputs": s.outputs,
            }
            for name, s in run.steps.items()
        },
    }


@app.get("/api/runs/{run_id}/steps/{step_name}/logs")
async def get_step_logs(run_id: str, step_name: str) -> Dict[str, Any]:
    """Fetch terminal/traceback logs for a specific step."""
    run = await app.state.store.get_run(run_id)
    if not run or step_name not in run.steps:
        raise HTTPException(status_code=404, detail="Step not found")
        
    step = run.steps[step_name]
    logs = []

    # If the step failed with a traceback, fetch the traceback artifact
    traceback_content = ""
    if step.error_traceback:
        traceback_content = step.error_traceback
    elif step.status in (StepStatus.FAILED, StepStatus.PERMANENTLY_FAILED):
        # Scan artifacts for error traceback URI
        for uri in step.artifact_uris:
            if "traceback" in uri or uri.endswith(".txt"):
                try:
                    traceback_content = app.state.artifact_store.get_text(uri)
                    break
                except Exception:
                    pass

    if traceback_content:
        logs.append(traceback_content)
    else:
        # Generate clean structured execution log
        logs.append(f"=== [{step.status.value.upper()}] Step Execution: {step_name} ===")
        logs.append(f"Started At: {step.started_at.isoformat() if step.started_at else 'N/A'}")
        if step.completed_at:
            logs.append(f"Completed At: {step.completed_at.isoformat()}")
            logs.append(f"Duration: {step.duration_seconds:.2f} seconds")
        logs.append(f"Retry Attempt: {step.retry_count}/{step.max_retries}")
        logs.append(f"LLM Calls Cost: ${step.llm_cost_cents / 100:.4f}")
        logs.append(f"Total Tokens: {step.total_tokens}")
        if step.inputs:
            logs.append("\nInputs:")
            for k, v in step.inputs.items():
                val_str = str(v)[:200] + "..." if len(str(v)) > 200 else str(v)
                logs.append(f"  - {k}: {val_str}")
        if step.outputs:
            logs.append("\nOutputs:")
            for k, v in step.outputs.items():
                val_str = str(v)[:200] + "..." if len(str(v)) > 200 else str(v)
                logs.append(f"  - {k}: {val_str}")

    return {"logs": "\n".join(logs)}


@app.post("/api/runs", status_code=202)
async def trigger_new_run(
    payload: RunTriggerPayload,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Start a new DevOps workflow execution in the background."""
    run_state = await app.state.store.create_run(
        flow_name="devops_workflow",
        repo_url=payload.repo_url,
        issue_url=payload.repo_url + "/issues",  # Mock placeholder
        tags={"source": "api"},
    )
    
    background_tasks.add_task(
        run_workflow_graph_in_background,
        run_id=str(run_state.id),
        repo_url=payload.repo_url,
        issue_title=payload.issue_title,
        issue_body=payload.issue_body,
        commit_sha=payload.commit_sha,
        test_command=payload.test_command,
        n_branches=payload.n_branches,
    )
    
    return {
        "message": "Workflow run queued.",
        "run_id": str(run_state.id),
        "status": run_state.status.value,
    }


@app.post("/api/runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: str,
    payload: ResumePayload,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Resume a failed run from the specified step in the background."""
    run = await app.state.store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    background_tasks.add_task(
        resume_workflow_graph_in_background,
        run_id=run_id,
        from_step=payload.from_step,
    )
    
    return {
        "message": f"Resume from step '{payload.from_step}' queued.",
        "run_id": run_id,
        "status": "resuming",
    }


# ── WebSockets Endpoint ───────────────────────────────────────────────────────

@app.websocket("/api/runs/{run_id}/stream")
async def websocket_stream(websocket: WebSocket, run_id: str) -> None:
    """Stream real-time run progress, cost, and step updates via WebSocket."""
    q = event_bus.subscribe()
    try:
        await websocket.accept()
        
        # Send initial full run state to synchronize the dashboard
        run = await app.state.store.get_run(run_id)
        if run:
            await websocket.send_json({
                "type": "initial_state",
                "status": run.status.value,
                "total_cost_cents": run.total_cost_cents,
                "total_tokens": run.total_tokens,
                "steps": {
                    name: {
                        "name": s.name,
                        "status": s.status.value,
                        "retry_count": s.retry_count,
                        "duration_seconds": s.duration_seconds,
                        "llm_cost_cents": s.llm_cost_cents,
                        "total_tokens": s.total_tokens,
                        "error_message": s.error_message,
                    }
                    for name, s in run.steps.items()
                }
            })

        while True:
            # Wait for event matching this run_id
            msg_run_id, event = await q.get()
            if msg_run_id == run_id:
                await websocket.send_json(event)
            q.task_done()
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(q)
