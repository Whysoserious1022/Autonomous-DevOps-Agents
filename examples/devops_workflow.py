"""
examples/devops_workflow.py
────────────────────────────
Full 3-agent DevOps pipeline (Phase 2 demo).

Demonstrates:
  - Explorer → Planner → Coder pipeline
  - Cascade caching: change the issue description, only Planner re-runs
  - Cost tracking across all LLM calls
  - Artifacts stored in CAS with SHA-256 addressing

Usage:
  # Set your API key first
  export CASCADE_LLM_MODEL=openai/gpt-4o
  export OPENAI_API_KEY=sk-...

  # First run: all 3 agents execute (~$0.10 for small repos)
  python examples/devops_workflow.py

  # Second run: Explorer SKIPPED (same commit SHA), Planner re-runs
  python examples/devops_workflow.py --issue "Different issue description"

  # Resume from coder step after a fix
  cascade resume --run-id <id> --from coder --flow devops_workflow.DevOpsFlow
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env

import asyncio
import os
import sys
from pathlib import Path

# Ensure cascade is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from cascade.agents.coder import CoderAgent
from cascade.agents.explorer import ExplorerAgent
from cascade.agents.planner import PlannerAgent
from cascade.agents.reviewer import ReviewerAgent
from cascade.agents.pr_creator import PRCreatorAgent
from cascade.core.decorator import CascadeFlow, step
from cascade.core.runner import FlowRunner


# ── DevOps Flow Definition ────────────────────────────────────────────────────

class DevOpsFlow(CascadeFlow):
    """
    Full autonomous DevOps agent pipeline.

    Steps:
      explorer → planner → coder → tester → reviewer → pr_creator

    Cache behaviour:
      explorer:  cross_run_cache=True  (keyed on commit_sha)
      planner:   cross_run_cache=False (re-runs when issue or branch changes)
      coder:     cross_run_cache=False (re-runs when patch changes or tests fail)
    """
    flow_name = "devops_workflow"

    @step(
        name="explorer",
        cross_run_cache=True,        # << Same commit SHA = instant skip across ALL runs
        description="Build AST knowledge graph of the target repository",
    )
    async def explore(self, inputs: dict) -> dict:
        """
        Node 1: Context Builder.
        Clones the repository, builds AST graph, LLM-ranks relevant files.
        Cache key: SHA-256(commit_sha + issue_body_hash + source_code).
        """
        agent = ExplorerAgent(artifact_store=self._artifact_store)
        return await agent.execute(inputs)

    @step(
        name="planner",
        depends_on=["explorer"],
        cross_run_cache=False,
        description="Generate Tree-of-Thoughts solution branches",
    )
    async def plan(self, inputs: dict) -> dict:
        """
        Node 2: Tree-of-Thoughts Planner.
        Generates 3 solution branches from the repo graph + issue.
        Only re-runs when issue description or explorer outputs change.
        """
        agent = PlannerAgent(artifact_store=self._artifact_store)
        return await agent.execute(inputs)

    @step(
        name="coder",
        depends_on=["planner", "explorer"],
        cross_run_cache=False,
        max_retries=3,               # Retry on Tester failure (Phase 3)
        description="Generate git-compatible unified diff patch",
    )
    async def code(self, inputs: dict) -> dict:
        """
        Node 3: Patch Generator.
        Converts selected ToT branch into a git-apply-compatible diff.
        On retry: receives test_results_xml from Tester as additional input.
        """
        agent = CoderAgent(artifact_store=self._artifact_store)
        return await agent.execute(inputs)

    @step(
        name="tester",
        depends_on=["coder", "explorer"],
        cross_run_cache=False,
        description="Run sandbox tests inside Docker container",
    )
    async def test(self, inputs: dict) -> dict:
        """
        Node 4: Sandbox Tester.
        Executes test suite in ephemeral container.
        """
        from cascade.agents.tester import TesterAgent
        agent = TesterAgent(artifact_store=self._artifact_store)
        return await agent.execute(inputs)

    @step(
        name="reviewer",
        depends_on=["tester", "coder"],
        cross_run_cache=False,
        description="Run static scanners and security guardrails on patch",
    )
    async def review(self, inputs: dict) -> dict:
        """
        Node 5: Guardrail Agent.
        Analyzes patch for secrets, complexity, and requests LLM review.
        """
        agent = ReviewerAgent(artifact_store=self._artifact_store)
        return await agent.execute(inputs)

    @step(
        name="pr_creator",
        depends_on=["reviewer", "tester", "coder", "planner", "explorer"],
        cross_run_cache=False,
        description="Pushes branch and creates pull request on GitHub",
    )
    async def create_pr(self, inputs: dict) -> dict:
        """
        Node 6: GitHub PR Creator.
        Creates git branch, applies patch, commits, pushes, and opens PR.
        """
        agent = PRCreatorAgent(artifact_store=self._artifact_store)
        return await agent.execute(inputs)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Cascade DevOps Workflow")
    parser.add_argument("--repo-url", default="https://github.com/tiangolo/fastapi",
                        help="GitHub repository URL")
    parser.add_argument("--issue", default="Request: Add a way to disable docs in production without removing routes",
                        help="GitHub issue title")
    parser.add_argument("--commit-sha", default="",
                        help="Specific commit SHA to analyze (empty = latest)")
    parser.add_argument("--n-branches", type=int, default=3,
                        help="Number of ToT branches to generate")
    parser.add_argument("--model", default="",
                        help="LiteLLM model string (default from CASCADE_LLM_MODEL env var)")
    parser.add_argument("--resume-from", default="",
                        help="Resume from step name (e.g., planner, coder)")
    args = parser.parse_args()

    # Override model if specified
    if args.model:
        os.environ["CASCADE_LLM_MODEL"] = args.model

    print("\n" + "=" * 65)
    print("  CASCADE -- DevOps Pipeline (Phase 2)")
    print("  Explorer -> Planner -> Coder")
    print("=" * 65)
    print(f"\n  Repo:   {args.repo_url}")
    print(f"  Issue:  {args.issue}")
    print(f"  Model:  {os.environ.get('CASCADE_LLM_MODEL', 'openai/gpt-4o')}")
    print()

    # Initial inputs threaded through the entire pipeline
    initial_inputs = {
        "repo_url": args.repo_url,
        "commit_sha": args.commit_sha,
        "issue_title": args.issue,
        "issue_body": (
            "This is a demo issue for Cascade Phase 2 validation. "
            "The actual issue body would come from GitHub API in production."
        ),
        "issue_number": 0,
        "n_branches": args.n_branches,
    }

    runner = await FlowRunner.create()

    if args.resume_from:
        # Find the most recent run of this flow to resume
        runs = [
            run for run in await runner.list_runs(limit=20)
            if run.flow_name == DevOpsFlow.flow_name
        ]
        if not runs:
            print("No previous runs found to resume.")
            sys.exit(1)
        run_state = await runner.resume(
            run_id=str(runs[0].id),
            from_step=args.resume_from,
            flow_class=DevOpsFlow,
        )
    else:
        run_state = await runner.run(
            DevOpsFlow,
            tags={"source": "example", "phase": "2"},
            **initial_inputs,
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    sep = "-" * 65
    lines = [
        "",
        sep,
        f"  Run ID:  {run_state.id}",
        f"  Status:  {run_state.status.value}",
        f"  Flow:    {run_state.flow_name}",
        "",
    ]

    total_cost = sum(
        s.llm_cost_cents or 0.0
        for s in run_state.steps.values()
    )
    lines.append(f"  Total LLM cost: ${total_cost / 100:.4f}")
    lines.append("")

    for step_name, step_state in run_state.steps.items():
        status_icon = {
            "completed": "OK",
            "skipped": ">>",
            "failed": "!!",
            "permanently_failed": "XX",
        }.get(step_state.status.value, "??")
        duration = f"{step_state.duration_seconds:.1f}s" if step_state.duration_seconds else "—"
        cost = f"${step_state.llm_cost_cents/100:.4f}" if step_state.llm_cost_cents else "$0.0000"
        lines.append(f"  [{status_icon}] {step_name:<12} {duration:>6}  {cost}")

    # Show key outputs
    if "coder" in run_state.steps:
        coder_out = run_state.steps["coder"].outputs or {}
        if coder_out.get("patch_uri"):
            lines.append(f"\n  Patch artifact: {coder_out['patch_uri']}")
        if coder_out.get("files_changed"):
            lines.append(f"  Files changed:  {', '.join(coder_out['files_changed'])}")

    lines += [
        "",
        "  Tip: Re-run with same args -- Explorer will be SKIPPED (commit cache hit)!",
        f"  cascade status --run-id {run_state.id}",
        "",
        sep,
    ]

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
