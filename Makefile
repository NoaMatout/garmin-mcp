.DEFAULT_GOAL := help
.PHONY: help setup test test-all lint typecheck fmt corpus corpus-clean clean

# Third-party FIT samples used by the opt-in corpus tests. MIT-licensed test
# fixtures from python-fitparse, covering Garmin (fr70 through fenix 5), Wahoo,
# Coros, Stryd, Zwift and the Strava mobile app. Never committed: they are
# third-party licensed, and GPS traces are personal data.
CORPUS_URL := https://github.com/dtcooper/python-fitparse/archive/refs/heads/master.tar.gz
CORPUS_DIR := data/corpus

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies and create the database
	uv sync
	uv run garmin-mcp init-db

test: ## Run the hermetic test suite (no data required)
	uv run pytest -q

test-all: corpus ## Run everything, including validation against real devices
	uv run pytest -q

lint: ## Check formatting and lint rules
	uv run ruff check src tests

fmt: ## Auto-fix what can be auto-fixed
	uv run ruff check --fix src tests
	uv run ruff format src tests

typecheck: ## Static type check
	uv run mypy

corpus: $(CORPUS_DIR) ## Download the real-device FIT corpus (gitignored)

$(CORPUS_DIR):
	@echo "Fetching real-device FIT corpus into $(CORPUS_DIR)/ ..."
	@mkdir -p $(CORPUS_DIR)
	@curl -sL $(CORPUS_URL) \
		| tar -xz --strip-components=3 -C $(CORPUS_DIR) \
		  python-fitparse-master/tests/files
	@echo "$$(ls $(CORPUS_DIR) | wc -l | tr -d ' ') files ready — run 'make test' to use them."

corpus-clean: ## Remove the downloaded corpus
	rm -rf $(CORPUS_DIR)

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
