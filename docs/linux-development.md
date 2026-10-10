# Linux development isolation

This fork prioritizes Codex and Claude Code. Daily work remains on the existing
installation while customization is developed and validated separately.

## Install and inspect

Run from this checkout:

```sh
make install-dev
agent-parley-dev preflight --repo /path/to/project --json
```

The installer publishes `~/.local/bin/agent-parley-dev`. It installs a wheel in
a new private snapshot under `~/.local/share/agent-parley-dev`, pins the state
home to `~/.local/state/agent-parley-dev`, and defaults to loopback port **8877**.
It does not replace `agent-parley`, install native plugins, edit shell startup
files, or change native authentication or permissions. `--home` overrides are
refused by the development wrapper. Python runs in isolated mode, so a local
checkout or `PYTHONPATH` cannot replace the installed package at wrapper entry.

New development state disables automatic wakes, prompts and reclamation.
Existing supervision settings are preserved. Native CLI accounts are inherited
unless an explicit credential profile is selected; a separate Parley state
directory does not create separate Codex or Claude accounts. The resource
registry intentionally remains shared, as explained below.

`preflight` reads configuration without initializing state, starting a service,
registering a repository, executing native clients, or changing Git's index. It
reports the loaded module, Python interpreter, version/revision, state home,
saved port and whether something listens there, recorded service process,
dirty-checkout consequences, selected native account directories, hook-group
counts, plugin records, project settings and effective supervision.
`--provider NAME --credentials PROFILE` inspects a specific launch binding.
Hook commands and credential values are omitted. Native policy observations are
not a claim to have evaluated all enterprise, project-trust, session or profile
precedence; loaded plugins and real hook composition need a native session.

An installed snapshot does not follow edits. Stop its development service with
`agent-parley-dev down`, then rerun `make install-dev` to install new code.
The installer refuses replacement while that state home's service is running.
`current.json` records the snapshot and wheel SHA-256. Each snapshot's
`receipt.json` also retains the prior launcher text. For rollback, stop the
development service and atomically restore that previous launcher with mode
0700; retain its referenced snapshot. No daily launcher needs to change.

Installer paths and port can be selected explicitly using
`uv run --locked python -m scripts.install_dev --help`. State must remain
outside the checkout and separate from the normal daily state. Keep test
repositories separate too: two installations that register the same repository
still share its Git refs and worktree registry.

## Registration and shared settings

Initial `setup`, `participant add`, or `run` refuses a dirty base checkout.
Staged, unstaged and untracked work stays in place. Commit it first, or explicitly
authorize moving it to a stash:

```sh
agent-parley-dev setup /path/to/project --stash
```

This uses Git's shared stash stack and prints the full object ID needed to
restore the exact entry. Existing registrations do not stash the base checkout.

Provider and credential definitions, removals and provider-budget changes are
shared across all projects in a state home. They now require an operator shell:
a lane token or a working directory inside a registered lane refuses the write.
Both the selected and inherited state homes are inspected. Native plugin
installation has the same lane guard, and the development wrapper refuses it
entirely. These guards prevent operational mistakes; processes running as the
same Unix user are not sandboxed from each other by Parley.

## Shared PC resources

Project file reservations remain project-scoped. The new MCP tools coordinate
resources across repositories **and across Parley state homes**:

| Tool | Input | Result |
| --- | --- | --- |
| `reserve_host_resources` | `resources`, 1–16 canonical names | All requested resources granted or conflicts with no new grants |
| `list_host_resources` | Empty object | Current advisory holdings and registry location |
| `release_host_resources` | `lease_ids`, 1–16 returned IDs | Release only the calling lane's exact lease generations |

Use consistent names such as `gpu:0`, `port:5432` and `device:android-1`.
Installations must share `AGENT_PARLEY_RESOURCE_HOME`, whose default is
`~/.local/state/agent-parley-host`. Different values create separate coordination
domains. The registry is user-owned, private (0700), atomically written and
serialized under a bounded lock. It permits at most 256 holdings.

There is no automatic expiry, takeover, retirement cleanup or service-restart
cleanup. Losing a process does not prove that a resource is free. A lane releases
after finishing; an operator can inspect and recover stranded holdings by their
exact IDs:

```sh
agent-parley-dev host-resources list --json
agent-parley-dev host-resources release LEASE_ID --json
```

A stale release cannot delete a later reservation of the same resource. Requests
by the same owner return existing holdings; a foreign release refuses the whole
request. Owner identity includes the state home, project and participant, never
the authentication token. The resource transaction is separate from the local
MCP event store; event telemetry is best effort and never decides a grant.

