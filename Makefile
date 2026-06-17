.PHONY: help dev install test lint fmt clean demo reset-db

PYTHON := python
PIP := pip
CASCADE := cascade

# ── Help ──────────────────────────────────────────────────────────────────────

help: ## Show this help message
	@echo ""
	@echo "  ██████╗ █████╗ ███████╗ ██████╗ █████╗ ██████╗ ███████╗"
	@echo " ██╔════╝██╔══██╗██╔════╝██╔════╝██╔══██╗██╔══██╗██╔════╝"
	@echo " ██║     ███████║███████╗██║     ███████║██║  ██║█████╗  "
	@echo " ██║     ██╔══██║╚════██║██║     ██╔══██║██║  ██║██╔══╝  "
	@echo " ╚██████╗██║  ██║███████║╚██████╗██║  ██║██████╔╝███████╗"
	@echo "  ╚═════╝╚═╝  ╚═╝╚══════╝ ╚═════╝╚═╝  ╚═╝╚═════╝ ╚══════╝"
	@echo ""
	@echo "  Stop re-reasoning. Start resuming."
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────

install: ## Install cascade package in editable mode (all extras)
	$(PIP) install -e ".[all]"

dev: install ## Install + copy .env.example to .env (if not exists)
	@if not exist .env (copy .env.example .env && echo "Created .env — fill in your API keys!") else (echo ".env already exists")

# ── Testing ───────────────────────────────────────────────────────────────────

test: ## Run full test suite with coverage
	pytest tests/ -v --tb=short --cov=cascade --cov-report=term-missing

test-fast: ## Run tests, stop on first failure
	pytest tests/ -x -q

test-phase1: ## Run only Phase 1 core tests
	pytest tests/test_decorator.py tests/test_state.py tests/test_artifact_store.py tests/test_resume.py -v

# ── Code Quality ──────────────────────────────────────────────────────────────

lint: ## Run ruff linter
	ruff check cascade/ tests/ examples/

fmt: ## Auto-format with ruff
	ruff format cascade/ tests/ examples/

typecheck: ## Run mypy type checker
	mypy cascade/ --ignore-missing-imports

# ── Demo ──────────────────────────────────────────────────────────────────────

demo: ## Run the 2-step hello_cascade demo (Phase 1 success metric)
	@echo ""
	@echo "Running hello_cascade.py — First run (cold):"
	@echo "─────────────────────────────────────────────"
	$(PYTHON) examples/hello_cascade.py
	@echo ""
	@echo "Running hello_cascade.py — Second run (should skip Step 1):"
	@echo "─────────────────────────────────────────────────────────────"
	$(PYTHON) examples/hello_cascade.py

demo-full: ## Run the full DevOps workflow demo (Phase 4)
	$(PYTHON) examples/devops_workflow.py

# ── Database ──────────────────────────────────────────────────────────────────

reset-db: ## Wipe the local cascade state (fresh start)
	@echo "Wiping ~/.cascade ..."
	$(PYTHON) -c "import shutil, pathlib; p = pathlib.Path.home() / '.cascade'; shutil.rmtree(p, ignore_errors=True); print('Done.')"

# ── Services (Docker) ─────────────────────────────────────────────────────────

services-up: ## Start MinIO + PostgreSQL via Docker Compose
	docker-compose up -d minio postgres

services-down: ## Stop all Docker Compose services
	docker-compose down

api: ## Start the FastAPI REST server
	uvicorn cascade.api.app:app --reload --host 0.0.0.0 --port 8000

dashboard: ## Start the React dashboard (requires Node.js)
	cd dashboard && npm run dev

stack: ## Start full stack (API + Dashboard + Services)
	docker-compose up --build

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean: reset-db ## Remove all generated files and artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Clean complete."
