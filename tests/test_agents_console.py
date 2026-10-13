import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from agent_parley import console, roles
from agent_parley.state import BridgeError


@pytest.fixture
def agent_console(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    httpd = console.serve(home, 0, "operator-secret")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield home, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def request(base, route, payload=None, cookie="", csrf=""):
    req = urllib.request.Request(
        base + route,
        data=json.dumps(payload or {}).encode(),
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie,
            "X-Parley-Csrf": csrf,
        },
        method="POST",
    )
    try:
        answer = urllib.request.urlopen(req)
    except urllib.error.HTTPError as error:
        answer = error
    with answer:
        return answer.status, json.loads(answer.read()), answer.headers


def credentials(base):
    status, payload, headers = request(
        base, "/api/login", {"token": "operator-secret"}
    )
    assert status == 200
    return headers["Set-Cookie"].split(";")[0], payload["csrf"]


@pytest.mark.parametrize("route", ["/api/agents", "/api/agents/configure"])
def test_inventory_and_configuration_require_cookie_and_csrf(
    agent_console, route
):
    _, base = agent_console
    assert request(base, route)[0] == 401
    cookie, csrf = credentials(base)
    assert request(base, route, cookie=cookie)[0] == 403
    assert request(base, route, cookie=cookie, csrf="wrong")[0] == 403


def test_inventory_uses_shared_read_only_source(agent_console, monkeypatch):
    home, base = agent_console
    calls = []

    def inventory(selected_home):
        calls.append(selected_home)
        return {"schema_version": 1, "projects": [], "read_only": True}

    monkeypatch.setattr(console.agents, "inventory", inventory)
    cookie, csrf = credentials(base)
    status, payload, _ = request(base, "/api/agents", {}, cookie, csrf)
    assert status == 200 and payload["read_only"] is True
    assert calls == [home]


@pytest.mark.parametrize(
    "changes",
    [
        {"participant": 123},
        {"display_name": []},
        {"role": []},
        {"preview": "false"},
        {"expected_revision": 123},
        {"preview": False},
        {"unsupported": "field"},
    ],
)
def test_configuration_validates_envelope_before_dispatch(
    agent_console, monkeypatch, changes
):
    _, base = agent_console
    calls = []
    monkeypatch.setattr(
        console.agents, "configure", lambda *a, **k: calls.append((a, k))
    )
    cookie, csrf = credentials(base)
    body = {
        "repo": "/repo",
        "participant": "opencode",
        "display_name": "Review",
    }
    body.update(changes)
    status, payload, _ = request(
        base, "/api/agents/configure", body, cookie, csrf
    )
    assert status == 400 and payload["error"]
    assert calls == []


def test_configuration_preview_and_apply_share_revision_contract(
    agent_console, monkeypatch
):
    home, base = agent_console
    calls = []

    def configure(selected_home, repo, participant, display_name, **kwargs):
        calls.append((selected_home, repo, participant, display_name, kwargs))
        return {"preview": kwargs["preview"], "revision": "r1"}

    monkeypatch.setattr(console.agents, "configure", configure)
    cookie, csrf = credentials(base)
    body = {
        "repo": "/repo",
        "participant": "opencode",
        "display_name": "Reviewer",
        "role": "readonly",
    }
    assert request(base, "/api/agents/configure", body, cookie, csrf)[0] == 200
    body.update(preview=False, expected_revision="r1")
    assert request(base, "/api/agents/configure", body, cookie, csrf)[0] == 200
    assert calls[0][0] == home
    assert str(calls[0][1]) == "/repo"
    assert calls[0][2:4] == ("opencode", "Reviewer")
    assert calls[0][4]["preview"] is True
    assert calls[1][4]["expected_revision"] == "r1"


