# Directories to check/format
dirs := "crsbench/ tests/"

# Run type checking with ty
typecheck:
    uv run --all-extras ty check

# Run linter with ruff
lint:
    uv run ruff check {{dirs}}

# Run linter and fix auto-fixable issues
lint-fix:
    uv run ruff check {{dirs}} --fix

# Run formatter with ruff
format:
    uv run ruff format {{dirs}}

# Check formatting without modifying files
format-check:
    uv run ruff format {{dirs}} --check

# Run tests (excluding integration tests)
test:
    uv run pytest tests/ -v -n auto -m "not integration"

# Run all checks (typecheck + lint + format check)
check:
    just typecheck
    just lint
    just format-check
