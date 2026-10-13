"""Exercises stable CLI envelopes, preview defaults and actionable failures."""

import json
import sys

import pytest

from agent_parley import cli, roster, views


def run(monkeypatch, capsys, home, *arguments):
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(home), *arguments]
    )
    status = cli.main()
    output = capsys.readouterr()
    return status, json.loads(output.out), output.err


@pytest.mark.parametrize("action", ["list", "status", "configure"])
def test_agents_help_explains_the_selected_verb(monkeypatch, capsys, action):
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "agents", action, "--help"]
    )
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert "usage: agent-parley agents" in capsys.readouterr().out


def test_empty_inventory_does_not_initialize_state(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "absent"
    status, result, error = run(
        monkeypatch, capsys, home, "agents", "list", "--json"
    )
    assert status == 0
    assert not error
    assert result["schema"] == views.SCHEMA
    assert result["kind"] == "agents_list"
    assert result["projects"] == []
    assert not home.exists()


@pytest.mark.parametrize("arguments", [("run",), ("credentials", "add")])
def test_profile_help_explains_copilot_isolation(
    monkeypatch, capsys, arguments
):
    monkeypatch.setattr(sys, "argv", ["agent-parley", *arguments, "--help"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert "Copilot" in capsys.readouterr().out


def test_preview_apply_retry_preserves_routing_and_audits_once(
    paired, bridge, repo, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]
    original = roster.read(directory)["participants"]["claude"]["display"]
    arguments = (
        "agents",
        "configure",
        "claude",
        "--repo",
        str(repo),
        "--display-name",
        "Reviewer one",
        "--json",
    )
    status, preview, _ = run(monkeypatch, capsys, bridge.home, *arguments)
    assert status == 0 and preview["preview"]
    assert (
        "display_name" not in roster.read(directory)["participants"]["claude"]
    )
    apply = (*arguments, "--apply", "--expected-revision", preview["revision"])
    status, applied, _ = run(monkeypatch, capsys, bridge.home, *apply)
    assert status == 0 and not applied["preview"]
    before_retry = (directory / "project.json").read_bytes()
    assert run(monkeypatch, capsys, bridge.home, *apply)[0] == 0
    assert (directory / "project.json").read_bytes() == before_retry
    entry = roster.read(directory)["participants"]["claude"]
    assert entry["display"] == original
    assert entry["display_name"] == "Reviewer one"


def test_invalid_role_and_missing_revision_use_shared_error_contract(
    paired, bridge, repo, monkeypatch, capsys
):
    arguments = (
        "agents",
        "configure",
        "claude",
        "--repo",
        str(repo),
        "--display-name",
        "Reviewer",
        "--json",
    )
    for extra in (("--role", "unknown"), ("--apply",)):
        status, result, error = run(
            monkeypatch, capsys, bridge.home, *arguments, *extra
        )
        assert status == 1
        assert result["schema"] == views.SCHEMA
        assert result["kind"] == "error"
        assert result["error"]["type"] == "bridge"
        assert error.startswith("agent-parley:")


def test_unknown_project_has_actionable_error(tmp_path, monkeypatch, capsys):
    status, result, error = run(
        monkeypatch,
        capsys,
        tmp_path / "state",
        "agents",
        "status",
        "--repo",
        str(tmp_path),
        "--json",
    )
    assert status == 1
    assert "agents list" in result["error"]["message"]
    assert "registered" in error


def test_partial_inventory_exits_nonzero_and_retains_readable_results(
    tmp_path, monkeypatch, capsys
):
    from agent_parley import agents

    monkeypatch.setattr(
        agents,
        "inventory",
        lambda home: {
            "projects": [],
            "warnings": [{"project": "/fixture", "error": "unavailable"}],
        },
    )
    status, result, _ = run(
        monkeypatch, capsys, tmp_path, "agents", "list", "--json"
    )
    assert status == 1
    assert result["kind"] == "agents_list"
    assert result["warnings"][0]["project"] == "/fixture"
