# agent-parley core

Coordinates native coding-agent CLIs (Codex, Claude, OpenCode, Copilot, Gemini/Antigravity) in per-lane Git worktrees via one local service. Does not run models or enforce permissions; reservations are advisory.

- Package `agent_parley/` (~80 flat modules, stdlib-only runtime), `scripts/` (gates, release), `tests/` (~150 files), `plugins/agent-parley/` (manifests + skill, force-included in wheel), `docs/architecture.md` (module responsibility table; read before changing boundaries).
- Two authorities: lane bearer token -> MCP tools in `server`; operator token -> `cli`/`console`. Operator-only commands call `roster.require_operator`; new console actions need an allowlist entry.
- `cli.Bridge` = mixins over `core.BridgeCore`; mixin bodies import from `cli` lazily (tests patch those names). Do not hoist.
- State lives outside target repos (`state`, `store` SQLite, `lanes` single lane record).
- `entry` must import only `sys` and `__version__`.
- Never touch the daily install (port 8876); dev install is `agent-parley-dev` (8877, console 8890).
- Lane roles/working rules: `AGENTS.md`; coding standard: `CONTRIBUTING.md`.

Orientation: `mem:modules` lists every module's purpose and which agent_parley modules it imports; read it to find where code lives and what a change touches. It is generated and stamped with a commit; if `python .serena/refresh_map.py --check` prints stale, run `python .serena/refresh_map.py` first. Full symbol list (grep only, not a memory): `.serena/index/symbol_index.md`. For current symbols and callers use `find_symbol`, `get_symbols_overview`, `find_referencing_symbols`, never the generated files.

Further: `mem:tech_stack`, `mem:suggested_commands`, `mem:conventions`, `mem:task_completion`.
