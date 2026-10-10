import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from agent_parley import console
from agent_parley.state import BridgeError


@pytest.fixture
def served(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"port": 9999, "token": "operator-secret"})
    )
    httpd = console.serve(home, 0, console.token(home))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None, cookie="", csrf="", method="POST"):
    request = urllib.request.Request(base + path, method=method)
    if body is not None:
        request.data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", f"{console.COOKIE}={cookie}")
    if csrf:
        request.add_header("X-Parley-Csrf", csrf)
    try:
        with urllib.request.urlopen(request) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def sign_in(base, token="operator-secret"):
    request = urllib.request.Request(base + "/api/login", method="POST")
    request.data = json.dumps({"token": token}).encode()
    with urllib.request.urlopen(request) as answer:
        payload = json.loads(answer.read())
        raw = answer.headers.get("Set-Cookie", "")
    return raw.split(";")[0].split("=", 1)[1], payload["csrf"]


def test_token_requires_a_readable_credential(tmp_path):
    with pytest.raises(BridgeError, match="Cannot read"):
        console.token(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"port": 1}))
    with pytest.raises(BridgeError, match="Cannot read"):
        console.token(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"token": ""}))
    with pytest.raises(BridgeError, match="Invalid"):
        console.token(tmp_path)


def test_validate_refuses_shell_metacharacters_and_bad_shapes(tmp_path):
    with pytest.raises(BridgeError):
        console.validate("identifier", "a; rm -rf /")
    with pytest.raises(BridgeError):
        console.validate("identifier", "Uppercase")
    with pytest.raises(BridgeError):
        console.validate("number", "12a")
    with pytest.raises(BridgeError):
        console.validate("lease", "nothex")
    with pytest.raises(BridgeError):
        console.validate("path", str(tmp_path / "absent"))
    with pytest.raises(BridgeError):
        console.validate("text", "x" * 201)
    with pytest.raises(BridgeError):
        console.validate("identifier", "")
    assert console.validate("identifier", " codex ") == "codex"
    assert console.validate("lease", "a" * 32) == "a" * 32
    assert console.validate("path", str(tmp_path)) == str(tmp_path.resolve())


def test_build_renders_only_allowlisted_actions(tmp_path):
    with pytest.raises(BridgeError, match="Unknown action"):
        console.build("rm", {})
    argv = console.build(
        "participant-add",
        {"name": "codex", "provider": "codex", "repo": str(tmp_path)},
    )
    assert argv[1:3] == ["-m", "agent_parley.cli"]
    assert argv[3:5] == ["participant", "add"]
    assert "codex" in argv and "--provider" in argv
    assert "--credentials" not in argv


def test_build_refuses_a_missing_required_argument(tmp_path):
    with pytest.raises(BridgeError):
        console.build("participant-add", {"repo": str(tmp_path)})


def test_catalog_describes_every_action():
    catalog = console.catalog()
    assert {entry["action"] for entry in catalog} == set(console.ACTIONS)
    assert all(entry["group"] and entry["label"] for entry in catalog)


def test_sessions_expire_and_discard():
    sessions = console.Sessions()
    entry = sessions.create()
    assert sessions.read(entry["id"]) == entry
    assert sessions.read("absent") is None
    assert sessions.read(None) is None
    sessions.discard(entry["id"])
    assert sessions.read(entry["id"]) is None
    stale = sessions.create()
    stale["expires"] = 0
    assert sessions.read(stale["id"]) is None


def test_the_page_is_served_without_a_session(served):
    with urllib.request.urlopen(served + "/") as answer:
        body = answer.read().decode()
    assert answer.status == 200
    assert "Agent Parley console" in body


def test_an_unauthenticated_caller_reaches_nothing(served):
    assert call(served, "/api/snapshot")[0] == 401
    assert call(served, "/api/action", {"action": "doctor"})[0] == 401
    assert call(served, "/api/session", method="GET")[0] == 401


