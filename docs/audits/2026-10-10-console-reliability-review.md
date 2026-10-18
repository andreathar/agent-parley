# Console reliability and workflow review

## Scope

Reviewed the customized checkout's operator console, CLI status selection,
host-resource registry selection, and existing lifecycle/handoff tests.
This is a focused source and local-test review, not certification of every
module, browser interaction, or live Claude/Codex session.

The daily installation, live services, native permissions, movie worktrees,
and remote repositories are outside this change. No installation or restart
is part of the patch.

## Confirmed defects and corrections

- Non-ASCII login/request tokens raised an exception during comparison.
  Reject them with normal authentication errors. A loopback HTTP regression
  covers both entry points.
- Malformed Content-Length raised an uncaught exception. Validate the length
  before reading and return a structured error; retain the existing size cap.
- Logout bypassed the request-token check promised for mutations. Validate
  before discarding the session; a refused logout leaves the session intact.
- Snapshot timestamps used local wall time with a UTC suffix. Format UTC.
- Subprocesses discarded an explicit host-resource registry override. Preserve
  that setting so console operations use the selected registry rather than
  falling back to the daily registry. A real subprocess regression verifies
  propagation without reading or modifying the daily registry.

The new root Mermaid diagram was also missing from CI path classification.
Root Mermaid documents now receive the same documentation category as root
Markdown, with a regression case.

The console regression cases failed before correction. Existing authentication,
action allowlisting, and shell-free subprocess execution remain in place.

## Project scope and task transitions

The CLI's JSON status route does not apply the working-directory default used
by the plain text status route. The console already uses that JSON route;
no evidence here supports restricting it to one project. No redundant project
selection patch was added.

Existing lifecycle tests cover dependency completion and unblocking. Existing
handoff tests cover acceptance transferring ownership and reservations.
The autonomous-work-loop test exercises three authorized issues in dependency
order, verification and real temporary Git merges, with simulated wake delivery.
Those tests do not establish that a live model received a hook or that the
movie task board is connected to these issue transitions.

Before automating the movie pipeline, map its approved tasks onto one durable
issue/dependency plan, with an explicit completion gate and next owner. Keep
image delivery, review completion and artistic approval distinct. Observe an
actual handoff in isolated development state before enabling live dispatch.
Do not create a second competing lifecycle just to send completion messages.

## Validation

Focused console tests: 20 passed, including six new regression cases.
Initial full gate: 2 failures, 2842 passed, 2 skipped. One failure exposed
the Mermaid classification gap; the other detected a source edit during a
temporary service test. Final `make check`: passed with sources held unchanged. Ruff, formatting,
Mypy, policy and package build passed; pytest reported 2846 passed and
2 skipped in 134.76 seconds. Fourteen Python PTY/fork deprecation warnings
remain. This validates local source/tests, not live model or browser behavior.

The two initial failures also passed in a focused rerun (28 tests).
Changes remain local and uncommitted; no daily installation was replaced.
