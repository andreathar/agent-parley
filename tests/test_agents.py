"""Checks identity-safe settings and honest coordination inventory evidence."""

import json

import pytest

from agent_parley import adapter_evidence, agents, lanes, roles, roster, store
from agent_parley.state import BridgeError, write_json


@pytest.fixture
def project(tmp_path):
    home = tmp_path / "home"
    directory = home / "projects/example"
    directory.mkdir(parents=True)
    root = tmp_path / "base"
    manifest = {
        "version": roster.MANIFEST_VERSION,
        "root": str(root),
        "base": "a" * 40,
        "unknown_project_setting": {"keep": True},
        "participants": {
            "first": {
                "provider": "opencode",
                "display": "RouteFirst",
                "lane": str(tmp_path / "first"),
                "branch": "parley/first",
                "credential": None,
                "unknown_lane_setting": 42,
            },
            "second": {
                "provider": "codex",
                "display": "RouteSecond",
                "lane": str(tmp_path / "second"),
                "branch": "parley/second",
                "credential": None,
            },
        },
    }
    write_json(directory / "project.json", manifest)
    spec = {
        "version": 1,
        "roles": {
            "review": {"write": False, "description": "Read results"},
            "implement": {"write": True},
        },
        "assign": {"first": {"role": "review"}},
    }
    roles.save(directory, spec)
    return home, directory, root


def read(project):
    return json.loads((project[1] / "project.json").read_text())


def row(project, name="first"):
    report = agents.inventory(project[0])
    assert report["warnings"] == []
    return next(
        entry
        for entry in report["projects"][0]["agents"]
        if entry["id"] == name
    )


