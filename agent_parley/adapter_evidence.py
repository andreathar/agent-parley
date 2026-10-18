"""Records sanitized launch declarations independently of runtime activation.

Receipts contain only identifiers and protocol metadata. They accept no native
settings, command lines, credential profiles, tokens or session transcripts.
The launcher retains its existing session lock while publishing the receipt;
atomic replacement keeps readers from observing a partial declaration.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path

from agent_parley import protocol, roster
from agent_parley.state import BridgeError, write_json

VERSION = 1
MAX_SKILLS = 100
MAX_BYTES = 32768


def location(directory: Path, participant: str) -> Path:
    """Returns the receipt path for a validated stable participant ID."""
    roster.identifier(participant, "participant")
    return directory / f"{participant}-launch-receipt.json"


def _skills(values: object) -> list[str]:
    """Validates bounded declarative skill IDs rather than skill content."""
    if (
        not isinstance(values, list)
        or len(values) > MAX_SKILLS
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > 240
            or not value.isprintable()
            for value in values
        )
    ):
        raise BridgeError("Launch receipt skills must be bounded skill IDs.")
    return sorted(set(values))


def _digest(shape: dict) -> str:
    """Fingerprints only validated declaration IDs and protocol metadata."""
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def record_launch(
    directory: Path,
    participant: str,
    adapter: str,
    *,
    configured_hooks: list[str],
    configured_skills: list[str],
    session_generation: str | None = None,
    now: float | None = None,
) -> dict:
    """Publishes one sanitized declaration of a prepared launch attempt.

    Args:
        directory: Existing private project state directory.
        participant: Immutable participant ID; not its editable label.
        adapter: Existing native adapter ID.
        configured_hooks: Hook IDs actually included in the launch settings.
        configured_skills: Skill IDs explicitly configured by this launch;
            an empty list when none are declared. A protocol prompt or file's
            presence is not a skill declaration.
        session_generation: Native session ID only when already known, such
            as an explicit resume. A launcher PID is never a native session.
        now: Finite Unix timestamp; current time by default.

    Returns:
        A receipt carrying a new launch generation and an allowlisted-shape
        digest. The launcher stores this generation in its activity record.
        This proves prepared declarations, not spawn success or activation.

    Raises:
        BridgeError: If identifiers, declarations or timestamp are invalid.
    """
    path = location(directory, participant)
    if adapter not in roster.ADAPTERS:
        raise BridgeError("Unknown launch receipt adapter.")
    if not isinstance(configured_hooks, list) or any(
        event not in roster.HOOK_EVENTS
        or event in roster.unavailable_hooks(adapter)
        for event in configured_hooks
    ):
        raise BridgeError("Launch receipt declares an unsupported hook.")
    skills = _skills(configured_skills)
    if session_generation is not None and (
        not isinstance(session_generation, str)
        or not session_generation
        or len(session_generation) > 240
        or not session_generation.isprintable()
    ):
        raise BridgeError("Launch receipt session must be a native session ID.")
    stamp = time.time() if now is None else now
    if (
        not isinstance(stamp, int | float)
        or isinstance(stamp, bool)
        or not math.isfinite(stamp)
        or stamp <= 0
    ):
        raise BridgeError(
            "Launch receipt requires a finite positive timestamp."
        )
    shape = {
        "adapter": adapter,
        "configured_hooks": [
            event for event in roster.HOOK_EVENTS if event in configured_hooks
        ],
        "configured_skills": skills,
        "protocol": protocol.PROTOCOL,
    }
    digest = _digest(shape)
    receipt = {
        "version": VERSION,
        "participant": participant,
        "generation": uuid.uuid4().hex,
        "configured_at": stamp,
        "session_generation": session_generation,
        "configuration_digest": digest,
        "evidence_kind": "prepared_launch_declaration",
        **shape,
    }
    write_json(path, receipt)
    return receipt


def read(directory: Path, participant: str, adapter: str) -> dict | None:
    """Reads only the bounded public receipt schema, never native settings."""
    path = location(directory, participant)
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("oversize receipt")
        document = json.loads(path.read_text())
        if (
            not isinstance(document, dict)
            or document.get("version") != VERSION
            or document.get("participant") != participant
            or document.get("adapter") != adapter
            or document.get("evidence_kind") != "prepared_launch_declaration"
        ):
            raise ValueError("receipt identity or schema mismatch")
        hooks = document.get("configured_hooks")
        if not isinstance(hooks, list) or any(
            event not in roster.HOOK_EVENTS
            or event in roster.unavailable_hooks(adapter)
            for event in hooks
        ):
            raise ValueError("unsupported declaration")
        skills = _skills(document.get("configured_skills"))
        declared_protocol = document.get("protocol")
        if type(declared_protocol) is not int or declared_protocol < 1:
            raise ValueError("invalid protocol")
        stamp = document.get("configured_at")
        if (
            not isinstance(stamp, int | float)
            or isinstance(stamp, bool)
            or not math.isfinite(stamp)
            or stamp <= 0
        ):
            raise ValueError("invalid timestamp")
        generation = document.get("generation")
        digest = document.get("configuration_digest")
        if not isinstance(generation, str) or not re.fullmatch(
            r"[a-f0-9]{32}", generation
        ):
            raise ValueError("missing generation")
        if not isinstance(digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", digest
        ):
            raise ValueError("invalid shape digest")
        shape = {
            "adapter": adapter,
            "protocol": declared_protocol,
            "configured_hooks": [
                event for event in roster.HOOK_EVENTS if event in hooks
            ],
            "configured_skills": skills,
        }
        if digest != _digest(shape):
            raise ValueError("declaration digest mismatch")
        return {
            key: document.get(key)
            for key in (
                "version",
                "participant",
                "adapter",
                "generation",
                "configured_at",
                "session_generation",
                "configuration_digest",
                "evidence_kind",
                "protocol",
                "configured_hooks",
            )
        } | {"configured_skills": skills}
    except (OSError, ValueError, TypeError, BridgeError) as error:
        raise BridgeError(
            f"Cannot inspect sanitized launch receipt {path.name}."
        ) from error
