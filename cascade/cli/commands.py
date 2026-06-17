"""
cascade/cli/commands.py
────────────────────────
Typer-based CLI for Cascade.

Commands:
  cascade run      --flow <module.FlowClass> [--issue-url URL] [--repo-url URL]
  cascade resume   --run-id <id> --from-step <step_name> --flow <module.FlowClass>
  cascade status   --run-id <id>
  cascade logs     --run-id <id> [--step <name>] [--format json|table]
  cascade ls       [--limit N]
  cascade clean    (wipe local cascade home)
  cascade version
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich import box

app = typer.Typer(
    name="cascade",
    help="[bold cyan]Cascade[/bold cyan] — Stateful orchestrator for autonomous DevOps agents.\n\nStop re-reasoning. [bold]Start resuming.[/bold]",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _import_flow_class(flow_path: str):
    """Import a flow class from a dotted path like 'my_module.MyFlow'."""
    parts = flow_path.rsplit(".", 1)
    if len(parts) != 2:
        console.print(f"[red]Error:[/red] flow must be 'module.ClassName', got: '{flow_path}'")
        raise typer.Exit(1)
    module_path, class_name = parts
    try:
        module = importlib.import_module(module_path)
        flow_class = getattr(module, class_name)
    except (ImportError, AttributeError) as e:
        console.print(f"[red]Error importing flow:[/red] {e}")
        raise typer.Exit(1) from e
    return flow_class


async def _get_runner():
    from cascade.core.runner import FlowRunner
    return await FlowRunner.create()


# ── Commands ──────────────────────────────────────────────────────────────────

@app.command("run")
def cmd_run(
    flow: Annotated[str, typer.Option("--flow", "-f", help="Dotted path to flow class (e.g. examples.devops_workflow.DevOpsFlow)")],
    issue_url: Annotated[Optional[str], typer.Option("--issue-url", "-i", help="GitHub issue URL")] = None,
    repo_url: Annotated[Optional[str], typer.Option("--repo-url", "-r", help="GitHub repo URL")] = None,
    tag: Annotated[Optional[list[str]], typer.Option("--tag", "-t", help="Tag in key=value format (repeatable)")] = None,
) -> None:
    """
    [bold green]▶ Start a new Cascade pipeline run.[/bold green]

    Example:
      cascade run --flow examples.hello_cascade.HelloFlow --issue-url https://github.com/org/repo/issues/42
    """
    flow_class = _import_flow_class(flow)

    tags: dict[str, str] = {}
    for t in (tag or []):
        if "=" in t:
            k, v = t.split("=", 1)
            tags[k.strip()] = v.strip()

    async def _run():
        runner = await _get_runner()
        await runner.run(flow_class, issue_url=issue_url, repo_url=repo_url, tags=tags)

    asyncio.run(_run())


@app.command("resume")
def cmd_resume(
    run_id: Annotated[str, typer.Option("--run-id", "-r", help="Run ID to resume")],
    from_step: Annotated[str, typer.Option("--from", "-s", help="Step name to restart from")],
    flow: Annotated[str, typer.Option("--flow", "-f", help="Dotted path to flow class")],
) -> None:
    """
    [bold yellow]⏩ Resume a failed run from a specific step.[/bold yellow]

    Steps before the target step will be loaded from cache (no re-execution).

    Example:
      cascade resume --run-id abc-123 --from tester --flow examples.devops_workflow.DevOpsFlow
    """
    flow_class = _import_flow_class(flow)

    async def _resume():
        runner = await _get_runner()
        await runner.resume(run_id=run_id, from_step=from_step, flow_class=flow_class)

    asyncio.run(_resume())


@app.command("status")
def cmd_status(
    run_id: Annotated[str, typer.Option("--run-id", "-r", help="Run ID to inspect")],
) -> None:
    """
    [bold]📊 Show detailed status for a specific run.[/bold]

    Example:
      cascade status --run-id abc-123
    """
    async def _status():
        runner = await _get_runner()
        run_state = await runner.get_run(run_id)
        if run_state is None:
            console.print(f"[red]Run '{run_id}' not found.[/red]")
            raise typer.Exit(1)

        STATUS_EMOJI = {
            "completed": "✅", "skipped": "⏭️", "failed": "❌",
            "permanently_failed": "💀", "running": "🔄", "pending": "⏳",
        }
        STATUS_COLORS = {
            "completed": "green", "skipped": "cyan", "failed": "red",
            "permanently_failed": "bold red", "running": "yellow", "pending": "dim",
        }

        console.print(Panel(
            f"[bold]Run ID:[/bold]    {run_state.id}\n"
            f"[bold]Flow:[/bold]      {run_state.flow_name}\n"
            f"[bold]Status:[/bold]    [{STATUS_COLORS.get(run_state.status.value, 'white')}]{run_state.status.value}[/{STATUS_COLORS.get(run_state.status.value, 'white')}]\n"
            f"[bold]Created:[/bold]   {run_state.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"[bold]Duration:[/bold]  {f'{run_state.duration_seconds:.1f}s' if run_state.duration_seconds else '—'}\n"
            f"[bold]Total Cost:[/bold] ${run_state.total_cost_cents/100:.4f}\n"
            f"[bold]Issue:[/bold]     {run_state.issue_url or '—'}",
            title="[bold cyan]Cascade Run Status[/bold cyan]",
            border_style="cyan",
        ))

        if run_state.steps:
            table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
            table.add_column("Step", style="white", min_width=20)
            table.add_column("Status")
            table.add_column("Retries", justify="center")
            table.add_column("Duration", justify="right")
            table.add_column("Cost ($)", justify="right")
            table.add_column("Tokens", justify="right")

            for name, s in run_state.steps.items():
                color = STATUS_COLORS.get(s.status.value, "white")
                emoji = STATUS_EMOJI.get(s.status.value, "?")
                table.add_row(
                    name,
                    f"[{color}]{emoji} {s.status.value}[/{color}]",
                    f"{s.retry_count}/{s.max_retries}",
                    f"{s.duration_seconds:.1f}s" if s.duration_seconds else "—",
                    f"${s.llm_cost_cents/100:.4f}" if s.llm_cost_cents > 0 else "—",
                    str(s.total_tokens) if s.total_tokens > 0 else "—",
                )
            console.print(table)

            # Show error if any step failed
            for name, s in run_state.steps.items():
                if s.error_message:
                    console.print(f"\n[red]✗ {name} error:[/red] {s.error_message}")

    asyncio.run(_status())


@app.command("logs")
def cmd_logs(
    run_id: Annotated[str, typer.Option("--run-id", "-r", help="Run ID")],
    step: Annotated[Optional[str], typer.Option("--step", "-s", help="Filter to specific step")] = None,
    format: Annotated[str, typer.Option("--format", help="Output format: table or json")] = "table",
) -> None:
    """
    [bold]📜 View step logs and artifacts for a run.[/bold]

    Example:
      cascade logs --run-id abc-123 --format json
      cascade logs --run-id abc-123 --step tester
    """
    async def _logs():
        runner = await _get_runner()
        run_state = await runner.get_run(run_id)
        if run_state is None:
            console.print(f"[red]Run '{run_id}' not found.[/red]")
            raise typer.Exit(1)

        steps_to_show = run_state.steps
        if step:
            if step not in steps_to_show:
                console.print(f"[red]Step '{step}' not found in run '{run_id}'.[/red]")
                raise typer.Exit(1)
            steps_to_show = {step: steps_to_show[step]}

        if format == "json":
            data = {
                "run_id": str(run_state.id),
                "flow_name": run_state.flow_name,
                "status": run_state.status.value,
                "steps": {
                    name: {
                        "status": s.status.value,
                        "retry_count": s.retry_count,
                        "duration_seconds": s.duration_seconds,
                        "llm_cost_cents": s.llm_cost_cents,
                        "total_tokens": s.total_tokens,
                        "input_hash": s.input_hash,
                        "artifact_uris": s.artifact_uris,
                        "error_message": s.error_message,
                        "outputs": s.outputs,
                    }
                    for name, s in steps_to_show.items()
                },
            }
            console.print(Syntax(json.dumps(data, indent=2), "json", theme="monokai"))
        else:
            for name, s in steps_to_show.items():
                console.print(Panel(
                    f"[bold]Input Hash:[/bold] {s.input_hash[:16]}...\n"
                    f"[bold]Artifact URIs:[/bold] {', '.join(s.artifact_uris) or '—'}\n"
                    f"[bold]Outputs:[/bold] {json.dumps(s.outputs, indent=2)[:500]}\n"
                    f"[bold]Error:[/bold] {s.error_message or '—'}",
                    title=f"[bold cyan]{name}[/bold cyan] [{s.status.value}]",
                    border_style="dim",
                ))

    asyncio.run(_logs())


@app.command("ls")
def cmd_ls(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max runs to show")] = 20,
) -> None:
    """
    [bold]📋 List all Cascade runs.[/bold]

    Example:
      cascade ls --limit 10
    """
    async def _ls():
        runner = await _get_runner()
        runs = await runner.list_runs(limit=limit)

        if not runs:
            console.print("[dim]No runs found. Start one with: cascade run --flow ...[/dim]")
            return

        table = Table(
            title=f"Cascade Runs (last {limit})",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Run ID", style="dim", min_width=10)
        table.add_column("Flow", style="white")
        table.add_column("Status")
        table.add_column("Created", justify="right")
        table.add_column("Cost ($)", justify="right")

        STATUS_COLORS = {
            "completed": "green", "failed": "red", "running": "yellow",
            "pending": "dim", "resumed": "cyan",
        }

        for run in runs:
            color = STATUS_COLORS.get(run.status.value, "white")
            table.add_row(
                str(run.id)[:12] + "...",
                run.flow_name,
                f"[{color}]{run.status.value}[/{color}]",
                run.created_at.strftime("%m-%d %H:%M"),
                f"${run.total_cost_cents/100:.4f}" if run.total_cost_cents > 0 else "—",
            )
        console.print(table)

    asyncio.run(_ls())


@app.command("clean")
def cmd_clean(
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """
    [bold red]🗑  Wipe all local Cascade state (DB + artifacts).[/bold red]

    This is IRREVERSIBLE. Use with caution.
    """
    import shutil
    from cascade.core.config import settings

    home = settings().home
    if not force:
        typer.confirm(
            f"This will delete ALL cascade data at '{home}'. Are you sure?",
            abort=True,
        )
    shutil.rmtree(home, ignore_errors=True)
    console.print(f"[green]✓ Wiped:[/green] {home}")


@app.command("version")
def cmd_version() -> None:
    """Show Cascade version."""
    from cascade import __version__
    console.print(f"Cascade [bold cyan]v{__version__}[/bold cyan]")
