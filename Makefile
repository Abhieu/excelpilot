.PHONY: help install test test-fast test-slow test-security test-e2e lint format typecheck check bench smoke clean

PY := .venv/bin/python

help:
	@echo "ExcelPilot"
	@echo ""
	@echo "  make install        Install dev dependencies into .venv"
	@echo "  make check          format-check + lint + typecheck + fast tests (pre-commit gate)"
	@echo "  make test           Full test suite (skips slow/benchmark/live)"
	@echo "  make test-fast      Unit + integration only"
	@echo "  make test-slow      Long-running tests against very large real workbooks"
	@echo "  make test-security  Security boundary tests only"
	@echo "  make test-e2e       End-to-end scenario tests only"
	@echo "  make bench          Benchmark suite (mocked; no paid calls)"
	@echo "  make smoke          CLI smoke tests"
	@echo "  make clean          Remove caches and build artifacts"

install:
	uv sync --extra dev

$(PY):
	uv sync --extra dev

test: $(PY)
	uv run pytest

test-fast: $(PY)
	uv run pytest -m "not benchmark and not live and not e2e and not slow"

test-slow: $(PY)
	uv run pytest -m slow -v

test-security: $(PY)
	uv run pytest -m security -v

test-e2e: $(PY)
	uv run pytest -m e2e -v

lint: $(PY)
	uv run ruff check app tests benchmarks fixtures
	uv run ruff format --check app tests benchmarks fixtures

format: $(PY)
	uv run ruff format app tests benchmarks fixtures
	uv run ruff check --fix app tests benchmarks fixtures

typecheck: $(PY)
	uv run mypy

check: lint typecheck test-fast

bench: $(PY)
	uv run python -m benchmarks.run

smoke: $(PY)
	uv run python -m tests.smoke

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache .hypothesis .coverage htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
