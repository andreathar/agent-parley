"""Reads launch scope and configuration without initializing any state.

Native files are observations, not a reconstruction of native policy
precedence. CLI flags, enterprise policy, project trust and authentication
remain the native client's responsibility. No client or service is launched.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

from agent_parley import host_resources, protocol, roster, start, supervision
from agent_parley.state import BridgeError


def _document(path: Path) -> dict:
    """Reads a JSON or TOML object, preserving invalid-file diagnostics."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            value = (
                tomllib.load(stream)
                if path.suffix == ".toml"
                else json.load(stream)
            )
        if isinstance(value, dict):
            return value
    except (OSError, ValueError) as error:
        raise BridgeError(f"Cannot read configuration at {path}.") from error
    raise BridgeError(f"Expected a configuration object at {path}.")


def _native(path: Path) -> dict:
    """Reports only policy selections and hook counts, never hook commands."""
    data = _document(path)
    hooks = data.get("hooks", {})
    permissions = data.get("permissions", {})
    return {
        "path": str(path),
        "exists": path.exists(),
        "policy": {
            key: data[key]
            for key in ("approval_policy", "sandbox_mode", "model")
            if isinstance(data.get(key), str)
        },
        "hook_groups": {
            key: len(value)
            for key, value in (hooks.items() if isinstance(hooks, dict) else ())
            if isinstance(value, list)
        },
        "permissions_configured": bool(data.get("permissions")),
        "permission_counts": {
            key: len(value)
            for key, value in (
                permissions.items() if isinstance(permissions, dict) else ()
            )
            if key in {"allow", "deny", "ask"} and isinstance(value, list)
        },
        "hook_controls": {
            key: data[key]
            for key in ("disableAllHooks", "allowManagedHooksOnly")
            if isinstance(data.get(key), bool)
        },
        "mcp_servers": sorted(data.get("mcp_servers", {})),
        "parley_plugins": {
            name: {"enabled": entry.get("enabled")}
            for name, entry in data.get("plugins", {}).items()
            if "parley" in name and isinstance(entry, dict)
        },
    }


def _client(home: Path, provider: str, profile: str | None, repo: Path) -> dict:
    """Reads the selected native account scope and installed plugin records."""
    entry = roster.provider(home, provider)
    adapter = entry["adapter"]
    variable = entry.get("home_env", "")
    selected = roster.config_home(home, entry, profile)
    account = roster.credential(home, profile) if profile else {}
    environment = {**entry.get("env", {}), **account.get("env", {})}
    selected = (
        selected or environment.get(variable) or os.environ.get(variable) or ""
    )
    base = Path(selected or f"~/.{adapter}").expanduser().resolve()
    filenames = (
        (
            base / "config.toml",
            base / "hooks.json",
            repo / ".codex/config.toml",
            repo / ".codex/hooks.json",
        )
        if adapter == "codex"
        else (
            base / "settings.json",
            repo / ".claude/settings.json",
            repo / ".claude/settings.local.json",
        )
    )
    installed = _document(base / "plugins/installed_plugins.json")
    records = installed.get("plugins", {})
    provenance = []
    if isinstance(records, dict):
        for name, entries in records.items():
            if "parley" not in name or not isinstance(entries, list):
                continue
            for record in entries:
                if isinstance(record, dict):
                    provenance.append(
                        {
                            "name": name,
                            **{
                                key: record[key]
                                for key in ("version", "scope", "installPath")
                                if key in record
                            },
                        }
                    )
    return {
        "provider": provider,
        "adapter": adapter,
        "executable": shutil.which(entry["command"]),
        "credential_profile": profile,
        "config_home": str(base),
        "account_scope": "profile" if profile else "inherited native account",
        "required_environment": {
            name: bool(os.environ.get(name))
            for name in sorted(
                set(
                    entry.get("require_env", [])
                    + account.get("require_env", [])
                )
            )
        },
        "configuration_files": [_native(path) for path in filenames]
        if adapter in {"codex", "claude"}
        else [],
        "plugin_records": provenance,
        "native_effective_policy": "not evaluated; native CLI owns precedence",
        "hook_composition": "not runtime verified; launch injects Parley hooks",
    }


def inspect(
    home: Path,
    repo: Path,
    provider: str | None = None,
    profile: str | None = None,
) -> dict:
    """Reports scope, settings and conflicts using local read-only evidence."""
    home, repo = home.expanduser().resolve(), repo.expanduser().resolve()
    manifest: dict = {}
    common_root = repo
    try:
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(repo),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.startswith("worktree "):
            common_root = Path(result.stdout.split("\0", 1)[0][9:]).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    for path in (home / "projects").glob("*/project.json"):
        data = _document(path)
        if data.get("root") == str(common_root):
            manifest = roster.normalize(data)
            break
    config = _document(home / "config.json")
    port = (
        config["port"]
        if "port" in config
        else int(os.environ.get("AGENT_PARLEY_PORT", "8876"))
    )
    if type(port) is not int or not 1024 <= port <= 65535:
        raise BridgeError("Configured port must be between 1024 and 65535.")
    with socket.socket() as probe:
        probe.settimeout(0.2)
        occupied = probe.connect_ex(("127.0.0.1", port)) == 0
    repository, dirty = start.checkout(common_root)
    warnings = []
    if dirty and not manifest:
        warnings.append(
            "Initial registration will refuse pending work; setup --stash "
            "is an explicit opt-in."
        )
    if occupied and not start.serving(home):
        warnings.append(
            "The selected port is occupied without a matching service record."
        )
    if not _document(home / "supervision.json"):
        warnings.append(
            "No home supervision overrides: built-in defaults apply."
        )
    clients = [
        _client(home, name, profile, common_root)
        for name in ([provider] if provider else ["codex", "claude"])
    ]
    return {
        "read_only": True,
        "launcher": {
            "python": sys.executable,
            "module": str(Path(__file__).resolve().parent),
            "version": protocol.launcher_version(),
            "revision": protocol.revision(),
        },
        "state_home": str(home),
        "state_exists": home.exists(),
        "port": port,
        "port_source": "saved configuration"
        if "port" in config
        else "environment/default",
        "port_occupied": occupied,
        "service_process_present": start.serving(home),
        "repository": {
            "path": str(common_root),
            "is_git": repository,
            "dirty": dirty,
            "registered": bool(manifest),
        },
        "project_settings": {
            key: manifest.get(key)
            for key in (
                "base",
                "branch_prefix",
                "verify",
                "initialize",
                "resources",
                "deadlines",
                "budget",
                "forge",
                "approval",
                "pull_request",
                "integration",
                "supervision",
            )
        },
        "effective_supervision": supervision.configuration(home, manifest),
        "clients": clients,
        "host_resources": host_resources.inspect(),
        "warnings": warnings,
        "evidence_limit": (
            "Local files and process/port observations; no model session "
            "or loaded-plugin proof."
        ),
    }