def test_preview_writes_nothing_and_lists_only_existing_roles(project):
    home, directory, root = project
    before = {
        str(path): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    result = agents.configure(home, root, "first", "Andre", role="implement")
    assert result["changed"] is True and result["preview"] is True
    assert result["operational_role_changed"] is False
    assert result["before"] == {"display_name": "RouteFirst", "role": ""}
    assert result["after"] == {"display_name": "Andre", "role": "implement"}
    assert {
        str(path): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    } == before
    assert not (directory / "setup.lock").exists()
    catalog = agents.inventory(home)["projects"][0]["roles"]
    assert {entry["id"] for entry in catalog} == {"implement", "review"}


def test_rename_preserves_route_id_session_history_and_role_grants(
    project, monkeypatch
):
    home, directory, root = project
    store.initialize(home)
    token = store.register(home, str(root), "RouteFirst")["registration_token"]
    actor = store.authenticate(home, token)
    store.register(home, str(root), "RouteSecond")
    store.call(
        home,
        actor,
        "send_message",
        {
            "to": ["RouteSecond"],
            "subject": "coordination metadata",
            "body_md": "retained private body",
            "idempotency_key": "test-send",
        },
    )
    with store.connect(home, write=True) as db:
        lanes.ensure(db)
        lanes.transition(
            db,
            str(root),
            "first",
            "starting",
            evidence="launcher",
            session="generation-1",
            now=100,
        )
        lanes.transition(
            db,
            str(root),
            "first",
            "working",
            evidence="PreToolUse",
            now=100,
        )
    monkeypatch.setattr(agents.time, "time", lambda: 120)
    before = row(project)
    spec = roles.location(directory).read_bytes()
    proposal = agents.configure(home, root, "first", "Andre", role="implement")
    applied = agents.configure(
        home,
        root,
        "first",
        "Andre",
        role="implement",
        preview=False,
        expected_revision=proposal["revision"],
    )
    after = row(project)
    assert after["id"] == before["id"] == "first"
    assert after["routing_name"] == before["routing_name"] == "RouteFirst"
    assert after["session_generation"] == before["session_generation"]
    assert after["tasks"] == before["tasks"]
    assert after["display_name"] == "Andre" and after["role"] == "implement"
    assert after["operational_role"] == {"role": "review"}
    assert roles.location(directory).read_bytes() == spec
    assert read(project)["unknown_project_setting"] == {"keep": True}
    assert read(project)["participants"]["first"]["unknown_lane_setting"] == 42
    assert len(after["audit"]) == 1
    assert applied["audit_id"] == after["audit"][0]["id"]
    assert "retained private body" not in json.dumps(after)


def test_repeated_apply_is_idempotent_and_isolates_the_other_agent(project):
    home, directory, root = project
    second = row(project, "second")
    proposal = agents.configure(home, root, "first", "Andre")
    result = agents.configure(
        home,
        root,
        "first",
        "Andre",
        preview=False,
        expected_revision=proposal["revision"],
    )
    again = agents.configure(
        home,
        root,
        "first",
        "Andre",
        preview=False,
        expected_revision=proposal["revision"],
    )
    assert result["changed"] is True and again["changed"] is False
    assert len(row(project)["audit"]) == 1
    after = row(project, "second")
    after.pop("revision")
    second.pop("revision")
    assert after == second


def test_stale_preview_preserves_concurrent_manifest_changes(project):
    home, directory, root = project
    proposal = agents.configure(home, root, "first", "Andre")
    document = read(project)
    document["participants"]["second"]["paused"] = True
    write_json(directory / "project.json", document)
    with pytest.raises(BridgeError, match="Configuration changed"):
        agents.configure(
            home,
            root,
            "first",
            "Andre",
            preview=False,
            expected_revision=proposal["revision"],
        )
    assert read(project) == document


def test_changed_role_catalog_invalidates_preview(project):
    home, directory, root = project
    proposal = agents.configure(home, root, "first", "Andre", role="review")
    spec = roles.load(directory)
    spec["roles"]["research"] = {"write": False}
    roles.save(directory, spec)
    with pytest.raises(BridgeError, match="Configuration changed"):
        agents.configure(
            home,
            root,
            "first",
            "Andre",
            role="review",
            preview=False,
            expected_revision=proposal["revision"],
        )


@pytest.mark.parametrize("name", ["", "\nAndre", "A" * 81, "\x1b[31m", None])
def test_invalid_names_are_rejected_without_changes(project, name):
    home, directory, root = project
    original = (directory / "project.json").read_bytes()
    with pytest.raises(BridgeError, match="Display name"):
        agents.configure(home, root, "first", name)
    assert (directory / "project.json").read_bytes() == original


def test_unknown_roles_and_ids_are_rejected_without_changes(project):
    home, directory, root = project
    original = (directory / "project.json").read_bytes()
    with pytest.raises(BridgeError, match="Unknown role"):
        agents.configure(home, root, "first", "Andre", role="invented")
    with pytest.raises(BridgeError, match="Unknown participant"):
        agents.configure(home, root, "RouteFirst", "Andre")
    assert (directory / "project.json").read_bytes() == original


def test_apply_requires_revision_and_operator_boundary(project, monkeypatch):
    home, _, root = project
    with pytest.raises(BridgeError, match="requires a revision"):
        agents.configure(home, root, "first", "Andre", preview=False)
    monkeypatch.setenv(roster.LANE_TOKEN, "synthetic-lane-token")
    with pytest.raises(BridgeError, match="operator authority"):
        agents.configure(
            home,
            root,
            "first",
            "Andre",
            preview=False,
            expected_revision="not-a-revision",
        )


def test_stale_activity_and_new_session_never_claim_transport_connection(
    project, monkeypatch
):
    home, directory, root = project
    store.initialize(home)
    with store.connect(home, write=True) as db:
        lanes.ensure(db)
        lanes.transition(
            db,
            str(root),
            "first",
            "starting",
            evidence="launcher",
            session="first-session",
            now=100,
        )
        lanes.transition(
            db,
            str(root),
            "first",
            "idle",
            evidence="Stop",
            now=100,
        )
    monkeypatch.setattr(agents.time, "time", lambda: 500)
    write_json(
        directory / "first-activity.json",
        {
            "event": "Stop",
            "updated": 100,
            "session_id": "first-session",
        },
    )
    (directory / "first-events.jsonl").write_text('{"event":"Stop","ts":100}\n')
    with store.connect(home, write=True) as db:
        lanes.sample(
            db,
            str(root),
            "first",
            {
                "process_alive": True,
                "activity": "idle",
                "evidence": "Stop",
            },
            dead_after=1000,
            now=500,
        )
    stale = row(project)
    assert stale["availability"]["state"] == "stale"
    assert stale["availability"]["recorded_state"] == "idle"
    assert stale["availability"]["transport_connected"] is None
    assert stale["availability"]["recorded_updated_at"] == 500
    assert stale["availability"]["observed_at"] == 100
    assert stale["availability"]["freshness_source"] == "native hook event"
    with store.connect(home, write=True) as db:
        lanes.transition(
            db,
            str(root),
            "first",
            "idle",
            evidence="new session",
            session="next-session",
            now=500,
        )
    reconnected = row(project)
    assert reconnected["availability"]["state"] == "stale"
    assert reconnected["session_generation"] == "next-session"
    assert reconnected["availability"]["transport_connected"] is None
    with (directory / "first-events.jsonl").open("a") as stream:
        stream.write('{"event":"PreToolUse","ts":500}\n')
    fresh = row(project)
    assert fresh["availability"]["freshness"] == "fresh"
    assert fresh["availability"]["observed_at"] == 500
    assert fresh["availability"]["transport_connected"] is None


def test_hook_files_and_packaged_skill_never_mean_native_activation(project):
    _, directory, _ = project
    (directory / "first-events.jsonl").write_text(
        '{"event":"PreToolUse","ts":100}\ninvalid json\n'
    )
    entry = row(project)
    assert entry["skills"][0]["available"] is True
    assert entry["skills"][0]["verified"] is None
    assert all(
        hook["configured"] is None and hook["verified"] is None
        for hook in entry["hooks"]
    )
    assert (
        next(hook for hook in entry["hooks"] if hook["id"] == "PreToolUse")[
            "detected"
        ]
        is True
    )
    assert (
        next(hook for hook in entry["hooks"] if hook["id"] == "SessionEnd")[
            "supported"
        ]
        is False
    )


def test_liveness_heartbeat_without_native_evidence_is_unknown(
    project, monkeypatch
):
    home, directory, root = project
    store.initialize(home)
    with store.connect(home, write=True) as db:
        lanes.ensure(db)
        lanes.transition(
            db,
            str(root),
            "first",
            "starting",
            evidence="launcher",
            session="attempted-session",
            now=500,
        )
    write_json(
        directory / "first-activity.json",
        {
            "event": "Stop",
            "updated": 500,
            "session_id": "",
            "activity": "starting",
        },
    )
    monkeypatch.setattr(agents.time, "time", lambda: 500)
    report = row(project)["availability"]
    assert report["state"] == "unknown"
    assert report["recorded_state"] == "starting"
    assert report["observed_at"] is None
    assert report["transport_connected"] is None


def test_native_activity_fallback_ages_without_a_retained_hook_log(
    project, monkeypatch
):
    _, directory, _ = project
    activity = directory / "first-activity.json"
    write_json(
        activity,
        {
            "event": "Stop",
            "updated": 100,
            "session_id": "native-session",
            "activity": "idle",
        },
    )
    monkeypatch.setattr(agents.time, "time", lambda: 500)
    stale = row(project)["availability"]
    assert stale["freshness"] == "stale"
    assert stale["freshness_source"] == "native activity record"
    write_json(
        activity,
        {
            "event": "PreToolUse",
            "updated": 500,
            "session_id": "native-session",
            "activity": "working",
        },
    )
    fresh = row(project)["availability"]
    assert fresh["freshness"] == "fresh"
    assert fresh["observed_at"] == 500
    assert fresh["transport_connected"] is None


def test_foreign_session_events_never_refresh_native_activity(
    project, monkeypatch
):
    _, directory, _ = project
    (directory / "first-events.jsonl").write_text(
        '{"event":"Stop","ts":100}\n'
        '{"event":"PreToolUse","ts":500,"reason_class":"session_mismatch"}'
        '\n{"event":"PreToolUse","ts":500,"reason_class":"superseded"}\n'
    )
    write_json(
        directory / "first-activity.json",
        {
            "event": "Stop",
            "updated": 500,
            "activity": "stopped",
            "session_id": "prior-native-session",
        },
    )
    monkeypatch.setattr(agents.time, "time", lambda: 500)
    report = row(project)["availability"]
    assert report["freshness"] == "stale"
    assert report["observed_at"] == 100
    assert report["freshness_source"] == "native hook event"


def test_missing_state_home_inventory_is_read_only(tmp_path):
    home = tmp_path / "missing"
    result = agents.inventory(home)
    assert result["projects"] == [] and result["warnings"] == []
    assert result["generated_at"].endswith("Z")
    assert not home.exists()


def test_scoped_skill_and_launch_artifact_detection_never_reads_secrets(
    project, monkeypatch
):
    home, directory, root = project
    native = directory / "first-opencode"
    (native / "plugin").mkdir(parents=True)
    config = native / "opencode.json"
    config.write_text('{"token":"PRIVATE_CREDENTIAL_DO_NOT_READ"}')
    (native / "plugin/agent-parley.js").write_text("private plugin contents")
    skill = root / ".agents/skills/research/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("PRIVATE_SKILL_DO_NOT_READ")
    original = type(config).read_text

    def protected_read(path, *args, **kwargs):
        if path in (config, skill, native / "plugin/agent-parley.js"):
            pytest.fail("Inventory must inspect filenames, not secret content")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(config), "read_text", protected_read)
    entry = row(project)
    scoped = next(
        item for item in entry["skills"] if item["name"] == "research"
    )
    assert scoped["detected"] is True and scoped["source"] == "project"
    assert scoped["configured"] is None and scoped["verified"] is None
    assert entry["plugin"]["detected"] is True
    assert entry["plugin"]["configured"] is None
    assert entry["plugin"]["verified"] is None
    assert all(
        item["detected"]
        for item in entry["artifacts"]
        if item.get("kind")
        not in {"native_context_journal", "sanitized_launch_declaration"}
    )
    assert "PRIVATE" not in json.dumps(agents.inventory(home))


