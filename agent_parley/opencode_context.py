"""Journals Parley context separately from native prompts and task ACKs.

Only held entries can be attached to a native session. An attempt is durable
before it leaves this helper, so an interruption in the native commit window
retains uncertainty instead of causing an automatic replay.
"""

import json
import math
import sys
import time
import uuid
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json

SCHEMA = 1
MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 512
STATES = {"held", "attempted", "uncertain", "part_update_observed"}
REASONS = {
    "plugin instance changed before native receipt",
    "configuration generation changed; no migration",
    "tool output has no correlated native receipt",
    "native session deleted; explicit recovery required",
}


def _identity(value: object, label: str) -> str:
    """Validates a bounded opaque routing or correlation identifier."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(character in value for character in "\r\n\0")
    ):
        raise BridgeError(f"OpenCode context needs a valid {label}.")
    return value


def _read(path: Path) -> dict:
    """Reads bounded journal state without repairing malformed evidence."""
    if not path.exists():
        return {"schema": SCHEMA, "revision": 0, "entries": []}
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("journal exceeds its bound")
        value = json.loads(path.read_text())
        if (
            not isinstance(value, dict)
            or value.get("schema") != SCHEMA
            or type(value.get("revision")) is not int
            or value["revision"] < 0
            or not isinstance(value.get("entries"), list)
            or len(value["entries"]) > MAX_ENTRIES
        ):
            raise ValueError("invalid journal envelope")
        identifiers = set()
        for entry in value["entries"]:
            if not isinstance(entry, dict):
                raise ValueError("invalid journal entry")
            for field in ("id", "session_id", "origin_generation"):
                _identity(entry.get(field), field)
            if (
                entry["id"] in identifiers
                or entry.get("state") not in STATES
                or not isinstance(entry.get("context"), str)
                or not entry["context"]
                or type(entry.get("created_at")) not in (int, float)
                or type(entry.get("updated_at")) not in (int, float)
                or not math.isfinite(entry["created_at"])
                or not math.isfinite(entry["updated_at"])
                or (
                    "channel" in entry
                    and entry["channel"] not in {"chat", "tool"}
                )
                or ("reason" in entry and entry["reason"] not in REASONS)
            ):
                raise ValueError("invalid journal entry")
            identifiers.add(entry["id"])
            for field in ("attempt_instance", "message_id", "native_part_id"):
                if field in entry:
                    _identity(entry[field], field)
            if entry.get("channel") is not None:
                _identity(entry.get("attempt_instance"), "attempt_instance")
            if entry["state"] in {"attempted", "part_update_observed"}:
                _identity(entry.get("attempt_instance"), "attempt_instance")
                if entry.get("channel") != "chat":
                    raise ValueError("invalid native attempt channel")
            if entry.get("channel") == "chat":
                _identity(entry.get("message_id"), "message_id")
                _identity(entry.get("native_part_id"), "native_part_id")
        return value
    except (OSError, ValueError, BridgeError, TypeError) as error:
        raise BridgeError(
            "Cannot read OpenCode context journal; preserve it and inspect "
            "the private journal before retrying. No automatic repair/replay."
        ) from error


def summary(path: Path) -> dict:
    """Returns journal metadata without context bodies or native session text.

    Args:
        path: Lane-private journal path.

    Returns:
        Schema, evidence limitations, counts and correlation metadata.

    Raises:
        BridgeError: If the journal cannot be validated.
    """
    value = _read(path)
    fields = (
        "id",
        "session_id",
        "state",
        "origin_generation",
        "created_at",
        "updated_at",
        "attempt_instance",
        "message_id",
        "native_part_id",
        "channel",
        "reason",
    )
    return {
        "schema": SCHEMA,
        "revision": value["revision"],
        "counts": {
            state: sum(entry["state"] == state for entry in value["entries"])
            for state in sorted(STATES)
        },
        "entries": [
            {key: entry[key] for key in fields if key in entry}
            for entry in value["entries"]
        ],
        "evidence_limit": (
            "part_update_observed is not a commit guarantee "
            "or task ACK/completion"
        ),
        "automatic_replay": "held only, within the same native session",
    }


def _change(
    value: dict,
    operation: str,
    generation: str,
    instance: str,
    request: dict,
    now: float,
) -> tuple[dict, bool]:
    """Applies one journal transition while the caller holds its lane lock."""
    entries = value["entries"]
    changed = False
    result: dict = {}
    if operation == "recover":
        for entry in entries:
            if (
                entry["state"] == "held"
                and entry["origin_generation"] != generation
            ):
                entry.update(
                    state="uncertain",
                    updated_at=now,
                    reason="configuration generation changed; no migration",
                )
                changed = True
            elif (
                entry["state"] == "attempted"
                and entry["attempt_instance"] != instance
            ):
                entry.update(
                    state="uncertain",
                    updated_at=now,
                    reason="plugin instance changed before native receipt",
                )
                changed = True
        result["receipt_keys"] = [
            [entry["session_id"], entry["message_id"], entry["native_part_id"]]
            for entry in entries
            if entry["state"] in {"attempted", "uncertain"}
            and entry.get("channel") == "chat"
        ]
        return result, changed
    session = _identity(request.get("session_id"), "session_id")
    if operation == "hold":
        context = request.get("context")
        if not isinstance(context, str) or not context:
            raise BridgeError("OpenCode context hold needs nonempty context.")
        if len(entries) >= MAX_ENTRIES:
            raise BridgeError(
                "OpenCode context journal is full; preserve pending entries "
                "and explicitly archive reviewed evidence before retrying."
            )
        entries.append(
            {
                "id": uuid.uuid4().hex,
                "session_id": session,
                "origin_generation": generation,
                "state": "held",
                "context": context,
                "created_at": now,
                "updated_at": now,
            }
        )
        return result, True
    if operation == "attempt":
        channel = request.get("channel")
        if channel not in {"chat", "tool"}:
            raise BridgeError("OpenCode context needs a supported channel.")
        if channel == "chat":
            message = _identity(request.get("message_id"), "message_id")
            part = _identity(request.get("native_part_id"), "native_part_id")
            if not message.startswith("msg") or not part.startswith("prt"):
                raise BridgeError(
                    "OpenCode context needs native message/part IDs."
                )
        selected = [
            entry
            for entry in entries
            if entry["session_id"] == session
            and entry["state"] == "held"
            and entry["origin_generation"] == generation
        ]
        for entry in selected:
            entry.update(
                state="attempted" if channel == "chat" else "uncertain",
                attempt_instance=instance,
                channel=channel,
                updated_at=now,
            )
            if channel == "chat":
                entry.update(message_id=message, native_part_id=part)
            else:
                entry["reason"] = "tool output has no correlated native receipt"
        return {"context": "\n\n".join(e["context"] for e in selected)}, bool(
            selected
        )
    if operation == "receipt":
        message = _identity(request.get("message_id"), "message_id")
        part = _identity(request.get("native_part_id"), "native_part_id")
        for entry in entries:
            if (
                entry["session_id"] == session
                and entry.get("message_id") == message
                and entry.get("native_part_id") == part
                and entry["state"] in {"attempted", "uncertain"}
                and entry.get("channel") == "chat"
            ):
                entry.update(state="part_update_observed", updated_at=now)
                entry.pop("reason", None)
                changed = True
        return {"confirmed": changed}, changed
    if operation == "close":
        for entry in entries:
            if entry["session_id"] == session and entry["state"] in {
                "held",
                "attempted",
            }:
                entry.update(
                    state="uncertain",
                    updated_at=now,
                    reason="native session deleted; explicit recovery required",
                )
                changed = True
        return result, changed
    raise BridgeError("Unsupported OpenCode context journal operation.")


def execute(
    path: Path,
    generation: str,
    instance: str,
    request: dict,
    *,
    now: float | None = None,
) -> dict:
    """Runs one durable transition, failing closed on I/O or contention.

    Args:
        path: Lane-private journal path outside the replaceable overlay.
        generation: Configuration generation, independent of display names.
        instance: Plugin instance ID used to mark interrupted attempts.
        request: Operation, session and only Parley context/correlation IDs.
        now: Clock override for deterministic transition tests.

    Returns:
        Operation result after any changed state is durable.

    Raises:
        BridgeError: If validation, capacity, locking or persistence fails.
    """
    generation = _identity(generation, "generation")
    instance = _identity(instance, "instance")
    if now is not None and not math.isfinite(now):
        raise BridgeError("OpenCode context needs a finite observation time.")
    operation = request.get("operation")
    if not isinstance(operation, str):
        raise BridgeError("OpenCode context needs an operation.")
    try:
        with lock(path.with_suffix(".lock"), timeout=0.5):
            value = _read(path)
            result, changed = _change(
                value,
                operation,
                generation,
                instance,
                request,
                time.time() if now is None else now,
            )
            if changed:
                value["revision"] += 1
                if len(json.dumps(value).encode()) > MAX_BYTES:
                    raise BridgeError(
                        "OpenCode context journal capacity exceeded; no "
                        "entries were discarded. Explicit review is required."
                    )
                write_json(path, value)
            return result
    except OSError as error:
        raise BridgeError(
            "Cannot persist OpenCode context; check private state storage. "
            "Native output was not authorized by this journal operation."
        ) from error


def main() -> int:
    """Runs the private JSON-stdin helper without shell interpretation."""
    try:
        if len(sys.argv) != 3:
            raise BridgeError("OpenCode journal helper needs path/generation.")
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise BridgeError("OpenCode journal request must be an object.")
        instance = _identity(request.pop("instance", None), "instance")
        result = execute(Path(sys.argv[1]), sys.argv[2], instance, request)
        print(json.dumps(result))
        return 0
    except (BridgeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
