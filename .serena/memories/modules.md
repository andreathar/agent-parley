<!-- source: 07673ea -->
# Module map (generated)

Regenerate: `python .serena/refresh_map.py`. Stale if the source
stamp above differs from `git rev-parse --short HEAD`.
Format: module (public symbols) purpose -> imports from agent_parley.
For live symbols use find_symbol/get_symbols_overview, not this file.

## agent_parley/

- `__init__` (2) Native coding agents, separate workspaces, shared coordination.
- `__main__` (0) Runs Agent Parley as a Python module. -> entry
- `adapter_evidence` (3) Records sanitized launch declarations independently of runtime activation. -> protocol, roster, state
- `agents` (2) Shares bounded agent inventory and presentation settings with CLI and UI. -> adapter_evidence, checkpoints, history, issues, lanes, lifecycle, mail_evidence, opencode_context, protocol, roles, roster, state, store, supervision, views, workspace
- `agents_cli` (3) Shares agent inventory and configuration with the operator console. -> agents, state
- `amp` (3) Adapts native Amp settings and tool hooks to shared checkpoints. -> protocol, state
- `approvals` (6) Records the operator decision that integration commands can require. -> metrics, roster, state
- `archive` (4) Export and import of the coordination state as one archive without secrets. -> attachments, roster, state, store
- `attachments` (15) Spills oversized coordination payloads to bounded files under state. -> state
- `blueprints` (11) Blueprints: ordered deterministic and agent steps one claim runs through. -> cli, forge, issues, roster, state, supervision, unattended, worktrees
- `budgets` (14) Derives lane consumption against advisory budgets and enforced run limits. -> checkpoints, notify, process, reclaim, records, roster, state, store
- `checkpoints` (88) Observes native checkpoints and reads coordination without model calls. -> amp, attachments, budgets, copilot, denials, dialogs, gemini, hook, issues, lanes, notify, opencode, policy, process, protocol, recommend, recovery, roles, roster, state, store, supervision
- `claims` (2) Issue claims, offers, handoffs and the advisory work-order plan. -> cli, convergence, recommend, recovery, reports
- `cli` (65) Launches native agents with isolated worktrees and in-house coordination. -> agents_cli, amp, approvals, archive, attachments, blueprints, budgets, checkpoints, claims, completion, console, convergence, copilot, dashboard, delivery, demo, dialogs, evidence, forecast, forge, gemini, history, host_resources, inbound, integration, issues, lanes, launch, lifecycle, merges, metrics, notify, opencode, plan, plugins, pool, preflight, problems, process, protocol, reclaim, recommend, recovery, retirement, retries, roles, roster, settings, start, state, status, store, supervision, tables, terminal, timeouts, unattended, views, watch, wiring, workspace_cli, worktrees
- `completion` (4) Shell completion generated from the command parser itself. -> issues, roster
- `console` (11) Serves a loopback operator console over the existing command surface. -> agents, state
- `convergence` (17) Accounts whether an owned issue converges on a verified result. -> issues, lifecycle, metrics, notify, state, store
- `copilot` (6) Adapts native Copilot CLI hook results without changing permissions. -> hook, roster, state
- `core` (1) State root, service lifecycle, projects and participants of the bridge. -> cli, pool, process, state, workspace
- `dashboard` (12) Renders a live read-only operator view of coordination and enforcement. -> approvals, blueprints, budgets, checkpoints, issues, lanes, metrics, plan, process, records, roster, state, store, supervision, tables, views
- `decisions` (18) Durable records of the situations that wait on an operator's answer. -> checkpoints, notify, state
- `delivery` (5) Delivers coordination to a lane whose CLI raises no delivery hook. -> checkpoints, issues, roster, state, store, supervision
- `demo` (18) Throwaway sandbox that runs the coordination story with stub lanes. -> demo_scenario, store
- `demo_scenario` (13) The coordination story the demo tells, one function per chapter. -> demo
- `denials` (6) Records a tool call the provider's own permission layer refused. -> decisions, state
- `dialogs` (25) Recognizes the native client dialogs a launcher's terminal is holding. -> checkpoints, decisions, lanes, notify, protocol, roster, state, supervision
- `entry` (1) Startup path that loads only what the typed invocation needs. -> cli, start
- `evidence` (4) Builds review evidence from retained coordination records. -> checkpoints, issues, state, store
- `forecast` (3) Forecasts a reservation collision from the project's co-change history.
- `forge` (25) Optional best-effort exchanges with the repository's issue tracker.
- `gemini` (3) Adapts native Gemini CLI settings and hooks without changing permissions. -> state
- `history` (3) Answers ownership questions over the records coordination already keeps. -> issues, metrics, state, store
- `hook` (9) Asks the running service for a hook decision, deciding locally if it cannot. -> checkpoints, process
- `host_resources` (6) Coordinates advisory host resources across projects and Parley homes. -> roster, state
- `inbound` (21) Status queries and decision answers an operator sends over the Telegram bot. -> cli, decisions, notify, roster, server, state, store
- `integration` (1) Lane merges, bulk integration runs, and operator approve/reject decisions. -> cli, mail
- `issues` (32) Local, repository-scoped issue ownership and explicit handoffs. -> attachments, lifecycle, recovery, retries, roster, state, status, store, supervision
- `lanes` (27) Owns the one authoritative state record of every lane. -> state
- `launch` (4) Identity registration, coordination protocol, hooks and native launch. -> adapter_evidence, amp, blueprints, checkpoints, cli, core, denials, forge, gemini, hook, issues, lifecycle, merges, opencode, reclaim, recovery, roster, workspace
- `lifecycle` (20) Durable execution state for authorized repository issues. -> attachments, issues, state, store
- `mail` (1) Sends operator mail and decisions, and reads lane mail and pending items. -> cli, core, store
- `mail_evidence` (1) Reads bounded mail metadata without interpreting silence as completion. -> roster, state, store
- `merges` (27) Gates, previews and ordering for integrating lane branches into the base. -> checkpoints, lifecycle, policy, process, roster, state, worktrees
- `metrics` (13) Measures lane idleness and waiting from records the runtime already keeps. -> attachments, checkpoints, issues, process, state, store
- `notify` (20) Outbound notification of the coordination changes an owner waits on. -> checkpoints, cli, decisions, problems, state
- `opencode` (3) Adapts native OpenCode configuration and plugin events to shared hooks. -> protocol, state
- `opencode_context` (3) Journals Parley context separately from native prompts and task ACKs. -> state
- `plan` (19) Versioned work-order plans recorded as advisory dependency edges. -> issues, lifecycle, roster, state
- `plugins` (12) Adds the Agent Parley plugin to each supported native CLI on PATH. -> protocol, roster
- `policy` (3) Detects assistant attribution in text a lane would publish.
- `pool` (10) Keeps spare lane worktrees prepared so a new lane skips the init wait. -> process, state, workspace, worktrees
- `preflight` (1) Reads launch scope and configuration without initializing any state. -> host_resources, protocol, roster, start, state, supervision
- `problems` (4) Derives every condition an operator should act on from one status reading. -> approvals, budgets, dialogs, issues, lanes, merges, plan, reclaim, records, recovery, roster, state, status, store, supervision, tables
- `process` (43) Identifies and stops server processes without PID-reuse races. -> state
- `protocol` (11) Compatibility contract between the launcher, the plugin and the service.
- `reclaim` (19) Decides which lane worktrees and branches a project may reclaim. -> forge, issues, lanes, pool, recovery, retirement, state, store, supervision
- `recommend` (7) Ranks the unclaimed issues a lane could take next, and says why. -> forecast, forge, issues, plan
- `records` (5) Reads usage and capacity from session records native CLIs already write. -> roster, state
- `recovery` (16) Captures and restores durable work for an abandoned issue claim. -> dialogs, issues, process, state
- `reports` (1) Records lane reports and reviews, and reads, exports and archives history. -> cli, core
- `retirement` (5) Withdrawal of one lane from a project at that lane's own request. -> issues, lifecycle, roster, state
- `retries` (7) Retry contracts shared by the coordination store and the issue ledger. -> state
- `roles` (10) Operator-defined role rules that refuse tool calls a lane may not make. -> state
- `roster` (46) Defines providers, credential profiles, and project participants. -> forge, state
- `server` (6) Serves the bounded coordination tools over authenticated local MCP HTTP. -> checkpoints, hook, inbound, metrics, protocol, recommend, retries, roster, state, store, supervision, terminal, waits
- `settings` (1) Per-project settings: verification, approval, branch, tracker and budgets. -> cli, core, dialogs, pool, unattended
- `start` (8) Start-here screen a bare `agent-parley` prints. -> cli, notify, plugins, process, status, store
- `state` (6) Small shared persistence helpers; safe to import in per-tool hooks.
- `status` (16) Reads launcher, store and service fitness, problems, and lane status. -> blueprints, cli, core, issues, lifecycle, metrics, pool, roster, supervision, tables
- `store` (63) Owns transactional coordination storage without third-party runtime code. -> attachments, forecast, host_resources, issues, metrics, protocol, recommend, retirement, retries, roster, state
- `supervision` (112) Observes lane availability and reminds holders about waiting peers. -> blueprints, budgets, checkpoints, cli, convergence, decisions, denials, dialogs, forge, issues, lanes, lifecycle, metrics, notify, plan, process, reclaim, recommend, records, recovery, retirement, roster, server, state, status, store, terminal, timeouts
- `tables` (17) Lays out coordination rows as one plain table for every operator view.
- `terminal` (16) Owns a native terminal and admits bounded coordination wake requests. -> dialogs, state, supervision
- `timeouts` (13) Applies the recommended default to a reversible decision left unanswered. -> cli, decisions, inbound, metrics, notify, roster, state, unattended
- `unattended` (7) Unattended integration authorized by an explicit project policy. -> cli, integration, lifecycle, metrics, roster, state
- `views` (22) Renders read-only command results as one machine-readable document. -> issues
- `waits` (3) Holds a lane's turn open until its next mail arrives or a bound expires. -> roster, state, store
- `watch` (5) Follows one lane's coordination events as a stream. -> blueprints, checkpoints, history, state, store, views
- `wiring` (3) Checks that running lanes are bound to the service, port and rules. -> preflight, roles, start, state
- `workspace` (10) Prepares lightweight checkouts and reports shared media evidence. -> agents, roster, state
- `workspace_cli` (3) Declares preview-first workspace and read-only media commands. -> workspace
- `worktrees` (10) Runs Git for the base checkout and prepares the lanes created from it. -> state

