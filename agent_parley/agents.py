"""Shares bounded agent inventory and presentation settings with CLI and UI.

Routing identities, native sessions and display names have separate lifetimes.
Changing a display name here never changes a mailbox, token or role grant.
Only coordination records are read: native session transcripts and credential
configuration are deliberately outside this inventory's evidence boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import shutil
import sqlite3
import time
from pathlib import Path

from agent_parley import (
    adapter_evidence,
    checkpoints,
    history,
    issues,
    lanes,
    lifecycle,
    mail_evidence,
    opencode_context,
    protocol,
    roles,
    roster,
    store,
    supervision,
    views,
    workspace,
)
from agent_parley.state import BridgeError, lock, write_json

SCHEMA_VERSION = 1
MAX_NAME = 80
MAX_AUDIT = 100
MAX_HISTORY = 100


def _revision(document: dict, rules: dict | None) -> str:
    """Hashes the full manifest and role catalog for compare-and-set edits."""
    value = json.dumps([document, rules], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _settings(participant: dict) -> dict:
    """Returns presentation fields without exposing a credential profile."""
    return {
        "display_name": participant.get("display_name", participant["display"]),
        "role": participant.get("function_role", ""),
    }


def _read(path: Path) -> dict:
    """Reads a manifest without normalizing away fields owned by other users."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise BridgeError(f"Cannot read project manifest at {path}.") from error
    if not isinstance(value, dict):
        raise BridgeError(f"Project manifest at {path} must be an object.")
    return value


def _project(home: Path, repo: Path) -> Path:
    """Resolves registered roots and lanes without starting Git or a service."""
    selected = repo.expanduser().resolve()
    for path in sorted((home / "projects").glob("*/project.json")):
        manifest = roster.normalize(_read(path))
        roots = [
            Path(manifest["root"]),
            *(
                Path(entry["lane"])
                for entry in manifest["participants"].values()
            ),
        ]
        if any(selected == root.resolve() for root in roots):
            return path.parent
    raise BridgeError(
        "Project is not registered; use agents list to select its root."
    )