def test_mailbox_expiration_survives_zero_pending_ack_and_rename(
    project, monkeypatch
):
    home, _, root = project
    store.initialize(home)
    token = store.register(home, str(root), "RouteSecond")["registration_token"]
    sender = store.authenticate(home, token)
    store.register(home, str(root), "RouteFirst")
    sent = store.call(
        home,
        sender,
        "send_message",
        {
            "to": ["RouteFirst"],
            "subject": "do not reveal subject",
            "body_md": "do not reveal body",
            "ack_required": True,
            "idempotency_key": "bounded-mail",
        },
    )
    with store.connect(home, write=True) as db:
        db.execute(
            "UPDATE messages SET ack_deadline_ts='2000-01-01 00:00:00',"
            "ack_retired_ts='2000-01-01 00:01:00' WHERE id=?",
            (sent["id"],),
        )
    monkeypatch.setattr(agents.time, "time", lambda: 1000000000)
    before = row(project)["tasks"]["messages"]
    assert before[0]["status"] == "expired"
    assert before[0]["task_state"] == "unknown"
    assert before[0]["dispatch_state"] == "unknown"
    assert "do not reveal" not in json.dumps(before)
    preview = agents.configure(home, root, "first", "New displayed name")
    agents.configure(
        home,
        root,
        "first",
        "New displayed name",
        preview=False,
        expected_revision=preview["revision"],
    )
    assert row(project)["tasks"]["messages"] == before


