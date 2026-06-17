"""
cascade/core/runner.py
───────────────────────
FlowRunner — the DAG execution engine for Cascade.

Responsibilities:
  1. Create or resume a Run in the metadata DB.
  2. Topologically sort @step methods by their depends_on graph.
  3. Execute steps in order, threading outputs into subsequent steps' inputs.
  4. Handle resume: skip steps before the resume point, re-execute from it.
  5. Report final run summary (cost, duration, step statuses).

Usage:
    runner = await FlowRunner.create()
    run_state = await runner.run(MyFlow, issue_url="https://github.com/...")
    run_state = await runner.resume(run_id="abc-123", from_step="tester")
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from cascade.core.config import settings
from cascade.core.decorator import CascadeFlow, StepMeta, _STEP_REGISTRY
from cascade.core.state import RunState, RunStatus, StepStatus
from cascade.storage.artifact_store import ArtifactStore, create_artifact_store
from cascade.storage.metadata import MetadataStore


# ── Module-level store accessor (used by decorator to avoid circular import) ──

def _get_store(flow: CascadeFlow) -> tuple[MetadataStore, ArtifactStore, UUID]:
    """Called by the @step decorator to get the current run's stores."""
    return flow._store, flow._artifact_store, flow._run_id


# ── Flow Runner ───────────────────────────────────────────────────────────────

class FlowRunner:
    """
    Orchestrates the execution of a CascadeFlow instance.

    Create via FlowRunner.create() (async factory) to ensure DB is initialized.
    """

    def __init__(self, store: MetadataStore, artifact_store: ArtifactStore) -> None:
        import os
        import sys
        self._store = store
        self._artifact_store = artifact_store
        if sys.platform == "win32":
            os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        self._console = Console(highlight=False)

    @classmethod
    async def create(
        cls,
        database_url: str | None = None,
        artifact_backend: str | None = None,
        **artifact_kwargs: Any,
    ) -> "FlowRunner":
        """
        Async factory. Initializes the metadata DB and artifact store.

        Args:
            database_url:     Override the DATABASE_URL from settings.
            artifact_backend: Override the artifact backend ("local" or "s3").
            **artifact_kwargs: Passed to the artifact store factory.
        """
        cfg = settings()

        db_url = database_url or cfg.resolved_database_url
        store = MetadataStore(db_url)
        await store.initialize()

        backend = artifact_backend or cfg.artifact_backend
        if backend == "local" and "root" not in artifact_kwargs:
            artifact_kwargs["root"] = cfg.resolved_artifact_local_root
        artifact_store = create_artifact_store(backend, **artifact_kwargs)

        return cls(store=store, artifact_store=artifact_store)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(
        self,
        flow_class: type[CascadeFlow],
        issue_url: str | None = None,
        repo_url: str | None = None,
        tags: dict[str, str] | None = None,
        **initial_inputs: Any,
    ) -> RunState:
        """
        Create a new run and execute the flow from the beginning.

        Args:
            flow_class:     The CascadeFlow subclass to instantiate and run.
            issue_url:      GitHub issue URL (for metadata / display).
            repo_url:       GitHub repo URL.
            tags:           Arbitrary tags for filtering in the dashboard.
            **initial_inputs: Keyword arguments passed as inputs to the first step.
        """
        run_state = await self._store.create_run(
            flow_name=flow_class.flow_name,
            issue_url=issue_url,
            repo_url=repo_url,
            tags=tags,
        )
        self._print_run_header(run_state, "NEW RUN")
        return await self._execute_flow(flow_class, run_state, from_step=None, initial_inputs=initial_inputs)

    async def resume(
        self,
        run_id: str,
        from_step: str,
        flow_class: type[CascadeFlow] | None = None,
        **override_inputs: Any,
    ) -> RunState:
        """
        Resume a previously failed or incomplete run from a specific step.

        Steps BEFORE from_step that are already completed will be SKIPPED
        (cache hit). The from_step itself is re-executed from scratch.

        Args:
            run_id:          The run ID to resume.
            from_step:       Step name to restart execution from.
            flow_class:      Flow class (inferred from run metadata if omitted).
            **override_inputs: Override inputs for the resumed step.
        """
        run_state = await self._store.get_run(run_id)
        if run_state is None:
            msg = f"Run '{run_id}' not found in metadata store."
            raise ValueError(msg)

        self._print_run_header(run_state, f"RESUME FROM '{from_step}'")

        if flow_class is None:
            msg = (
                f"flow_class must be provided when resuming run '{run_id}'. "
                "Automatic flow class inference is not yet supported."
            )
            raise ValueError(msg)

        # Update run status to RESUMED
        await self._store.update_run_status(run_id, RunStatus.RESUMED)

        return await self._execute_flow(
            flow_class, run_state, from_step=from_step, initial_inputs=override_inputs
        )

    async def get_run(self, run_id: str) -> RunState | None:
        """Fetch run state (with all steps) by ID."""
        return await self._store.get_run(run_id)

    async def list_runs(self, limit: int = 50) -> list[RunState]:
        """List all runs, most recent first."""
        return await self._store.list_runs(limit=limit)

    # ── Internal execution engine ─────────────────────────────────────────────

    async def _execute_flow(
        self,
        flow_class: type[CascadeFlow],
        run_state: RunState,
        from_step: str | None,
        initial_inputs: dict[str, Any],
    ) -> RunState:
        """
        Core DAG execution loop.

        1. Instantiate the flow, inject stores.
        2. Topological sort of steps by depends_on.
        3. Execute each step, passing previous step outputs forward.
        4. If from_step is set, steps before it use their cached DB outputs.
        """
        flow = flow_class()
        flow._store = self._store
        flow._artifact_store = self._artifact_store
        flow._run_id = run_state.id

        await self._store.update_run_status(str(run_state.id), RunStatus.RUNNING)

        steps = flow.get_steps()
        if not steps:
            self._console.print("[yellow]Warning: No @step methods found in flow.[/yellow]")
            return run_state

        ordered = _topological_sort(steps)
        current_inputs = dict(initial_inputs)
        all_outputs: dict[str, dict[str, Any]] = {}

        try:
            for step_meta in ordered:
                # Merge outputs from all dependency steps into inputs
                merged_inputs = _merge_dependency_outputs(
                    step_meta, all_outputs, current_inputs
                )

                step_func = getattr(flow, step_meta.func.__name__, None)
                if step_func is None:
                    msg = f"Step method '{step_meta.func.__name__}' not found on {flow_class.__name__}"
                    raise AttributeError(msg)

                step_state = await step_func(merged_inputs)
                all_outputs[step_meta.name] = step_state.outputs

                # Abort on unrecoverable failure
                if step_state.status == StepStatus.PERMANENTLY_FAILED:
                    self._console.print(
                        f"\n[bold red]Pipeline aborted: '{step_meta.name}' permanently failed.[/bold red]"
                    )
                    await self._store.update_run_status(str(run_state.id), RunStatus.FAILED)
                    break

            else:
                # All steps completed successfully
                await self._store.update_run_status(str(run_state.id), RunStatus.COMPLETED)

        except Exception as exc:
            self._console.print_exception()
            await self._store.update_run_status(str(run_state.id), RunStatus.FAILED)

        # Refresh and return final state
        final_state = await self._store.get_run(str(run_state.id))
        self._print_run_summary(final_state or run_state)
        return final_state or run_state

    # ── Console output ─────────────────────────────────────────────────────────

    def _print_run_header(self, run_state: RunState, label: str) -> None:
        self._console.print(Panel(
            f"[bold cyan]Cascade[/bold cyan] [dim]v0.1.0[/dim]\n"
            f"[white]{label}[/white]  [dim]{str(run_state.id)[:8]}[/dim]\n"
            f"Flow: [green]{run_state.flow_name}[/green]",
            title="[bold]>> CASCADE[/bold]",
            border_style="cyan",
            expand=False,
        ))

    def _print_run_summary(self, run_state: RunState) -> None:
        table = Table(
            title=f"Run Summary — {str(run_state.id)[:8]}",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Step", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Duration", justify="right")
        table.add_column("Cost ($)", justify="right")
        table.add_column("Tokens", justify="right")

        STATUS_COLORS = {
            "completed": "green",
            "skipped": "cyan",
            "failed": "red",
            "permanently_failed": "bold red",
            "running": "yellow",
            "pending": "dim",
        }

        for name, step in run_state.steps.items():
            color = STATUS_COLORS.get(step.status.value, "white")
            icon = {
                "completed": "OK",
                "skipped": ">>",
                "failed": "!!",
                "permanently_failed": "XX",
                "running": ">>",
                "pending": "--",
            }.get(step.status.value, "?")

            duration = f"{step.duration_seconds:.1f}s" if step.duration_seconds else "—"
            cost = f"${step.llm_cost_cents/100:.4f}" if step.llm_cost_cents > 0 else "—"
            tokens = str(step.total_tokens) if step.total_tokens > 0 else "—"

            table.add_row(
                name,
                f"[{color}]{icon} {step.status.value}[/{color}]",
                duration,
                cost,
                tokens,
            )

        status_color = "green" if run_state.status == RunStatus.COMPLETED else "red"
        self._console.print(table)
        self._console.print(
            f"[bold]Run status:[/bold] [{status_color}]{run_state.status.value}[/{status_color}]  "
            f"[bold]Total cost:[/bold] [yellow]${run_state.total_cost_cents/100:.4f}[/yellow]"
        )


