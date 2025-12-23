#!/bin/bash
# Stop hook for Ruff linting and ty type checking
# Runs once when Claude finishes responding
# Exit code 2 + stderr shows errors to Claude for fixing

cd "$CLAUDE_PROJECT_DIR" || exit 0

# Run Ruff format and check with auto-fix
uv run ruff format crsbench/ tests/ >/dev/null 2>&1 || true
uv run ruff check crsbench/ tests/ --fix >/dev/null 2>&1 || true

# Check if lint errors remain
lint_output=$(uv run ruff check crsbench/ tests/ 2>&1)
lint_exit=$?

if [ $lint_exit -ne 0 ]; then
  echo "Ruff lint errors (please fix):" >&2
  echo "$lint_output" >&2
  exit 2
fi

# Run type checking
type_output=$(uv run --all-extras ty check 2>&1)
type_exit=$?

if [ $type_exit -ne 0 ]; then
  echo "Type check errors (please fix):" >&2
  echo "$type_output" >&2
  exit 2
fi

exit 0
