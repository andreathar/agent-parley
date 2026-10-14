# Task completion

1. Focused: `uv run --locked pytest -q <files>`, `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked mypy`.
2. `uv run --locked python scripts/check_policy.py` (comments, docstrings, imports, attribution, version drift).
3. Final gate: `make check` (sync, ruff, format, mypy, policy, build, `pytest -q -n auto`), same as CI.
4. State evidence level of claims (source read / transport test / hook observed / live model).
