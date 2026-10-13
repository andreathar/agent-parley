"""Checks durable holding and uncertainty across native commit interruptions."""

import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_parley import opencode, opencode_context
from agent_parley.state import BridgeError, write_json


def execute(path, operation, now=100, generation="g1", instance="i1", **fields):
    return opencode_context.execute(
        path, generation, instance, {"operation": operation, **fields}, now=now
    )


def hold(path, session="ses_one", **fields):
    return execute(
        path, "hold", session_id=session, context="Parley only", **fields
    )


def attempt(path, session="ses_one", **fields):
    return execute(
        path,
        "attempt",
        session_id=session,
        channel="chat",
        message_id="msg_one",
        native_part_id="prt_one",
        **fields,
    )


def receipt(path, session="ses_one", **fields):
    return execute(
        path,
        "receipt",
        session_id=session,
        message_id="msg_one",
        native_part_id="prt_one",
        **fields,
    )


def test_held_survives_reload_in_same_session_and_generation(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    execute(path, "recover", now=101, instance="i2")
    assert attempt(path, instance="i2", now=102)["context"] == "Parley only"
    entry = opencode_context.summary(path)["entries"][0]
    assert entry["state"] == "attempted"
    assert entry["updated_at"] == 102
    assert entry["created_at"] == 100


def test_attempt_survives_restart_as_uncertain_without_replay(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    attempt(path)
    execute(path, "recover", instance="i2", now=101)
    assert attempt(path, instance="i2", now=102)["context"] == ""
    assert opencode_context.summary(path)["counts"]["uncertain"] == 1
    assert "Parley only" in path.read_text()


def test_new_generation_never_migrates_held_context(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    execute(path, "recover", generation="g2", instance="i2", now=101)
    assert attempt(path, generation="g2", instance="i2")["context"] == ""
    summary = opencode_context.summary(path)
    assert summary["counts"]["uncertain"] == 1
    assert summary["entries"][0]["origin_generation"] == "g1"


def test_session_isolation_and_close_preserve_without_replay(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    assert attempt(path, session="ses_other")["context"] == ""
    execute(path, "close", session_id="ses_one", now=101)
    assert attempt(path)["context"] == ""
    assert "Parley only" in path.read_text()
    assert opencode_context.summary(path)["counts"]["uncertain"] == 1


def test_exact_receipt_is_idempotent_when_late_or_out_of_order(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    assert receipt(path)["confirmed"] is False
    attempt(path)
    execute(path, "recover", instance="i2", now=101)
    assert receipt(path, session="ses_other", now=102)["confirmed"] is False
    assert receipt(path, instance="i2", now=103)["confirmed"] is True
    summary = opencode_context.summary(path)
    assert summary["counts"]["part_update_observed"] == 1
    assert summary["entries"][0]["updated_at"] == 103
    revision = summary["revision"]
    assert receipt(path, instance="i2", now=104)["confirmed"] is False
    assert opencode_context.summary(path)["revision"] == revision
    assert "ACK/completion" in summary["evidence_limit"]


def test_tool_output_uncertain_and_never_replayed(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    result = execute(path, "attempt", session_id="ses_one", channel="tool")
    assert result["context"] == "Parley only"
    execute(path, "recover", instance="i2")
    assert attempt(path, instance="i2")["context"] == ""
    assert receipt(path, instance="i2")["confirmed"] is False
    assert opencode_context.summary(path)["counts"]["uncertain"] == 1


@pytest.mark.parametrize(
    "content",
    [
        "invalid",
        "[]",
        '{"schema":999}',
        '{"schema":1,"revision":0,"entries":[{}]}',
    ],
)
def test_malformed_journal_preserved_and_fails_closed(tmp_path, content):
    path = tmp_path / "context.json"
    path.write_text(content)
    with pytest.raises(BridgeError, match="No automatic repair/replay"):
        hold(path)
    assert path.read_text() == content
    with pytest.raises(BridgeError):
        opencode_context.summary(path)


def test_disk_failure_prevents_authorizing_output(tmp_path, monkeypatch):
    path = tmp_path / "context.json"
    hold(path)
    before = path.read_bytes()

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr(opencode_context, "write_json", fail)
    with pytest.raises(BridgeError, match="Cannot persist"):
        attempt(path)
    assert path.read_bytes() == before
    assert opencode_context.summary(path)["counts"]["held"] == 1


def test_capacity_limit_never_discards_pending_context(tmp_path, monkeypatch):
    path = tmp_path / "context.json"
    hold(path)
    before = path.read_bytes()
    monkeypatch.setattr(opencode_context, "MAX_ENTRIES", 1)
    with pytest.raises(BridgeError, match="journal is full"):
        hold(path)
    assert path.read_bytes() == before
    monkeypatch.setattr(opencode_context, "MAX_ENTRIES", 512)
    monkeypatch.setattr(opencode_context, "MAX_BYTES", len(before) + 1)
    with pytest.raises(BridgeError, match="capacity exceeded"):
        execute(path, "hold", session_id="ses_one", context="More context")
    assert path.read_bytes() == before


def test_summary_excludes_context_bodies(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    summary = opencode_context.summary(path)
    assert "Parley only" not in json.dumps(summary)
    assert "context" not in summary["entries"][0]


def test_cross_instance_lock_allows_only_one_attachment(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda instance: attempt(path, instance=instance), ["i1", "i2"]
            )
        )
    assert sorted(result["context"] for result in results) == [
        "",
        "Parley only",
    ]


def test_invalid_native_ids_leave_held_untouched(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    with pytest.raises(BridgeError, match="native message/part IDs"):
        execute(
            path,
            "attempt",
            session_id="ses_one",
            channel="chat",
            message_id="invalid",
            native_part_id="invalid",
        )
    assert opencode_context.summary(path)["counts"]["held"] == 1


def test_invalid_entry_state_is_not_repaired(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    data = json.loads(path.read_text())
    data["entries"][0]["state"] = "completed"
    write_json(path, data)
    before = path.read_bytes()
    with pytest.raises(BridgeError):
        execute(path, "recover", instance="i2")
    assert path.read_bytes() == before


def test_untrusted_metadata_is_rejected_without_echoing_body(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    data = json.loads(path.read_text())
    data["entries"][0]["reason"] = "secret native tool content"
    write_json(path, data)
    with pytest.raises(BridgeError) as error:
        opencode_context.summary(path)
    assert "secret native tool content" not in str(error.value)


def test_uncertain_chat_without_correlation_fails_closed(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    data = json.loads(path.read_text())
    data["entries"][0].update(
        state="uncertain", channel="chat", attempt_instance="i1"
    )
    write_json(path, data)
    before = path.read_bytes()
    with pytest.raises(BridgeError):
        execute(path, "recover", instance="i2")
    assert path.read_bytes() == before


def test_native_prompt_and_tool_response_are_never_persisted(tmp_path):
    path = tmp_path / "context.json"
    execute(
        path,
        "hold",
        session_id="ses_one",
        context="Parley only",
        user_prompt="private prompt",
        tool_response="private tool",
    )
    assert "private prompt" not in path.read_text()
    assert "private tool" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_overlay_rebuild_preserves_journal_and_changes_generation(tmp_path):
    account = tmp_path / "account"
    account.mkdir()
    hooks = {
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "command": shlex.join(
                            [
                                sys.executable,
                                "--version",
                            ]
                        )
                    }
                ]
            }
        ]
    }
    journal = tmp_path / "lane-opencode-context.json"
    hold(journal)
    before = journal.read_bytes()
    commands = []
    for _ in range(2):
        overlay = opencode.configure(
            tmp_path, "lane", "http://127.0.0.1:1/mcp/", hooks, str(account)
        )
        plugin = (overlay / "plugin" / "agent-parley.js").read_text()
        commands.append(
            json.loads(
                plugin.split("const JOURNAL_COMMAND = ", 1)[1].split(";\n", 1)[
                    0
                ]
            )
        )
    assert journal.read_bytes() == before
    assert commands[0][3] == commands[1][3] == str(journal.resolve())
    assert commands[0][4] != commands[1][4]
    assert commands[0][:3] == [
        sys.executable,
        "-m",
        "agent_parley.opencode_context",
    ]


def test_cross_process_helper_lock_allows_only_one_attachment(tmp_path):
    path = tmp_path / "context.json"
    hold(path)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_parley.opencode_context",
                str(path),
                "g1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]

    def communicate(index):
        process = processes[index]
        stdout, stderr = process.communicate(
            json.dumps(
                {
                    "operation": "attempt",
                    "instance": f"i{index}",
                    "session_id": "ses_one",
                    "channel": "chat",
                    "message_id": "msg_one",
                    "native_part_id": "prt_one",
                }
            ),
            timeout=5,
        )
        assert process.returncode == 0, stderr
        return json.loads(stdout)["context"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(communicate, range(2)))
    assert sorted(outputs) == ["", "Parley only"]
