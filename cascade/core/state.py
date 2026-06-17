"""
cascade/core/state.py
─────────────────────
Pydantic models for all Cascade runtime state objects.

StepState   — Represents one execution of a single @step node.
RunState    — Represents the full lifecycle of a pipeline run.
StepStatus  — Enumeration of step lifecycle states.
"""

from __future__ import annotations

import hashlib
import inspect
import textwrap
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import orjson
from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class StepStatus(str, Enum):
    """Lifecycle states for a single step execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"          # Cache hit — outputs loaded from store
    PERMANENTLY_FAILED = "permanently_failed"  # Retry budget exhausted


class RunStatus(str, Enum):
    """Lifecycle states for a full pipeline run."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RESUMED = "resumed"


# ── Step State ────────────────────────────────────────────────────────────────

class StepState(BaseModel):
    """
    The atomic unit of Cascade state. One record per step execution.

    Every field here is persisted to the metadata DB and used to determine
    whether a step can be skipped (cache hit) or must be re-executed.
    """

    model_config = {"arbitrary_types_allowed": True}

    # ── Identity ──────────────────────────────────────────────────────────────
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    name: str = Field(description="Unique step name within a flow.")
    parent_step: UUID | None = Field(
        default=None,
        description="Step ID that produced the primary input for this step.",
    )

    # ── Cache Key ─────────────────────────────────────────────────────────────
    input_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of serialized inputs + step source code. "
            "Determines cache hit / miss. Computed automatically if empty."
        ),
    )

    # ── Data ──────────────────────────────────────────────────────────────────
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Serializable inputs passed into this step.",
    )
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Serializable outputs produced by this step.",
    )
    artifact_uris: list[str] = Field(
        default_factory=list,
        description=(
            "Content-addressed URIs (sha256://<hash>) pointing to large objects "
            "in the artifact store (patches, graphs, logs, etc.)."
        ),
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status: StepStatus = StepStatus.PENDING
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    error_message: str | None = None
    error_traceback: str | None = None

    # ── Timing ────────────────────────────────────────────────────────────────
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # ── Cost Tracking ─────────────────────────────────────────────────────────
    llm_cost_cents: float = Field(default=0.0, ge=0.0)
    total_tokens: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

    # ── Metadata ──────────────────────────────────────────────────────────────
    step_version: str = Field(
        default="",
        description="SHA-256 of the step's source code at decoration time.",
    )
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_must_be_slug(cls, v: str) -> str:
        """Step names must be valid Python identifiers (used as DB keys)."""
        if not v.replace("_", "").replace("-", "").isalnum():
            msg = f"Step name '{v}' must only contain alphanumeric characters, underscores, or hyphens."
            raise ValueError(msg)
        return v.lower().replace("-", "_")

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock time for this step, or None if not yet completed."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def can_retry(self) -> bool:
        """True if this step has retries remaining."""
        return self.retry_count < self.max_retries

    @property
    def is_terminal(self) -> bool:
        """True if the step is in a state that cannot transition further."""
        return self.status in (
            StepStatus.COMPLETED,
            StepStatus.SKIPPED,
            StepStatus.PERMANENTLY_FAILED,
        )

    def mark_running(self) -> "StepState":
        """Transition to RUNNING, recording start time."""
        return self.model_copy(
            update={
                "status": StepStatus.RUNNING,
                "started_at": datetime.now(tz=timezone.utc),
            }
        )

    def mark_completed(
        self,
        outputs: dict[str, Any],
        artifact_uris: list[str] | None = None,
        llm_cost_cents: float = 0.0,
        total_tokens: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> "StepState":
        """Transition to COMPLETED with outputs."""
        return self.model_copy(
            update={
                "status": StepStatus.COMPLETED,
                "outputs": outputs,
                "artifact_uris": artifact_uris or self.artifact_uris,
                "completed_at": datetime.now(tz=timezone.utc),
                "llm_cost_cents": llm_cost_cents,
                "total_tokens": total_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def mark_failed(self, error_message: str, traceback: str = "") -> "StepState":
        """Transition to FAILED or PERMANENTLY_FAILED (if no retries left)."""
        new_retry_count = self.retry_count + 1
        new_status = (
            StepStatus.PERMANENTLY_FAILED
            if new_retry_count >= self.max_retries
            else StepStatus.FAILED
        )
        return self.model_copy(
            update={
                "status": new_status,
                "retry_count": new_retry_count,
                "error_message": error_message,
                "error_traceback": traceback,
                "completed_at": datetime.now(tz=timezone.utc),
            }
        )

    def mark_skipped(self, outputs: dict[str, Any], artifact_uris: list[str] | None = None) -> "StepState":
        """Transition to SKIPPED — outputs loaded from cache."""
        return self.model_copy(
            update={
                "status": StepStatus.SKIPPED,
                "outputs": outputs,
                "artifact_uris": artifact_uris or self.artifact_uris,
                "completed_at": datetime.now(tz=timezone.utc),
            }
        )


# ── Run State ─────────────────────────────────────────────────────────────────

class RunState(BaseModel):
    """Represents the top-level lifecycle of an entire pipeline run."""

    id: UUID = Field(default_factory=uuid4)
    flow_name: str
    status: RunStatus = RunStatus.PENDING
    issue_url: str | None = None
    repo_url: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Aggregated cost across all steps
    total_cost_cents: float = 0.0
    total_tokens: int = 0

    # Map of step_name → StepState for the current run
    steps: dict[str, StepState] = Field(default_factory=dict)

    # User-defined tags (e.g., {"env": "production", "repo": "fastapi"})
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.status in (RunStatus.COMPLETED, RunStatus.FAILED)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    def get_step(self, name: str) -> StepState | None:
        return self.steps.get(name)

    def cost_summary(self) -> dict[str, float]:
        """Returns per-step cost breakdown."""
        return {name: state.llm_cost_cents for name, state in self.steps.items()}


# ── Hash Utilities ────────────────────────────────────────────────────────────

def compute_input_hash(inputs: dict[str, Any], step_source: str) -> str:
    """
    Compute a deterministic SHA-256 hash over the step's inputs and source code.

    This is the cache key: identical inputs + identical code → same hash → skip.
    The step source code is included so that code changes invalidate the cache.
    """
    # Serialize inputs deterministically using orjson (handles more types than json)
    try:
        inputs_bytes = orjson.dumps(inputs, option=orjson.OPT_SORT_KEYS)
    except TypeError:
        # Fall back to repr for non-serializable inputs (e.g., complex objects)
        inputs_bytes = repr(inputs).encode("utf-8")

    # Normalize source: strip leading whitespace (handles indented methods)
    normalized_source = textwrap.dedent(step_source).strip().encode("utf-8")

    hasher = hashlib.sha256()
    hasher.update(inputs_bytes)
    hasher.update(b"\x00")  # Separator
    hasher.update(normalized_source)
    return hasher.hexdigest()


def compute_code_hash(func: Any) -> str:
    """Compute SHA-256 of a function's source code (used as step_version)."""
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        source = func.__qualname__
    return hashlib.sha256(textwrap.dedent(source).strip().encode()).hexdigest()[:16]
