# PBXonix Agent.
# Recipes use `>` instead of a leading tab (GNU Make 3.82+).
.RECIPEPREFIX = >
.DEFAULT_GOAL := help

PY ?= python3

.PHONY: help
help: ## Show this help
> @grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
>   | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: dev
dev: ## Install in editable mode with dev extras
> $(PY) -m pip install -e ".[dev]"

.PHONY: test
test: ## Run the test suite
> $(PY) -m pytest -q

.PHONY: test-oldest
test-oldest: ## Run the suite on the oldest supported interpreter (3.6)
# Stock Issabel 4 / CentOS 7. pytest 7 dropped 3.6, hence the pin. This is the
# guard that stops a dataclass or a future import from slipping back in.
> docker run --rm -v "$(CURDIR):/src" -w /src python:3.6-slim \
>   sh -c 'pip install -q "pytest<7" && python -m pytest -q'

.PHONY: lint
lint: ## Lint
> $(PY) -m ruff check pbxonix_agent tests

.PHONY: fmt
fmt: ## Format
> $(PY) -m ruff format pbxonix_agent tests

.PHONY: build
build: ## Build the wheel
> rm -rf dist
> $(PY) -m build --wheel

.PHONY: check
check: ## Show what the agent can see on this machine
> $(PY) -m pbxonix_agent check --config ./packaging/agent.conf.example
