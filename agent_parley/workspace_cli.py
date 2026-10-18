"""Declares preview-first workspace and read-only media commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_parley import workspace


def declare(commands: Any) -> None:
    """Adds media diagnostics and future workspace profile configuration."""
    for group in ("workspace", "media"):
        parser = commands.add_parser(
            group, help="Inspect shared media roots and lightweight workspaces."
        )
        actions = parser.add_subparsers(dest="action", required=True)
        for action in (
            ("status", "doctor") if group == "media" else ("status", "plan")
        ):
            sub = actions.add_parser(
                action,
                help="Read paths and evidence; never hydrate or grant access.",
            )
            sub.add_argument("--repo", type=Path, default=Path.cwd())
            sub.add_argument("--json", action="store_true")
            if action == "doctor":
                sub.add_argument(
                    "--sample",
                    help="Relative media path for a bounded host read probe.",
                )
        if group == "workspace":
            sub = actions.add_parser(
                "config",
                help="Preview a future-lane profile; no existing lanes change.",
            )
            sub.add_argument("--repo", type=Path, default=Path.cwd())
            sub.add_argument(
                "--catalog",
                default="",
                help="Existing catalog path relative to the canonical repo.",
            )
            sub.add_argument(
                "--disable",
                action="store_true",
                help="Use full checkout for future lanes.",
            )
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument(
                "--preview", action="store_true", help="Preview only (default)."
            )
            mode.add_argument("--apply", action="store_true")
            sub.add_argument("--expected-revision")
            sub.add_argument("--json", action="store_true")


def execute(home: Path, args: argparse.Namespace) -> dict:
    """Executes configuration or bounded diagnostics without starting state."""
    if args.action == "config":
        return workspace.configure(
            home,
            args.repo,
            args.catalog,
            preview=not args.apply,
            expected_revision=args.expected_revision,
            disable=args.disable,
        )
    result = workspace.status(
        home, args.repo, sample=getattr(args, "sample", None)
    )
    if args.command == "media" and args.action == "doctor":
        result["warnings"].append(
            "Native media access is unverified. Confirm a harmless read "
            "through the chosen native client with existing permissions; "
            "a host probe is not native proof. Read-only enforcement is "
            "unavailable; do not broaden access to bypass a refusal."
        )
    if args.action == "plan":
        result["excluded_paths"] = workspace.exclusions(Path(result["repo"]))
        result["exclusion_source"] = (
            "current index; each new lane recomputes its base exclusions"
        )
        result["materialization"] = (
            "no-checkout, per-worktree sparse exclusions, skip LFS smudge"
        )
    return result


def render(result: dict) -> str:
    """Keeps text diagnostics as complete as their stable JSON equivalent."""
    return json.dumps(result, indent=2, ensure_ascii=False)
