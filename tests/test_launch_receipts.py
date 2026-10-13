"""Checks adapter launch declarations without executing native agents."""

import json

import pytest

from agent_parley import adapter_evidence, launch, roster


@pytest.mark.parametrize("adapter", roster.ADAPTERS)
def test_receipt_uses_real_adapter_hooks_without_commands(tmp_path, adapter):
    hooks = {
        event: [{"hooks": [{"command": "not-a-command secret-token"}]}]
        for event in roster.HOOK_EVENTS
    }
    receipt = launch.launch_receipt(
        tmp_path, "lane", adapter, hooks, "native-resume"
    )
    actual = adapter_evidence.read(tmp_path, "lane", adapter)
    assert actual == receipt
    assert receipt["session_generation"] == "native-resume"
    assert receipt["configured_skills"] == []
    assert not (
        set(receipt["configured_hooks"])
        & set(roster.unavailable_hooks(adapter))
    )
    assert "secret-token" not in json.dumps(receipt)
    assert receipt["evidence_kind"] == "prepared_launch_declaration"


def test_new_launch_has_new_generation_without_native_session(tmp_path):
    first = launch.launch_receipt(tmp_path, "lane", "codex", {}, None)
    second = launch.launch_receipt(tmp_path, "lane", "codex", {}, None)
    assert first["generation"] != second["generation"]
    assert second["session_generation"] is None
    assert second["configured_hooks"] == []