## scripts/

- `__init__` (0) Repository verification and release tooling.
- `acceptance` (16) Unattended acceptance run for a live lane estate. -> metrics, problems, server, supervision
- `benchmark` (2) Measures local coordination latency and payload size without model calls. -> server, store
- `check_issue_hygiene` (3) Validates live issue metadata through read-only GitHub requests.
- `check_mcp_manifests` (5) Validates the MCP directory manifests against each directory's schema.
- `check_policy` (8) Enforces documentation, dependency, and release metadata boundaries. -> policy
- `check_pr_hygiene` (5) Validates live pull-request metadata through read-only GitHub requests.
- `ci_scope` (6) Decides which legs of the Check workflow a pull request needs.
- `codex_bundle` (8) Builds and checks the skills-only archive the Codex plugin directory reviews.
- `install_dev` (2) Installs an immutable development snapshot under a distinct command. -> roster, start, state
- `pages_video` (2) Rewrites the README launch video line for the GitHub Pages build.
- `record_demo` (22) Records the coordination demo and renders it as an animated SVG. -> demo, demo_scenario, server
- `record_live` (16) Records real ``claude`` and ``codex`` lanes and renders an animated SVG. -> demo
- `release_artifacts` (4) Builds the versioned release bundle from verified package artifacts.
- `release_publish` (28) Publishes explicit releases using one immutable set of verified assets.
