.PHONY: test test-fast test-api test-agents test-reports test-cache coverage lint dev mastra

PYTHON := .venv/bin/python
PYTEST  := .venv/bin/pytest

test:
	$(PYTEST) tests/ -v --tb=short

test-fast:
	$(PYTEST) tests/ -v -x --tb=short -m "not slow"

test-api:
	$(PYTEST) tests/test_api/ -v --tb=short

test-agents:
	$(PYTEST) tests/test_agents/ -v --tb=short

test-reports:
	$(PYTEST) tests/test_reports/ -v --tb=short

test-cache:
	$(PYTEST) tests/test_cache/ -v --tb=short

test-scrapers:
	$(PYTEST) tests/test_scrapers/ -v --tb=short -m "not slow"

coverage:
	$(PYTEST) tests/ --cov=. --cov-report=html --cov-report=term-missing \
	  --cov-omit=".venv/*,tests/*,*/node_modules/*" -m "not slow"

lint:
	.venv/bin/ruff check . --exclude .venv,mastra

# ── Dev servers ───────────────────────────────────────────────────────────────

dev:
	@echo "Starting Python API (port 8000)..."
	.venv/bin/uvicorn main:app --reload --port 8000

mastra:
	@echo "Starting Mastra dashboard (port 4111)..."
	@echo "Open http://localhost:4111 (or whatever port Mastra picks) to see workflow runs."
	@cd mastra && npm run dev

# Run both servers side by side (requires tmux)
dev-full:
	@command -v tmux >/dev/null 2>&1 || { echo "tmux required: brew install tmux"; exit 1; }
	tmux new-session -d -s shopos -n api  "cd $(PWD) && make dev"
	tmux new-window  -t shopos -n mastra "cd $(PWD) && make mastra"
	@echo "Both servers running in tmux session 'shopos'."
	@echo "  API:     http://localhost:8000"
	@echo "  Mastra:  http://localhost:4111"
	@echo "Attach:  tmux attach -t shopos"