def _name(value: object) -> str:
    """Validates printable, bounded display names without changing IDs."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or not value.isprintable()
        or len(value) > MAX_NAME
    ):
        raise BridgeError(
            "Display name must be printable text of 1 to "
            f"{MAX_NAME} characters."
        )
    return value.strip()


def configure(
    home: Path,
    repo: Path,
    participant: str,
    display_name: str,
    *,
    role: str | None = None,
    preview: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """Previews or atomically changes one participant's presentation settings.

    Args:
        home: Existing private coordination state root.
        repo: Registered project root or participant worktree.
        participant: Immutable participant identifier, never a display name.
        display_name: Bounded printable name for operator views.
        role: Existing role ID as a presentation preference; empty clears it.
            None preserves the preference. Operational assignments never change.
        preview: True returns the proposed change without creating lock files.
        expected_revision: Revision from a preview, required when applying.

    Returns:
        Versioned before/after values, revisions and the recorded audit ID.
        An already applied identical request is a no-op, including repeated
        clicks carrying the previous revision.

    Raises:
        BridgeError: On invalid input, unknown participant or role, stale
            revision, unreadable state or an apply without operator authority.
    """
    home = home.expanduser().resolve()
    directory = _project(home, repo)
    name = _name(display_name)
    if type(preview) is not bool:
        raise BridgeError("Preview must be true or false.")
    if not preview:
        roster.require_operator(home, "Changing agent presentation settings")
        if not expected_revision:
            raise BridgeError("Apply requires a revision from a fresh preview.")
        with lock(directory / "setup.lock"), lock(directory / "roles.lock"):
            return _configure(
                directory, participant, name, role, preview, expected_revision
            )
    return _configure(
        directory, participant, name, role, preview, expected_revision
    )


def _configure(
    directory: Path,
    participant: str,
    display_name: str,
    role: str | None,
    preview: bool,
    expected_revision: str | None,
) -> dict:
    """Validates and publishes settings plus audit in the same manifest."""
    path = directory / "project.json"
    document = _read(path)
    manifest = roster.normalize(json.loads(json.dumps(document)))
    if participant not in manifest["participants"]:
        raise BridgeError("Unknown participant; select its immutable agent ID.")
    rules = roles.load(directory)
    catalog = (rules or {}).get("roles", {})
    if role is not None and (
        not isinstance(role, str) or role and role not in catalog
    ):
        raise BridgeError("Unknown role; choose an existing project role ID.")
    entry = manifest["participants"][participant]
    roster.identifier(participant, "participant")
    before = _settings(entry)
    after = {
        "display_name": display_name,
        "role": before["role"] if role is None else role,
    }
    revision = _revision(document, rules)
    changed = before != after
    if changed and expected_revision and expected_revision != revision:
        raise BridgeError(
            "Configuration changed; preview again before applying."
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "repo": manifest["root"],
        "participant": participant,
        "preview": preview,
        "changed": changed,
        "revision": revision,
        "next_revision": revision,
        "before": before,
        "after": after,
        "operational_role_changed": False,
        "role_semantics": (
            "presentation preference; operational grants unchanged"
        ),
    }
    if not changed:
        if not preview:
            result["applied_revision"] = revision
        return result
    updated = json.loads(json.dumps(document))
    if "participants" not in updated:
        updated["participants"] = manifest["participants"]
    target = updated["participants"][participant]
    target.update(display_name=display_name, function_role=after["role"])
    proposal = _revision(updated, rules)
    result["next_revision"] = proposal
    if preview:
        return result
    audit = {
        "id": proposal,
        "at": time.time(),
        "actor": "operator",
        "before": before,
        "after": after,
        "previous_revision": revision,
    }
    target["config_history"] = [
        *target.get("config_history", [])[-(MAX_AUDIT - 1) :],
        audit,
    ]
    write_json(path, updated)
    result.update(audit_id=proposal, applied_revision=_revision(updated, rules))
    return result


def _availability(
    record: dict | None,
    native: tuple[float, str],
    now: float,
    ceiling: float,
) -> dict:
    """Ages native evidence independently from supervision's state heartbeat."""
    observed, source = native
    age = max(0.0, now - observed) if observed else None
    freshness = (
        "unknown" if age is None else ("fresh" if age <= ceiling else "stale")
    )
    recorded = (record or {}).get("state", "unknown")
    state = {
        "working": "executing",
        "idle": "idle",
        "blocked": "blocked",
        "stopped": "disconnected",
        "dead": "disconnected",
        "reclaimed": "disconnected",
        "starting": "configured",
    }.get(recorded, "unknown")
    if freshness != "fresh":
        state = freshness
    return {
        "state": state,
        "recorded_state": recorded,
        "freshness": freshness,
        "observed_at": observed or None,
        "age_seconds": int(age) if age is not None else None,
        "stale_after_seconds": ceiling,
        "freshness_source": source,
        "recorded_updated_at": (record or {}).get("updated"),
        "transport_connected": None,
        "evidence": (record or {}).get("evidence", ""),
    }


