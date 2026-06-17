"""
cascade/core/decorator.py
──────────────────────────
The @step decorator — the heart of Cascade.

Usage:
    class MyFlow(CascadeFlow):

        @step(name="explorer", max_retries=3)
        async def explore(self, inputs: dict) -> dict:
            ...
            return {"repo_graph_uri": uri}

        @step(name="planner", depends_on=["explorer"])
        async def plan(self, inputs: dict) -> dict:
            ...

Decorator behaviour (in order):
  1. Compute input_hash = SHA-256(serialized inputs + step source code)
  2. Query metadata DB:
     a. CROSS-RUN GLOBAL CACHE: Any run with this (step_name, input_hash, status=completed)?
        → SKIP: log ⏭, load outputs from artifact store, return cached state.
     b. IN-RUN CACHE: This run_id, this step, completed?
        → SKIP: log ⏭, return cached state.
  3. CACHE MISS: Mark step RUNNING in DB (write-ahead), execute function.
  4. SUCCESS: Mark COMPLETED, persist outputs + artifacts to store.
  5. FAILURE: Mark FAILED (or PERMANENTLY_FAILED if retries exhausted).
     Persist error traceback to artifact store for audit.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import traceback
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from cascade.core.state import StepState, StepStatus, compute_code_hash, compute_input_hash

if TYPE_CHECKING:
    from cascade.core.runner import FlowRunner


# ── Step Registration ─────────────────────────────────────────────────────────

# Registry of all decorated steps: { step_name → StepMeta }
_STEP_REGISTRY: dict[str, "StepMeta"] = {}


class StepMeta:
    """Metadata attached to a @step-decorated function."""

    def __init__(
        self,
        name: str,
        func: Callable,
        depends_on: list[str],
        max_retries: int,
        timeout_seconds: int,
        cross_run_cache: bool,
        tags: dict[str, str],
    ) -> None:
        self.name = name
        self.func = func
        self.depends_on = depends_on
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.cross_run_cache = cross_run_cache
        self.tags = tags
        self.code_hash = compute_code_hash(func)
        self.is_async = inspect.iscoroutinefunction(func)


# ── Decorator ─────────────────────────────────────────────────────────────────

def step(
    name: str | None = None,
    *,
    depends_on: list[str] | None = None,
    max_retries: int = 3,
    timeout_seconds: int = 600,
    cross_run_cache: bool = True,
    tags: dict[str, str] | None = None,
) -> Callable:
    """
    Decorator that transforms a flow method into a resumable, cached Cascade step.

    Args:
        name:             Step identifier (defaults to function name).
        depends_on:       List of step names that must complete before this one.
        max_retries:      Max retry attempts before PERMANENTLY_FAILED.
        timeout_seconds:  Wall-clock timeout for this step's execution.
        cross_run_cache:  If True, look for cache hits across ALL runs (not just current run).
                          Disable for steps that must be unique per run (e.g., PR creation).
        tags:             Arbitrary metadata tags ({"env": "prod"}).

    Example:
        @step(name="explorer", max_retries=2, cross_run_cache=True)
        async def explore(self, inputs: dict) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        step_name = name or func.__name__

        meta = StepMeta(
            name=step_name,
            func=func,
            depends_on=depends_on or [],
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            cross_run_cache=cross_run_cache,
            tags=tags or {},
        )
        _STEP_REGISTRY[step_name] = meta

        @functools.wraps(func)
        async def async_wrapper(self: "CascadeFlow", inputs: dict[str, Any]) -> StepState:
            return await _execute_step(self, meta, inputs)

        @functools.wraps(func)
        def sync_wrapper(self: "CascadeFlow", inputs: dict[str, Any]) -> StepState:
            return asyncio.get_event_loop().run_until_complete(
                _execute_step(self, meta, inputs)
            )

        wrapper = async_wrapper if meta.is_async else sync_wrapper
        # Attach metadata to the wrapper for introspection
        wrapper.__cascade_step__ = meta  # type: ignore[attr-defined]
        wrapper.__cascade_name__ = step_name  # type: ignore[attr-defined]

        return wrapper

    return decorator


