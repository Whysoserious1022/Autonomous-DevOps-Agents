"""
cascade/storage/metadata.py
────────────────────────────
SQLAlchemy 2.0 async ORM for the Cascade metadata store.

Two tables:
  runs  — One row per pipeline execution.
  steps — One row per step execution attempt (including retries).

The store uses write-ahead semantics: a step row is INSERTed with
status='running' BEFORE execution, so a crash leaves a clear 'failed'
marker that resume logic can detect.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import orjson
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship

from cascade.core.state import RunState, RunStatus, StepState, StepStatus


# ── ORM Base ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── ORM Models ────────────────────────────────────────────────────────────────

class RunRow(Base):
    __tablename__ = "runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    flow_name = Column(String(255), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending")
    issue_url = Column(Text, nullable=True)
    repo_url = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=timezone.utc))
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    total_cost_cents = Column(Float, default=0.0)
    total_tokens = Column(Integer, default=0)
    tags = Column(JSON, default=dict)

    steps = relationship("StepRow", back_populates="run", cascade="all, delete-orphan")

    def to_domain(self) -> RunState:
        return RunState(
            id=uuid.UUID(self.id),
            flow_name=self.flow_name,
            status=RunStatus(self.status),
            issue_url=self.issue_url,
            repo_url=self.repo_url,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            total_cost_cents=self.total_cost_cents or 0.0,
            total_tokens=self.total_tokens or 0,
            tags=self.tags or {},
        )


class StepRow(Base):
    __tablename__ = "steps"

    __table_args__ = (
        # One completed record per (run, step_name, input_hash) — the cache key
        UniqueConstraint("run_id", "name", "input_hash", "status", name="uq_step_cache"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    parent_step = Column(String(36), nullable=True)

    input_hash = Column(String(64), nullable=False, index=True)
    step_version = Column(String(16), nullable=False, default="")

    inputs = Column(JSON, default=dict)
    outputs = Column(JSON, default=dict)
    artifact_uris = Column(JSON, default=list)

    status = Column(String(32), nullable=False, default="pending", index=True)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    error_message = Column(Text, nullable=True)
    error_traceback = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    llm_cost_cents = Column(Float, default=0.0)
    total_tokens = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    tags = Column(JSON, default=dict)

    run = relationship("RunRow", back_populates="steps")

    def to_domain(self) -> StepState:
        return StepState(
            id=uuid.UUID(self.id),
            run_id=uuid.UUID(self.run_id),
            name=self.name,
            parent_step=uuid.UUID(self.parent_step) if self.parent_step else None,
            input_hash=self.input_hash,
            step_version=self.step_version or "",
            inputs=self.inputs or {},
            outputs=self.outputs or {},
            artifact_uris=self.artifact_uris or [],
            status=StepStatus(self.status),
            retry_count=self.retry_count or 0,
            max_retries=self.max_retries or 3,
            error_message=self.error_message,
            error_traceback=self.error_traceback,
            started_at=self.started_at,
            completed_at=self.completed_at,
            llm_cost_cents=self.llm_cost_cents or 0.0,
            total_tokens=self.total_tokens or 0,
            prompt_tokens=self.prompt_tokens or 0,
            completion_tokens=self.completion_tokens or 0,
            tags=self.tags or {},
        )


# ── Metadata Store ────────────────────────────────────────────────────────────

class MetadataStore:
    """
    Async SQLAlchemy metadata store for Cascade runs and steps.

    Usage:
        store = MetadataStore("sqlite+aiosqlite:///~/.cascade/cascade.db")
        await store.initialize()
        run = await store.create_run("devops_workflow")
    """

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def initialize(self) -> None:
        """Create engine, run DDL migrations (CREATE TABLE IF NOT EXISTS)."""
        self._engine = create_async_engine(
            self._url,
            echo=False,
            future=True,
            # SQLite-specific: enable WAL mode for concurrent readers
            connect_args={"check_same_thread": False} if "sqlite" in self._url else {},
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()

    def _session(self) -> AsyncSession:
        if not self._session_factory:
            msg = "MetadataStore not initialized. Call await store.initialize() first."
            raise RuntimeError(msg)
        return self._session_factory()

    # ── Run Operations ────────────────────────────────────────────────────────

    async def create_run(
        self,
        flow_name: str,
        issue_url: str | None = None,
        repo_url: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> RunState:
        run = RunRow(
            id=str(uuid.uuid4()),
            flow_name=flow_name,
            status=RunStatus.PENDING.value,
            issue_url=issue_url,
            repo_url=repo_url,
            tags=tags or {},
            created_at=datetime.now(tz=timezone.utc),
        )
        async with self._session() as session:
            session.add(run)
            await session.commit()
            await session.refresh(run)
        return run.to_domain()

    async def get_run(self, run_id: str) -> RunState | None:
        async with self._session() as session:
            result = await session.execute(select(RunRow).where(RunRow.id == run_id))
            row = result.scalar_one_or_none()
            if row is None:
                return None
            # Load associated steps
            steps_result = await session.execute(
                select(StepRow).where(StepRow.run_id == run_id)
            )
            step_rows = steps_result.scalars().all()
            run_state = row.to_domain()
            run_state.steps = {s.name: s.to_domain() for s in step_rows}
        return run_state

    async def list_runs(self, limit: int = 50) -> list[RunState]:
        async with self._session() as session:
            result = await session.execute(
                select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)
            )
            rows = result.scalars().all()
        return [r.to_domain() for r in rows]

    async def update_run_status(self, run_id: str, status: RunStatus) -> None:
        updates: dict[str, Any] = {"status": status.value}
        if status == RunStatus.RUNNING:
            updates["started_at"] = datetime.now(tz=timezone.utc)
        elif status in (RunStatus.COMPLETED, RunStatus.FAILED):
            updates["completed_at"] = datetime.now(tz=timezone.utc)
        async with self._session() as session:
            await session.execute(update(RunRow).where(RunRow.id == run_id).values(**updates))
            await session.commit()

    async def update_run_cost(self, run_id: str, cost_cents: float, tokens: int) -> None:
        async with self._session() as session:
            result = await session.execute(select(RunRow).where(RunRow.id == run_id))
            row = result.scalar_one_or_none()
            if row:
                row.total_cost_cents = (row.total_cost_cents or 0.0) + cost_cents
                row.total_tokens = (row.total_tokens or 0) + tokens
                await session.commit()

    # ── Step Operations ───────────────────────────────────────────────────────

    async def get_cached_step(
        self, run_id: str, step_name: str, input_hash: str
    ) -> StepState | None:
        """
        Cache lookup: return a completed StepState if one exists for this
        (run_id, step_name, input_hash) combination. Returns None on miss.
        """
        async with self._session() as session:
            result = await session.execute(
                select(StepRow).where(
                    StepRow.run_id == run_id,
                    StepRow.name == step_name,
                    StepRow.input_hash == input_hash,
                    StepRow.status == StepStatus.COMPLETED.value,
                )
            )
            row = result.scalar_one_or_none()
        return row.to_domain() if row else None

    async def find_completed_step_globally(
        self, step_name: str, input_hash: str
    ) -> StepState | None:
        """
        Cross-run cache lookup: finds any completed step with this input_hash
        across ALL runs. Used for content-addressed caching (e.g., same repo SHA).
        """
        async with self._session() as session:
            result = await session.execute(
                select(StepRow)
                .where(
                    StepRow.name == step_name,
                    StepRow.input_hash == input_hash,
                    StepRow.status == StepStatus.COMPLETED.value,
                )
                .order_by(StepRow.completed_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return row.to_domain() if row else None

    async def upsert_step(self, state: StepState) -> StepState:
        """Insert or update a step row (identified by state.id)."""
        async with self._session() as session:
            result = await session.execute(
                select(StepRow).where(StepRow.id == str(state.id))
            )
            row = result.scalar_one_or_none()

            if row is None:
                row = StepRow(id=str(state.id))
                session.add(row)

            # Map domain → ORM
            row.run_id = str(state.run_id)
            row.name = state.name
            row.parent_step = str(state.parent_step) if state.parent_step else None
            row.input_hash = state.input_hash
            row.step_version = state.step_version
            row.inputs = state.inputs
            row.outputs = state.outputs
            row.artifact_uris = state.artifact_uris
            row.status = state.status.value
            row.retry_count = state.retry_count
            row.max_retries = state.max_retries
            row.error_message = state.error_message
            row.error_traceback = state.error_traceback
            row.started_at = state.started_at
            row.completed_at = state.completed_at
            row.llm_cost_cents = state.llm_cost_cents
            row.total_tokens = state.total_tokens
            row.prompt_tokens = state.prompt_tokens
            row.completion_tokens = state.completion_tokens
            row.tags = state.tags

            await session.commit()
            await session.refresh(row)
        return row.to_domain()

    async def get_step(self, step_id: str) -> StepState | None:
        async with self._session() as session:
            result = await session.execute(select(StepRow).where(StepRow.id == step_id))
            row = result.scalar_one_or_none()
        return row.to_domain() if row else None

    async def list_steps(self, run_id: str) -> list[StepState]:
        async with self._session() as session:
            result = await session.execute(
                select(StepRow)
                .where(StepRow.run_id == run_id)
                .order_by(StepRow.started_at)
            )
            rows = result.scalars().all()
        return [r.to_domain() for r in rows]

    async def delete_steps(self, run_id: str, names: list[str]) -> None:
        """Delete specific step records from the database for a run (e.g., on resume)."""
        from sqlalchemy import delete
        async with self._session() as session:
            await session.execute(
                delete(StepRow).where(
                    StepRow.run_id == run_id,
                    StepRow.name.in_(names),
                )
            )
            await session.commit()
