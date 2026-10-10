"""Serves a loopback operator console over the existing command surface.

Every mutation is dispatched through the same command-line entry point an
operator would type, so lane guards, refusals, deadlines and audit records
apply unchanged. The console adds no authority of its own: it cannot reach a
command that is absent from its allowlist, and it never edits state directly.

The listener binds the loopback interface only and answers nothing before a
caller presents the state home's bearer token. That token is readable only by
the owner of the private state directory, which makes it the operator proof
this console requires.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_parley.state import BridgeError

COOKIE = "parley_console"
SESSION_SECONDS = 3600
MAX_BODY = 65536
COMMAND_TIMEOUT = 180
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")
LEASE = re.compile(r"^[0-9a-f]{32}$")

ACTIONS: dict[str, dict] = {
    "service-up": {
        "group": "Service",
        "label": "Start the coordination service",
        "argv": ["up"],
        "params": [],
    },
    "service-down": {
        "group": "Service",
        "label": "Stop the coordination service",
        "argv": ["down"],
        "params": [],
    },
    "doctor": {
        "group": "Service",
        "label": "Run diagnostics",
        "argv": ["doctor"],
        "params": [],
    },
    "problems": {
        "group": "Service",
        "label": "List escalations",
        "argv": ["problems"],
        "params": [],
    },
    "participant-add": {
        "group": "Lanes",
        "label": "Add a lane",
        "argv": ["participant", "add"],
        "params": [
            {"name": "name", "kind": "identifier"},
            {"name": "provider", "kind": "identifier", "flag": "--provider"},
            {
                "name": "credentials",
                "kind": "identifier",
                "flag": "--credentials",
                "required": False,
            },
            {"name": "repo", "kind": "path", "flag": "--repo"},
        ],
    },
    "participant-list": {
        "group": "Lanes",
        "label": "List lanes",
        "argv": ["participant", "list"],
        "params": [{"name": "repo", "kind": "path", "flag": "--repo"}],
    },
    "participant-pause": {
        "group": "Lanes",
        "label": "Pause a lane",
        "argv": ["participant", "pause"],
        "params": [
            {"name": "name", "kind": "identifier"},
            {"name": "repo", "kind": "path", "flag": "--repo"},
        ],
    },
    "participant-resume": {
        "group": "Lanes",
        "label": "Resume a lane",
        "argv": ["participant", "resume"],
        "params": [
            {"name": "name", "kind": "identifier"},
            {"name": "repo", "kind": "path", "flag": "--repo"},
        ],
    },
    "participant-stop": {
        "group": "Lanes",
        "label": "Stop a lane session",
        "argv": ["participant", "stop"],
        "params": [
            {"name": "name", "kind": "identifier"},
            {"name": "repo", "kind": "path", "flag": "--repo"},
        ],
    },
    "participant-restart": {
        "group": "Lanes",
        "label": "Restart a lane",
        "argv": ["participant", "restart"],
        "params": [
            {"name": "name", "kind": "identifier"},
            {"name": "repo", "kind": "path", "flag": "--repo"},
        ],
    },
    "issue-list": {
        "group": "Work",
        "label": "List claims and offers",
        "argv": ["issue", "list"],
        "params": [{"name": "repo", "kind": "path", "flag": "--repo"}],
    },
    "issue-assign": {
        "group": "Work",
        "label": "Offer an issue to a lane",
        "argv": ["issue", "assign"],
        "params": [
            {"name": "number", "kind": "number"},
            {"name": "name", "kind": "identifier"},
            {
                "name": "reason",
                "kind": "text",
                "flag": "--reason",
                "required": False,
            },
            {"name": "repo", "kind": "path", "flag": "--repo"},
        ],
    },
    "issue-show": {
        "group": "Work",
        "label": "Show one issue",
        "argv": ["issue", "show"],
        "params": [
            {"name": "number", "kind": "number"},
            {"name": "repo", "kind": "path", "flag": "--repo"},
        ],
    },
    "host-list": {
        "group": "Host resources",
        "label": "List host leases",
        "argv": ["host-resources", "list"],
        "params": [],
    },
    "host-release": {
        "group": "Host resources",
        "label": "Release a host lease",
        "argv": ["host-resources", "release"],
        "params": [{"name": "lease_id", "kind": "lease"}],
    },
    "preflight": {
        "group": "Inspect",
        "label": "Read launch scope",
        "argv": ["preflight"],
        "params": [{"name": "repo", "kind": "path", "flag": "--repo"}],
    },
    "setup": {
        "group": "Inspect",
        "label": "Register a repository",
        "argv": ["setup"],
        "params": [{"name": "repo", "kind": "path"}],
    },
}


def token(home: Path) -> str:
    """Reads the bearer token that proves ownership of this state home.

    Args:
        home: Private state root holding the generated configuration.

    Returns:
        The configured bearer token.

    Raises:
        BridgeError: If the configuration is absent or has no token.
    """
    path = home / "config.json"
    try:
        value = json.loads(path.read_text())["token"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BridgeError(
            f"Cannot read the console credential at {path}. Start the "
            "service once so its configuration exists."
        ) from error
    if not isinstance(value, str) or not value:
        raise BridgeError(f"Invalid console credential at {path}.")
    return value


def validate(kind: str, value: object) -> str:
    """Checks one command argument against its declared kind.

    Args:
        kind: Declared parameter kind from the action allowlist.
        value: Caller-supplied argument.

    Returns:
        The argument rendered as a command-line string.

    Raises:
        BridgeError: If the argument is missing or malformed.
    """
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"A {kind} value is required.")
    text = value.strip()
    if kind == "identifier" and not IDENTIFIER.match(text):
        raise BridgeError(f"{text!r} is not a valid name.")
    if kind == "lease" and not LEASE.match(text):
        raise BridgeError("A lease identifier is 32 hexadecimal characters.")
    if kind == "number" and not text.isdigit():
        raise BridgeError("An issue number must be digits.")
    if kind == "path":
        resolved = Path(text).expanduser()
        if not resolved.is_absolute() or not resolved.is_dir():
            raise BridgeError(f"{text!r} is not an existing directory.")
        text = str(resolved.resolve())
    if kind == "text" and (len(text) > 200 or not text.isprintable()):
        raise BridgeError("Text must be printable and at most 200 characters.")
    return text


def build(action: str, params: dict) -> list[str]:
    """Renders one allowlisted action into an exact argument vector.

    Args:
        action: Allowlisted action identifier.
        params: Caller-supplied arguments by parameter name.

    Returns:
        The argument vector to execute, interpreter included.

    Raises:
        BridgeError: If the action is unknown or an argument is invalid.
    """
    entry = ACTIONS.get(action)
    if entry is None:
        raise BridgeError(f"Unknown action: {action}.")
    argv = [sys.executable, "-m", "agent_parley.cli", *entry["argv"]]
    for spec in entry["params"]:
        supplied = params.get(spec["name"])
        if supplied in (None, "") and not spec.get("required", True):
            continue
        rendered = validate(spec["kind"], supplied)
        if flag := spec.get("flag"):
            argv.extend([flag, rendered])
        else:
            argv.append(rendered)
    return argv


def run(home: Path, argv: list[str]) -> dict:
    """Executes one rendered command and reports its complete outcome.

    Args:
        home: State root the command must operate on.
        argv: Argument vector produced by the action allowlist.

    Returns:
        The exit status and captured output of the command.
    """
    environment = {
        "AGENT_PARLEY_HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "NO_COLOR": "1",
    }
    if resource_home := os.environ.get("AGENT_PARLEY_RESOURCE_HOME"):
        environment["AGENT_PARLEY_RESOURCE_HOME"] = resource_home
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": 124,
            "stdout": "",
            "stderr": f"The command exceeded {COMMAND_TIMEOUT} seconds.",
        }
    return {
        "status": done.returncode,
        "stdout": done.stdout[-MAX_BODY:],
        "stderr": done.stderr[-MAX_BODY:],
    }


class Sessions:
    """Holds short-lived console sessions and their request tokens."""

    def __init__(self) -> None:
        """Creates an empty session table guarded by its own lock."""
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self) -> dict:
        """Issues one session identifier with its paired request token.

        Returns:
            The new session identifier and its cross-request token.
        """
        identifier = secrets.token_urlsafe(32)
        entry = {
            "id": identifier,
            "csrf": secrets.token_urlsafe(32),
            "expires": time.time() + SESSION_SECONDS,
        }
        with self._lock:
            self._entries[identifier] = entry
        return entry

    def read(self, identifier: str | None) -> dict | None:
        """Returns a live session, discarding one that has expired.

        Args:
            identifier: Session identifier presented by the caller.

        Returns:
            The stored session, or None when absent or expired.
        """
        if not identifier:
            return None
        with self._lock:
            entry = self._entries.get(identifier)
            if entry is None:
                return None
            if entry["expires"] < time.time():
                del self._entries[identifier]
                return None
            return entry

    def discard(self, identifier: str | None) -> None:
        """Ends one session immediately.

        Args:
            identifier: Session identifier to forget.
        """
        if identifier:
            with self._lock:
                self._entries.pop(identifier, None)


class Console(BaseHTTPRequestHandler):
    """Answers console routes for one private state home."""

    server_version = "AgentParleyConsole"
    sys_version = ""
    home: Path
    secret: str
    sessions: Sessions

    def log_message(self, format: str, *args: object) -> None:
        """Keeps request lines out of the operator's terminal.

        Args:
            format: Server-supplied format string.
            args: Server-supplied format arguments.
        """

    def _session(self) -> dict | None:
        """Returns the live session named by the request cookie."""
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return self.sessions.read(value)
        return None

    def _body(self) -> dict:
        """Reads one bounded JSON request body.

        Returns:
            The decoded object, or an empty object when absent.

        Raises:
            BridgeError: If the body is oversized or not a JSON object.
        """
        raw_length = self.headers.get("Content-Length") or "0"
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise BridgeError("Invalid request body length.")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise BridgeError("Invalid request body length.") from error
        if length > MAX_BODY:
            raise BridgeError("The request body is too large.")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except ValueError as error:
            raise BridgeError("The request body is not valid JSON.") from error
        if not isinstance(value, dict):
            raise BridgeError("The request body must be an object.")
        return value

    def _send(
        self,
        status: int,
        payload: dict,
        *,
        cookie: str = "",
        clear: bool = False,
    ) -> None:
        """Writes one JSON response with conservative browser headers.

        Args:
            status: HTTP status code.
            payload: Response object.
            cookie: Session identifier to set, when starting a session.
            clear: Whether to expire the session cookie.
        """
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={cookie}; HttpOnly; SameSite=Strict; Path=/",
            )
        if clear:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
            )
        self.end_headers()
        self.wfile.write(body)

    def _page(self) -> None:
        """Writes the single-page console document."""
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Serves the console document and read-only routes."""
        if self.path == "/":
            self._page()
            return
        if self.path == "/api/session":
            session = self._session()
            if session is None:
                self._send(401, {"error": "Sign in with the state token."})
                return
            self._send(200, {"csrf": session["csrf"], "actions": catalog()})
            return
        self._send(404, {"error": "Unknown route."})

    def do_POST(self) -> None:
        """Serves sign-in, sign-out and allowlisted command dispatch."""
        try:
            payload = self._body()
        except BridgeError as error:
            self._send(400, {"error": str(error)})
            return
        if self.path == "/api/login":
            supplied = payload.get("token")
            if (
                not isinstance(supplied, str)
                or not supplied.isascii()
                or not secrets.compare_digest(supplied, self.secret)
            ):
                time.sleep(0.5)
                self._send(401, {"error": "That token was refused."})
                return
            opened = self.sessions.create()
            self._send(
                200,
                {"csrf": opened["csrf"], "actions": catalog()},
                cookie=opened["id"],
            )
            return
        session = self._session()
        if session is None:
            self._send(401, {"error": "Sign in with the state token."})
            return
        presented = self.headers.get("X-Parley-Csrf", "")
        if not presented.isascii() or not secrets.compare_digest(
            presented, session["csrf"]
        ):
            self._send(403, {"error": "The request token did not match."})
            return
        if self.path == "/api/logout":
            self.sessions.discard(session["id"])
            self._send(200, {"ok": True}, clear=True)
            return
        if self.path == "/api/snapshot":
            self._send(200, snapshot(self.home))
            return
        if self.path == "/api/action":
            action = payload.get("action")
            params = payload.get("params")
            if not isinstance(action, str) or not isinstance(params, dict):
                self._send(400, {"error": "Name an action and its params."})
                return
            try:
                argv = build(action, params)
            except BridgeError as error:
                self._send(400, {"error": str(error)})
                return
            self._send(200, {"action": action, **run(self.home, argv)})
            return
        self._send(404, {"error": "Unknown route."})


