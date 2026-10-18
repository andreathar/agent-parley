"""Protects checkout, shared configuration and preflight read boundaries."""

import json
import sys
from pathlib import Path

import pytest

from agent_parley import cli, preflight, roster
from agent_parley.state import BridgeError, write_json
from scripts import install_dev


@pytest.mark.parametrize("operation", ["setup", "participant", "launch"])
def test_dirty_initial_registration_keeps_index_work_and_stash(
    bridge, repo, operation, monkeypatch
):
    (repo / "shared.txt").write_text("staged\n")
    cli.git(repo, "add", "shared.txt")
    (repo / "shared.txt").write_text("working\n")
    (repo / "untracked.txt").write_text("scratch\n")
    before = cli.git(repo, "diff", "--cached")
    stash = cli.git(repo, "stash", "list")
    if operation == "launch":
        monkeypatch.setattr(cli.shutil, "which", lambda name: sys.executable)
    with pytest.raises(BridgeError, match="uncommitted changes"):
        if operation == "setup":
            bridge.setup(repo)
        elif operation == "participant":
            bridge.add_participant(repo, "codex")
        else:
            bridge.launch("codex", repo, "Read only")
    assert cli.git(repo, "diff", "--cached") == before
    assert cli.git(repo, "stash", "list") == stash
    assert (repo / "shared.txt").read_text() == "working\n"
    assert (repo / "untracked.txt").read_text() == "scratch\n"
    assert not list((bridge.home / "projects").glob("*/project.json"))


@pytest.mark.parametrize("kind", ["provider", "credentials"])
def test_lane_registry_mutations_refuse_before_creating_state(
    tmp_path, monkeypatch, capsys, kind
):
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "private-lane-token")
    home = tmp_path / "absent"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(home),
            kind,
            "add",
            "other",
            *(
                ["--adapter", "codex", "--executable", "codex"]
                if kind == "provider"
                else []
            ),
        ],
    )
    assert cli.main() == 1
    assert "operator authority" in capsys.readouterr().err
    assert not home.exists()


def test_credential_api_refuses_before_creating_native_home(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_PARLEY_TOKEN", "lane")
    native = tmp_path / "native-account"
    with pytest.raises(BridgeError, match="operator authority"):
        roster.define_credential(tmp_path, "account", str(native), [], [])
    assert not native.exists()


def test_lane_directory_cannot_mutate_another_selected_home(
    bridge, repo, paired, monkeypatch, tmp_path
):
    monkeypatch.chdir(Path(paired["lanes"]["codex"]))
    monkeypatch.setenv("AGENT_PARLEY_HOME", str(bridge.home))
    with pytest.raises(BridgeError, match="operator authority"):
        roster.define_provider(tmp_path, "new", "codex", "codex", "", [], [])
    assert not (tmp_path / "providers.json").exists()


def test_operator_provider_edit_stays_inside_selected_home(bridge):
    roster.define_provider(bridge.home, "local", "codex", "codex", "", [], [])
    assert roster.provider(bridge.home, "local")["adapter"] == "codex"
    roster.remove(bridge.home, "provider", "local")
    assert "local" not in roster.providers(bridge.home)


def test_preflight_creates_no_state_and_does_not_disclose_native_commands(
    tmp_path, repo, monkeypatch
):
    native = tmp_path / "native"
    native.mkdir()
    (native / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"hooks": [{"command": "private-command-secret"}]}]
                },
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(native))
    monkeypatch.setenv(
        "AGENT_PARLEY_RESOURCE_HOME", str(tmp_path / "resources")
    )
    home = tmp_path / "absent"
    index = (repo / ".git/index").read_bytes()
    report = preflight.inspect(home, repo)
    assert report["read_only"] is True
    assert report["clients"][0]["configuration_files"][1]["hook_groups"] == {
        "Stop": 1
    }
    assert "private-command-secret" not in json.dumps(report)
    assert not home.exists()
    assert not (tmp_path / "resources").exists()
    assert (repo / ".git/index").read_bytes() == index


