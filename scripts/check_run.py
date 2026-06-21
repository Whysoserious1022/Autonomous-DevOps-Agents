"""Quick script to check the status of a specific run."""
import urllib.request
import json

API = "http://localhost:8000"
run_id = "94ee7713-cc45-44d4-a84d-20d21532c559"

try:
    r = urllib.request.urlopen(f"{API}/api/runs/{run_id}", timeout=10)
    run = json.loads(r.read())
    print(f"Run status: {run['status']}")
    print("Steps:")
    for name, step in run.get("steps", {}).items():
        if name == "__checkpoint__":
            continue
        dur = f"{step.get('duration_seconds', 0):.1f}s" if step.get("duration_seconds") else "---"
        cost = f"${step.get('llm_cost_cents', 0)/100:.4f}"
        tokens = step.get("total_tokens", 0)
        err = f"  ERROR: {step.get('error_message','')[:60]}" if step.get("error_message") else ""
        print(f"  {step['status']:15} | {name:12} | {dur:6} | {cost:8} | {tokens} tokens{err}")
    print()
    total_cost = run.get("total_cost_cents", 0) / 100
    total_tokens = run.get("total_tokens", 0)
    print(f"Total cost: ${total_cost:.4f}  |  Total tokens: {total_tokens}")
except Exception as e:
    print(f"Error: {e}")