# ── Core Execution Logic ──────────────────────────────────────────────────────

async def _execute_step(
    flow: "CascadeFlow",
    meta: StepMeta,
    inputs: dict[str, Any],
) -> StepState:
    """
    The main execution engine for a single step.

    Handles:
    - Input hash computation
    - Cache lookup (in-run and cross-run)
    - Write-ahead DB update
    - Execution with timeout
    - Success / failure state transitions
    - Cost accumulation on the parent run
    """
    from cascade.core.runner import _get_store  # noqa: PLC0415  (avoid circular import)

    store, artifact_store, run_id = _get_store(flow)

    # ── 1. Compute cache key ──────────────────────────────────────────────────
    input_hash = compute_input_hash(inputs, inspect.getsource(meta.func))

    # ── 2. Cache lookup ───────────────────────────────────────────────────────
    cached: StepState | None = None

    # 2a. Cross-run global cache (e.g., same repo SHA = skip Explorer entirely)
    if meta.cross_run_cache:
        cached = await store.find_completed_step_globally(meta.name, input_hash)
        if cached:
            flow._console.log(
                f"[bold cyan]>> SKIP[/bold cyan] [white]{meta.name}[/white] "
                f"[dim](global cache hit -- run {str(cached.run_id)[:8]})[/dim]"
            )

    # 2b. In-run cache (same run_id)
    if cached is None:
        cached = await store.get_cached_step(str(run_id), meta.name, input_hash)
        if cached:
            flow._console.log(
                f"[bold cyan]>> SKIP[/bold cyan] [white]{meta.name}[/white] "
                f"[dim](in-run cache hit)[/dim]"
            )

    if cached is not None:
        skipped = cached.mark_skipped(
            outputs=cached.outputs,
            artifact_uris=cached.artifact_uris,
        )
        await store.upsert_step(skipped)
        return skipped

    # ── 3. Write-ahead: insert RUNNING record ─────────────────────────────────
    running_state = StepState(
        id=uuid4(),
        run_id=run_id,
        name=meta.name,
        inputs=inputs,
        input_hash=input_hash,
        step_version=meta.code_hash,
        status=StepStatus.RUNNING,
        max_retries=meta.max_retries,
        tags=meta.tags,
    ).mark_running()

    await store.upsert_step(running_state)

    flow._console.log(
        f"[bold green]>> RUN [/bold green] [white]{meta.name}[/white] "
        f"[dim](hash: {input_hash[:12]}...)[/dim]"
    )

    # ── 4. Execute with timeout ───────────────────────────────────────────────
    try:
        if meta.is_async:
            raw_result = await asyncio.wait_for(
                meta.func(flow, inputs),
                timeout=meta.timeout_seconds,
            )
        else:
            raw_result = await asyncio.get_event_loop().run_in_executor(
                None,
                functools.partial(meta.func, flow, inputs),
            )

        result = raw_result if isinstance(raw_result, dict) else {"result": raw_result}

        # ── 5. SUCCESS: persist outputs ───────────────────────────────────────
        artifact_uris: list[str] = []

        # Auto-store large outputs in the artifact store
        outputs, artifact_uris = _auto_artifact_outputs(result, artifact_store)

        completed_state = running_state.mark_completed(
            outputs=outputs,
            artifact_uris=artifact_uris,
            llm_cost_cents=result.get("__cost_cents__", 0.0),
            total_tokens=result.get("__total_tokens__", 0),
            prompt_tokens=result.get("__prompt_tokens__", 0),
            completion_tokens=result.get("__completion_tokens__", 0),
        )
        await store.upsert_step(completed_state)

        # Accumulate cost on the parent run
        if completed_state.llm_cost_cents > 0:
            await store.update_run_cost(
                str(run_id),
                completed_state.llm_cost_cents,
                completed_state.total_tokens,
            )

        duration = completed_state.duration_seconds or 0
        flow._console.log(
            f"[bold green]OK DONE[/bold green] [white]{meta.name}[/white] "
            f"[dim]({duration:.1f}s, ${completed_state.llm_cost_cents/100:.4f})[/dim]"
        )
        return completed_state

    except asyncio.TimeoutError:
        err_msg = f"Step '{meta.name}' timed out after {meta.timeout_seconds}s"
        return await _handle_failure(flow, store, artifact_store, running_state, err_msg, "TimeoutError")

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        return await _handle_failure(flow, store, artifact_store, running_state, str(exc), tb)


