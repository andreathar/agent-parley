# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

AGENTS.md (imported above) holds the operating rules: lane roles, worktree and
state isolation, the no-attribution policy, and the evidence levels to state.
`CONTRIBUTING.md` holds the coding standard and the coordination invariants.
This file adds commands and the architecture that spans several files.

## Commands

Use `uv` with `--locked`; the checkout has a `.venv`. Ruff, mypy and pytest are
dev-group tools, so run them through `uv run --locked` or `.venv/bin/`.

```sh
make check                                   # the final gate, also CI: sync, ruff check,
                                             # ruff format --check, mypy, policy, build, pytest
PYTEST_XDIST_AUTO_NUM_WORKERS=4 make check   # cap workers on a shared machine
uv run --locked pytest -q tests/test_console.py            # one file
uv run --locked pytest -q tests/test_console.py -k name    # one test
uv run --locked ruff check . && uv run --locked ruff format --check .
uv run --locked mypy                         # covers agent_parley/ and scripts/
uv run --locked python scripts/check_policy.py   # the repository policy gate alone
```

`make install`, `make install-system` and `make install-dev` install tools onto
the host. Never run `install` or `install-system` from a lane: they target the
daily tool. `make install-dev` is the Linux-fork path and publishes a separate
`agent-parley-dev` command (state `~/.local/state/agent-parley-dev`, port 8877,
console on 8890). It installs an immutable snapshot, so edits are not picked up
until the dev service is stopped (`agent-parley-dev down`) and it is rerun.
The daily installation uses port 8876 and must not be touched. See
`docs/linux-development.md`.

When running anything that touches coordination state, point it at throwaway
directories. `tests/conftest.py` already does this for pytest by redirecting
`AGENT_PARLEY_HOME`, `AGENT_PARLEY_RESOURCE_HOME`, `CLAUDE_CONFIG_DIR` and
`CODEX_HOME` into `tmp_path` and removing `AGENT_PARLEY_TOKEN`. Set the same
variables, plus `AGENT_PARLEY_PORT` to a free port, for manual runs.

`rtk` is not installed here, so run the commands above directly.

## What `scripts/check_policy.py` rejects

These are not in Ruff, so `ruff check` passing is not enough:

- Any `#` comment in `agent_parley/` or `scripts/`. Put rationale in a docstring.
- A missing docstring on any module, class or function there, private helpers
  included.
- Any import outside the standard library, `agent_parley` (and `scripts` inside
  `scripts/`). `pyproject.toml` `dependencies` must stay empty.
- Attribution text in any tracked file or commit message, and a verbatim
  repeated Markdown paragraph.
- Version drift between `agent_parley/__init__.py`, `pyproject.toml`, both
  plugin manifests, the marketplace files, `.release-manifest.json` and
  `uv.lock`. Never bump one by hand.

Tests are exempt from the docstring and annotation rules; name each test after
the scenario it covers.

## Architecture

Agent Parley coordinates several native coding-agent CLIs (Codex, Claude Code,
OpenCode, Copilot, Antigravity through the Gemini adapter), each in its own Git
worktree ("lane"), through one local service. It does not run model requests or
enforce permissions. `docs/architecture.md` has the per-module responsibility
table; read it before changing a module boundary.

**Two authorities reach the service, and they must stay separate.** A lane holds
its own bearer token and reaches only the MCP coordination tools served by
`server`. The operator holds the state-home token and reaches the command line
(`cli`), including the loopback `console`, which dispatches only an allowlisted
argv through that same CLI. Operator-only commands call
`roster.require_operator` and refuse from a lane token or from a working
directory inside a lane. A new command that changes shared settings needs that
guard, and a new console action needs an allowlist entry with typed arguments.

**The `Bridge` command object is assembled from mixins.** `cli.Bridge` inherits
`SettingsMixin`, `IntegrationMixin`, `ClaimsMixin`, `StatusMixin` and
`LaunchMixin`, all rooted at `core.BridgeCore`. Mixin method bodies import the
names they use from `cli` when they run (`from agent_parley.cli import json,
lock, ...`) and never at import time, because `cli` imports the mixins. Tests
patch those names on `cli`, so keep that pattern when moving a method between
modules, and do not hoist those imports.

**State is a private root outside every target repository.** `state` publishes
JSON and text atomically under operation locks; `store` is the SQLite schema
(mail, leases, tool events) and `lanes` is the single authoritative lane state
record with a closed transition table. Per-lane files are evidence read into
that record, never a second answer. Issue mutations serialize under one
operation lock, and ownership never expires or transfers by timeout.

**Enforcement and telemetry share a substrate on purpose.** A hook decision is
written to the participant event log before `notify` sees it, so notification
can report an outcome but never change one. Served MCP calls record their event
inside the transaction carrying the call's effect. Telemetry writes are best
effort with a zero lock wait and may be lost. Host resource leases
(`host_resources`) use a separate user-wide registry shared across state homes,
with explicit generation-bound release and no expiry.

**Supervision is one periodic poll.** `supervision` samples presence, applies
the lane-evidence spool, sends bounded wakes with backoff, refreshes the forge
and pull-request readings, and reclaims; `problems` derives operator actions
from a single `status` reading; `convergence` and `budgets` account separately.
Hooks (`hook`, `checkpoints`) make one loopback request to the service and fall
back to in-process `checkpoints` if it cannot answer.

**Provider adapters** (`gemini`, `copilot`, `opencode`, `amp`, plus the Codex and
Claude paths in `launch`) each translate that CLI's hook events, MCP tool names
and settings files into the shared `protocol`. A lane's configuration is
lane-private and removed on retirement, so never write to the user's global
native configuration.

**Startup is deliberately light.** `entry` imports only `sys` and `__version__`;
it answers version flags, refuses native Windows, prints `start`'s screen for a
bare invocation, and defers to `cli` for everything else. Do not add imports to
`entry`.

Plugin manifests and the shared skill live in `plugins/agent-parley/` and are
force-included in the wheel; `scripts/codex_bundle.py` and
`scripts/check_mcp_manifests.py` validate them as part of the policy gate.
CI scoping lives in `scripts/ci_scope.py`; `docs/releases.md` covers the
automatic release flow, which only `feat`, `fix` and `perf` commits touching
`agent_parley/` or `plugins/agent-parley/` can trigger.
