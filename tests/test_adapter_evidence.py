"""Tests credential-free launch declarations and their evidence boundaries."""

import json

import pytest

from agent_parley import adapter_evidence, protocol
from agent_parley.state import BridgeError


def test_receipt_digest_is_stable_but_launch_generations_are_distinct(tmp_path):
    first = adapter_evidence.record_launch(
        tmp_path,
        "reviewer",
        "opencode",
        configured_hooks=["Stop", "PreToolUse"],
        configured_skills=["coordinate"],
        now=100,
    )
    second = adapter_evidence.record_launch(
        tmp_path,
        "reviewer",
        "opencode",
        configured_hooks=["PreToolUse", "Stop", "Stop"],
        configured_skills=["coordinate", "coordinate"],
        now=200,
    )
    assert first["configuration_digest"] == second["configuration_digest"]
    assert first["generation"] != second["generation"]
    assert second["protocol"] == protocol.PROTOCOL
    assert second["evidence_kind"] == "prepared_launch_declaration"
    assert adapter_evidence.read(tmp_path, "reviewer", "opencode") == second
    assert set(second) == {
        "version",
        "participant",
        "adapter",
        "generation",
        "configured_at",
        "session_generation",
        "configuration_digest",
        "evidence_kind",
        "configured_hooks",
        "configured_skills",
        "protocol",
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"adapter": "invented"},
        {"adapter": "amp", "configured_hooks": ["Stop"]},
        {"configured_hooks": ["invented"]},
        {"configured_skills": ["coordinate\nsecret"]},
        {"now": float("nan")},
        {"now": True},
        {"session_generation": "\x1b"},
        {"participant": "../../native-session"},
    ],
)
def test_invalid_receipts_write_nothing(tmp_path, fields):
    values = {
        "participant": "reviewer",
        "adapter": "opencode",
        "configured_hooks": ["Stop"],
        "configured_skills": [],
        "now": 100,
    } | fields
    with pytest.raises(BridgeError):
        adapter_evidence.record_launch(tmp_path, **values)
    assert list(tmp_path.iterdir()) == []


def test_reader_ignores_unknown_fields_and_rejects_changed_declarations(
    tmp_path,
):
    receipt = adapter_evidence.record_launch(
        tmp_path,
        "reviewer",
        "opencode",
        configured_hooks=["Stop"],
        configured_skills=[],
        now=100,
    )
    path = adapter_evidence.location(tmp_path, "reviewer")
    path.write_text(json.dumps(receipt | {"unknown_secret": "DO_NOT_EXPOSE"}))
    assert "DO_NOT_EXPOSE" not in json.dumps(
        adapter_evidence.read(tmp_path, "reviewer", "opencode")
    )
    path.write_text(json.dumps(receipt | {"configured_hooks": ["PreToolUse"]}))
    with pytest.raises(BridgeError, match="sanitized launch receipt"):
        adapter_evidence.read(tmp_path, "reviewer", "opencode")


def test_receipt_api_accepts_no_commands_or_native_settings(tmp_path):
    with pytest.raises(TypeError):
        adapter_evidence.record_launch(
            tmp_path,
            "reviewer",
            "opencode",
            configured_hooks=["Stop"],
            configured_skills=[],
            native_settings={"token": "NEVER_ACCEPT"},
        )
    assert list(tmp_path.iterdir()) == []


def test_missing_receipt_is_a_read_only_unknown(tmp_path):
    directory = tmp_path / "not-created"
    assert adapter_evidence.read(directory, "reviewer", "claude") is None
    assert not directory.exists()