def observed_launch(project, monkeypatch, *, now=120, receipt_skills=None):
    home, directory, root = project
    store.initialize(home)
    with store.connect(home, write=True) as db:
        lanes.ensure(db)
        lanes.transition(
            db,
            str(root),
            "first",
            "starting",
            session="native-1",
            evidence="launcher",
            now=90,
        )
        lanes.transition(
            db, str(root), "first", "idle", evidence="Stop", now=100
        )
    receipt = adapter_evidence.record_launch(
        directory,
        "first",
        "opencode",
        configured_hooks=["SessionStart", "Stop"],
        configured_skills=receipt_skills or [],
        now=90,
    )
    activity = {
        "event": "Stop",
        "updated": 100,
        "session_id": "native-1",
        "session_started": 95,
        "launch_generation": receipt["generation"],
        "activity": "idle",
    }
    write_json(directory / "first-activity.json", activity)
    (directory / "first-events.jsonl").write_text(
        '{"event":"SessionStart","ts":96}\n{"event":"Stop","ts":100}\n'
    )
    monkeypatch.setattr(agents.time, "time", lambda: now)
    return receipt, activity


def test_current_receipt_and_native_observation_have_separate_provenance(
    project, monkeypatch
):
    receipt, _ = observed_launch(project, monkeypatch)
    entry = row(project)
    assert entry["configuration_current"] is True
    assert entry["launch_receipt"] == receipt
    hook = next(item for item in entry["hooks"] if item["id"] == "Stop")
    assert hook["configured"] is True
    assert hook["configured_at"] == 90
    assert hook["verified"] is True
    assert hook["verification_state"] == "observed_recently"
    assert hook["verification_session"] == "native-1"
    assert hook["active"] is True and hook["observed_active_at"] == 100
    assert entry["skills"][0]["configured"] is None
    assert entry["skills"][0]["active"] is None
    assert entry["plugin"]["configured"] is True
    assert entry["plugin"]["verified"] is None
    assert entry["plugin"]["active"] is None


