# Shared media and lightweight workspaces

This opt-in profile keeps canonical media in the main repository and prepares
future lane checkouts with sparse exclusions before any LFS smudge. Existing
lanes, credentials, native permissions and media stay unchanged. Objects in a
linked Git repository are shared; materialized checkout files need not be.

Preview an existing registry relative to the registered canonical root:

```sh
agent-parley workspace config --repo /path/to/repo --catalog projects/film/asset-registry.csv --preview --json
```

Applying requires the revision from that preview. The manifest owns the
profile and bounded operator audit history; it is not another asset catalog.
The command refuses nonzero spare pools and inherited initialization commands.
Review those separately; no configuration command silently clears them.
`workspace plan --json` reports media/LFS paths in the current index. New
lanes recompute exclusions from their actual base commit before checkout.
Literal paths with unsupported sparse-pattern characters fail explicitly.

Only an explicitly selected new participant creates a lane. The new worktree
starts with `--no-checkout`; tracked media extensions and every filter=lfs file
are excluded before text/code checkout. LFS skip-smudge is scoped to preparation.
The repository-local worktreeConfig extension enables per-worktree sparse
settings. This is Git metadata configuration, not a native permission change.
Bare repositories and explicit core.worktree configurations refuse this mode.
A preparation failure preserves the partial lane and branch for inspection;
it never falls back to a full checkout or removes another checkout.
Readmission of an explicitly retired lane uses the same preparation.

```sh
agent-parley run reviewer --provider codex --repo /path/to/repo --output-task review-1
```

A shared-media launch needs a stable output-task ID; --issue supplies issue-N.
Resume reuses that ID from this lane's activity record. Profiles must match
between the lane and project: changing or disabling the future profile never
converts an existing lane, and launch refuses mismatches. Outputs are scoped
to lane/outputs/TASK_ID. The canonical root cannot overlap that output path.
Existing ignored output files retain the lane through ordinary retirement.

Adapters receive AGENT_PARLEY_MEDIA_ROOT, AGENT_PARLEY_ASSET_CATALOG and
AGENT_PARLEY_OUTPUT_ROOT alongside the existing project identity. Claude,
Codex, Copilot, Gemini and OpenCode carry these explicit references through
their usual launcher environment/context. Presets inherit the real adapter.
Amp/unknown adapters refuse; no permission or plugin trust is broadened.
A supported path-reference contract does not prove native read access.

`media status --json` reads the profile and paths. `media doctor --sample
inputs/clip.mp4 --json` reads at most80bytes of that canonical sample on the
host, detects an LFS pointer and records host_read_verified separately from
native_read_verified (unknown). Doctor returns nonzero while native access
is unverified, even after a successful host probe. No doctor hydrates, downloads
or writes an
original. The Agents panel shows paths, profile match and capabilities with
the same evidence limits. Registry validation belongs to the project resolver;
Parley reports catalog_validation=not_checked rather than guessing validity.

Readonly filesystem enforcement is not implemented by a reference, symlink,
hardlink or this profile. Existing role hooks are operational refusals, not a
sandbox. Native access denied by a platform is a blocker; do not copy inputs,
add writable directories or change security settings to bypass it. A native
read-only mount or authenticated reader would need separate implementation,
platform support and explicit permission decisions.

## Movies resolver

The Movies staging patch adds scripts/shared_media.py and read-only `manage.py
assets validate|resolve|output` commands. Inputs resolve against the explicit
canonical workspace/film root, while output paths resolve against a separate
per-task root and refuse existing files. Absolute paths, traversal, Windows
path confusion and escaping symlinks fail. Registry CSV is parsed with exact
column widths, line provenance, duplicate-ID/hash/approval checks across
legacy and current record widths. It preserves
legacy originals and proposes only values deducible from a six-field record,
an embedded hash and an unambiguous existing path. Proposals do not verify
bytes or approve media. Invalid registry data blocks ID-based resolution.

Kling's existing fingerprint and CLI input validation reuse that resolver.
`--media-root ... --resolve-reference ...` is a read-only path probe. Shared
media paid submission refuses before account, upload or provider calls: there
is no separate shared-ledger/approval implementation. Existing canonical
producer submissions and their approval/idempotency gates retain their flow.
No path reference gives a reviewer spending authority.

## Rollout and rollback

Verify source hashes and apply the reviewed patch selectively. Build/install
and coordinator/console activation are separate approvals; restarting them
alone creates no lanes or grants. Resolve registry errors before relying on
asset IDs. Choose one new lane explicitly for a pilot after confirming native
read capability and output boundaries. Stage derivatives with IDs/version,
recorded and verified hashes distinguished, provenance and review evidence.
The producer publishes a new version after explicit review/CAS; advisory
reservations are not a cross-process write lock. No original is overwritten.

Rollback the manifest profile by revision for future lanes; existing sparse
lanes retain their profile and refuse incompatible launches. Preserve outputs
and history. Do not automatically hydrate excluded LFS or delete lanes. Keep
worktreeConfig enabled while any worktree uses it; removing that shared Git
extension without reviewing all worktrees is unsafe. Source rollback uses the
saved baseline only if the affected files still match this patch's hashes.
