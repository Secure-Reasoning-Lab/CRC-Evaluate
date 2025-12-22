# Run type checking with ty
typecheck:
    uv run --all-extras ty check

# Run linter with ruff
lint:
    uv run ruff check .

# Run linter and fix auto-fixable issues
lint-fix:
    uv run ruff check . --fix

# Run formatter with ruff
format:
    uv run ruff format .

# Check formatting without modifying files
format-check:
    uv run ruff format . --check

# Run all checks (typecheck + lint + format check)
check:
    just typecheck
    just lint
    just format-check
