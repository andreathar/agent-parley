"""Prepares lightweight checkouts and reports shared media evidence.

The project manifest owns the profile. This module neither grants native
filesystem access nor treats a shared path as a read-only mount. Full
checkouts remain the default; enabling the profile affects future lanes only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from agent_parley import roster
from agent_parley.state import BridgeError, lock, write_json

MODE = "shared-media"
VERSION = 1
MEDIA_SUFFIXES = frozenset(
    ".png .jpg .jpeg .webp .gif .tif .tiff .exr .mp4 .mov .mkv .webm "
    ".mp3 .wav .flac .aac .ogg .m4a .pdf .zip .7z".split()
)
ADAPTERS = frozenset({"claude", "codex", "copilot", "gemini", "opencode"})
MAX_FILES = 100000


def relative(value: str) -> Path:
    """Validates a portable, literal path relative to an explicit root."""
    if (
        not isinstance(value, str)
        or not value
        or not value.isprintable()
        or "\\" in value
        or Path(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or ":" in value
    ):
        raise BridgeError("Use a relative path without traversal or a drive.")
    return Path(value)


def resolve(root: Path, value: str, *, existing: bool = True) -> Path:
    """Resolves one input or output without following an escaping symlink."""
    path = root.resolve() / relative(value)
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise BridgeError("Path escapes its declared root through a symlink.")
    if existing and not resolved.is_file():
        raise BridgeError(f"File is absent: {value}; check the canonical root.")
    return resolved


def profile(manifest: dict) -> dict | None:
    """Validates the manifest's optional profile without changing defaults."""
    value = manifest.get("workspace")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "mode", "media_root", "catalog"}
        or value["version"] != VERSION
        or type(value["version"]) is not int
        or value["mode"] != MODE
        or not isinstance(value["media_root"], str)
        or not isinstance(value["catalog"], str)
    ):
        raise BridgeError(
            "Invalid workspace profile; preview workspace config."
        )
    root = Path(value["media_root"])
    if (
        not root.is_absolute()
        or root.resolve() != Path(manifest["root"]).resolve()
    ):
        raise BridgeError("Media root must be this project's canonical root.")
    relative(value["catalog"])
    if Path(value["catalog"]).suffix.lower() not in {".csv", ".json"}:
        raise BridgeError("Catalog must be existing CSV or JSON metadata.")
    if manifest.get("pool"):
        raise BridgeError(
            "Shared media requires the spare pool disabled; no full spare "
            "checkout will be used.",
            next_command="agent-parley pool show --json",
        )
    if manifest.get("initialize"):
        raise BridgeError(
            "Shared media refuses inherited initialization commands; review "
            "and disable them before selecting this profile.",
            next_command="agent-parley init show",
        )
    return dict(value)


