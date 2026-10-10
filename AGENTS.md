# agent-parley

This repository's operating guidance is self-contained. User-global agent
configuration belongs to the operator and must not be changed by repository
setup, development installation, or tests. Keep development state and its
executable separate from the daily Agent Parley installation.

Follow `CONTRIBUTING.md` for Google Python style guidance, the project's explicit
tooling adaptations, and coordination invariants. New production APIs require
type annotations and Google-style documentation; Ruff enforces the configured
rules. Keep the 80-character formatting target and do not weaken checks.

- Python 3.12+; coordination runtime uses only the standard library.
- Follow module boundaries in docs/architecture.md. Document behavior and
  rationale in docstrings or docs; do not add Python inline comments.
- Keep coordination state outside target repositories. Preserve native CLI
  authentication and permissions; never add bypass flags.
- Read the implementation before changing launch, identity, or process handling.
- Reservations are advisory. Do not describe them as enforced filesystem locks.
- Use RTK explicitly for development commands when available.
- Do not add generator credits, assistant attribution, robot signatures, or
  assistant coauthor trailers to code, commits, PRs, issues, or comments.
  Use neutral contribution and release text. Enforce this through the policy
  and PR hygiene gates; do not remove required license notices.
- Run focused checks while editing. The final gate is `make check`, also used by CI.
- Integration tests use real local MCP transport and temporary Git repositories.
  Distinguish those results from verification of live model behavior.

## Coordination roles

Roles describe a lane's function, not its provider. A lane keeps its role until
the operator reassigns it, so swapping the native client behind a lane does not
move its responsibilities. Call `list_participants` for the current roster.

| Role | Holds | Never does |
| --- | --- | --- |
| Implementer | Production edits for its claimed issues, in its own worktree | Review its own work as independent verification |
| Reviewer | Read-only inspection of a peer's commits, findings reported as mail | Commit to a peer's branch or the base checkout |
| Verifier | Gate runs and acceptance evidence in disposable state | Present a transport test as live model behavior |
| Operator | Priorities, remote writes, shared settings, publishing | Delegate its authority to a lane |

One file has one owner at a time. To change a file another lane owns, send a
request and wait for its reply rather than editing across the boundary.

Issues carry responsibility. Claim before working, hand off with an offer the
recipient accepts, and release rather than holding idle work. A role grants no
claim by itself, and a claim grants no authority outside the claimed issue.

## Working rules for lanes

1. Edit only your own worktree. Never reset, clean, switch, merge or modify a
   peer worktree or the base checkout.
2. Run gates in your own worktree with coordination state pointed at throwaway
   directories. Never run an installation target against the daily tool.
3. Treat the operator's daily installation, its state root, its service port
   and user-global native configuration as read-only.
4. Remote writes of any kind need the operator's explicit approval. Prepare a
   proposal instead of acting.
5. Reservations and host resource leases are advisory. Release them explicitly;
   a crashed process does not free what it held.
6. State the evidence level behind a claim: read the source, ran a transport
   test, observed a hook fire, or verified a live model response.
