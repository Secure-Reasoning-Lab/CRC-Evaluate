# Run type checking with ty
typecheck:
    uv run --all-extras ty check --exclude "claude_reference_projects/" --exclude "oss-fuzz" --exclude "oss-crs" --exclude "crsbench/hint_generation/sarif_model.py"