def _git(root: Path, *args: str, data: str | None = None) -> str:
    """Runs bounded local Git with LFS fetching and optional locks disabled."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            input=data,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BridgeError(
            "Workspace Git failed; checkout preserved."
        ) from error
    if result.returncode:
        raise BridgeError(
            f"Workspace Git refused {args[0]}: {result.stderr.strip()[:500]}; "
            "checkout preserved; no fallback to a full checkout."
        )
    return result.stdout.strip() if "-z" not in args else result.stdout


def exclusions(root: Path, *, index: bool = False) -> list[str]:
    """Finds tracked media and LFS files without hashing their contents."""
    names = _git(root, "ls-files", "-z").split("\0")
    names = [name for name in names if name]
    if len(names) > MAX_FILES:
        raise BridgeError("Too many tracked files; inspect workspace scope.")
    attrs = _git(
        root,
        "check-attr",
        *(["--cached"] if index else []),
        "-z",
        "--stdin",
        "filter",
        data="\0".join(names) + "\0",
    ).split("\0")
    lfs = {
        attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "lfs"
    }
    excluded = sorted(
        name
        for name in names
        if name in lfs or Path(name).suffix.lower() in MEDIA_SUFFIXES
    )
    for name in excluded:
        relative(name)
        if any(char in name for char in "*?[]!#"):
            raise BridgeError(
                "Media path contains sparse-pattern metacharacters; choose "
                "a literal supported path before configuring this profile."
            )
    return excluded


def add(
    root: Path, lane: Path, branch: str, base: str, *, new_branch: bool = True
) -> None:
    """Creates only an empty worktree before installing sparse exclusions.

    Git's repository-local worktreeConfig extension is enabled to keep the
    sparse settings private to this checkout. Native permissions and global
    Git configuration are untouched. A failure preserves the partial lane.
    """
    if _git(root, "config", "--get", "core.bare") == "true":
        raise BridgeError("A bare repository cannot own canonical media.")
    common = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "core.worktree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if common.returncode == 0:
        raise BridgeError("Explicit core.worktree requires a reviewed layout.")
    _git(root, "config", "extensions.worktreeConfig", "true")
    arguments = ["worktree", "add", "--no-checkout"]
    if new_branch:
        arguments.extend(["-b", branch, str(lane), base])
    else:
        arguments.extend([str(lane), branch])
    _git(root, *arguments)
    _git(lane, "read-tree", "HEAD")
    excluded = exclusions(lane, index=True)
    info = Path(_git(lane, "rev-parse", "--git-path", "info/sparse-checkout"))
    if not info.is_absolute():
        info = lane / info
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("/*\n" + "".join(f"!/{name}\n" for name in excluded))
    _git(lane, "config", "--worktree", "core.sparseCheckout", "true")
    _git(lane, "config", "--worktree", "core.sparseCheckoutCone", "false")
    _git(lane, "read-tree", "--reset", "-u", "HEAD")
    if any((lane / name).exists() for name in excluded):
        raise BridgeError(
            "Media was materialized; launch refused; preserve lane."
        )


def capability(adapter: str) -> dict:
    """Reports path-reference support separately from native access proof."""
    return {
        "adapter": adapter,
        "supported": adapter in ADAPTERS,
        "input_transport": "explicit_path_reference",
        "native_read_verified": None,
        "readonly_enforced": False,
        "evidence": "launcher contract only; no native read or mount proof",
    }


def binding(manifest: dict, entry: dict, adapter: str) -> dict:
    """Describes one lane's paths without claiming media activation."""
    selected = profile(manifest)
    return {
        "mode": MODE if selected else "full",
        "media_root": selected["media_root"] if selected else None,
        "catalog": selected["catalog"] if selected else None,
        "lane": entry["lane"],
        "lane_exists": Path(entry["lane"]).is_dir(),
        "profile_matches": entry.get("workspace_profile") == selected,
        "output_root": entry.get("output_root"),
        "capability": capability(adapter),
    }


def launch_roots(
    manifest: dict, entry: dict, adapter: str, task_id: str
) -> dict:
    """Validates roots before registration and returns process-only values."""
    selected = profile(manifest)
    if not selected:
        if entry.get("workspace_profile"):
            raise BridgeError(
                "This lane retains its shared-media profile; restore the "
                "matching project profile before launch."
            )
        return {}
    if not capability(adapter)["supported"]:
        raise BridgeError(f"{adapter} cannot launch a shared-media workspace.")
    if entry.get("workspace_profile") != selected:
        raise BridgeError(
            "Existing lane has another workspace profile; keep it and choose "
            "a new explicit participant. No media will be copied."
        )
    roster.identifier(task_id, "output task")
    root = Path(selected["media_root"])
    resolve(root, selected["catalog"])
    lane = Path(entry["lane"])
    output = resolve(lane, f"outputs/{task_id}", existing=False)
    if output.exists() and not output.is_dir():
        raise BridgeError("Output task path is a file; choose another task ID.")
    if output.is_relative_to(root.resolve()):
        raise BridgeError("Output root overlaps the canonical media root.")
    return {
        "AGENT_PARLEY_MEDIA_ROOT": str(root),
        "AGENT_PARLEY_ASSET_CATALOG": str(resolve(root, selected["catalog"])),
        "AGENT_PARLEY_OUTPUT_ROOT": str(output),
    }


