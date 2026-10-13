"""Adapts native OpenCode configuration and plugin events to shared hooks.

OpenCode reads ``opencode.json`` from its configuration directory and runs
JavaScript plugins from the ``plugin`` subdirectory there; it has no hook
command contract of its own. The lane therefore receives a private copy of
that directory, selected with ``OPENCODE_CONFIG_DIR`` for one launch only,
holding the user's configuration plus the lane's MCP server and one plugin
that runs the configured hook command for each supported native event.

The service marks coordination text delivered on every event, but only
``chat.message`` and ``tool.execute.after`` can carry text into the session.
The plugin therefore journals returned Parley context outside its replaceable
configuration overlay. Only held context in the same native session and
configuration generation can reach the next message or tool result. An attempt
is durable before native output changes, and interrupted attempts are retained
as uncertain rather than replayed. A correlated native part update is an
observation, not a task acknowledgement or a guarantee of storage commit.

The checkpoint and journal are separate transactions. A failure after the
service returns context but before the private hold is durable remains a
delivery uncertainty in the service history; this journal cannot recover
context that never reached it.
"""

import json
import os
import shlex
import shutil
import sys
import uuid
from pathlib import Path

from agent_parley import protocol
from agent_parley.state import BridgeError, write_json, write_text

EVENTS = {
    "session.created": "SessionStart",
    "chat.message": "UserPromptSubmit",
    "tool.execute.before": "PreToolUse",
    "tool.execute.after": "PostToolUse",
    "permission.ask": "PermissionRequest",
    "session.idle": "Stop",
}
CONFIG = "opencode.json"
PLUGIN = """import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";

const COMMAND = __COMMAND__;
const JOURNAL_COMMAND = __JOURNAL_COMMAND__;

function decide(payload) {
  const result = spawnSync(COMMAND[0], COMMAND.slice(1), {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 3000,
  });
  if (result.status === 2) {
    throw new Error(result.stderr || "Agent Parley checkpoint refused");
  }
  if (result.status !== 0) {
    return {};
  }
  try {
    return JSON.parse(result.stdout || "{}");
  } catch {
    return {};
  }
}

export const AgentParley = async ({ client, directory }) => {
  const instance = randomUUID();
  const journal = (operation, fields) => {
    const result = spawnSync(JOURNAL_COMMAND[0], JOURNAL_COMMAND.slice(1), {
      input: JSON.stringify({ operation, instance, ...fields }),
      encoding: "utf8",
      timeout: 3000,
    });
    if (result.status !== 0) {
      throw new Error(result.stderr || "Agent Parley context journal failed; "
        + "no native output or automatic replay is authorized.");
    }
    try {
      return JSON.parse(result.stdout);
    } catch {
      throw new Error("Agent Parley context journal returned invalid evidence; "
        + "no native output or automatic replay is authorized.");
    }
  };
  const recovered = journal("recover", {});
  const receiptKeys = new Set(
    recovered.receipt_keys.map((key) => JSON.stringify(key))
  );
  const idle = new Set();
  const started = new Set();
  const payload = (name, sessionID, fields) => ({
    hook_event_name: name,
    session_id: sessionID,
    cwd: directory,
    ...fields,
  });
  const hold = (sessionID, reply) => {
    if (reply.context) {
      journal("hold", { session_id: sessionID, context: reply.context });
    }
    return reply;
  };
  return {
    event: async ({ event }) => {
      if (event.type === "message.part.updated") {
        const part = event.properties.part;
        const key = JSON.stringify([
          part?.sessionID, part?.messageID, part?.id,
        ]);
        if (receiptKeys.has(key)) {
          const result = journal("receipt", {
            session_id: part.sessionID,
            message_id: part.messageID,
            native_part_id: part.id,
          });
          if (result.confirmed) receiptKeys.delete(key);
        }
      } else if (event.type === "session.created") {
        const id = event.properties.info.id;
        if (!started.has(id)) {
          started.add(id);
          hold(id, decide(payload("session.created", id, {})));
        }
      } else if (event.type === "session.deleted") {
        const id = event.properties.info.id;
        journal("close", { session_id: id });
        idle.delete(id);
        started.delete(id);
      } else if (event.type === "session.status"
          && event.properties.status.type !== "idle") {
        idle.delete(event.properties.sessionID);
      } else if (event.type === "session.idle"
          || (event.type === "session.status"
            && event.properties.status.type === "idle")) {
        const id = event.properties.sessionID;
        if (idle.has(id)) return;
        idle.add(id);
        const reply = hold(id, decide(payload("session.idle", id, {})));
        if (reply.decision === "block" && reply.reason) {
          const result = await client.session.prompt({
            path: { id },
            body: { parts: [{ type: "text", text: reply.reason }] },
          });
          if (result?.error) {
            throw new Error("Agent Parley continuation was not accepted; "
              + "check OpenCode session status. No automatic retry.");
          }
        }
      }
    },
    "chat.message": async (input, output) => {
      const id = input.sessionID;
      idle.delete(id);
      hold(id, decide(payload("chat.message", id, {})));
      const message = output.message;
      if (!message?.id || message.sessionID !== id) {
        throw new Error("Agent Parley needs a matching OpenCode UserMessage "
          + "with id and sessionID; context remains pending.");
      }
      const partID = "prt_" + randomUUID().replaceAll("-", "");
      const { context } = journal("attempt", {
        session_id: id,
        message_id: message.id,
        native_part_id: partID,
        channel: "chat",
      });
      if (context) {
        receiptKeys.add(JSON.stringify([id, message.id, partID]));
        output.parts.push({
          id: partID,
          sessionID: id,
          messageID: message.id,
          type: "text",
          text: context,
          synthetic: true,
        });
      }
    },
    "tool.execute.before": async (input, output) => {
      idle.delete(input.sessionID);
      const reply = hold(
        input.sessionID,
        decide(
          payload("tool.execute.before", input.sessionID, {
            tool_name: input.tool,
            tool_input: output.args,
          }),
        ),
      );
      if (reply.decision === "deny") {
        throw new Error(reply.reason || "Agent Parley refused this call");
      }
    },
    "tool.execute.after": async (input, output) => {
      hold(
        input.sessionID,
        decide(
          payload("tool.execute.after", input.sessionID, {
            tool_name: input.tool,
            tool_response: output.output,
          }),
        ),
      );
      const { context } = journal("attempt", {
        session_id: input.sessionID,
        channel: "tool",
      });
      if (context) {
        output.output = [output.output, context]
          .filter((part) => part)
          .join("\\n\\n");
      }
    },
    "permission.ask": async (input) => {
      hold(
        input.sessionID,
        decide(
          payload("permission.ask", input.sessionID, {
            tool_name: input.type,
            tool_input: input.metadata,
          }),
        ),
      );
    },
  };
};
"""


