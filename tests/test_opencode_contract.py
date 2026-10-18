"""Executes the generated plugin against native event and part contracts."""

import json
import shutil
import subprocess
import sys

import pytest

from agent_parley import opencode, opencode_context

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node"
)


def run_plugin(tmp_path, script, response=None, generation="generation-one"):
    counter = tmp_path / "events.json"
    reply = {"context": "Coordination"} if response is None else response
    command = (
        "import json,pathlib,sys; value=json.load(sys.stdin); "
        f"p=pathlib.Path({str(counter)!r}); "
        "events=json.loads(p.read_text()) if p.exists() else []; "
        "events.append(value); p.write_text(json.dumps(events)); "
        f"print(json.dumps({reply!r}))"
    )
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(
        opencode.PLUGIN.replace(
            "__COMMAND__", json.dumps([sys.executable, "-c", command])
        ).replace(
            "__JOURNAL_COMMAND__",
            json.dumps(
                [
                    sys.executable,
                    "-m",
                    "agent_parley.opencode_context",
                    str(tmp_path / "context.json"),
                    generation,
                ]
            ),
        )
    )
    setup = """
const { AgentParley } = await import(process.argv[1]);
const prompts = [];
const client = { session: { prompt: async (value) => {
  prompts.push(value);
  return { data: {} };
}}};
let hooks = await AgentParley({ client, directory: "/lane" });
const message = (sessionID, id) => ({
  message: { sessionID, id }, parts: [],
});
const event = async (type, sessionID, extra = {}) => hooks.event({
  event: { type, properties: { sessionID, info: { id: sessionID }, ...extra } },
});
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            setup + script,
            plugin.as_uri(),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    events = json.loads(counter.read_text()) if counter.exists() else []
    return json.loads(result.stdout), events


def test_chat_parts_reproduce_legacy_failure_and_fit_persistence(tmp_path):
    shown, _ = run_plugin(
        tmp_path,
        """
const missing = (part) => ["id", "sessionID", "messageID"].filter(
  (field) => typeof part[field] !== "string" || !part[field]
);
const legacy = missing({ type: "text", text: "Coordination" });
const output = message("ses_one", "msg_one");
await hooks["chat.message"]({ sessionID: "ses_one" }, output);
console.log(JSON.stringify({
  legacy, output, missing: missing(output.parts[0]),
}));
""",
    )
    assert shown["legacy"] == ["id", "sessionID", "messageID"]
    assert shown["missing"] == []
    part = shown["output"]["parts"][0]
    assert part["id"].startswith("prt_")
    assert part["sessionID"] == "ses_one"
    assert part["messageID"] == "msg_one"
    assert part["synthetic"] is True


def test_invalid_message_keeps_context_and_isolates_sessions(tmp_path):
    shown, events = run_plugin(
        tmp_path,
        """
await event("session.created", "ses_one");
const invalid = message("ses_other", "msg_wrong");
let error;
try { await hooks["chat.message"]({ sessionID: "ses_one" }, invalid); }
catch (failure) { error = failure.message; }
const other = message("ses_other", "msg_other");
await hooks["chat.message"]({ sessionID: "ses_other" }, other);
const output = message("ses_one", "msg_one");
await hooks["chat.message"]({ sessionID: "ses_one" }, output);
console.log(JSON.stringify({ error, invalid, other, output }));
""",
    )
    assert "context remains pending" in shown["error"]
    assert shown["invalid"]["parts"] == []
    assert shown["other"]["parts"][0]["text"] == "Coordination"
    assert shown["output"]["parts"][0]["text"].count("Coordination") == 3
    assert len(events) == 4


def test_idle_status_and_legacy_idle_do_not_double_continue(tmp_path):
    shown, events = run_plugin(
        tmp_path,
        """
await event("session.status", "ses_one", { status: { type: "idle" } });
await event("session.idle", "ses_one");
await event("session.status", "ses_one", { status: { type: "busy" } });
await event("session.idle", "ses_one");
console.log(JSON.stringify({ prompts }));
""",
        {"decision": "block", "reason": "Review inbox"},
    )
    assert len(shown["prompts"]) == 2
    assert len(events) == 2
    assert all(item["hook_event_name"] == "session.idle" for item in events)


def test_duplicate_start_and_deleted_session_do_not_leak_context(tmp_path):
    shown, events = run_plugin(
        tmp_path,
        """
await event("session.created", "ses_old");
await event("session.created", "ses_old");
await event("session.deleted", "ses_old");
await event("server.connected", "ses_old");
const output = message("ses_old", "msg_new");
await hooks["chat.message"]({ sessionID: "ses_old" }, output);
console.log(JSON.stringify({ output, prompts }));
""",
    )
    assert shown["output"]["parts"][0]["text"] == "Coordination"
    assert shown["prompts"] == []
    assert len(events) == 2


def test_continuation_error_is_actionable_without_automatic_retry(tmp_path):
    shown, events = run_plugin(
        tmp_path,
        """