async def _handle_failure(
    flow: "CascadeFlow",
    store: Any,
    artifact_store: Any,
    state: StepState,
    error_message: str,
    error_traceback: str,
) -> StepState:
    """Persist failure state and store traceback as an artifact."""
    # Store traceback in artifact store for post-mortem debugging
    tb_uri = artifact_store.put_text(
        f"Step: {state.name}\nError: {error_message}\n\n{error_traceback}"
    )

    failed_state = state.mark_failed(error_message, error_traceback)
    failed_state = failed_state.model_copy(
        update={"artifact_uris": state.artifact_uris + [tb_uri]}
    )
    await store.upsert_step(failed_state)

    icon = "XX" if failed_state.status == StepStatus.PERMANENTLY_FAILED else "!!"
    label = "DEAD" if failed_state.status == StepStatus.PERMANENTLY_FAILED else "FAIL"
    flow._console.log(
        f"[bold red]{icon} {label}[/bold red] [white]{state.name}[/white] "
        f"[dim](retry {failed_state.retry_count}/{state.max_retries})[/dim] "
        f"[red]{error_message[:80]}[/red]"
    )
    return failed_state


def _auto_artifact_outputs(
    outputs: dict[str, Any],
    artifact_store: Any,
) -> tuple[dict[str, Any], list[str]]:
    """
    Scan output dict for large values and automatically offload them
    to the artifact store, replacing them with CAS URIs.

    Convention:
      - Keys ending in '_blob' or '_data' with bytes values → stored as artifacts
      - Keys ending in '_json' with dict/list values → stored as JSON artifacts
      - All other values remain inline (must be JSON-serializable)
    """
    import orjson  # noqa: PLC0415

    cleaned: dict[str, Any] = {}
    artifact_uris: list[str] = []

    for key, value in outputs.items():
        # Skip internal cost-tracking keys
        if key.startswith("__") and key.endswith("__"):
            continue
        if isinstance(value, bytes) and len(value) > 4096:
            uri = artifact_store.put_bytes(value)
            cleaned[f"{key}__uri"] = uri
            artifact_uris.append(uri)
        elif isinstance(value, (dict, list)) and key.endswith("_json"):
            try:
                uri = artifact_store.put_json(value)
                cleaned[f"{key}__uri"] = uri
                artifact_uris.append(uri)
            except (TypeError, ValueError):
                cleaned[key] = value
        else:
            cleaned[key] = value

    return cleaned, artifact_uris


# ── Flow Base Class ────────────────────────────────────────────────────────────

class CascadeFlow:
    """
    Base class for all Cascade flows.

    Subclass this and decorate methods with @step to define your pipeline.

    Example:
        class DevOpsFlow(CascadeFlow):
            flow_name = "devops_workflow"

            @step(name="explorer")
            async def explore(self, inputs: dict) -> dict:
                ...

            @step(name="planner", depends_on=["explorer"])
            async def plan(self, inputs: dict) -> dict:
                ...
    """

    flow_name: str = "unnamed_flow"

    def __init__(self) -> None:
        import os
        import sys
        from rich.console import Console  # noqa: PLC0415

        # Force UTF-8 encoding on Windows without wrapping stdout
        if sys.platform == "win32":
            os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        self._console = Console(highlight=False)
        # These are injected by FlowRunner at runtime
        self._store: Any = None
        self._artifact_store: Any = None
        self._run_id: Any = None

    def get_steps(self) -> dict[str, StepMeta]:
        """Return all @step-decorated methods in this flow."""
        steps: dict[str, StepMeta] = {}
        for attr_name in dir(self):
            attr = getattr(self.__class__, attr_name, None)
            if attr and hasattr(attr, "__cascade_step__"):
                meta: StepMeta = attr.__cascade_step__
                steps[meta.name] = meta
        return steps