def test_old_native_observation_is_historical_without_active_claim(
    project, monkeypatch
):
    observed_launch(project, monkeypatch, now=500)
    entry = row(project)
    hook = next(item for item in entry["hooks"] if item["id"] == "Stop")
    assert hook["configured"] is True
    assert hook["verified"] is True
    assert hook["verification_state"] == "observed_stale"
    assert hook["active"] is None
    assert entry["availability"]["freshness"] == "stale"


def test_receipt_from_another_launch_cannot_configure_the_current_session(
    project, monkeypatch
):
    _, activity = observed_launch(project, monkeypatch)
    activity["launch_generation"] = "next-launch"
    activity["session_id"] = "other-native-session"
    write_json(project[1] / "first-activity.json", activity)
    entry = row(project)
    assert entry["configuration_current"] is False
    assert all(item["configured"] is None for item in entry["hooks"])
    assert all(item["verified"] is None for item in entry["hooks"])
    assert all(
        item["active"] is None for item in entry["hooks"] if item["supported"]
    )


def test_explicit_skill_declaration_never_proves_skill_loading(
    project, monkeypatch
):
    observed_launch(project, monkeypatch, receipt_skills=["coordinate"])
    skill = row(project)["skills"][0]
    assert skill["configured"] is True
    assert skill["verified"] is None and skill["active"] is None
    assert skill["configured_at"] == 90


def test_invalid_receipt_does_not_hide_the_agent_or_expose_content(project):
    adapter_evidence.location(project[1], "first").write_text(
        '{"secret":"DO_NOT_EXPOSE","version":900}'
    )
    entry = row(project)
    assert entry["launch_receipt"] is None
    assert entry["receipt_error"]
    assert "DO_NOT_EXPOSE" not in json.dumps(entry)


def test_platform_guard_gaps_come_from_existing_adapter_catalog(project):
    document = read(project)
    document["participants"]["first"]["provider"] = "amp"
    write_json(project[1] / "project.json", document)
    capability = row(project)["capabilities"]
    assert capability["launch_supported"] is False
    assert capability["required_hook_gaps"] == ["SessionStart", "Stop"]


def test_journal_inventory_omits_context_and_never_converts_unknown_to_success(
    project,
):
    path = project[1] / "first-opencode-context.json"
    write_json(
        path,
        {
            "schema": 1,
            "revision": 1,
            "entries": [
                {
                    "id": "context-1",
                    "session_id": "native-1",
                    "origin_generation": "config-1",
                    "created_at": 100,
                    "updated_at": 100,
                    "state": "held",
                    "context": "DO_NOT_EXPOSE_CONTEXT",
                    "reason": "configuration generation changed; no migration",
                }
            ],
        },
    )
    artifact = next(
        item
        for item in row(project)["artifacts"]
        if item.get("kind") == "native_context_journal"
    )
    assert artifact["summary"]["counts"]["held"] == 1
    assert "DO_NOT_EXPOSE" not in json.dumps(artifact)
    assert "reason" not in artifact["summary"]["entries"][0]
    path.write_text("malformed private state")
    unknown = next(
        item
        for item in row(project)["artifacts"]
        if item.get("kind") == "native_context_journal"
    )
    assert unknown["state"] == "unknown" and unknown["error"]
    assert "summary" not in unknown
