"""
tests/conftest.py
──────────────────
Shared pytest fixtures for Cascade test suite.
Uses in-memory SQLite and temporary artifact directories for isolation.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio

# Ensure the project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))




@pytest_asyncio.fixture
async def metadata_store(tmp_path: Path):
    """In-memory SQLite metadata store for test isolation."""
    from cascade.storage.metadata import MetadataStore

    db_url = f"sqlite+aiosqlite:///{tmp_path}/test_cascade.db"
    store = MetadataStore(db_url)
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def artifact_store(tmp_path: Path):
    """Temporary local artifact store for test isolation."""
    from cascade.storage.artifact_store import LocalArtifactStore

    return LocalArtifactStore(root=tmp_path / "artifacts")


@pytest_asyncio.fixture
async def flow_runner(tmp_path: Path):
    """FlowRunner backed by temp SQLite + temp artifact store."""
    from cascade.core.runner import FlowRunner

    runner = await FlowRunner.create(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        artifact_backend="local",
        root=tmp_path / "artifacts",
    )
    yield runner
    await runner._store.close()
