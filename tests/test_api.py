"""
tests/test_api.py
──────────────────
Tests for the Cascade FastAPI server.
"""

from __future__ import annotations

import asyncio
import uuid
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from cascade.api.app import app, ObservableMetadataStore
from cascade.core.state import RunState, RunStatus, StepState, StepStatus
from cascade.storage.artifact_store import LocalArtifactStore


@pytest.fixture(autouse=True)
def mock_background_tasks():
    """Mock the background task functions to prevent real graph execution in tests."""
    with patch("cascade.api.app.run_workflow_graph_in_background") as mock_run, \
         patch("cascade.api.app.resume_workflow_graph_in_background") as mock_resume:
        yield mock_run, mock_resume


@pytest.fixture
def test_client(tmp_path):
    """Provide a TestClient with pre-initialized mock stores, bypassing startup/shutdown events."""
    # Prevent default lifespan startup/shutdown from overriding test store
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()

    store = ObservableMetadataStore(f"sqlite+aiosqlite:///{tmp_path}/api_test.db")
    asyncio.run(store.initialize())
    
    artifact_store = LocalArtifactStore(root=tmp_path / "artifacts")
    
    app.state.store = store
    app.state.artifact_store = artifact_store

    with TestClient(app) as client:
        yield client

    asyncio.run(store.close())


def test_list_runs_empty(test_client):
    response = test_client.get("/api/runs")
    assert response.status_code == 200
    assert response.json() == []


def test_trigger_and_get_run(test_client):
    # Trigger a run
    payload = {
        "repo_url": "https://github.com/test/repo",
        "issue_title": "test issue",
        "issue_body": "test description",
        "n_branches": 2
    }
    response = test_client.post("/api/runs", json=payload)
    assert response.status_code == 202
    data = response.json()
    assert "run_id" in data
    assert data["status"] == "pending"

    run_id = data["run_id"]

    # Get the run details
    response_get = test_client.get(f"/api/runs/{run_id}")
    assert response_get.status_code == 200
    run_details = response_get.json()
    assert run_details["id"] == run_id
    assert run_details["flow_name"] == "devops_workflow"
    assert run_details["repo_url"] == "https://github.com/test/repo"


def test_get_nonexistent_run(test_client):
    bad_id = str(uuid.uuid4())
    response = test_client.get(f"/api/runs/{bad_id}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Run not found"


def test_get_step_logs(test_client):
    store = app.state.store
    
    run_id = uuid.uuid4()
    step_id = uuid.uuid4()
    
    # Pre-create run and step in DB
    async def create_data():
        await store.create_run("devops_workflow", repo_url="https://github.com/test", tags={})
        from cascade.storage.metadata import RunRow
        async with store._session() as session:
            r = RunRow(id=str(run_id), flow_name="devops_workflow", status="failed")
            session.add(r)
            await session.commit()
            
        step_state = StepState(
            id=step_id,
            run_id=run_id,
            name="explorer",
            input_hash="hash123",
            status=StepStatus.FAILED,
            inputs={"some": "input"},
            outputs={"some": "output"},
            error_message="Failed step",
            error_traceback="Traceback: line 42",
        )
        await store.upsert_step(step_state)

    asyncio.run(create_data())

    # Get step logs
    response = test_client.get(f"/api/runs/{run_id}/steps/explorer/logs")
    assert response.status_code == 200
    data = response.json()
    assert "Traceback: line 42" in data["logs"]


def test_resume_run_endpoint(test_client):
    run_id = uuid.uuid4()
    
    # Pre-create run
    async def create_data():
        from cascade.storage.metadata import RunRow
        async with app.state.store._session() as session:
            r = RunRow(id=str(run_id), flow_name="devops_workflow", status="failed")
            session.add(r)
            await session.commit()

    asyncio.run(create_data())

    # Resume the run
    response = test_client.post(f"/api/runs/{run_id}/resume", json={"from_step": "tester"})
    assert response.status_code == 202
    data = response.json()
    assert data["run_id"] == str(run_id)
    assert data["status"] == "resuming"


def test_websocket_stream_connect(test_client):
    run_id = str(uuid.uuid4())
    
    # Pre-create run
    async def create_data():
        from cascade.storage.metadata import RunRow
        async with app.state.store._session() as session:
            r = RunRow(id=str(run_id), flow_name="devops_workflow", status="running")
            session.add(r)
            await session.commit()

    asyncio.run(create_data())

    # Connect to WebSocket
    with test_client.websocket_connect(f"/api/runs/{run_id}/stream") as websocket:
        data = websocket.receive_json()
        assert data["type"] == "initial_state"
        assert data["status"] == "running"
