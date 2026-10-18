"""Checks native-client discovery separately from observable MCP bindings."""

import json

import pytest

from agent_parley import wiring


def process(arguments=None, environment=None):
    return {
        "pid": 123,
        "arguments": arguments or ["opencode", "--session", "ses_one"],
        "env": environment or {},
    }


def test_opencode_mcp_binding_is_read_from_private_config(tmp_path):
    (tmp_path / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "agent_parley": {
                        "type": "remote",
                        "url": "http://127.0.0.1:8876/mcp/",
                        "headers": {"Authorization": "secret-value"},
                    }
                }
            }
        )
    )
    state, url, detail = wiring._binding(
        process(environment={"OPENCODE_CONFIG_DIR": str(tmp_path)})
    )
    assert state == "configured"
    assert url == "http://127.0.0.1:8876/mcp/"
    assert "secret-value" not in detail


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "unknown"),
        ("not json", "unknown"),
        ("[]", "unknown"),
        ('{"mcp":[]}', "unknown"),
        ('{"mcp":{}}', "absent"),
        ('{"mcp":{"agent_parley":{"enabled":false}}}', "absent"),
        ('{"mcp":{"agent_parley":{"type":"local"}}}', "unknown"),
    ],
)
def test_binding_unknown_is_distinct_from_absent(tmp_path, content, expected):
    if content is not None:
        (tmp_path / "opencode.json").write_text(content)
    state, url, _ = wiring._binding(
        process(environment={"OPENCODE_CONFIG_DIR": str(tmp_path)})
    )
    assert state == expected
    assert url is None


def test_native_client_without_url_or_token_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "_processes", lambda _: [process()])
    checks = wiring._participant(
        tmp_path, "opencode", {"lane": str(tmp_path)}, 8876, []
    )
    assert any(
        item["name"] == "pid 123 process" and item["state"] == "pass"
        for item in checks
    )
    assert any(
        item["name"] == "pid 123 mcp url" and item["state"] == "unknown"
        for item in checks
    )
    assert "No running lane client found" not in json.dumps(checks)
    assert any(
        item["name"] == "pid 123 state home" and item["state"] == "unknown"
        for item in checks
    )


def test_child_shell_with_token_is_not_mistaken_for_native_client(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        wiring,
        "_processes",
        lambda _: [process(["bash"], {"AGENT_PARLEY_TOKEN": "redacted"})],
    )
    checks = wiring._participant(
        tmp_path, "opencode", {"lane": str(tmp_path)}, 8876, []
    )
    assert checks[-1]["name"] == "process"
    assert checks[-1]["state"] == "warn"


@pytest.mark.parametrize("url", ["http://127.0.0.1:bad/", "http://x:1/"])
def test_invalid_or_wrong_endpoint_is_reported_without_credentials(
    tmp_path, monkeypatch, url
):
    arguments = ["codex", "-c", f'mcp_servers.agent_parley.url="{url}"']
    monkeypatch.setattr(wiring, "_processes", lambda _: [process(arguments)])
    checks = wiring._participant(
        tmp_path, "codex", {"lane": str(tmp_path)}, 8876, []
    )
    assert any(
        item["name"] == "pid 123 mcp url" and item["state"] == "fail"
        for item in checks
    )


@pytest.mark.parametrize(
    ("variable", "filename", "key"),
    [
        ("GEMINI_CLI_SYSTEM_SETTINGS_PATH", "settings.json", "mcpServers"),
        ("COPILOT_HOME", "mcp-config.json", "mcpServers"),
        ("AMP_SETTINGS_FILE", "settings.json", "amp.mcpServers"),
    ],
)
def test_other_file_based_adapter_bindings(tmp_path, variable, filename, key):
    path = tmp_path / filename
    path.write_text(
        json.dumps(
            {
                key: {
                    "agent_parley": {
                        "httpUrl"
                        if variable == "GEMINI_CLI_SYSTEM_SETTINGS_PATH"
                        else "url": "http://127.0.0.1:8/"
                    }
                }
            }
        )
    )
    configured = str(tmp_path) if variable == "COPILOT_HOME" else str(path)
    state, url, _ = wiring._binding(process(environment={variable: configured}))
    assert state == "configured"
    assert url == "http://127.0.0.1:8/"


def test_unreadable_command_line_config_is_unknown(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{invalid")
    state, _, detail = wiring._binding(
        process(["claude", "--mcp-config", str(path)])
    )
    assert state == "unknown"
    assert "Cannot inspect" in detail


def test_url_credentials_and_query_are_not_reported(tmp_path, monkeypatch):
    url = "http://user:secret-value@127.0.0.1:8876/mcp/?token=secret-value"
    arguments = ["codex", "-c", f'mcp_servers.agent_parley.url="{url}"']
    monkeypatch.setattr(wiring, "_processes", lambda _: [process(arguments)])
    checks = wiring._participant(
        tmp_path, "codex", {"lane": str(tmp_path)}, 8876, []
    )
    assert "secret-value" not in json.dumps(checks)
    assert any(
        item["name"] == "pid 123 mcp url" and item["state"] == "pass"
        for item in checks
    )