def status(home: Path, repo: Path, *, sample: str | None = None) -> dict:
    """Reads configured paths; an optional sample probes only the host."""
    from agent_parley.agents import _project, _read

    directory = _project(home, repo)
    manifest = roster.normalize(_read(directory / "project.json"))
    selected = profile(manifest)
    result = {
        "read_only": True,
        "repo": manifest["root"],
        "profile": selected,
        "warnings": [],
        "native_read_verified": None,
        "readonly_enforced": False,
        "catalog_validation": "not_checked",
        "participants": [],
    }
    for entry in manifest["participants"].values():
        adapter = roster.provider(home, entry["provider"])["adapter"]
        result["participants"].append(binding(manifest, entry, adapter))
    if selected:
        root = Path(selected["media_root"])
        result["media_root_exists"] = root.is_dir()
        result["catalog_exists"] = resolve(
            root, selected["catalog"], existing=False
        ).is_file()
        if not result["catalog_exists"]:
            result["warnings"].append("Catalog missing; check media root.")
        if sample:
            if Path(sample).suffix.lower() not in MEDIA_SUFFIXES:
                raise BridgeError(
                    "Doctor sample must be a supported media file."
                )
            path = resolve(root, sample)
            with path.open("rb") as stream:
                prefix = stream.read(80)
            pointer = prefix.startswith(
                b"version https://git-lfs.github.com/spec/v1"
            )
            result["sample"] = {
                "path": sample,
                "host_read_verified": True,
                "lfs_pointer": pointer,
                "bytes": path.stat().st_size,
            }
            if pointer:
                result["warnings"].append(
                    "Sample is an LFS pointer; hydrate only the canonical "
                    "source explicitly."
                )
    return result


def configure(
    home: Path,
    repo: Path,
    catalog: str,
    *,
    preview: bool = True,
    expected_revision: str | None = None,
    disable: bool = False,
) -> dict:
    """Previews or audits a CAS profile update affecting future lanes only."""
    from agent_parley.agents import _project

    directory = _project(home, repo)
    if type(preview) is not bool or type(disable) is not bool:
        raise BridgeError("Preview and disable must be booleans.")
    if not preview:
        roster.require_operator(home, "Changing future workspace profiles")
        if not expected_revision:
            raise BridgeError(
                "Apply requires --expected-revision from preview."
            )
        with lock(directory / "setup.lock"):
            return _configure(
                directory, catalog, False, expected_revision, disable
            )
    return _configure(directory, catalog, True, expected_revision, disable)


def _configure(
    directory: Path,
    catalog: str,
    preview: bool,
    revision: str | None,
    disable: bool,
) -> dict:
    """Preserves all other manifest fields and appends bounded audit history."""
    from agent_parley.agents import _read

    path = directory / "project.json"
    document = _read(path)
    manifest = roster.normalize(json.loads(json.dumps(document)))
    before = manifest.get("workspace") if disable else profile(manifest)
    after = (
        None
        if disable
        else {
            "version": VERSION,
            "mode": MODE,
            "media_root": manifest["root"],
            "catalog": catalog,
        }
    )
    if after:
        profile({**manifest, "workspace": after})
        resolve(Path(manifest["root"]), catalog)
    current = hashlib.sha256(
        json.dumps(document, sort_keys=True).encode()
    ).hexdigest()
    changed = before != after
    if changed and revision and revision != current:
        raise BridgeError(
            "Configuration changed; preview workspace config again."
        )
    result = {
        "preview": preview,
        "revision": current,
        "changed": changed,
        "before": before,
        "after": after,
        "future_lanes_only": True,
        "permissions_changed": False,
        "impact": (
            "new lanes enable repository-local worktreeConfig; "
            "existing lanes and canonical media stay"
        ),
    }
    if not preview and changed:
        if after:
            document["workspace"] = after
        else:
            document.pop("workspace", None)
        history = document.get("workspace_history", [])
        if not isinstance(history, list):
            raise BridgeError("Workspace audit history must be a list.")
        document["workspace_history"] = [
            *history[-39:],
            {
                "actor": "operator",
                "at": time.time(),
                "before": before,
                "after": after,
                "previous_revision": current,
            },
        ]
        write_json(path, document)
        result["applied_revision"] = hashlib.sha256(
            json.dumps(document, sort_keys=True).encode()
        ).hexdigest()
    if not preview and not changed:
        result["applied_revision"] = current
    return result