def test_a_wrong_token_is_refused_and_a_right_one_opens_a_session(served):
    status, payload = call(served, "/api/login", {"token": "wrong"})
    assert status == 401
    assert "refused" in payload["error"]
    cookie, csrf = sign_in(served)
    assert cookie and csrf
    status, payload = call(served, "/api/session", cookie=cookie, method="GET")
    assert status == 200
    assert payload["csrf"] == csrf


def test_a_session_without_its_request_token_cannot_act(served):
    cookie, csrf = sign_in(served)
    assert call(served, "/api/snapshot", {}, cookie)[0] == 403
    assert call(served, "/api/snapshot", {}, cookie, "wrong")[0] == 403
    assert call(served, "/api/snapshot", {}, cookie, csrf)[0] == 200


def test_an_unknown_action_and_route_are_refused(served):
    cookie, csrf = sign_in(served)
    status, payload = call(
        served, "/api/action", {"action": "rm", "params": {}}, cookie, csrf
    )
    assert status == 400
    assert "Unknown action" in payload["error"]
    assert call(served, "/api/nothing", {}, cookie, csrf)[0] == 404


def test_signing_out_ends_the_session(served):
    cookie, csrf = sign_in(served)
    assert call(served, "/api/logout", {}, cookie, csrf)[0] == 200
    assert call(served, "/api/snapshot", {}, cookie, csrf)[0] == 401


def test_a_malformed_body_is_refused(served):
    request = urllib.request.Request(served + "/api/login", method="POST")
    request.data = b"not json"
    try:
        with urllib.request.urlopen(request) as answer:
            status = answer.status
    except urllib.error.HTTPError as error:
        status = error.code
    assert status == 400


def test_serve_refuses_a_privileged_port(tmp_path):
    with pytest.raises(BridgeError, match="between 1024"):
        console.serve(tmp_path, 80, "secret")


def test_non_ascii_credentials_are_refused(served):
    assert call(served, "/api/login", {"token": "inválido"})[0] == 401
    cookie, _ = sign_in(served)
    assert call(served, "/api/snapshot", {}, cookie, "inválido")[0] == 403


def test_logout_requires_csrf_and_preserves_session_on_refusal(served):
    cookie, csrf = sign_in(served)
    assert call(served, "/api/logout", {}, cookie)[0] == 403
    assert call(served, "/api/session", cookie=cookie, method="GET")[0] == 200
    assert call(served, "/api/logout", {}, cookie, csrf)[0] == 200


@pytest.mark.parametrize("length", ["invalid", "-1"])
def test_invalid_content_length_is_refused(served, length):
    address = urllib.parse.urlsplit(served)
    connection = http.client.HTTPConnection(address.hostname, address.port)
    try:
        connection.request(
            "POST", "/api/login", headers={"Content-Length": length}
        )
        response = connection.getresponse()
        assert response.status == 400
        assert "length" in json.loads(response.read())["error"].lower()
    finally:
        connection.close()


def test_snapshot_timestamp_uses_utc(tmp_path, monkeypatch):
    expected = console.time.gmtime(0)
    monkeypatch.setattr(console.time, "gmtime", lambda: expected)
    monkeypatch.setattr(
        console, "run", lambda home, argv: {"stdout": "{}", "stderr": ""}
    )
    assert console.snapshot(tmp_path)["generated_at"] == "1970-01-01T00:00:00Z"


def test_commands_preserve_the_selected_host_registry(tmp_path, monkeypatch):
    resource_home = tmp_path / "isolated resources"
    monkeypatch.setenv("AGENT_PARLEY_RESOURCE_HOME", str(resource_home))
    result = console.run(
        tmp_path / "state",
        [
            console.sys.executable,
            "-c",
            "import os; print(os.getenv('AGENT_PARLEY_RESOURCE_HOME', ''))",
        ],
    )
    assert result["status"] == 0
    assert result["stdout"].strip() == str(resource_home)
