#1/bin/sh

uv run --all-extras ty check
uv run ruff check crsbench/ tests/
uv run pytest tests/*
