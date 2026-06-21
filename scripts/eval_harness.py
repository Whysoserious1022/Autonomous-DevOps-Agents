"""
scripts/eval_harness.py
───────────────────────
SWE-bench Lite Evaluation Harness for Cascade DevOps Agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure cascade package is on the python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cascade.core.runner import FlowRunner
from examples.devops_workflow import DevOpsFlow

# ── Sample SWE-bench Lite Instances for Mock Evaluation ────────────────────────

MOCK_SWE_BENCH_INSTANCES = [
    {
        "instance_id": "fastapi__fastapi-1234",
        "repo": "tiangolo/fastapi",
        "base_commit": "abc123def",
        "problem_statement": "Add a way to disable docs in production without removing routes.",
        "test_patch": "diff --git a/tests/test_docs.py b/tests/test_docs.py\n...",
    },
    {
        "instance_id": "fastapi__fastapi-5678",
        "repo": "tiangolo/fastapi",
        "base_commit": "def456ghi",
        "problem_statement": "OPTIONS preflight requests fail on CORS routes with 404.",
        "test_patch": "diff --git a/tests/test_cors.py b/tests/test_cors.py\n...",
    }
]

# ── Harness Implementation ───────────────────────────────────────────────────

async def run_evaluation(instances_file: str | None, limit: int) -> None:
    """Run evaluation on SWE-bench Lite instances."""
    instances = MOCK_SWE_BENCH_INSTANCES
    if instances_file and Path(instances_file).exists():
        try:
            with open(instances_file, encoding="utf-8") as f:
                instances = json.load(f)
        except Exception as e:
            print(f"Error loading {instances_file}: {e}. Falling back to mocks.")

    instances = instances[:limit]
    print(f"\n==================================================")
    print(f"  CASCADE SWE-BENCH LITE EVALUATION HARNESS")
    print(f"  Evaluating {len(instances)} instances")
    print(f"==================================================\n")

    results = []
    runner = await FlowRunner.create()

    for idx, inst in enumerate(instances, 1):
        inst_id = inst.get("instance_id")
        repo = inst.get("repo")
        commit = inst.get("base_commit")
        desc = inst.get("problem_statement")

        print(f"[{idx}/{len(instances)}] Running {inst_id} ...")
        print(f"  Repo: {repo} @ {commit}")
        print(f"  Issue: {desc[:80]}...")

        # Setup input arguments for Cascade DevOpsFlow
        initial_inputs = {
            "repo_url": f"https://github.com/{repo}",
            "commit_sha": commit,
            "issue_title": f"SWE-bench: {inst_id}",
            "issue_body": desc,
            "issue_number": idx,
        }

        # Run flow
        start_time = asyncio.get_event_loop().time()
        try:
            run_state = await runner.run(
                DevOpsFlow,
                inputs=initial_inputs,
                tags={"eval_instance": inst_id, "mode": "eval"}
            )
            duration = asyncio.get_event_loop().time() - start_time
            
            # Determine if fix succeeded
            # In a real harness, we would apply the patch and execute the test suite (test_patch)
            # For this harness, we read the result output and evaluate if coder generated patch successfully.
            coder_step = run_state.steps.get("coder")
            coder_success = coder_step.status.value == "completed" if coder_step else False
            test_step = run_state.steps.get("tester")
            tests_passed = test_step.outputs.get("test_passed", False) if test_step else False

            resolved = coder_success and tests_passed

            results.append({
                "instance_id": inst_id,
                "resolved": resolved,
                "duration_seconds": duration,
                "total_cost_usd": run_state.total_cost_cents / 100.0,
                "status": run_state.status.value,
                "error": None
            })

            status_str = "SUCCESS" if resolved else "FAILED"
            print(f"  Result: {status_str} (Cost: ${run_state.total_cost_cents/100:.4f}, Duration: {duration:.1f}s)")

        except Exception as e:
            duration = asyncio.get_event_loop().time() - start_time
            results.append({
                "instance_id": inst_id,
                "resolved": False,
                "duration_seconds": duration,
                "total_cost_usd": 0.0,
                "status": "error",
                "error": str(e)
            })
            print(f"  Result: ERROR ({e})")
        print("-" * 50)

    # Print summary report
    solved_count = sum(1 for r in results if r["resolved"])
    solve_rate = (solved_count / len(results)) * 100 if results else 0
    total_cost = sum(r["total_cost_usd"] for r in results)

    print(f"\n==================================================")
    print(f"  EVALUATION SUMMARY")
    print(f"==================================================")
    print(f"  Total Instances: {len(results)}")
    print(f"  Solved:          {solved_count} / {len(results)} ({solve_rate:.1f}%)")
    print(f"  Total Cost:      ${total_cost:.4f}")
    print(f"  Average Time:    {sum(r['duration_seconds'] for r in results)/len(results):.1f}s" if results else "N/A")
    print(f"==================================================\n")

    # Save details
    with open("eval_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("Saved detailed results to 'eval_results.json'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cascade SWE-bench Evaluation")
    parser.add_argument("--instances", default=None, help="Path to JSON file of SWE-bench instances")
    parser.add_argument("--limit", type=int, default=5, help="Maximum instances to evaluate")
    args = parser.parse_args()

    asyncio.run(run_evaluation(args.instances, args.limit))
