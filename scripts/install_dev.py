"""Installs an immutable development snapshot under a distinct command.

The normal tool installation and native configuration files are never
edited. A new snapshot is built on each invocation; older snapshots remain
available for rollback. A running development service must be stopped before
switching its launcher so one state home never serves mixed source versions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_parley import roster, start
from agent_parley.state import BridgeError, write_json, write_text


def install(
    root: Path, destination: Path, state: Path, command: Path, port: int
) -> dict:
    """Builds and installs a private snapshot, publishing its launcher last."""
    destination, state, command = (
        path.expanduser().resolve() for path in (destination, state, command)
    )
    roster.require_operator(state, "Installing the development snapshot")
    daily = Path(start.DEFAULT_HOME).expanduser().resolve()
    if state == daily or daily in state.parents or state in daily.parents:
        raise BridgeError(
            "Development state must be separate from daily state."
        )
    if state == root or root in state.parents:
        raise BridgeError("Development state must stay outside the repository.")
    if command.name != "agent-parley-dev":
        raise BridgeError(
            "The development command must be named agent-parley-dev."
        )
    if not 1024 <= port <= 65535 or port == 8876:
        raise BridgeError(
            "Choose a development port other than daily port 8876."
        )
    saved = state / "config.json"
    if saved.exists():
        try:
            saved_port = json.loads(saved.read_text())["port"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BridgeError(
                "Cannot read the saved development port."
            ) from error
        if saved_port != port:
            raise BridgeError(
                f"Development state already uses port {saved_port}; "
                "select that port explicitly or choose a new state home."
            )
    if start.serving(state):
        raise BridgeError(
            "Stop the development service before replacing its snapshot."
        )
    if (
        command.exists()
        and "Parley development snapshot" not in command.read_text()
    ):
        raise BridgeError(
            f"Refusing to replace an unrelated command: {command}."
        )
    for path in (destination, state):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.stat().st_mode & 0o077:
            raise BridgeError(f"Development directory must be private: {path}.")
    uv = shutil.which("uv")
    if uv is None:
        raise BridgeError("Install uv before building a development snapshot.")
    snapshot = Path(tempfile.mkdtemp(prefix="snapshot-", dir=destination))
    wheels = snapshot / "wheels"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(wheels)], cwd=root, check=True
    )
    wheel = next(wheels.glob("*.whl"))
    environment = snapshot / "venv"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)], check=True
    )
    python = environment / "bin/python"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            str(wheel),
        ],
        check=True,
    )
    revision = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "from agent_parley import protocol; print(protocol.revision())",
        ],
        cwd=snapshot,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    receipt = {
        "source": str(root),
        "snapshot": str(snapshot),
        "python": str(python),
        "state_home": str(state),
        "port": port,
        "command": str(command),
        "revision": revision,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "previous_launcher": command.read_text() if command.exists() else None,
    }
    wrapper = (
        "#!/bin/sh\n"
        "# Parley development snapshot; installed by scripts/install_dev.py.\n"
        'for argument in "$@"; do\n'
        '  case "$argument" in\n'
        "    --h|--ho|--hom|--home|--h=*|--ho=*|--hom=*|--home=*)\n"
        "      echo 'Development state is pinned.' >&2; "
        "exit 2;;\n"
        "  esac\n"
        "done\n"
        'if [ "${1-}" = plugins ] && [ "${2-}" = install ]; then\n'
        "  echo 'Development does not install user-global plugins.' >&2\n"
        "  exit 2\n"
        "fi\n"
        f"export AGENT_PARLEY_HOME={shlex.quote(str(state))}\n"
        f"export AGENT_PARLEY_PORT={port}\n"
        f'exec {shlex.quote(str(python))} -I -m agent_parley.cli "$@"\n'
    )
    overrides = state / "supervision.json"
    if not overrides.exists():
        write_json(
            overrides, {"wake": False, "prompts": False, "reclaim": False}
        )
    write_json(snapshot / "receipt.json", receipt)
    command.parent.mkdir(parents=True, exist_ok=True)
    write_text(command, wrapper)
    command.chmod(0o700)
    write_json(destination / "current.json", receipt)
    return receipt


def main() -> int:
    """Installs an explicitly separate development launcher and state scope."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("~/.local/share/agent-parley-dev"),
    )
    parser.add_argument(
        "--state", type=Path, default=Path("~/.local/state/agent-parley-dev")
    )
    parser.add_argument(
        "--command", type=Path, default=Path("~/.local/bin/agent-parley-dev")
    )
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                install(
                    Path(__file__).resolve().parents[1],
                    args.destination,
                    args.state,
                    args.command,
                    args.port,
                ),
                indent=2,
            )
        )
    except (BridgeError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