def test_configuration_validation_and_revision_conflicts_are_actionable(
    agent_console, monkeypatch
):
    _, base = agent_console
    cookie, csrf = credentials(base)
    body = {
        "repo": "/repo",
        "participant": "opencode",
        "display_name": "Review",
    }

    def invalid(*args, **kwargs):
        raise BridgeError("Unknown role; choose a role from the catalog.")

    monkeypatch.setattr(console.agents, "configure", invalid)
    assert request(base, "/api/agents/configure", body, cookie, csrf)[0] == 400

    def conflict(*args, **kwargs):
        raise BridgeError(
            "Configuration changed; preview again before applying."
        )

    monkeypatch.setattr(console.agents, "configure", conflict)
    status, payload, _ = request(
        base, "/api/agents/configure", body, cookie, csrf
    )
    assert status == 409 and "preview again" in payload["error"]


def test_http_preview_apply_preserves_id_grants_and_prevents_stale_edit(
    bridge, repo, paired
):
    directory = bridge.project(repo)[1]
    roles.save(directory, {"version": 1, "roles": {"review": {"write": False}}})
    httpd = console.serve(bridge.home, 0, "operator-secret")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        cookie, csrf = credentials(base)
        status, inventory, _ = request(base, "/api/agents", {}, cookie, csrf)
        assert status == 200
        assert inventory["projects"][0]["roles"][0]["id"] == "review"
        body = {
            "repo": str(repo),
            "participant": "codex",
            "display_name": "Peer reviewer",
            "role": "review",
        }
        status, preview, _ = request(
            base, "/api/agents/configure", body, cookie, csrf
        )
        assert status == 200 and preview["preview"] is True
        body.update(preview=False, expected_revision=preview["revision"])
        status, result, _ = request(
            base, "/api/agents/configure", body, cookie, csrf
        )
        assert status == 200 and result["operational_role_changed"] is False
        _, repeated, _ = request(
            base, "/api/agents/configure", body, cookie, csrf
        )
        assert repeated["changed"] is False
        body["display_name"] = "Another reviewer"
        assert (
            request(base, "/api/agents/configure", body, cookie, csrf)[0] == 409
        )
        _, inventory, _ = request(base, "/api/agents", {}, cookie, csrf)
        agents = inventory["projects"][0]["agents"]
        selected = next(agent for agent in agents if agent["id"] == "codex")
        peer = next(agent for agent in agents if agent["id"] == "claude")
        assert selected["participant"] == "codex"
        assert selected["display_name"] == "Peer reviewer"
        assert selected["role"] == "review"
        assert selected["operational_role"] == {}
        assert peer["display_name"] != selected["display_name"]
        assert roles.load(directory)["roles"]["review"]["write"] is False
        body.update(preview=True, role="absent")
        assert (
            request(base, "/api/agents/configure", body, cookie, csrf)[0] == 400
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_agent_editor_runs_edit_cancel_back_repeated_click_and_ordering(
    tmp_path,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "Node is unavailable for the executable console script test"
        )
    script = re.search(r"<script>(.*?)</script>", console.PAGE, re.S).group(1)
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = {};
const pending = [];
const document = { getElementById(id) {
  return elements[id] ||= { value: '', textContent: '', innerHTML: '',
    hidden: false,
    disabled: false, querySelectorAll() { return []; }, setAttribute() {},
    focus() { this.focused = true; } };
}};
const context = vm.createContext({ document, location: {reload() {}},
  setInterval() {}, fetch() { return new Promise(() => {}); } });
vm.runInContext(SCRIPT, context);
context.api = function(path, body) {
  return new Promise((resolve, reject) => pending.push({path, body, resolve,
    reject}));
};
const inventory = { projects: [{root:'/repo', roles:[{id:'review'}], agents:[
  {participant:'opencode',display_name:'Open ' +
    'Code',role:'review',session_generation:'s1',
   availability:{state:'stale',freshness:'stale',transport_connected:null,
     recorded_state:'working',freshness_source:'native_hook'},
    tasks:{messages:[{id:59,state:'queued'}]},
   hooks:[{id:'Stop',supported:false,configured:null,
     detected:false,verified:null}]}]}] };
const flush = () => new Promise(resolve => setImmediate(resolve));
const preview = {ok:true,data:{revision:'r1',before:{display_name:'Open Code'},
  after:{display_name:'Review'},changed:true}};
(async () => {
  context.activeTab='agents'; context.drawAgents(inventory);
  assert.match(context.evidenceRows([]), /No recorded inventory/);
  assert.match(context.taskRows([]), /No recorded tasks/);
  assert.equal(context.observedAt(0),'1970-01-01T00:00:00.000Z');
  assert.match(elements.agentlist.innerHTML, /stale/);
  assert.match(elements.agentlist.innerHTML, /queued/);
  assert.match(elements.agentlist.innerHTML, /unknown/);
  assert.match(elements.agentlist.innerHTML, /Transport connected/);
  assert.match(elements.agentlist.innerHTML, /Last recorded state/);
  assert.match(elements.agentlist.innerHTML, /native_hook/);
  context.openAgent(0);
  assert.equal(elements.agentname.focused,true);
  assert.equal(elements.agentname.value,'Open Code');
  assert.match(elements.agentrole.innerHTML,/review/);
  elements.agentname.value=''; context.previewAgent();
  assert.equal(pending.length,0);
  elements.agentname.value='Review'; context.previewAgent();
    context.previewAgent();
  assert.equal(pending.length,1);
  context.cancelAgentEdit(); pending.shift().resolve(preview); await flush();
  assert.equal(context.agentEdit,null);
  assert.equal(elements.agenteditor.hidden,true);
  context.openAgent(0); elements.agentname.value='Review';
    context.previewAgent();
  pending.shift().resolve(preview); await flush();
  assert.equal(elements.agentapply.hidden,false);
  assert.equal(elements.agentname.disabled,true);
  context.backAgentEdit(); assert.equal(context.agentEdit.preview,undefined);
  assert.equal(elements.agentname.disabled,false);
  context.applyAgent(); assert.equal(pending.length,0);
  context.previewAgent(); pending.shift().resolve(preview); await flush();
  context.applyAgent(); context.applyAgent(); assert.equal(pending.length,1);
  assert.equal(pending[0].body.expected_revision,'r1');
  assert.equal(pending[0].body.participant,'opencode');
  assert.equal(pending[0].body.preview,false);
  context.cancelAgentEdit(); assert.notEqual(context.agentEdit,null);
  context.selectTab('dashboard'); assert.equal(context.activeTab,'agents');
  pending.shift().resolve({ok:true,data:{changed:true}}); await flush();
  assert.equal(context.agentEdit,null);
    assert.equal(pending[0].path,'/api/agents');
  pending.shift().resolve({ok:true,data:inventory}); await flush();
  context.refreshAgents(); context.refreshAgents();
  const first=pending.shift(), second=pending.shift();
  second.resolve({ok:true,data:{projects:[],generated_at:'new'}}); await
    flush();
  first.resolve({ok:true,data:{projects:[],generated_at:'old'}}); await flush();
  assert.match(elements.agentstamp.textContent,/new/);
  context.drawAgents(inventory); context.openAgent(0); context.previewAgent();
  context.backAgentEdit(); pending.shift().resolve(preview); await flush();
  assert.equal(context.agentEdit.preview,undefined);
  context.previewAgent(); pending.shift().resolve(preview); await flush();
  context.applyAgent(); pending.shift().reject(new Error('lost reply'));
    await flush();
  assert.match(elements.agentediterr.textContent,/may have been recorded/);
  assert.equal(context.agentApplying,false);
  assert.equal(context.agentEdit.preview,undefined);
  assert.equal(pending[0].path,'/api/agents');
  assert.equal(context.esc("<script>'\""),'&lt;script&gt;&#39;&quot;');
  context.cancelAgentEdit();
  inventory.projects[0].agents[0].role='removed';
  context.drawAgents(inventory); context.openAgent(0);
  assert.match(elements.agentrole.innerHTML,/Unavailable catalog ID: removed/);
  assert.equal(elements.agentrole.value,'removed');
  assert.match(elements.agentediterr.textContent,/explicitly clear/);
})().catch(error => { console.error(error); process.exitCode=1; });
"""
    harness = "const SCRIPT=" + json.dumps(script) + ";\n" + harness
    path = tmp_path / "console-contract.cjs"
    path.write_text(harness)
    done = subprocess.run(
        [node, str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert done.returncode == 0, done.stderr