def test_preflight_saved_port_wins_over_invalid_environment(
    bridge, repo, monkeypatch
):
    monkeypatch.setenv("AGENT_PARLEY_PORT", "not-a-port")
    report = preflight.inspect(bridge.home, repo)
    assert report["port"] == bridge.config["port"]


def test_preflight_reports_opencode_default_configuration_path(
    bridge, repo, monkeypatch
):
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)

    report = preflight.inspect(bridge.home, repo, "opencode")

    assert Path(report["clients"][0]["config_home"]) == (
        Path.home() / ".config" / "opencode"
    )


@pytest.mark.parametrize("command", ["preflight", "host-resources"])
def test_read_only_cli_does_not_initialize_a_missing_home(
    tmp_path, repo, monkeypatch, capsys, command
):
    home = tmp_path / "uninitialized"
    arguments = (
        ["preflight", "--repo", str(repo), "--json"]
        if command == "preflight"
        else ["host-resources", "list", "--json"]
    )
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "--home", str(home), *arguments]
    )
    assert cli.main() == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert not home.exists()
    assert not (tmp_path / "agent_parley_resource_home").exists()


def test_preflight_reports_missing_environment_without_launching(bridge, repo):
    roster.define_provider(
        bridge.home,
        "custom",
        "codex",
        "codex",
        "CODEX_HOME",
        [],
        ["MISSING_TEST_KEY"],
    )
    report = preflight.inspect(bridge.home, repo, "custom")
    assert report["clients"][0]["required_environment"] == {
        "MISSING_TEST_KEY": False
    }


def test_preflight_uses_home_and_project_supervision_overrides(bridge, repo):
    bridge.setup(repo)
    _, directory = bridge.project(repo)
    data = json.loads((directory / "project.json").read_text())
    data["supervision"] = {"wake": True, "interval": 90}
    write_json(directory / "project.json", data)
    write_json(bridge.home / "supervision.json", {"wake": False})
    report = preflight.inspect(bridge.home, repo)
    assert report["effective_supervision"]["wake"] is False
    assert report["effective_supervision"]["interval"] == 90


def test_dev_installer_refuses_daily_state_before_writing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        install_dev.start, "DEFAULT_HOME", str(tmp_path / "daily")
    )
    with pytest.raises(BridgeError, match="separate from daily"):
        install_dev.install(
            tmp_path,
            tmp_path / "build",
            tmp_path / "daily",
            tmp_path / "agent-parley-dev",
            8877,
        )
    assert not (tmp_path / "build").exists()


def test_dev_installer_refuses_mismatched_saved_port(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    write_json(state / "config.json", {"port": 8876})
    with pytest.raises(BridgeError, match="already uses port"):
        install_dev.install(
            tmp_path / "source",
            tmp_path / "build",
            state,
            tmp_path / "agent-parley-dev",
            8877,
        )
    assert not (tmp_path / "build").exists()


def test_wiring_reports_unregistered_project_without_creating_state(
    tmp_path, repo
):
    from agent_parley import wiring

    home = tmp_path / "absent"
    report = wiring.inspect(home, repo, ["NAME"])
    assert report["ok"] is False
    assert not home.exists()
    assert any(
        item["name"] == "registered" and item["state"] == "fail"
        for item in report["checks"]
    )


def test_wiring_url_binding_is_read_from_codex_and_claude_arguments(tmp_path):
    from agent_parley import wiring

    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"agent_parley": {"url": "http://x:1/"}}})
    )
    codex = ["-c", 'mcp_servers.agent_parley.url="http://127.0.0.1:9/mcp/"']
    assert wiring._bound_url(codex) == "http://127.0.0.1:9/mcp/"
    assert wiring._bound_url(["--mcp-config", str(config)]) == "http://x:1/"
    assert wiring._bound_url(["other"]) is None