client.session.prompt = async (value) => {
  prompts.push(value); return { error: { message: "Session unavailable" } };
};
let error;
try { await event("session.idle", "ses_one"); }
catch (failure) { error = failure.message; }
await event("session.idle", "ses_one");
console.log(JSON.stringify({ error, prompts }));
""",
        {"decision": "block", "reason": "Review inbox"},
    )
    assert "No automatic retry" in shown["error"]
    assert len(shown["prompts"]) == 1
    assert len(events) == 1


def test_plugin_process_reload_recovers_only_held_matching_session(tmp_path):
    run_plugin(
        tmp_path,
        """
await event("session.created", "ses_one");
console.log(JSON.stringify({ held: true }));
""",
    )
    shown, _ = run_plugin(
        tmp_path,
        """
const other = message("ses_other", "msg_other");
await hooks["chat.message"]({ sessionID: "ses_other" }, other);
const output = message("ses_one", "msg_one");
await hooks["chat.message"]({ sessionID: "ses_one" }, output);
console.log(JSON.stringify({ other, output }));
""",
        {},
    )
    assert shown["other"]["parts"] == []
    assert shown["output"]["parts"][0]["text"] == "Coordination"
    summary = opencode_context.summary(tmp_path / "context.json")
    assert summary["counts"]["attempted"] == 1


def test_journal_attempt_precedes_push_and_reload_does_not_replay(tmp_path):
    shown, _ = run_plugin(
        tmp_path,
        """
const output = message("ses_one", "msg_one");
output.parts.push = () => { throw new Error("native append interrupted"); };
let error;
try { await hooks["chat.message"]({ sessionID: "ses_one" }, output); }
catch (failure) { error = failure.message; }
console.log(JSON.stringify({ error }));
""",
    )
    assert shown["error"] == "native append interrupted"
    path = tmp_path / "context.json"
    entry = opencode_context.summary(path)["entries"][0]
    assert entry["state"] == "attempted"
    assert entry["native_part_id"].startswith("prt_")
    shown, _ = run_plugin(
        tmp_path,
        """
const output = message("ses_one", "msg_after_reload");
await hooks["chat.message"]({ sessionID: "ses_one" }, output);
console.log(JSON.stringify({ output }));
""",
        {},
    )
    assert shown["output"]["parts"] == []
    assert opencode_context.summary(path)["counts"]["uncertain"] == 1
    part = {
        "id": entry["native_part_id"],
        "sessionID": "ses_one",
        "messageID": entry["message_id"],
    }
    shown, _ = run_plugin(
        tmp_path,
        "const part = "
        + json.dumps(part)
        + ";\n"
        + """
await event("message.part.updated", "ses_one", { part });
await event("message.part.updated", "ses_one", { part });
const output = message("ses_one", "msg_after_receipt");
await hooks["chat.message"]({ sessionID: "ses_one" }, output);
console.log(JSON.stringify({ output }));
""",
        {},
    )
    assert shown["output"]["parts"] == []
    assert opencode_context.summary(path)["counts"]["part_update_observed"] == 1


def test_new_configuration_generation_preserves_without_migration(tmp_path):
    run_plugin(
        tmp_path,
        """
await event("session.created", "ses_one");
console.log(JSON.stringify({ held: true }));
""",
    )
    shown, _ = run_plugin(
        tmp_path,
        """
const output = message("ses_one", "msg_one");
await hooks["chat.message"]({ sessionID: "ses_one" }, output);
console.log(JSON.stringify({ output }));
""",
        {},
        generation="generation-two",
    )
    assert shown["output"]["parts"] == []
    assert (
        opencode_context.summary(tmp_path / "context.json")["counts"][
            "uncertain"
        ]
        == 1
    )


def test_malformed_journal_blocks_plugin_initialization(tmp_path):
    path = tmp_path / "context.json"
    path.write_text("invalid journal")
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_plugin(tmp_path, "console.log(JSON.stringify({}));", {})
    assert "No automatic repair/replay" in error.value.stderr
    assert path.read_text() == "invalid journal"


def test_tool_result_context_is_preserved_as_uncertain_after_reload(tmp_path):
    shown, _ = run_plugin(
        tmp_path,
        """
const output = { output: "private native tool result" };
await hooks["tool.execute.after"](
  { sessionID: "ses_one", tool: "bash" }, output
);
console.log(JSON.stringify({ output }));
""",
    )
    assert "Coordination" in shown["output"]["output"]
    path = tmp_path / "context.json"
    assert "private native tool result" not in path.read_text()
    shown, _ = run_plugin(
        tmp_path,
        """
const output = message("ses_one", "msg_one");
await hooks["chat.message"]({ sessionID: "ses_one" }, output);
console.log(JSON.stringify({ output }));
""",
        {},
    )
    assert shown["output"]["parts"] == []
    assert opencode_context.summary(path)["counts"]["uncertain"] == 1
