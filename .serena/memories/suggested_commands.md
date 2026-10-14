# Suggested commands

- `rtk` is not installed; run commands directly via `uv run --locked ...`.
- One file: `uv run --locked pytest -q tests/test_x.py`; one test: add `-k name`.
- Lint/format: `uv run --locked ruff check . && uv run --locked ruff format --check .`
- Types: `uv run --locked mypy`
- Policy gate alone: `uv run --locked python scripts/check_policy.py`
- Cap workers on shared box: `PYTEST_XDIST_AUTO_NUM_WORKERS=4 make check`
- Manual runs touching state: set `AGENT_PARLEY_HOME`, `AGENT_PARLEY_RESOURCE_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` to temp dirs, `AGENT_PARLEY_PORT` to a free port, unset `AGENT_PARLEY_TOKEN`.
- Never run `make install` / `install-system` from a lane. `make install-dev` (see `docs/linux-development.md`) installs an immutable snapshot; `agent-parley-dev down` before rerunning.
