"""
cascade/api/app.py
──────────────────
FastAPI API server for Project Cascade.
Provides REST endpoints for inspecting runs/steps, WebSocket for real-time streaming,
GitHub Webhook receiver, and the autonomous GitHub Issue Poller management API.
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env

import asyncio
import hashlib
import hmac
import json
import os
import traceback
import uuid
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, WebSocket, WebSocketDisconnect
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
    version="1.0.0",
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

    def broadcast(self, data: dict[str, Any]) -> None:
        """Publish a global event (not tied to a specific run)."""
        for q in self._listeners:
            q.put_nowait(("__global__", data))


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

    # Initialize the GitHub Poller (managed lifecycle)
    from cascade.watcher.github_poller import GithubPoller

    async def _poller_trigger(repo_url: str, issue: Any) -> None:
        """Called by the poller when a new agent-task issue is found."""
        run_state = await app.state.store.create_run(
            flow_name="devops_workflow",
            repo_url=repo_url,
            issue_url=issue.html_url,
            tags={"source": "github_poller", "issue_number": str(issue.number)},
        )
        asyncio.create_task(
            run_workflow_graph_in_background(
                run_id=str(run_state.id),
                repo_url=repo_url,
                issue_title=issue.title,
                issue_body=issue.body or "No description provided.",
                commit_sha="",
                test_command="",
                n_branches=3,
                issue_number=issue.number,
            )
        )
        event_bus.broadcast({
            "type": "poller_triggered",
            "repo_url": repo_url,
            "issue_number": issue.number,
            "issue_title": issue.title,
            "run_id": str(run_state.id),
        })
        print(f"[Cascade] Poller auto-triggered run {run_state.id} for issue #{issue.number}")

    app.state.poller = GithubPoller(
        trigger_callback=_poller_trigger,
        poll_interval=int(os.getenv("CASCADE_POLL_INTERVAL", "60")),
    )
    app.state.poller_status = "idle"

    print(
        f"[Cascade] Startup complete. "
        f"DB={cfg.resolved_database_url!r}, "
        f"Artifacts={cfg.artifact_backend!r}, "
        f"LLM={cfg.llm_model!r}"
    )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if hasattr(app.state, "store"):
        await app.state.store.close()
    if hasattr(app.state, "poller"):
        await app.state.poller.shutdown()


# ── API Schemas ───────────────────────────────────────────────────────────────

class RunTriggerPayload(BaseModel):
    repo_url: str
    issue_title: str
    issue_body: Optional[str] = "No description provided."
    issue_number: Optional[int] = 0
    commit_sha: Optional[str] = ""
    test_command: Optional[str] = ""
    n_branches: Optional[int] = 3


class ResumePayload(BaseModel):
    from_step: str


class WatchRepoPayload(BaseModel):
    repo_url: str
    label: Optional[str] = "agent-task"
    poll_interval: Optional[int] = 60


# ── Background Task Runner ────────────────────────────────────────────────────

async def run_workflow_graph_in_background(
    run_id: str,
    repo_url: str,
    issue_title: str,
    issue_body: str,
    commit_sha: str,
    test_command: str,
    n_branches: int,
    issue_number: int = 0,
) -> None:
    """Executes the full compiled DevOps pipeline graph as a background task."""
    try:
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
            "issue_number": issue_number,
            "n_branches": n_branches,
            "test_command": test_command,
            "retry_count": 0,
        }

        await app.state.store.update_run_status(run_id, RunStatus.RUNNING)
        await graph.run(initial_state)
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
        # Invalidate/delete database cache for from_step and downstream steps
        from cascade.core.runner import _topological_sort
        flow = DevOpsFlow()
        steps = flow.get_steps()
        ordered = _topological_sort(steps)
        ordered_names = [m.name for m in ordered]
        if from_step in ordered_names:
            idx = ordered_names.index(from_step)
            steps_to_delete = ordered_names[idx:]
            await app.state.store.delete_steps(run_id, steps_to_delete)

        await app.state.store.update_run_status(run_id, RunStatus.RESUMED)

        graph = build_devops_graph(
            flow_class=DevOpsFlow,
            store=app.state.store,
            artifact_store=app.state.artifact_store,
            run_id=run_id,
        )

        config = graph.resume(from_node=from_step)
        await graph._graph.ainvoke(None, config=config)
        await app.state.store.update_run_status(run_id, RunStatus.COMPLETED)
    except Exception as e:
        print(f"Error resuming DevOps pipeline: {e}")
        traceback.print_exc()
        await app.state.store.update_run_status(run_id, RunStatus.FAILED)


# ── REST Endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "version": "1.0.0",
        "poller_watching": app.state.poller.list_watched() if hasattr(app.state, "poller") else [],
    }


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
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if step_name not in run.steps:
        return {
            "logs": f"=== Step {step_name} has not started yet ===\nWaiting for execution..."
        }

    step = run.steps[step_name]
    logs = []

    traceback_content = ""
    if step.error_traceback:
        traceback_content = step.error_traceback
    elif step.status in (StepStatus.FAILED, StepStatus.PERMANENTLY_FAILED):
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
    issue_url = (
        f"{payload.repo_url}/issues/{payload.issue_number}"
        if payload.issue_number
        else f"{payload.repo_url}/issues"
    )
    run_state = await app.state.store.create_run(
        flow_name="devops_workflow",
        repo_url=payload.repo_url,
        issue_url=issue_url,
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
        issue_number=payload.issue_number or 0,
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


# ── GitHub Poller Management Endpoints ────────────────────────────────────────

@app.get("/api/poller/watched")
async def get_watched_repos() -> Dict[str, Any]:
    """List all repositories currently being watched by the poller."""
    watched = app.state.poller.list_watched() if hasattr(app.state, "poller") else []
    return {
        "watching": watched,
        "count": len(watched),
        "label": "agent-task",
    }


@app.post("/api/poller/watch", status_code=200)
async def start_watching_repo(payload: WatchRepoPayload) -> Dict[str, Any]:
    """Start autonomously watching a GitHub repository for agent-task issues."""
    if not hasattr(app.state, "poller"):
        raise HTTPException(status_code=503, detail="Poller not initialized")

    app.state.poller.watch(payload.repo_url)
    event_bus.broadcast({
        "type": "poller_watch_started",
        "repo_url": payload.repo_url,
    })

    return {
        "message": f"Now watching {payload.repo_url}",
        "repo_url": payload.repo_url,
        "label": payload.label,
        "poll_interval_seconds": payload.poll_interval,
        "watching": app.state.poller.list_watched(),
    }


@app.post("/api/poller/unwatch")
async def stop_watching_repo(payload: WatchRepoPayload) -> Dict[str, Any]:
    """Stop watching a repository."""
    if not hasattr(app.state, "poller"):
        raise HTTPException(status_code=503, detail="Poller not initialized")

    stopped = app.state.poller.unwatch(payload.repo_url)
    event_bus.broadcast({
        "type": "poller_watch_stopped",
        "repo_url": payload.repo_url,
    })

    return {
        "message": f"Stopped watching {payload.repo_url}" if stopped else f"Not watching {payload.repo_url}",
        "repo_url": payload.repo_url,
        "was_watching": stopped,
        "watching": app.state.poller.list_watched(),
    }


# ── GitHub Webhook Endpoint ───────────────────────────────────────────────────

@app.post("/api/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """
    GitHub Webhook receiver.

    Configure on GitHub: Settings → Webhooks → Add webhook
    - Payload URL: http://your-server:8000/api/webhook/github
    - Content type: application/json
    - Events: Issues, Issue comments

    Set CASCADE_WEBHOOK_SECRET env var to verify request signatures.
    """
    # Verify HMAC signature (if webhook secret is set)
    webhook_secret = os.getenv("CASCADE_WEBHOOK_SECRET", "")
    if webhook_secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        body = await request.body()
        expected = "sha256=" + hmac.new(
            webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    else:
        body = await request.body()

    event_type = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body)
    action = payload.get("action", "")

    # Only process "labeled" events where label is "agent-task"
    if event_type != "issues" or action != "labeled":
        return {"message": "Event ignored", "event": event_type, "action": action}

    label_name = payload.get("label", {}).get("name", "")
    if label_name != "agent-task":
        return {"message": f"Label '{label_name}' ignored — not agent-task"}

    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    repo_url = repo.get("html_url", "")
    issue_number = issue.get("number", 0)
    issue_title = issue.get("title", "No title")
    issue_body = issue.get("body", "") or "No description provided."
    issue_url = issue.get("html_url", "")

    # Create run record
    run_state = await app.state.store.create_run(
        flow_name="devops_workflow",
        repo_url=repo_url,
        issue_url=issue_url,
        tags={
            "source": "github_webhook",
            "issue_number": str(issue_number),
            "label": label_name,
        },
    )

    # Queue the pipeline execution
    background_tasks.add_task(
        run_workflow_graph_in_background,
        run_id=str(run_state.id),
        repo_url=repo_url,
        issue_title=issue_title,
        issue_body=issue_body,
        commit_sha="",
        test_command="",
        n_branches=3,
        issue_number=issue_number,
    )

    event_bus.broadcast({
        "type": "webhook_triggered",
        "repo_url": repo_url,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "run_id": str(run_state.id),
    })

    print(f"[Webhook] Triggered run {run_state.id} for issue #{issue_number}: {issue_title!r}")

    return {
        "message": "Workflow triggered via webhook",
        "run_id": str(run_state.id),
        "issue_number": issue_number,
        "issue_title": issue_title,
    }


# ── Additional REST Endpoints ────────────────────────────────────────────────

@app.get("/api/stats")
async def get_aggregate_stats() -> Dict[str, Any]:
    """
    Aggregate statistics across all pipeline runs.
    Returns counts by status, total costs, token usage, and recent activity.
    """
    runs = await app.state.store.list_runs(limit=500)

    total_runs = len(runs)
    completed = sum(1 for r in runs if r.status == RunStatus.COMPLETED)
    failed = sum(1 for r in runs if r.status == RunStatus.FAILED)
    running = sum(1 for r in runs if r.status == RunStatus.RUNNING)
    pending = sum(1 for r in runs if r.status == RunStatus.PENDING)

    total_cost_cents = sum(r.total_cost_cents for r in runs)
    total_tokens = sum(r.total_tokens for r in runs)

    # Average cost for completed runs only
    completed_runs = [r for r in runs if r.status == RunStatus.COMPLETED]
    avg_cost_cents = (
        sum(r.total_cost_cents for r in completed_runs) / len(completed_runs)
        if completed_runs else 0.0
    )

    # Recent 10 runs
    recent = [
        {
            "id": str(r.id),
            "status": r.status.value,
            "flow_name": r.flow_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "total_cost_cents": r.total_cost_cents,
        }
        for r in runs[:10]
    ]

    return {
        "total_runs": total_runs,
        "by_status": {
            "completed": completed,
            "failed": failed,
            "running": running,
            "pending": pending,
        },
        "total_cost_cents": total_cost_cents,
        "total_cost_dollars": total_cost_cents / 100,
        "total_tokens": total_tokens,
        "avg_cost_cents_per_run": avg_cost_cents,
        "recent_runs": recent,
    }


@app.get("/api/runs/{run_id}/cost-breakdown")
async def get_run_cost_breakdown(run_id: str) -> Dict[str, Any]:
    """
    Per-step cost breakdown for a specific run.
    Returns LLM token costs and token usage per agent/step.
    """
    run = await app.state.store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    steps_breakdown = []
    for name, s in run.steps.items():
        steps_breakdown.append({
            "step_name": name,
            "status": s.status.value,
            "llm_cost_cents": s.llm_cost_cents,
            "llm_cost_dollars": s.llm_cost_cents / 100,
            "total_tokens": s.total_tokens,
            "prompt_tokens": s.prompt_tokens,
            "completion_tokens": s.completion_tokens,
            "duration_seconds": s.duration_seconds,
            "cost_per_token": (
                s.llm_cost_cents / s.total_tokens if s.total_tokens > 0 else 0.0
            ),
        })

    # Sort by cost descending
    steps_breakdown.sort(key=lambda x: x["llm_cost_cents"], reverse=True)

    return {
        "run_id": run_id,
        "total_cost_cents": run.total_cost_cents,
        "total_cost_dollars": run.total_cost_cents / 100,
        "total_tokens": run.total_tokens,
        "steps": steps_breakdown,
    }


@app.get("/api/runs/{run_id}/knowledge-graph")
async def get_run_knowledge_graph(run_id: str) -> Dict[str, Any]:
    """
    Retrieve the repository knowledge graph (AST structure) for a run.
    Returns the RepoGraph built by the Explorer agent, if available.
    """
    run = await app.state.store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Look for explorer step outputs which contain the repo_graph_uri
    explorer_step = run.steps.get("explorer")
    if not explorer_step:
        return {
            "available": False,
            "message": "Explorer step has not completed yet.",
        }

    repo_graph_uri = explorer_step.outputs.get("repo_graph_uri", "")
    if not repo_graph_uri:
        return {
            "available": False,
            "message": "Explorer step completed but repo_graph_uri is missing from outputs.",
        }

    try:
        graph_data = app.state.artifact_store.get_json(repo_graph_uri)
        return {
            "available": True,
            "repo_url": graph_data.get("repo_url", ""),
            "commit_sha": graph_data.get("commit_sha", ""),
            "total_files_analyzed": graph_data.get("total_files_analyzed", 0),
            "summary": graph_data.get("summary", ""),
            "files": graph_data.get("files", []),
            "artifact_uri": repo_graph_uri,
        }
    except (KeyError, Exception) as e:
        return {
            "available": False,
            "message": f"Could not load knowledge graph from artifact store: {e}",
        }


@app.post("/api/runs/{run_id}/cancel", status_code=200)
async def cancel_run(run_id: str) -> Dict[str, Any]:
    """
    Request cancellation of a running pipeline run.
    Sets the run status to FAILED to signal that it should stop.
    Note: This is a best-effort operation — in-flight LLM calls may complete.
    """
    run = await app.state.store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status not in (RunStatus.RUNNING, RunStatus.PENDING, RunStatus.RESUMED):
        return {
            "message": f"Run is not active (status: {run.status.value}). Cannot cancel.",
            "run_id": run_id,
            "status": run.status.value,
        }

    await app.state.store.update_run_status(run_id, RunStatus.FAILED)
    event_bus.broadcast({
        "type": "run_cancelled",
        "run_id": run_id,
    })

    return {
        "message": "Run cancellation requested.",
        "run_id": run_id,
        "status": RunStatus.FAILED.value,
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
            msg_run_id, event = await q.get()
            if msg_run_id in (run_id, "__global__"):
                await websocket.send_json(event)
            q.task_done()
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(q)


@app.websocket("/api/global/stream")
async def websocket_global_stream(websocket: WebSocket) -> None:
    """Global event stream: poller events, new runs, system-wide broadcasts."""
    q = event_bus.subscribe()
    try:
        await websocket.accept()

        # Send initial poller state
        watched = app.state.poller.list_watched() if hasattr(app.state, "poller") else []
        await websocket.send_json({
            "type": "initial_poller_state",
            "watching": watched,
        })

        while True:
            msg_run_id, event = await q.get()
            # Send global events and all run updates
            await websocket.send_json({**event, "run_id_context": msg_run_id})
            q.task_done()
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(q)
