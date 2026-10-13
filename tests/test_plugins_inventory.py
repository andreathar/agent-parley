"""Checks professional plugin previews and truthful adapter JSON views."""

import json
import sys

from agent_parley import cli, plugins, roster, views


def test_install_preview_executes_no_native_command(tmp_path, monkeypatch):
    executable = tmp_path / "claude"
    monkeypatch.setattr(
        plugins, "detected", lambda: [(plugins.CLIENTS[0], str(executable))]
    )

    def refused(*args, **kwargs):
        raise AssertionError("Preview must not execute a native command")

    monkeypatch.setattr(plugins, "run", refused)
    result = plugins.preview()
    assert result["preview"] is True
    assert result["changed"] is False
    assert result["operations"][0]["adapter"] == "claude"
    assert result["operations"][0]["if_plugin_missing"] == [
        str(executable),
        *plugins.CLIENTS[0].install,
    ]


def test_every_adapter_reports_real_required_gaps_without_runtime_claim(
    monkeypatch,
):
    monkeypatch.setattr(
        plugins,
        "status",
        lambda: [
            f"claude: plugin {plugins.PLUGIN} installed",
            "codex: marketplace not added",
        ],
    )
    result = plugins.inspection()
    rows = {row["adapter"]: row for row in result["platforms"]}
    assert set(rows) == set(roster.ADAPTERS)
    assert rows["claude"]["configured"] is True
    assert rows["codex"]["configured"] is False
    assert rows["amp"]["launch_supported"] is False
    assert rows["amp"]["required_hook_gaps"] == ["SessionStart", "Stop"]
    assert rows["gemini"]["unavailable_hooks"] == ["PermissionRequest"]
    assert rows["opencode"]["unavailable_hooks"] == ["SessionEnd"]
    assert all(row["active"] is None for row in rows.values())
    assert all(row["verified"] is None for row in rows.values())


def test_status_json_uses_envelope_and_failure_exit(monkeypatch, capsys):
    monkeypatch.setattr(
        plugins, "status", lambda: ["claude: unknown (read timed out)"]
    )
    monkeypatch.setattr(
        sys, "argv", ["agent-parley", "plugins", "status", "--json"]
    )
    assert cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == views.SCHEMA
    assert result["kind"] == "plugins_status"
    assert result["warnings"][0]["adapter"] == "claude"


def test_install_preview_json_does_not_initialize_state(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "absent"
    monkeypatch.setattr(plugins, "detected", lambda: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(home),
            "plugins",
            "install",
            "--preview",
            "--json",
        ],
    )
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["preview"] is True
    assert result["operations"] == []
    assert not home.exists()


def test_native_status_probe_uses_short_timeout(monkeypatch):
    client = plugins.CLIENTS[0]
    monkeypatch.setattr(
        plugins, "detected", lambda: [(client, "/fixture/claude")]
    )
    timeouts = []

    def probe(executable, arguments, *, timeout):
        timeouts.append(timeout)
        return "agent-parley" if arguments == client.marketplaces else "[]"

    monkeypatch.setattr(plugins, "run", probe)
    plugins.status()
    assert timeouts == [plugins.READ_TIMEOUT, plugins.READ_TIMEOUT]
