.PHONY: test test-fast test-api test-agents test-reports test-cache coverage lint

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