def catalog() -> list[dict]:
    """Describes the allowlisted actions a console client may dispatch.

    Returns:
        One entry per action with its group, label and parameters.
    """
    return [
        {
            "action": name,
            "group": entry["group"],
            "label": entry["label"],
            "params": [
                {
                    "name": spec["name"],
                    "kind": spec["kind"],
                    "required": spec.get("required", True),
                }
                for spec in entry["params"]
            ],
        }
        for name, entry in sorted(ACTIONS.items())
    ]


def snapshot(home: Path) -> dict:
    """Collects the read-only views the console dashboard renders.

    Args:
        home: State root to report on.

    Returns:
        Decoded status and host resource readings with any failure text.
    """
    result: dict = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    for key, argv in (
        ("status", ["status", "--json"]),
        ("hosts", ["host-resources", "list", "--json"]),
    ):
        done = run(home, [sys.executable, "-m", "agent_parley.cli", *argv])
        try:
            result[key] = json.loads(done["stdout"])
        except ValueError:
            result[key] = {"error": done["stderr"] or done["stdout"]}
    return result


def serve(home: Path, port: int, secret: str) -> ThreadingHTTPServer:
    """Builds the loopback console listener without starting its loop.

    Args:
        home: Private state root the console operates on.
        port: Loopback port to bind, or zero to select a free one.
        secret: Bearer token a caller must present to sign in.

    Returns:
        The bound server, ready for serve_forever.

    Raises:
        BridgeError: If the port is outside the unprivileged range.
    """
    if port and not 1024 <= port <= 65535:
        raise BridgeError("Choose a console port between 1024 and 65535.")
    handler = type(
        "BoundConsole",
        (Console,),
        {"home": home, "secret": secret, "sessions": Sessions()},
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(home: Path, port: int) -> int:
    """Runs the console until the operator interrupts it.

    Args:
        home: Private state root the console operates on.
        port: Loopback port to bind, or zero to select a free one.

    Returns:
        The process exit status.
    """
    try:
        httpd = serve(home, port, token(home))
    except (BridgeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    bound = httpd.server_address[1]
    print(f"Console ready at http://127.0.0.1:{bound}/")
    print("Sign in with the token in the state home's config.json.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Parley console</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #0e1116; color: #d7dde6;
  font: 14px/1.5 ui-sans-serif, system-ui, sans-serif;
}
header {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 20px; border-bottom: 1px solid #222a35; background: #11161d;
}
header h1 { font-size: 15px; margin: 0; font-weight: 600; }
header .spacer { flex: 1; }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #6b7280; }
.dot.ok { background: #35c46b; }
.dot.bad { background: #e5484d; }
main { padding: 20px; max-width: 1180px; margin: 0 auto; }
.grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
}
article {
  background: #141a22; border: 1px solid #222a35;
  border-radius: 9px; padding: 14px;
}
article h2 {
  font-size: 12px; margin: 0 0 10px; text-transform: uppercase;
  letter-spacing: .07em; color: #8b97a8; font-weight: 600;
}
table { width: 100%; border-collapse: collapse; }
td, th { text-align: left; padding: 4px 6px 4px 0; vertical-align: top; }
th { color: #8b97a8; font-weight: 500; }
tr + tr td { border-top: 1px solid #1d242e; }
button {
  background: #1f6feb; color: #fff; border: 0; border-radius: 6px;
  padding: 7px 13px; font: inherit; cursor: pointer;
}
button.ghost { background: #222a35; color: #d7dde6; }
button:hover { filter: brightness(1.12); }
input, select {
  background: #0e1116; color: #d7dde6; border: 1px solid #2a3340;
  border-radius: 6px; padding: 7px 9px; font: inherit; width: 100%;
}
label { display: block; margin: 8px 0 0; font-size: 12px; color: #8b97a8; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-end; }
.row > div { flex: 1 1 165px; }
pre {
  background: #0a0d12; border: 1px solid #222a35; border-radius: 8px;
  padding: 12px; overflow: auto; max-height: 340px; white-space: pre-wrap;
  word-break: break-word; margin: 0;
}
.pill {
  display: inline-block; padding: 1px 7px; border-radius: 99px;
  font-size: 11px; background: #222a35; color: #9fb0c6;
}
.pill.ok { background: #10361f; color: #5ddc8c; }
.pill.bad { background: #3a1416; color: #ff8f93; }
.err { color: #ff8f93; margin: 8px 0 0; }
.muted { color: #6f7b8c; }
#login { max-width: 420px; margin: 8vh auto; }
.stack > * + * { margin-top: 14px; }
</style>
</head>
<body>
<header>
  <span class="dot" id="light"></span>
  <h1>Agent Parley console</h1>
  <span class="muted" id="home"></span>
  <span class="spacer"></span>
  <span class="muted" id="stamp"></span>
  <button class="ghost" id="refresh" hidden>Refresh</button>
  <button class="ghost" id="signout" hidden>Sign out</button>
</header>
<main>
  <section id="login">
    <article>
      <h2>Operator sign in</h2>
      <p class="muted">
        Paste the token from this state home's <code>config.json</code>.
        It is readable only by the owner of that private directory.
      </p>
      <label for="token">Bearer token</label>
      <input id="token" type="password" autocomplete="off">
      <p class="err" id="loginerr"></p>
      <p><button id="enter">Sign in</button></p>
    </article>
  </section>
  <section id="app" class="stack" hidden>
    <div class="grid">
      <article><h2>Service</h2><div id="service"></div></article>
      <article><h2>Projects and lanes</h2><div id="lanes"></div></article>
      <article><h2>Host resources</h2><div id="hosts"></div></article>
    </div>
    <article><h2>Operator actions</h2><div id="actions"></div></article>
    <article><h2>Output</h2><pre id="output">Ready.</pre></article>
  </section>
</main>
<script>
var csrf = "";
var actions = [];
var el = function (id) { return document.getElementById(id); };

function esc(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function api(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Parley-Csrf": csrf },
    body: JSON.stringify(body || {})
  }).then(function (r) {
    return r.json().then(function (d) { return { ok: r.ok, data: d }; });
  });
}

function show(text) { el("output").textContent = text; }

function signIn() {
  var value = el("token").value.trim();
  if (!value) { el("loginerr").textContent = "Enter the token."; return; }
  api("/api/login", { token: value }).then(function (res) {
    if (!res.ok) { el("loginerr").textContent = res.data.error; return; }
    csrf = res.data.csrf;
    actions = res.data.actions;
    el("token").value = "";
    el("login").hidden = true;
    el("app").hidden = false;
    el("refresh").hidden = false;
    el("signout").hidden = false;
    drawActions();
    refresh();
  });
}

function signOut() {
  api("/api/logout", {}).then(function () { location.reload(); });
}

function refresh() {
  api("/api/snapshot", {}).then(function (res) {
    if (!res.ok) { show(res.data.error || "Refused."); return; }
    draw(res.data);
  });
}

function draw(snap) {
  var status = snap.status || {};
  var ready = status.server && status.server.ready;
  el("light").className = "dot " + (ready ? "ok" : "bad");
  el("home").textContent = status.state_directory || "";
  el("stamp").textContent = snap.generated_at || "";
  var rows = [
    ["State", (status.server && status.server.state) || "unknown"],
    ["Directory", status.state_directory || ""],
    ["Outbound", status.outbound && status.outbound.enabled ? "on" : "off"],
    ["Inbound", status.inbound && status.inbound.enabled ? "on" : "off"]
  ];
  if (status.error) { rows.push(["Error", status.error]); }
  el("service").innerHTML = table(rows);
  drawLanes(status.projects || []);
  drawHosts(snap.hosts || {});
}

function table(rows) {
  return "<table>" + rows.map(function (r) {
    return "<tr><th>" + esc(r[0]) + "</th><td>" + esc(r[1]) + "</td></tr>";
  }).join("") + "</table>";
}

function drawLanes(projects) {
  if (!projects.length) {
    el("lanes").innerHTML = "<p class='muted'>No registered projects.</p>";
    return;
  }
  var html = "";
  projects.forEach(function (p) {
    html += "<p><strong>" + esc(p.root) + "</strong></p>";
    var lanes = p.participants || p.lanes || [];
    if (!lanes.length) {
      html += "<p class='muted'>No lanes yet.</p>";
      return;
    }
    html += "<table><tr><th>Lane</th><th>Provider</th>"
      + "<th>State</th><th>Claims</th></tr>";
    lanes.forEach(function (l) {
      var state = (l.condition && l.condition.state) || l.lane_state || "-";
      var good = state === "working" || state === "ready";
      var bad = state === "dead" || state === "stopped";
      var claims = (l.claims || []).map(function (c) {
        return typeof c === "object" ? (c.issue || c.number || "?") : c;
      });
      html += "<tr><td>" + esc(l.participant || l.identity)
        + "</td><td class='muted'>" + esc(l.provider || "-")
        + "</td><td><span class='pill "
        + (good ? "ok" : (bad ? "bad" : "")) + "'>" + esc(state)
        + "</span></td><td>"
        + esc(claims.join(", ") || "-") + "</td></tr>";
    });
    html += "</table>";
  });
  el("lanes").innerHTML = html;
}

function drawHosts(hosts) {
  if (hosts.error) {
    el("hosts").innerHTML = "<p class='err'>" + esc(hosts.error) + "</p>";
    return;
  }
  var held = hosts.holdings || [];
  var head = "<p class='muted'>" + esc(hosts.registry || "")
    + "<br>Advisory, no expiry.</p>";
  if (!held.length) {
    el("hosts").innerHTML = head + "<p class='muted'>Nothing held.</p>";
    return;
  }
  var html = head + "<table><tr><th>Resource</th><th>Holder</th></tr>";
  held.forEach(function (h) {
    html += "<tr><td>" + esc(h.resource) + "</td><td>"
      + esc(h.participant) + "<br><span class='muted'>"
      + esc(h.lease_id) + "</span></td></tr>";
  });
  el("hosts").innerHTML = html + "</table>";
}

function drawActions() {
  var groups = {};
  actions.forEach(function (a) {
    (groups[a.group] = groups[a.group] || []).push(a);
  });
  var html = "";
  Object.keys(groups).forEach(function (name) {
    html += "<p><strong>" + esc(name) + "</strong></p><div class='row'>";
    groups[name].forEach(function (a) {
      html += "<div><button class='ghost' data-act='" + esc(a.action)
        + "'>" + esc(a.label) + "</button></div>";
    });
    html += "</div>";
  });
  html += "<div id='form'></div>";
  el("actions").innerHTML = html;
  Array.prototype.forEach.call(
    el("actions").querySelectorAll("button[data-act]"),
    function (b) {
      b.onclick = function () { openForm(b.getAttribute("data-act")); };
    }
  );
}

function openForm(name) {
  var spec = actions.filter(function (a) { return a.action === name; })[0];
  if (!spec) { return; }
  if (!spec.params.length) { dispatch(name, {}); return; }
  var html = "<p><strong>" + esc(spec.label) + "</strong></p><div class='row'>";
  spec.params.forEach(function (p) {
    html += "<div><label>" + esc(p.name)
      + (p.required ? "" : " <span class='muted'>(optional)</span>")
      + "</label><input data-p='" + esc(p.name) + "'></div>";
  });
  html += "<div><button id='go'>Run</button></div></div>";
  el("form").innerHTML = html;
  el("go").onclick = function () {
    var params = {};
    Array.prototype.forEach.call(
      el("form").querySelectorAll("input[data-p]"),
      function (i) { params[i.getAttribute("data-p")] = i.value; }
    );
    dispatch(name, params);
  };
}

function dispatch(name, params) {
  show("Running " + name + " ...");
  api("/api/action", { action: name, params: params }).then(function (res) {
    if (!res.ok) { show(res.data.error || "Refused."); return; }
    var d = res.data;
    show("exit " + d.status + "\\n\\n" + (d.stdout || "") + (d.stderr || ""));
    refresh();
  });
}

el("enter").onclick = signIn;
el("token").onkeydown = function (e) { if (e.key === "Enter") { signIn(); } };
el("refresh").onclick = refresh;
el("signout").onclick = signOut;
fetch("/api/session").then(function (r) {
  if (!r.ok) { return; }
  return r.json().then(function (d) {
    csrf = d.csrf;
    actions = d.actions;
    el("login").hidden = true;
    el("app").hidden = false;
    el("refresh").hidden = false;
    el("signout").hidden = false;
    drawActions();
    refresh();
  });
});
setInterval(function () { if (csrf) { refresh(); } }, 15000);
</script>
</body>
</html>
"""