def configure(
    directory: Path,
    name: str,
    url: str,
    hooks: dict,
    source_dir: str | None = None,
) -> Path:
    """Copies the user's OpenCode configuration into a lane-private directory.

    The lane's MCP server is added under ``mcp`` and one plugin file is
    written that runs the shared hook command for each event in ``EVENTS``.
    A fixed-argv private helper journals Parley context beside this overlay,
    so rebuilding the overlay never deletes that evidence. Each configuration
    receives a new generation; held context from an earlier generation is
    retained as uncertain, without migrating it into a new launch.
    Every other key of the user's ``opencode.json`` is carried unchanged, the
    source directory is never written, and OpenCode's own authentication
    store outside the configuration directory stays in effect.

    Args:
        directory: Private project state directory.
        name: Validated participant name.
        url: Authenticated loopback MCP endpoint.
        hooks: Common checkpoint hook definitions.
        source_dir: Configuration directory selected by the launch
            environment; OpenCode's default location when unset.

    Returns:
        Directory for OPENCODE_CONFIG_DIR in this launch only.

    Raises:
        BridgeError: If the user's configuration cannot safely accept the
            lane server or plugin.
    """
    source = Path(
        source_dir
        or os.environ.get("OPENCODE_CONFIG_DIR")
        or (Path.home() / ".config" / "opencode")
    )
    if (source / "opencode.jsonc").exists() and not (source / CONFIG).exists():
        raise BridgeError(
            f"{source / 'opencode.jsonc'} cannot be carried into a lane "
            "overlay unchanged; save it as opencode.json first."
        )
    path = source / CONFIG
    settings = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings, dict):
        raise BridgeError("OpenCode configuration must be an object.")
    servers = settings.setdefault("mcp", {})
    plugins = settings.setdefault("plugin", [])
    if not isinstance(servers, dict) or not isinstance(plugins, list):
        raise BridgeError(
            "OpenCode mcp and plugin settings must be an object and an array."
        )
    if "agent_parley" in servers:
        raise BridgeError(
            "OpenCode configuration already defines agent_parley."
        )
    servers["agent_parley"] = {
        "type": "remote",
        "url": url,
        "enabled": True,
        "headers": {
            "Authorization": "Bearer {env:AGENT_PARLEY_TOKEN}",
            protocol.HEADER: str(protocol.PROTOCOL),
        },
    }
    command = shlex.split(hooks["PreToolUse"][0]["hooks"][0]["command"])
    command += ["--adapter", "opencode"]
    overlay = directory / f"{name}-opencode"
    shutil.rmtree(overlay, ignore_errors=True)
    (overlay / "plugin").mkdir(parents=True, mode=0o700)
    write_json(overlay / CONFIG, settings)
    write_text(
        overlay / "plugin" / "agent-parley.js",
        PLUGIN.replace("__COMMAND__", json.dumps(command)).replace(
            "__JOURNAL_COMMAND__",
            json.dumps(
                [
                    sys.executable,
                    "-m",
                    "agent_parley.opencode_context",
                    str(
                        (directory / f"{name}-opencode-context.json").resolve()
                    ),
                    uuid.uuid4().hex,
                ]
            ),
        ),
    )
    return overlay


def payload(value: dict) -> dict:
    """Translates plugin event and MCP tool names for shared checkpoints."""
    translated = dict(value)
    event = value.get("hook_event_name", "")
    translated["hook_event_name"] = EVENTS.get(event, event)
    tool = str(value.get("tool_name", ""))
    if tool.startswith("agent_parley_"):
        translated["tool_name"] = tool.replace(
            "agent_parley_", "mcp__agent_parley__", 1
        )
    return translated


def response(value: dict) -> dict:
    """Flattens shared checkpoint output into the fields the plugin reads."""
    details = value.get("hookSpecificOutput", {})
    result: dict = {}
    if details.get("permissionDecision") == "deny":
        result.update(
            decision="deny", reason=details.get("permissionDecisionReason", "")
        )
    elif value.get("decision") == "block":
        result.update(decision="block", reason=value.get("reason", ""))
    if context := details.get("additionalContext"):
        result["context"] = context
    return result
