"""
examples/hello_cascade.py
──────────────────────────
Phase 1 Success Metric: 2-step pipeline demo.

Expected behaviour:
  First run:  Step 1 executes. Step 2 executes. State persisted.
  Second run: Step 1 is SKIPPED (cache hit). Step 2 executes.
              (Step 2 inputs differ slightly, so it re-runs.)
  Third run:  Both steps SKIPPED.

Run this twice and watch the ⏭ SKIP message appear for step_one:

    python examples/hello_cascade.py

Then run:

    cascade status --run-id <id shown above>
"""

import asyncio
import sys
from pathlib import Path

# Allow running from project root without `pip install -e .`
sys.path.insert(0, str(Path(__file__).parent.parent))

from cascade.core.decorator import CascadeFlow, step
from cascade.core.runner import FlowRunner


class HelloFlow(CascadeFlow):
    """
    The simplest possible Cascade flow — two sequential steps.

    Demonstrates:
    1. @step decorator usage
    2. Output threading (step_one → step_two)
    3. Cache skipping on re-run
    """

    flow_name = "hello_cascade"

    @step(name="step_one", max_retries=1)
    async def step_one(self, inputs: dict) -> dict:
        """
        Simulates an expensive operation (e.g., cloning a repo, building AST).
        In reality this would cost tokens and take minutes.
        """
        import time

        print("  [step_one] Performing expensive computation...")
        await asyncio.sleep(0.5)  # Simulate work

        message = f"Hello from Step 1! Input was: {inputs.get('greeting', 'none')}"
        return {
            "message": message,
            "computed_value": 42,
        }

    @step(name="step_two", depends_on=["step_one"], max_retries=1)
    async def step_two(self, inputs: dict) -> dict:
        """
        Consumes Step 1's output and produces its own artifact.
        """
        print("  [step_two] Processing step_one output...")
        await asyncio.sleep(0.2)

        upstream_message = inputs.get("step_one.message", "(no message)")
        result = f"{upstream_message} → processed by Step 2!"
        return {
            "final_result": result,
            "success": True,
        }


async def main() -> None:
    print("\n" + "=" * 60)
    print("  CASCADE — Hello World Demo")
    print("  Stop re-reasoning. Start resuming.")
    print("=" * 60 + "\n")

    runner = await FlowRunner.create()

    # ── Run the flow ──────────────────────────────────────────────
    print("Starting HelloFlow pipeline...\n")
    run_state = await runner.run(
        HelloFlow,
        greeting="world",
        tags={"demo": "hello_cascade"},
    )

    # ── Print final outputs ───────────────────────────────────────────────────
    sep = "-" * 60
    lines = [
        "",
        sep,
        f"  Run ID: {run_state.id}",
        f"  Status: {run_state.status.value}",
    ]

    if "step_two" in run_state.steps:
        step_two_out = run_state.steps["step_two"].outputs
        lines.append(f"  Output: {step_two_out.get('final_result', '?')}")

    lines += [
        "",
        "  Run this script again to see step_one get SKIPPED (cache hit)!",
        f"  cascade status --run-id {run_state.id}",
        "",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
