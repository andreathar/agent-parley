"""Operator-defined role rules that refuse tool calls a lane may not make.

A project may carry one ``roles.json`` in its common state directory, outside
every target repository. It names roles with capabilities and assigns lanes to
them. A lane with no assignment is not restricted, so the file is an opt-in
that never changes an unconfigured project. A file that cannot be read or
fails validation refuses every write and shell call until it is repaired,
because an unreadable rule must not read as permission.

The rules are an operational barrier, not a sandbox. A shell command pattern
can be bypassed by quoting or by running a script, and a refusal depends on
the native client honoring a denied ``PreToolUse`` decision. Only the operator
edits the file, through commands that require the operator token.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path

from agent_parley.state import BridgeError, lock, write_json

VERSION = 1
FILENAME = "roles.json"
SHELL_TOOLS = frozenset({"bash", "shell", "exec_command", "local_shell"})
WRITE_TOOLS = frozenset(
    {
        "write",
        "edit",
        "multiedit",
        "notebookedit",
        "create",
        "str_replace_editor",
        "apply_patch",
    }
)
PATH_FIELDS = ("file_path", "notebook_path", "path")
COMMAND_FIELDS = ("command", "cmd")
PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
CAPABILITIES = frozenset({"write", "shell"})
ROLE_KEYS = frozenset(
    {
        "description",
        "write",
        "shell",
        "write_paths",
        "deny_paths",
        "deny_commands",
        "deny_tools",
    }
)
ASSIGNMENT_KEYS = frozenset({"role", "disabled"})
MAX_PATTERNS = 64
MAX_PATTERN_BYTES = 256


def location(directory: Path) -> Path:
    """Names the rules file of one project.

    Args:
        directory: Common project state directory.

    Returns:
        Path of ``roles.json`` inside it.
    """
    return directory / FILENAME


def _strings(value: object, where: str) -> list[str]:
    """Validates a bounded list of non-empty pattern strings.

    Args:
        value: Parsed JSON value.
        where: Field name used in the error text.

    Returns:
        The list of patterns.

    Raises:
        BridgeError: If the value is not a bounded list of short strings.
    """
    if not isinstance(value, list) or len(value) > MAX_PATTERNS:
        raise BridgeError(f"{where} must be a list of at most 64 strings.")
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item.encode()) > MAX_PATTERN_BYTES
        ):
            raise BridgeError(f"{where} holds an invalid pattern.")
    return value


def validate(spec: object) -> dict:
    """Checks a rules document and returns it normalized.

    Args:
        spec: Parsed ``roles.json`` content.

    Returns:
        The document with ``roles`` and ``assign`` always present.

    Raises:
        BridgeError: If the shape, a key, a pattern or a regular expression
            is invalid, or an assignment names an undefined role.
    """
    if not isinstance(spec, dict) or spec.get("version") != VERSION:
        raise BridgeError(f"roles.json must be an object with version {VERSION}.")
    roles = spec.get("roles", {})
    assign = spec.get("assign", {})
    if not isinstance(roles, dict) or not isinstance(assign, dict):
        raise BridgeError("roles and assign must be objects.")
    for name, role in roles.items():
        if not isinstance(role, dict) or set(role) - ROLE_KEYS:
            raise BridgeError(f"Role {name} has an unknown or invalid field.")
        for flag in CAPABILITIES:
            if flag in role and not isinstance(role[flag], bool):
                raise BridgeError(f"Role {name} {flag} must be true or false.")
        for key in ("write_paths", "deny_paths", "deny_commands", "deny_tools"):
            if key in role:
                _strings(role[key], f"Role {name} {key}")
        for pattern in role.get("deny_commands", []):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise BridgeError(
                    f"Role {name} has an invalid command pattern."
                ) from exc
    for lane, entry in assign.items():
        if not isinstance(entry, dict) or set(entry) - ASSIGNMENT_KEYS:
            raise BridgeError(f"Assignment for {lane} is invalid.")
        if entry.get("role") not in roles:
            raise BridgeError(f"Assignment for {lane} names an unknown role.")
        disabled = entry.get("disabled", [])
        if not isinstance(disabled, list) or set(disabled) - CAPABILITIES:
            raise BridgeError(f"Assignment for {lane} disables an unknown capability.")
    return {"version": VERSION, "roles": roles, "assign": assign}


def load(directory: Path) -> dict | None:
    """Reads the project's rules.

    Args:
        directory: Common project state directory.

    Returns:
        The validated document, or None when the project has no rules file.

    Raises:
        BridgeError: If the file exists but cannot be parsed or validated.
    """
    path = location(directory)
    if not path.exists():
        return None
    try:
        return validate(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        raise BridgeError(f"Cannot read {path.name}: {exc}") from exc


def save(directory: Path, spec: dict) -> None:
    """Validates and atomically writes the project's rules.

    Args:
        directory: Common project state directory.
        spec: Document to store.

    Raises:
        BridgeError: If the document is invalid.
    """
    document = validate(spec)
    with lock(directory / "roles.lock"):
        write_json(location(directory), document)


def digest(spec: dict | None) -> str:
    """Hashes a rules document so wiring can report which rules are loaded.

    Args:
        spec: Validated document, or None.

    Returns:
        A short stable hash, or an empty string when there are no rules.
    """
    if spec is None:
        return ""
    body = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def effective(spec: dict, lane: str) -> dict | None:
    """Resolves the capabilities one lane holds.

    Args:
        spec: Validated document.
        lane: Assigned native lane name.

    Returns:
        The role with the lane's disabled capabilities applied, or None when
        the lane has no assignment.
    """
    entry = spec["assign"].get(lane)
    if entry is None:
        return None
    role = dict(spec["roles"][entry["role"]])
    for capability in entry.get("disabled", []):
        role[capability] = False
    role["name"] = entry["role"]
    return role


def _paths(tool_input: object, lane: Path) -> list[str | None]:
    """Lists the lane-relative paths a write call names.

    Args:
        tool_input: Native tool input.
        lane: Lane worktree.

    Returns:
        One entry per named path; None marks a path outside the lane.
    """
    if not isinstance(tool_input, dict):
        return []
    named = [
        value
        for field in PATH_FIELDS
        if isinstance(value := tool_input.get(field), str) and value
    ]
    for field in ("patch", "input", "diff"):
        value = tool_input.get(field)
        if isinstance(value, str):
            named.extend(match.strip() for match in PATCH_PATH.findall(value))
    found: list[str | None] = []
    for item in named:
        target = Path(item)
        if not target.is_absolute():
            target = lane / target
        try:
            found.append(target.resolve().relative_to(lane.resolve()).as_posix())
        except (OSError, ValueError):
            found.append(None)
    return found


def _matches(path: str, patterns: list[str]) -> bool:
    """Reports whether a lane-relative path matches any glob.

    Args:
        path: POSIX path relative to the lane.
        patterns: Glob patterns where ``*`` also crosses directories.

    Returns:
        True on the first match.
    """
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def broken(directory: Path, payload: dict) -> str | None:
    """Refuses mutating calls while the rules file cannot be read.

    Args:
        directory: Common project state directory.
        payload: Native ``PreToolUse`` hook payload.

    Returns:
        A reason when the file is unreadable and the call can mutate, else
        None.
    """
    try:
        load(directory)
    except BridgeError as exc:
        tool = str(payload.get("tool_name", "")).lower()
        if tool in WRITE_TOOLS or tool in SHELL_TOOLS:
            return (
                f"Role rules are unreadable ({exc}). Writes and shell calls "
                "stay refused until the operator repairs roles.json."
            )
    return None


def decide(spec: dict, lane_name: str, payload: dict, lane: Path) -> str | None:
    """Decides whether a lane's role refuses one tool call.

    Args:
        spec: Validated document.
        lane_name: Assigned native lane name.
        payload: Native ``PreToolUse`` hook payload.
        lane: Lane worktree.

    Returns:
        The refusal reason naming the role and the rule, or None when the
        call is allowed or the lane has no role.
    """
    role = effective(spec, lane_name)
    if role is None:
        return None
    tool = str(payload.get("tool_name", ""))
    key = tool.lower()
    who = f"Role {role['name']}"
    if _matches(tool, role.get("deny_tools", [])):
        return f"{who} does not allow the {tool} tool."
    tool_input = payload.get("tool_input")
    if key in SHELL_TOOLS:
        if not role.get("shell", True):
            return f"{who} has shell calls disabled."
        command = ""
        if isinstance(tool_input, dict):
            command = " ".join(
                str(tool_input.get(field, "")) for field in COMMAND_FIELDS
            )
        for pattern in role.get("deny_commands", []):
            if re.search(pattern, command):
                return f"{who} forbids commands matching {pattern!r}."
        return None
    if key not in WRITE_TOOLS:
        return None
    if not role.get("write", True):
        return f"{who} is read-only."
    allowed = role.get("write_paths")
    for path in _paths(tool_input, lane):
        if path is None:
            return f"{who} may not write outside its worktree."
        if _matches(path, role.get("deny_paths", [])):
            return f"{who} may not write {path}."
        if allowed is not None and not _matches(path, allowed):
            return f"{who} may write only {', '.join(allowed)}; not {path}."
    return None
