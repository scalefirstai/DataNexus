# DataNexus — developer task runner.
#
# Usage: `make help` lists targets. All Python tasks run inside the
# project-local .venv so you do not pollute your system Python.

PYTHON      ?= python3.12
VENV        ?= .venv
BIN         := $(VENV)/bin
PIP         := $(BIN)/pip
PY          := $(BIN)/python
PYTEST      := $(BIN)/pytest
UVICORN     := $(BIN)/uvicorn
BLACK       := $(BIN)/black
RUFF        := $(BIN)/ruff
MYPY        := $(BIN)/mypy

APP         := extract_service.main:app
HOST        ?= 127.0.0.1
PORT        ?= 8000

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help.
	@awk 'BEGIN{FS":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------- setup

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

.PHONY: install
install: $(VENV)/bin/activate ## Create venv and install runtime + dev deps.
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt

.PHONY: install-runtime
install-runtime: $(VENV)/bin/activate ## Install runtime deps only.
	$(PIP) install -r requirements.txt

# ---------------------------------------------------------------- run

.PHONY: run
run: ## Start the API with --reload on $(HOST):$(PORT).
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT) --reload

.PHONY: smoke
smoke: ## End-to-end smoke script (assumes API is running).
	$(PY) scripts/smoke.py

# ---------------------------------------------------------------- test

.PHONY: test
test: ## Run the pytest suite.
	$(PYTEST) -q

.PHONY: test-cov
test-cov: ## Run tests with coverage report.
	$(PYTEST) --cov=extract_service --cov-report=term-missing

.PHONY: test-watch
test-watch: ## Re-run tests on file change (needs pytest-watch).
	$(BIN)/ptw -- -q

# ---------------------------------------------------------------- quality

.PHONY: lint
lint: ## Run ruff + black --check.
	$(RUFF) check .
	$(BLACK) --check .

.PHONY: format
format: ## Auto-format with black + ruff --fix.
	$(BLACK) .
	$(RUFF) check --fix .

.PHONY: typecheck
typecheck: ## Run mypy.
	$(MYPY) extract_service consumer_example

.PHONY: check
check: lint typecheck test ## Lint + typecheck + tests.

# ---------------------------------------------------------------- housekeeping

.PHONY: clean
clean: ## Remove caches, build artefacts, and local runtime data.
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf build dist *.egg-info
	rm -rf data/ /tmp/extract_run/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.py[co]" -delete

.PHONY: distclean
distclean: clean ## Also remove the virtualenv.
	rm -rf $(VENV)
