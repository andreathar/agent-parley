# Tech stack

- Python >=3.12 (CI matrix 3.12-3.14, plus macOS/WSL legs chosen by `scripts/ci_scope.py`). Empty runtime `dependencies`; stdlib only.
- Build: hatchling + hatch-fancy-pypi-readme; package manager `uv` with `--locked` and `uv.lock`.
- Dev group: pytest, pytest-xdist, ruff, mypy, mcp, pre-commit, httpx, pyyaml.
- Ruff: line-length 80, rules E,F,I,B,UP,D(google),ANN001/201/202,PLE,PLW. mypy strict-ish (`disallow_untyped_defs`) over `agent_parley` and `scripts`.
- Versions are synced across many files by release tooling; never bump by hand.
