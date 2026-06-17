<div align="center">

# ⚡ Cascade

### Stateful Orchestrator for Autonomous DevOps Agents

**Stop re-reasoning. Start resuming.**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Phase](https://img.shields.io/badge/phase-1%20%E2%80%94%20core%20engine-orange.svg)](docs/roadmap.md)

</div>

---

## The Problem

Autonomous coding agents (Devin, SWE-agent) are stateless. When your agent fails at step 3 (Testing), it throws away:

- **$0.50** spent on step 1 (Repo Analysis — cloned 80K files, built AST)
- **$0.80** spent on step 2 (Tree-of-Thoughts Planning — 200K tokens of reasoning)

...and restarts from scratch. **Every. Single. Time.**

## The Solution

Cascade is a **decorator-based workflow engine** where every step is:

| Property | Meaning |
|---|---|
| **Versioned** | SHA-256 of inputs + source code is the cache key |
| **Resumable** | `cascade resume --from tester` skips everything before |
| **Auditable** | Every prompt, response, and diff stored as a CAS artifact |
| **Cost-tracked** | Per-step LLM spend accumulated on the run |

```python
from cascade import CascadeFlow, step

class DevOpsFlow(CascadeFlow):
    flow_name = "devops_workflow"

    @step(name="explorer", cross_run_cache=True)   # Cache across ALL runs
    async def explore(self, inputs: dict) -> dict:
        # Clone repo, build AST — expensive. Run once per commit SHA.
        return {"repo_graph_uri": "sha256://ab12..."}

    @step(name="planner", depends_on=["explorer"])
    async def plan(self, inputs: dict) -> dict:
        # Tree of Thoughts — stored as artifact, re-run without re-exploring
        return {"tot_branches_uri": "sha256://cd34..."}

    @step(name="coder", depends_on=["planner"])
    async def code(self, inputs: dict) -> dict:
        return {"patch_uri": "sha256://ef56..."}

    @step(name="tester", depends_on=["coder"], max_retries=3)
    async def test(self, inputs: dict) -> dict:
        # Docker sandbox — retries feed test_results back to coder
        return {"passed": True}
```

**Run it:**
```bash
cascade run --flow my_module.DevOpsFlow --issue-url https://github.com/org/repo/issues/42
```

**Failure at Tester? Fix the code and resume:**
```bash
cascade resume --run-id abc-123 --from tester --flow my_module.DevOpsFlow
```

Explorer and Planner steps are **instantly skipped** — loaded from cache. Only Tester re-runs.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   CASCADE CORE                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │         Metadata Store (SQLite / PostgreSQL)     │   │
│  │   runs table ─── steps table ─── cost tracking  │   │
│  └─────────────────────────────────────────────────┘   │
│                          │                              │
│  ┌─────────────────────────────────────────────────┐   │
│  │       Content-Addressed Artifact Store (CAS)    │   │
│  │   sha256://<hash> → bytes on Local FS / S3      │   │
│  └─────────────────────────────────────────────────┘   │
│                          │                              │
│  ┌─────────────────────────────────────────────────┐   │
│  │            @step Decorator                      │   │
│  │   1. Compute input_hash                         │   │
│  │   2. Cache lookup (in-run + cross-run)          │   │
│  │   3. Write-ahead → Execute → Persist            │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Cache Hit Decision Tree

```
@step called
    │
    ▼
Compute SHA-256(inputs + source_code)
    │
    ├── Global cache hit? (any run, same hash, status=completed)
    │       YES → ⏭ SKIP — load outputs from artifact store
    │       NO ↓
    ├── In-run cache hit? (this run_id, same hash, status=completed)
    │       YES → ⏭ SKIP
    │       NO ↓
    └── Execute → persist outputs → mark COMPLETED
```

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/your-org/cascade
cd cascade
pip install -e ".[dev]"
```

### 2. Copy environment config

```bash
cp .env.example .env
# Edit .env — add your OPENAI_API_KEY (or ANTHROPIC_API_KEY)
```

### 3. Run the Phase 1 demo (no API key needed)

```bash
python examples/hello_cascade.py
# First run: both steps execute
# Second run: step_one is ⏭ SKIPPED — cache hit!
```

### 4. Explore the CLI

```bash
cascade ls                              # List all runs
cascade status --run-id <id>           # Detailed step status
cascade logs --run-id <id> --format json
cascade resume --run-id <id> --from step_name --flow my.Flow
```

---

## CLI Reference

| Command | Description |
|---|---|
| `cascade run --flow <path>` | Start a new pipeline run |
| `cascade resume --run-id <id> --from <step>` | Resume from a failed step |
| `cascade status --run-id <id>` | Show step-by-step status |
| `cascade logs --run-id <id>` | View artifacts and outputs |
| `cascade ls` | List all runs |
| `cascade clean` | Wipe local cascade state |

---

## Development

```bash
make test         # Run full test suite with coverage
make test-phase1  # Run only Phase 1 core tests
make lint         # Ruff linter
make fmt          # Auto-format
make demo         # Run hello_cascade.py twice
make clean        # Wipe all state
```

---

## Project Roadmap

| Phase | Status | Description |
|---|---|---|
| **1 — Core Engine** | ✅ **Done** | `@step` decorator, SQLite store, CAS artifacts, Typer CLI |
| **2 — AI Integration** | 🔄 Planned | LangGraph + Explorer (AST) + Planner (ToT) |
| **3 — Sandbox** | 🔄 Planned | Docker tester + Coder↔Tester retry loop |
| **4 — Production** | 🔄 Planned | S3/MinIO, Reviewer, PR Creator, FastAPI + React Dashboard |

---

## KPIs

| Metric | Target | Notes |
|---|---|---|
| Cache hit rate | > 70% | On consecutive runs with similar repo contexts |
| Retry cost reduction | > 50% | Cascade vs. naive re-run cost |
| Resume time | < 5s | From failure point, excluding LLM call |
| SWE-bench solve rate | ≥ 25% | Phase 4+ target |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Core Runtime | Python 3.12 |
| Workflow Engine | LangGraph + Custom Persistence |
| Metadata DB | SQLite (dev) / PostgreSQL (prod) |
| Object Storage | Local FS (dev) / S3/MinIO (prod) |
| LLM Gateway | LiteLLM (OpenAI, Anthropic, Gemini, Ollama) |
| Containerization | Docker SDK |
| Serialization | orjson + pickle |
| CLI | Typer + Rich |
| API | FastAPI + WebSockets |
| Dashboard | React + Vite + ReactFlow |

---

<div align="center">
Made with ⚡ by Cascade Contributors · Apache 2.0 License
</div>
