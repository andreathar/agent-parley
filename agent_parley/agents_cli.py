"""Shares agent inventory and configuration with the operator console."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_parley.state import BridgeError


def declare(commands: Any) -> None:
    """Declares agent views and preview-first operator configuration.

    Args:
        commands: Lazy root command index used by the existing CLI.
    """
    command = commands.add_parser(
        "agents", help="Inspect agents or preview names and function choices."
    )
    actions = command.add_subparsers(dest="action", required=True)
    for name in ("list", "status"):
        viewing = actions.add_parser(
            name, help="Read agents, task receipts and adapter capabilities."
        )
        viewing.add_argument("--repo", type=Path)
        viewing.add_argument("--json", action="store_true")
    configuring = actions.add_parser(
        "configure",
        help="Preview a name and function preference; no grants change.",
    )
    configuring.add_argument("participant", help="Stable participant ID.")
    configuring.add_argument("--repo", type=Path, default=Path.cwd())
    configuring.add_argument("--display-name", required=True)
    configuring.add_argument(
        "--role", help="Existing function ID; omitted keeps the preference."
    )
    mode = configuring.add_mutually_exclusive_group()
    mode.add_argument(
        "--preview", action="store_true", help="Preview only (the default)."
    )
    mode.add_argument(
        "--apply", action="store_true", help="Apply the reviewed preview."
    )
    configuring.add_argument(
        "--expected-revision",
        help="Revision returned by preview; required to apply safely.",
    )
    configuring.add_argument("--json", action="store_true")


def execute(home: Path, args: argparse.Namespace) -> dict:
    """Executes the same validated operations the console exposes.

    Args:
        home: Selected private state home.
        args: Parsed agents command arguments.

    Returns:
        Structured inventory or preview/apply result.

    Raises:
        BridgeError: If the project, participant or edited input is invalid.
    """
    from agent_parley import agents

    if args.action == "configure":
        return agents.configure(
            home,
            args.repo,
            args.participant,
            args.display_name,
            role=args.role,
            preview=not args.apply,
            expected_revision=args.expected_revision,
        )
    result = agents.inventory(home)
    if args.repo is not None:
        root = str(args.repo.expanduser().resolve())
        result["projects"] = [
            project for project in result["projects"] if project["root"] == root
        ]
        if not result["projects"]:
            raise BridgeError(
                f"No registered project for {root}; use agents list --json "
                "to select a registered root."
            )
    return result


def render(result: dict) -> str:
    """Formats inventory or a concrete configuration preview as text.

    Args:
        result: Shared structured agent operation result.

    Returns:
        Human-readable text with identity, availability and evidence limits.
    """
    if "projects" not in result:
        return json.dumps(result, indent=2, ensure_ascii=False)
    lines = []
    for project in result["projects"]:
        lines.append(project["root"])
        for agent in project["agents"]:
            availability = agent["availability"]
            lines.append(
                f"  {agent['id']}: {agent['display_name']} "
                f"({agent['provider']}) — {availability['state']}; "
                f"{availability.get('freshness', 'unknown')}"
            )
    for warning in result.get("warnings", []):
        lines.append(
            f"Warning: {warning['project']}: {warning['error']} "
            "Use agent-parley doctor --json to check state access."
        )
    if not lines:
        lines.append("No registered projects; use agent-parley setup --help.")
    lines.append(
        "Use --json for hooks, skills, tasks and configuration revisions. "
        "File detection does not prove activation; ACK means review, "
        "not task acceptance or completion."
    )
    return "\n".join(lines)
