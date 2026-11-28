test:
    uv run pytest

lint:
    uv run ruff check citeforge tests
    uv run ruff format --check citeforge tests
    uv run pyrefly check