def _observed_hooks(directory: Path, participant: str) -> dict[str, float]:
    """Reads only event names and times from bounded coordination hook logs."""
    observed: dict[str, float] = {}
    ignored = {reason.value for reason in checkpoints.UNOBSERVED} | {
        checkpoints.Reason.POLLED_DELIVERY.value
    }
    for suffix in ("events.1.jsonl", "events.jsonl"):
        path = directory / f"{participant}-{suffix}"
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 262144))
                text = stream.read(262144).decode(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines()[-2000:]:
            try:
                event = json.loads(line)
                if (
                    isinstance(event, dict)
                    and event.get("event") in roster.HOOK_EVENTS
                    and event.get("reason_class") not in ignored
                ):
                    stamp = float(event.get("ts", 0) or 0)
                    if not math.isfinite(stamp) or stamp <= 0:
                        continue
                    key = str(event["event"])
                    observed[key] = max(observed.get(key, 0), stamp)
            except (ValueError, TypeError):
                continue
    return observed


def _native_observation(
    directory: Path, name: str, observed: dict[str, float], now: float
) -> tuple[float, str]:
    """Reads native checkpoint age without adopting launcher or poll clocks.

    Retained hook timestamps take precedence because other runtime writers
    can update an activity file while preserving its previous event name.
    A valid native activity timestamp is the fallback for older projects
    without a retained hook log. Neither source establishes MCP connectivity.
    """
    stamps = [stamp for stamp in observed.values() if stamp <= now]
    if stamps:
        return max(stamps), "native hook event"
    activity = checkpoints.activity(directory, name)
    stamp = activity.get("updated")
    if (
        activity.get("event") in roster.HOOK_EVENTS
        and bool(activity.get("session_id"))
        and (
            activity.get("activity") not in {"starting", "stopped"}
            or activity.get("event") == "SessionEnd"
        )
        and isinstance(stamp, int | float)
        and not isinstance(stamp, bool)
        and math.isfinite(stamp)
        and 0 < stamp <= now
    ):
        return float(stamp), "native activity record"
    return 0.0, "no native observation"


def _skills(
    manifest: dict, entry: dict, declaration: dict | None
) -> list[dict]:
    """Lists scoped skill filenames without reading or activating prose."""
    root = protocol.plugin_root() / "plugins/agent-parley"
    collected = [
        {
            "id": "coordinate",
            "name": "coordinate",
            "source": "shipped",
            "path": str(root / "skills/coordinate/SKILL.md"),
            "available": (root / "skills/coordinate/SKILL.md").is_file(),
            "configured": None,
            "detected": None,
            "verified": None,
            "evidence": "shipped skill file; native loading not observed",
        }
    ]
    roots = {"project": Path(manifest["root"]), "lane": Path(entry["lane"])}
    seen: set[str] = set()
    for scope, base in roots.items():
        for folder in (".agents/skills", ".claude/skills", ".codex/skills"):
            for path in sorted((base / folder).glob("*/SKILL.md"))[:100]:
                resolved = str(path.resolve())
                if resolved in seen or not path.is_file():
                    continue
                seen.add(resolved)
                collected.append(
                    {
                        "id": f"{scope}:{path.relative_to(base)}",
                        "name": path.parent.name,
                        "source": scope,
                        "path": str(path),
                        "available": True,
                        "configured": None,
                        "detected": True,
                        "verified": None,
                        "evidence": (
                            "scoped skill filename; native loading unknown"
                        ),
                    }
                )
    for skill in collected:
        declared = (declaration or {}).get("configured_skills", [])
        skill["configured"] = True if skill["id"] in declared else None
        skill["active"] = None
        skill["configuration_scope"] = "explicit launch declarations only"
        if skill["configured"] and declaration is not None:
            skill["configured_at"] = declaration["configured_at"]
            skill["evidence"] = (
                "sanitized launch declaration; native skill loading unknown"
            )
    return collected


def _artifacts(directory: Path, name: str, adapter: str) -> list[dict]:
    """Detects generated launch paths while leaving their credentials unread."""
    candidates = {
        "claude": [f"{name}-mcp.json"],
        "gemini": [f"{name}-gemini-settings.json"],
        "amp": [f"{name}-amp-settings.json"],
        "opencode": [
            f"{name}-opencode/opencode.json",
            f"{name}-opencode/plugin/agent-parley.js",
        ],
    }.get(adapter, [])
    artifacts = [
        {
            "path": str(directory / path),
            "detected": (directory / path).is_file(),
            "configured": None,
            "verified": None,
            "evidence": "lane-private launch artifact; contents not inspected",
        }
        for path in candidates
    ]
    if adapter == "opencode":
        path = directory / f"{name}-opencode-context.json"
        journal: dict = {
            "path": str(path),
            "kind": "native_context_journal",
            "detected": path.is_file(),
            "configured": None,
            "verified": None,
            "evidence": "coordination context journal; metadata only",
        }
        if path.is_file():
            try:
                summary = opencode_context.summary(path)
                summary["entries"] = [
                    {
                        key: value
                        for key, value in entry.items()
                        if key != "reason"
                    }
                    for entry in summary["entries"]
                ]
                journal["summary"] = summary
            except BridgeError as error:
                journal.update(state="unknown", error=str(error))
        artifacts.append(journal)
    return artifacts


def _hook_verification(
    event: str,
    supported: bool,
    record: dict | None,
    activity: dict,
    observed: dict[str, float],
    now: float,
    ceiling: float,
) -> dict:
    """Correlates observed callbacks with a native session, never a PID."""
    stamp = observed.get(event, 0.0)
    session = activity.get("session_id")
    started = activity.get("session_started")
    updated = activity.get("updated")
    matches = bool(session) and session == (record or {}).get("session")
    finite = 0 < stamp <= now
    window = (
        isinstance(started, int | float)
        and not isinstance(started, bool)
        and math.isfinite(started)
        and finite
        and started <= stamp
    )
    latest = (
        event == activity.get("event")
        and finite
        and isinstance(updated, int | float)
        and not isinstance(updated, bool)
        and math.isfinite(updated)
        and abs(updated - stamp) <= 3
    )
    verified = supported and matches and finite and (window or latest)
    fresh = verified and stamp is not None and now - stamp <= ceiling
    live = (record or {}).get("state") in {
        "starting",
        "working",
        "idle",
        "blocked",
    }
    return {
        "verified": True if verified else None,
        "active": False
        if not supported
        else True
        if fresh and live and event != "SessionEnd"
        else None,
        "observed_active_at": stamp if verified else None,
        "verification_state": "unsupported"
        if not supported
        else "observed_recently"
        if fresh
        else "observed_stale"
        if verified
        else "unknown",
        "verification_kind": "native_event_observed" if verified else None,
        "verification_session": session if verified else None,
        "verification_scope": "retained callback observation; ongoing native "
        "activation and future delivery are not proved",
    }


def _assets(
    adapter: str,
    observed: dict[str, float],
    artifacts: list[dict],
    declaration: dict | None,
    record: dict | None,
    activity: dict,
    now: float,
    ceiling: float,
) -> dict:
    """Reports shipped adapter capabilities separately from runtime loading."""
    manifest = protocol.manifests(protocol.plugin_root()).get(adapter)
    declared = protocol.installed(manifest) if manifest else protocol.UNKNOWN
    return {
        "hooks": [
            {
                "id": event,
                "supported": event not in roster.unavailable_hooks(adapter),
                "configured": event in declaration["configured_hooks"]
                if declaration
                else None,
                "configured_at": declaration["configured_at"]
                if declaration
                else None,
                "configuration_scope": "prepared launch declaration only",
                "detected": event in observed,
                "observed_at": observed.get(event),
                "evidence": "historical coordination hook event"
                if event in observed
                else "no retained hook observation",
                **_hook_verification(
                    event,
                    event not in roster.unavailable_hooks(adapter),
                    record,
                    activity,
                    observed,
                    now,
                    ceiling,
                ),
            }
            for event in roster.HOOK_EVENTS
        ],
        "plugin": {
            "id": "agent-parley",
            "available": bool(manifest and manifest.is_file()),
            "configured": True
            if declaration and adapter == "opencode"
            else None,
            "configured_at": declaration["configured_at"]
            if declaration and adapter == "opencode"
            else None,
            "configuration_scope": "launch JavaScript plugin declarations; "
            "native package installation is not inferred",
            "detected": any(
                item["detected"]
                and item["path"].endswith("plugin/agent-parley.js")
                for item in artifacts
            )
            if adapter == "opencode"
            else None,
            "verified": None,
            "active": None,
            "mode": "native plugin" if manifest else "launch adapter",
            "protocol": declared if declared != protocol.UNKNOWN else None,
            "compatible": protocol.compatible(declared) if manifest else None,
            "evidence": "shipped manifest; native installation not inspected"
            if manifest
            else "launcher adapter; no plugin manifest",
        },
        "capabilities": {
            "adapter": adapter,
            "delivery": roster.delivery_path(adapter),
            "unavailable_hooks": roster.unavailable_hooks(adapter),
            "required_hook_gaps": [
                event
                for event in roster.REQUIRED_HOOKS
                if event in roster.unavailable_hooks(adapter)
            ],
            "launch_supported": not any(
                event in roster.unavailable_hooks(adapter)
                for event in roster.REQUIRED_HOOKS
            ),
        },
        "artifacts": artifacts,
    }


def _agent(
    home: Path,
    directory: Path,
    manifest: dict,
    name: str,
    providers: dict,
    rules: dict | None,
    record: dict | None,
    revision: str,
    now: float,
    ledger: dict,
    retained: list[dict],
) -> dict:
    """Joins one stable participant to its retained coordination evidence."""
    entry = manifest["participants"][name]
    roster.identifier(name, "participant")
    provider = providers.get(entry["provider"], {})
    adapter = provider.get("adapter", "unknown")
    configured = supervision.configuration(home, manifest)
    observed = _observed_hooks(directory, name)
    activity = checkpoints.activity(directory, name)
    receipt_error = ""
    try:
        receipt = adapter_evidence.read(directory, name, adapter)
    except BridgeError as error:
        receipt, receipt_error = None, str(error)
    generation = activity.get("launch_generation")
    current_receipt = (
        generation == receipt["generation"] if receipt and generation else None
    )
    declaration = receipt if current_receipt else None
    artifacts = _artifacts(directory, name, adapter)
    receipt_path = adapter_evidence.location(directory, name)
    artifacts.append(
        {
            "path": str(receipt_path),
            "kind": "sanitized_launch_declaration",
            "detected": receipt_path.is_file(),
            "configured": True if declaration else None,
            "verified": None,
            "evidence": (
                "prepared declarations; spawn and runtime activation unknown"
            ),
            "summary": receipt,
            "configuration_current": current_receipt,
            "error": receipt_error,
        }
    )
    current = [
        {
            "id": number,
            "issue": number,
            "claim_id": task.get("claim_id"),
            "state": lifecycle.state(task)["state"],
            "title": task.get("title", ""),
        }
        for number, task in ledger.get("issues", {}).items()
        if task.get("owner") == name
    ]
    return {
        "id": name,
        "participant": name,
        "repo": manifest["root"],
        **_settings(entry),
        "routing_name": entry["display"],
        "provider": entry["provider"],
        "adapter": adapter,
        "workspace": {
            **workspace.binding(manifest, entry, adapter),
            "output_root": activity.get("output_root"),
        },
        "revision": revision,
        "operational_role": (rules or {}).get("assign", {}).get(name, {}),
        "session_generation": (record or {}).get("session") or None,
        "availability": _availability(
            record,
            _native_observation(directory, name, observed, now),
            now,
            configured["inactive_after"],
        ),
        "configured": True,
        "executable_detected": bool(
            provider.get("command") and shutil.which(provider["command"])
        ),
        "paused": entry.get("paused", False),
        "retired": bool(entry.get("retired", False)),
        "tasks": {
            "current": current,
            "history": [
                item for item in retained if item["participant"] == name
            ][-MAX_HISTORY:],
            "messages": mail_evidence.messages(
                home, manifest["root"], entry["display"], now=now
            ),
        },
        "audit": entry.get("config_history", [])[-MAX_AUDIT:],
        "skills": _skills(manifest, entry, declaration),
        "launch_receipt": receipt,
        "configuration_current": current_receipt,
        "receipt_error": receipt_error,
        **_assets(
            adapter,
            observed,
            artifacts,
            declaration,
            record,
            activity,
            now,
            configured["inactive_after"],
        ),
    }


def inventory(home: Path) -> dict:
    """Reads every registered participant using the existing state catalogs.

    Args:
        home: Coordination state root, never a native client's session home.

    Returns:
        Stable schema shared by CLI and console. Activity freshness is bounded,
        an expired ACK is never success, and file availability never implies
        native activation. Individual unreadable projects produce warnings.
    """
    home = home.expanduser().resolve()
    now = time.time()
    projects = []
    warnings = []
    providers = roster.providers(home)
    for path in sorted((home / "projects").glob("*/project.json")):
        try:
            document = _read(path)
            manifest = roster.normalize(json.loads(json.dumps(document)))
            for name in manifest["participants"]:
                roster.identifier(name, "participant")
            rules = roles.load(path.parent)
            revision = _revision(document, rules)
            state: dict = {}
            if (home / store.DATABASE).exists():
                database = (home / store.DATABASE).resolve().as_uri()
                with contextlib.closing(
                    sqlite3.connect(
                        f"{database}?mode=ro", uri=True, timeout=0.5
                    )
                ) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    state = lanes.read_all(db, manifest["root"])
            ledger = issues.snapshot(path.parent)
            retained = history.records(home, path.parent, manifest)
            projects.append(
                {
                    "id": manifest["root"],
                    "root": manifest["root"],
                    "revision": revision,
                    "roles": [
                        {
                            "id": name,
                            "description": entry.get("description", ""),
                        }
                        for name, entry in sorted(
                            (rules or {}).get("roles", {}).items()
                        )
                    ],
                    "agents": [
                        _agent(
                            home,
                            path.parent,
                            manifest,
                            name,
                            providers,
                            rules,
                            state.get(name),
                            revision,
                            now,
                            ledger,
                            retained,
                        )
                        for name in sorted(manifest["participants"])
                    ],
                }
            )
        except (
            BridgeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as error:
            warnings.append({"project": str(path.parent), "error": str(error)})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": views.timestamp(now),
        "read_only": True,
        "projects": projects,
        "warnings": warnings,
        "evidence_limit": "coordination records and shipped files; native "
        "sessions, credentials and runtime loading not read",
    }
