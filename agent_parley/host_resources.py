"""Coordinates advisory host resources across projects and Parley homes.

One user-owned registry is shared by cooperating installations. Holdings do
not expire: a crashed process does not prove that its device or port is free.
Release names an exact lease generation, preventing stale cleanup from
releasing a later reservation. This does not bind ports or enforce GPU access.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from agent_parley import roster
from agent_parley.state import BridgeError, lock, write_json

DEFAULT_HOME = "~/.local/state/agent-parley-host"
TOOLS = (
    "reserve_host_resources",
    "release_host_resources",
    "list_host_resources",
)
MAX_REQUEST = 16
MAX_HOLDINGS = 256


def home() -> Path:
    """Resolves the registry shared by cooperating user installations."""
    return (
        Path(os.environ.get("AGENT_PARLEY_RESOURCE_HOME") or DEFAULT_HOME)
        .expanduser()
        .resolve()
    )


def owner(state_home: Path, actor: dict) -> dict[str, str]:
    """Identifies a lane without recording its authentication credential."""
    identity = {
        "state_home": str(state_home.resolve()),
        "project": str(Path(actor["project"]).resolve()),
        "participant": str(actor["name"]),
    }
    return {
        "owner": hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest(),
        **identity,
    }


def _private(directory: Path, *, create: bool = False) -> None:
    """Requires private state without changing existing permissions."""
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.exists() and not directory.is_dir():
        raise BridgeError("Host resource home must be a directory.")
    if directory.exists() and (
        directory.stat().st_uid != os.getuid()
        or directory.stat().st_mode & 0o077
    ):
        raise BridgeError(
            f"Host resource directory must be owned by you and private: "
            f"{directory}. Use a directory with mode 0700."
        )


def _read(directory: Path) -> dict:
    """Reads an atomic snapshot, refusing damaged or unknown state."""
    _private(directory)
    path = directory / "resources.json"
    if not path.exists():
        return {"version": 1, "holdings": {}}
    try:
        data = json.loads(path.read_text())
        valid = (
            isinstance(data, dict)
            and data.get("version") == 1
            and isinstance(data.get("holdings"), dict)
            and len(data["holdings"]) <= MAX_HOLDINGS
        )
        if valid:
            for resource, entry in data["holdings"].items():
                valid = (
                    isinstance(entry, dict)
                    and entry.get("resource") == resource
                    and roster.resource_name(resource)
                    and all(
                        isinstance(entry.get(field), str) and entry[field]
                        for field in (
                            "lease_id",
                            "owner",
                            "state_home",
                            "project",
                            "participant",
                        )
                    )
                )
                if not valid:
                    break
        if valid:
            return data
    except (OSError, ValueError, TypeError) as error:
        raise BridgeError(
            f"Cannot read host resource registry: {path}."
        ) from error
    raise BridgeError(f"Invalid host resource registry: {path}.")


def inspect() -> dict:
    """Lists host holdings without creating files or probing devices."""
    directory = home()
    return {
        "registry": str(directory),
        "advisory": True,
        "expires": False,
        "holdings": list(_read(directory)["holdings"].values()),
    }


def _items(value: object, label: str) -> list[str]:
    """Validates a bounded, nonempty list of unique string identifiers."""
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_REQUEST
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise BridgeError(f"{label} needs 1 to {MAX_REQUEST} strings.")
    return sorted(set(value))


def reserve(state_home: Path, actor: dict, resources: object) -> dict:
    """Atomically reserves every resource or returns conflicts without grants.

    Repeated reservations by the same owner return the existing leases.
    Distinct state homes and projects remain distinct owners even if their
    participant names match. All callers must use the same registry and
    canonical resource names to coordinate.
    """
    names = _items(resources, "resources")
    roster.resources(names)
    identity = owner(state_home, actor)
    directory = home()
    try:
        _private(directory, create=True)
        with lock(directory / "resources.lock", timeout=5):
            data = _read(directory)
            holdings = data["holdings"]
            conflicts = [
                holdings[name]
                for name in names
                if name in holdings
                and holdings[name]["owner"] != identity["owner"]
            ]
            if conflicts:
                return {"granted": [], "conflicts": conflicts}
            if len(set(holdings) | set(names)) > MAX_HOLDINGS:
                raise BridgeError("Host resource registry is full.")
            changed = False
            for name in names:
                if name not in holdings:
                    holdings[name] = {
                        **identity,
                        "resource": name,
                        "lease_id": uuid.uuid4().hex,
                        "created_at": time.time(),
                    }
                    changed = True
            if changed:
                write_json(directory / "resources.json", data)
            return {
                "granted": [holdings[name] for name in names],
                "conflicts": [],
            }
    except OSError as error:
        raise BridgeError("Host resource registry is unavailable.") from error


def release(lease_ids: object, *, owner_id: str) -> dict:
    """Releases exact generations owned by a lane, or by an operator.

    An empty owner ID is reserved for the guarded operator CLI. Unknown IDs
    are already released; a known foreign ID refuses the entire request.
    """
    ids = _items(lease_ids, "lease_ids")
    if any(
        len(item) != 32 or any(c not in "0123456789abcdef" for c in item)
        for item in ids
    ):
        raise BridgeError("lease_ids must be the returned lease identifiers.")
    directory = home()
    if not directory.exists():
        return {"released": []}
    try:
        _private(directory)
        with lock(directory / "resources.lock", timeout=5):
            data = _read(directory)
            held = data["holdings"]
            selected = [v for v in held.values() if v["lease_id"] in ids]
            if owner_id and any(v["owner"] != owner_id for v in selected):
                raise BridgeError(
                    "A lane can release only its own host leases."
                )
            for entry in selected:
                del held[entry["resource"]]
            if selected:
                write_json(directory / "resources.json", data)
            return {"released": selected}
    except OSError as error:
        raise BridgeError("Host resource registry is unavailable.") from error


def call(state_home: Path, actor: dict, tool: str, args: dict) -> dict:
    """Serves an authenticated host operation in its independent transaction."""
    if tool == "list_host_resources" and not args:
        return inspect()
    if tool == "reserve_host_resources" and set(args) == {"resources"}:
        return reserve(state_home, actor, args["resources"])
    if tool == "release_host_resources" and set(args) == {"lease_ids"}:
        return release(
            args["lease_ids"], owner_id=owner(state_home, actor)["owner"]
        )
    raise BridgeError("Unexpected or missing host resource arguments.")
