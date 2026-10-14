# Conventions

- No `#` comments in `agent_parley/` or `scripts/`; rationale goes in docstrings. Every module/class/function (private too) needs a Google-style docstring and type annotations. Tests are exempt; name tests after the scenario.
- Imports only stdlib, `agent_parley` (and `scripts` inside `scripts/`).
- No attribution/generator credits or assistant coauthor trailers in code, commits, PRs, issues (enforced by policy gate). Repeated verbatim Markdown paragraphs are rejected.
- Never add permission-bypass flags; never write user-global native CLI config; lane config is lane-private.
- Reservations are advisory; never describe them as locks.
- Integration tests use real local MCP transport and temp Git repos; do not present as live-model verification.
- Commit types `feat`/`fix`/`perf` touching `agent_parley/` or `plugins/agent-parley/` trigger auto release.