# ── DAG Topology ──────────────────────────────────────────────────────────────

def _topological_sort(steps: dict[str, StepMeta]) -> list[StepMeta]:
    """
    Kahn's algorithm for topological sort of step dependency graph.
    Raises ValueError on circular dependencies.
    """
    from collections import deque

    in_degree: dict[str, int] = {name: 0 for name in steps}
    adjacency: dict[str, list[str]] = {name: [] for name in steps}

    for name, meta in steps.items():
        for dep in meta.depends_on:
            if dep not in steps:
                msg = f"Step '{name}' depends on '{dep}' which is not registered."
                raise ValueError(msg)
            adjacency[dep].append(name)
            in_degree[name] += 1

    queue: deque[str] = deque(n for n, d in in_degree.items() if d == 0)
    sorted_steps: list[StepMeta] = []

    while queue:
        current = queue.popleft()
        sorted_steps.append(steps[current])
        for neighbor in adjacency[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(sorted_steps) != len(steps):
        msg = "Circular dependency detected in step graph."
        raise ValueError(msg)

    return sorted_steps


def _merge_dependency_outputs(
    step_meta: StepMeta,
    all_outputs: dict[str, dict[str, Any]],
    initial_inputs: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the inputs dict for a step by merging:
    1. Initial inputs (from CLI / run() call)
    2. Outputs from all dependency steps

    Dependency outputs are namespaced: {"<dep_step_name>.<key>": value}
    to prevent collisions between steps.
    """
    merged = dict(initial_inputs)
    for dep_name in step_meta.depends_on:
        dep_outputs = all_outputs.get(dep_name, {})
        for key, value in dep_outputs.items():
            # Namespaced key: "explorer.repo_graph_uri"
            merged[f"{dep_name}.{key}"] = value
    return merged