These are advisory reservations, not OS locks or a device-use detector. They
cannot prevent port binding or GPU use by external programs or older Parley
builds. The untouched daily 0.18.0 installation does not gain these tools until
a separately reviewed migration installs the fork for daily work.

## Operator console

```sh
agent-parley-dev console --port 8890
```

The console serves a single page on the loopback interface only. It answers
nothing before a caller presents the bearer token stored in that state home's
`config.json`, which only the owner of the private state directory can read.
A successful sign-in returns a session cookie marked `HttpOnly` and
`SameSite=Strict`, plus a separate request token that every mutating call must
echo in `X-Parley-Csrf`. Sessions expire after an hour.

Every mutation is dispatched through the same command line an operator would
type, so lane guards, operator refusals, deadlines and audit records apply
unchanged. The console adds no authority of its own and holds a fixed action
allowlist: a command absent from that list cannot be reached, and each argument
is validated against its declared kind before the vector is built. Arguments
are passed as a list and never through a shell. Serving the console is itself
an operator action and is refused from a lane.

The dashboard reads `status --json` and `host-resources list --json` and
refreshes every fifteen seconds. It reports the service state, registered
projects with their lanes, and advisory host holdings with their lease IDs.

## Supported agents

| Agent | Provider | Adapter | Launch |
| --- | --- | --- | --- |
| Codex | `codex` | `codex` | Native lane with lifecycle hooks |
| Claude Code | `claude` | `claude` | Native lane with lifecycle hooks |
| OpenCode | `opencode` | `opencode` | Native lane with lifecycle hooks |
| Copilot CLI | `copilot` | `copilot` | Native lane; requires its own profile |
| Antigravity | `antigravity` | `gemini` | Native lane through the `agy` command |

Antigravity is reached by mapping its `agy` command onto the Gemini adapter,
because that CLI reads the same settings document, `mcpServers` entries and
hook event names the adapter already writes:

```sh
agent-parley-dev provider add antigravity --adapter gemini \
  --executable agy --home-env GEMINI_CLI_HOME
```

Copilot requires a dedicated credential profile. Sharing one profile across
lanes lets a retirement remove another project's hooks, because cleanup
identifies ownership by participant name alone:

```sh
agent-parley-dev credentials add copilot-lane --config-home DIR
```

Sign in to each profile once with its own native CLI before launching a lane
that uses it. A separate Parley state directory does not create a separate
native account.

An editor that cannot be launched headlessly can still join as a participant by
placing the authenticated endpoint and its own bearer token in that editor's
MCP configuration. Such a participant has the full coordination toolset but no
native lifecycle hooks, so the supervisor cannot wake it or observe a stall.

## Validation and adoption

Focused regressions exercise untouched dirty checkouts, shared mutation refusal,
read-only preflight, cross-project/cross-home resource conflicts, multi-process
contention, stale releases, persistence and authenticated local MCP transport.
The existing native launch tests cover Codex/Claude command construction. The
complete repository gate is `PYTEST_XDIST_AUTO_NUM_WORKERS=4 make check` on this
16-thread machine.

Transport and command-construction tests are not live model or native hook
composition proof. Before daily migration, use a disposable committed repository
and two separate operator terminals for the Codex and Claude lanes. Confirm each
receives its own identity, observes peer mail, claims distinct work, refuses a
contested host resource, hands off an issue and resumes after a service restart.
Confirm existing user hooks still fire and native permission prompts remain
effective. Keep this acceptance evidence separate from unit and MCP results.

On 2026-10-10, the installed Codex 0.162.1 app-server was queried with
`config/read` and `hooks/list` using seven inert session hook definitions.
It listed all seven session hooks alongside the two existing user hooks. The
user hooks were trusted but disabled; the injected probe definitions were
enabled but marked modified, so listing them was not execution permission.
The effective native approval and sandbox settings were unchanged. This probe
started no model turn and did not write hook trust. New development hook
definitions must be reviewed through the native `/hooks` interface.
This matches the [official Codex hook contract](https://learn.chatgpt.com/docs/hooks).

Claude's [hook reference](https://code.claude.com/docs/en/hooks) and
[settings precedence](https://code.claude.com/docs/en/settings) document additive
hook/list composition and session-only `--settings` values. These documentation
checks do not establish that this machine executed every Claude hook. No new
live Claude or Codex model acceptance session is claimed by this milestone.

The audit in `audits/2026-10-10-linux-fork-audit.md` is a historical baseline;
its settings inventory describes the pre-customization 0.18.0 checkout. This
document describes the changes in the development branch. Publishing, changing
marketplace ownership, release automation and daily migration are separate work.
