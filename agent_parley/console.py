"""Serves a loopback operator console over the existing command surface.

Command mutations use the command-line entry point. Agent presentation settings
use the same validated, audited service as the CLI. The console adds no
authority of its own and never edits state directly.

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

from agent_parley import agents
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
        if self.path in {"/api/agents", "/api/agents/configure"}:
            try:
                result = (
                    agents.inventory(self.home)
                    if self.path == "/api/agents"
                    else configure_agent(self.home, payload)
                )
            except BridgeError as error:
                code = (
                    409
                    if str(error).startswith("Configuration changed;")
                    else 400
                )
                self._send(code, {"error": str(error)})
                return
            self._send(200, result)
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


def configure_agent(home: Path, payload: dict) -> dict:
    """Validates the HTTP envelope and shares the CLI configuration service.

    Args:
        home: Private coordination state root.
        payload: Repository, stable participant ID and presentation changes.

    Returns:
        A preview or the audited result of applying the preview revision.

    Raises:
        BridgeError: If the envelope or shared configuration contract fails.
    """
    allowed = {
        "repo",
        "participant",
        "display_name",
        "role",
        "preview",
        "expected_revision",
    }
    if payload.keys() - allowed:
        raise BridgeError("Unknown agent configuration fields.")
    for field in ("repo", "participant", "display_name"):
        if not isinstance(payload.get(field), str):
            raise BridgeError(f"Agent {field} must be a string.")
    role = payload.get("role")
    if role is not None and not isinstance(role, str):
        raise BridgeError("Agent role must be a catalog ID.")
    preview = payload.get("preview", True)
    if not isinstance(preview, bool):
        raise BridgeError("Agent preview must be true or false.")
    revision = payload.get("expected_revision")
    if revision is not None and not isinstance(revision, str):
        raise BridgeError("Agent expected_revision must be a string.")
    if not preview and not revision:
        raise BridgeError("Preview the configuration before applying it.")
    return agents.configure(
        home,
        Path(payload["repo"]),
        payload["participant"],
        payload["display_name"],
        role=role,
        preview=preview,
        expected_revision=revision,
    )


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
button:disabled { opacity: .55; cursor: wait; }
nav { display: flex; gap: 8px; }
nav button[aria-selected="true"] { background: #1f6feb; }
.agent-heading { display: flex; gap: 12px; align-items: center; }
.agent-heading h3 { flex: 1; margin: 0; }
.scroll { overflow-x: auto; }
.agent-evidence { margin-top: 12px; }
.agent-evidence summary { cursor: pointer; }
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
    <nav aria-label="Console sections">
      <button class="ghost" id="dashboardtab"
          aria-selected="true">Dashboard</button>
      <button class="ghost" id="agentstab" aria-selected="false">Agents</button>
    </nav>
    <section id="dashboard" class="stack">
    <div class="grid">
      <article><h2>Service</h2><div id="service"></div></article>
      <article><h2>Projects and lanes</h2><div id="lanes"></div></article>
      <article><h2>Host resources</h2><div id="hosts"></div></article>
    </div>
    <article><h2>Operator actions</h2><div id="actions"></div></article>
    <article><h2>Output</h2><pre id="output">Ready.</pre></article>
    </section>
    <section id="agentsview" class="stack" hidden>
      <article>
        <h2>Agents</h2>
        <p>Routing IDs and session generations remain distinct from display
          names. Availability is observed evidence; a running owner or an
          installed file alone does not prove an agent connection.</p>
        <p class="muted">Message dispatch, explicit ACK and completion are
          separate. ACK records explicit review, not acceptance of a task.
          Zero pending ACKs never prove success. Tasks and messages below
          retain their reported states.</p>
        <p class="err" id="agenterr" role="status"></p>
        <p id="agentstamp" class="muted"></p>
      </article>
      <div id="agentlist" class="stack"></div>
      <article id="agenteditor" hidden>
        <h2>Agent presentation settings</h2>
        <div id="agentidentity"></div>
        <p class="muted">The selected function is a presentation preference from
          the existing role catalog. It does not change operational grants,
          native permissions, hooks or skills.</p>
        <label for="agentname">Display name</label>
        <input id="agentname" maxlength="80" autocomplete="off">
        <label for="agentrole">Function preference (presentation only)</label>
        <select id="agentrole"></select>
        <p class="err" id="agentediterr" role="status"></p>
        <pre id="agentpreview" hidden></pre>
        <p class="row">
          <button id="agentpreviewbutton">Preview changes</button>
          <button id="agentapply" hidden>Apply preview</button>
          <button class="ghost" id="agentback" hidden>Back to edit</button>
          <button class="ghost" id="agentcancel">Cancel</button>
        </p>
      </article>
    </section>
  </section>
</main>
<script>
var csrf = "";
var actions = [];
var activeTab = "dashboard";
var agentRows = [];
var agentRead = 0;
var snapshotRead = 0;
var editorEpoch = 0;
var agentEdit = null;
var agentBusy = false;
var agentApplying = false;
var el = function (id) { return document.getElementById(id); };

function esc(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
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
  cancelAgentEdit();
  agentRead += 1;
  snapshotRead += 1;
  api("/api/logout", {}).then(function () { location.reload(); });
}

function refresh() {
  if (activeTab === "agents") { refreshAgents(); return; }
  var read = ++snapshotRead;
  api("/api/snapshot", {}).then(function (res) {
    if (read !== snapshotRead || activeTab !== "dashboard") { return; }
    if (!res.ok) { show(res.data.error || "Refused."); return; }
    draw(res.data);
  }).catch(function () {
    if (read === snapshotRead && activeTab === "dashboard") {
      show("Unable to refresh. Check the console connection.");
    }
  });
}

function selectTab(name) {
  if (agentApplying) { return; }
  activeTab = name;
  agentRead += 1;
  snapshotRead += 1;
  el("dashboard").hidden = name !== "dashboard";
  el("agentsview").hidden = name !== "agents";
  el("dashboardtab").setAttribute("aria-selected", name === "dashboard");
  el("agentstab").setAttribute("aria-selected", name === "agents");
  if (name !== "agents") { cancelAgentEdit(); }
  refresh();
}

function observed(value) {
  if (value === true) { return "yes"; }
  if (value === false) { return "no"; }
  return "unknown";
}

function details(value) {
  return typeof value === "object" && value !== null
    ? JSON.stringify(value) : String(value === undefined ? "unknown" : value);
}

function observedAt(value) {
  return typeof value === "number" ? new Date(value * 1000).toISOString()
    : (value || "unknown");
}

function evidenceRows(items) {
  if (!items || !items.length) {
    return "<p class='muted'>No recorded inventory.</p>";
  }
  var html = "<div class='scroll'><table><tr><th>ID</th>"
    + "<th>Supported</th><th>Available</th><th>Configured</th>"
    + "<th>Detected</th><th>Verified</th><th>Evidence</th>"
    + "<th>Source / path</th></tr>";
  items.forEach(function (item) {
    html += "<tr><td>" + esc(item.id || item.name || item.path) + "</td><td>"
      + esc(observed(item.supported)) + "</td><td>"
      + esc(observed(item.available))
      + "</td><td>" + esc(observed(item.configured)) + "</td><td>"
      + esc(observed(item.detected)) + "</td><td>"
      + esc(observed(item.verified)) + "</td><td>"
      + esc(details(item.evidence || "unknown")) + "</td><td>"
      + esc([item.source, item.path].filter(Boolean).join(" / ")
        || "unknown") + "</td></tr>";
  });
  return html + "</table></div>";
}

function taskRows(items) {
  if (!items || !items.length) {
    return "<p class='muted'>No recorded tasks or messages.</p>";
  }
  return "<div class='scroll'><table><tr><th>ID</th><th>State</th>"
    + "<th>Task state</th><th>Details</th></tr>"
    + items.map(function (item) {
      var record = typeof item === "object" ? item : { detail: item };
      return "<tr><td>" + esc(record.message_id || record.id || record.audit_id
        || record.issue || record.claim_id || "unknown") + "</td><td>"
        + esc(record.status || record.state || record.action || "unknown")
        + "</td><td>" + esc(record.task_state || "unknown")
        + "</td><td><details><summary>"
        + esc(record.subject || record.title || record.detail
          || record.kind || "Recorded evidence")
        + "</summary><pre>" + esc(details(record))
        + "</pre></details></td></tr>";
    }).join("") + "</table></div>";
}

function refreshAgents() {
  var read = ++agentRead;
  api("/api/agents", {}).then(function (res) {
    if (read !== agentRead || activeTab !== "agents") { return; }
    if (!res.ok) {
      el("agenterr").textContent = res.data.error || "Inventory refused.";
      return;
    }
    drawAgents(res.data);
  }).catch(function () {
    if (read === agentRead && activeTab === "agents") {
      el("agenterr").textContent =
        "Unable to read agents. Refresh after checking the connection.";
    }
  });
}

function drawAgents(inventory) {
  agentRows = [];
  el("agenterr").textContent = (inventory.warnings || [])
    .map(details).join("; ");
  el("agentstamp").textContent = "Observed: "
    + observedAt(inventory.generated_at);
  var html = "";
  (inventory.projects || []).forEach(function (project) {
    html += "<h3>" + esc(project.root) + "</h3>";
    (project.agents || []).forEach(function (agent) {
      var index = agentRows.length;
      agentRows.push({ agent: agent, project: project });
      var availability = agent.availability || {};
      var mediaWorkspace = agent.workspace || {};
      var mediaCapability = mediaWorkspace.capability || {};
      html += "<article><div class='agent-heading'><h3>"
        + esc(agent.display_name || agent.participant)
        + "</h3><button class='ghost'"
        + " data-agent='" + index + "'>Configure</button></div>";
      html += table([
        ["Routing ID", agent.participant || agent.id],
        ["Platform", (agent.capabilities || {}).adapter],
        ["Workspace mode", (agent.workspace || {}).mode || "unknown"],
        ["Workspace path", (agent.workspace || {}).lane || "unknown"],
        ["Workspace exists", observed((agent.workspace || {}).lane_exists)],
        ["Workspace profile matches", observed(mediaWorkspace.profile_matches)],
        ["Canonical media", mediaWorkspace.media_root || "not configured"],
        ["Asset catalog", (agent.workspace || {}).catalog || "not configured"],
        ["Task outputs", mediaWorkspace.output_root || "not allocated"],
        ["Media adapter supported", observed(mediaCapability.supported)],
        ["Native media read verified",
          observed(mediaCapability.native_read_verified)],
        ["Read-only protection enforced",
          observed(mediaCapability.readonly_enforced)],
        ["Configured", observed(agent.configured)],
        ["Executable detected", observed(agent.executable_detected)],
        ["Paused / retired", observed(agent.paused) + " / "
          + observed(agent.retired)],
        ["Session generation", agent.session_generation || "unknown"],
        ["Availability", availability.state || "unknown"],
        ["Transport connected", observed(availability.transport_connected)],
        ["Freshness", availability.freshness || "unknown"],
        ["Last observed", observedAt(availability.observed_at)],
        ["Observation source", availability.freshness_source || "unknown"],
        ["Stale after (seconds)", availability.stale_after_seconds],
        ["Last recorded state", availability.recorded_state || "unknown"],
        ["Last recorded update", observedAt(availability.recorded_updated_at)],
        ["Function preference", agent.role || "none"],
        ["Operational role", (agent.operational_role || {}).role || "none"],
        ["Disabled capabilities", details(
          (agent.operational_role || {}).disabled || [])],
        ["Delivery mechanism", (agent.capabilities || {}).delivery],
        ["Unavailable hooks", details(
          (agent.capabilities || {}).unavailable_hooks || [])],
        ["Plugin mode", (agent.plugin || {}).mode || "unknown"],
        ["Plugin compatibility", observed((agent.plugin || {}).compatible)]
      ]);
      html += "<details class='agent-evidence'><summary>"
        + "Hooks, skills and plugin evidence</summary><p class='muted'>"
        + "Configured and detected are observations. Verified requires "
        + "runtime evidence; no control here activates a hook or skill.</p>"
        + "<h4>Plugin</h4>" + evidenceRows(agent.plugin ? [agent.plugin] : [])
        + "<h4>Hooks</h4>" + evidenceRows(agent.hooks)
        + "<h4>Skills</h4>" + evidenceRows(agent.skills)
        + "<h4>Detected adapter files</h4>" + evidenceRows(agent.artifacts)
        + "</details>";
      var tasks = agent.tasks || {};
      html += "<details class='agent-evidence'><summary>"
        + "Tasks and message history</summary>"
        + "<h4>Current tasks</h4>" + taskRows(tasks.current)
        + "<h4>Coordination history</h4>" + taskRows(tasks.history)
        + "<h4>Messages: queued / dispatched / acknowledged / terminal</h4>"
        + taskRows(tasks.messages) + "</details>";
      html += "<details class='agent-evidence'><summary>Configuration audit"
        + "</summary>" + taskRows(agent.audit) + "</details></article>";
    });
  });
  el("agentlist").innerHTML = html ||
    "<article>No registered agents.</article>";
  Array.prototype.forEach.call(
    el("agentlist").querySelectorAll("button[data-agent]"),
    function (button) {
      button.onclick = function () {
        openAgent(Number(button.getAttribute("data-agent")));
      };
    }
  );
}

function setAgentBusy(value) {
  agentBusy = value;
  ["agentpreviewbutton", "agentapply", "agentname", "agentrole"]
    .forEach(function (id) {
      el(id).disabled = value;
    });
}

function openAgent(index) {
  if (agentBusy) { return; }
  cancelAgentEdit();
  var row = agentRows[index];
  if (!row) { return; }
  agentEdit = { repo: row.project.root,
    participant: row.agent.participant || row.agent.id };
  el("agentidentity").innerHTML = table([
    ["Routing ID (immutable)", agentEdit.participant],
    ["Repository", agentEdit.repo],
    ["Session generation", row.agent.session_generation || "unknown"]
  ]);
  el("agentname").value = row.agent.display_name || agentEdit.participant;
  el("agentrole").innerHTML = "<option value=''>No function preference</option>"
    + (row.project.roles || []).map(function (role) {
      return "<option value='" + esc(role.id) + "'>" + esc(role.id)
        + (role.description ? " — " + esc(role.description) : "") + "</option>";
    }).join("");
  var savedRole = row.agent.role || "";
  if (savedRole && !(row.project.roles || []).some(function (role) {
    return role.id === savedRole;
  })) {
    el("agentrole").innerHTML += "<option value='" + esc(savedRole)
      + "'>Unavailable catalog ID: " + esc(savedRole) + "</option>";
    el("agentediterr").textContent =
      "The saved function is absent from the catalog. Choose an existing "
      + "function or explicitly clear the preference before previewing.";
  }
  el("agentrole").value = row.agent.role || "";
  el("agenteditor").hidden = false;
  el("agentname").focus();
}

function backAgentEdit() {
  if (agentApplying) { return; }
  editorEpoch += 1;
  if (agentEdit) { delete agentEdit.preview; delete agentEdit.changes; }
  setAgentBusy(false);
  el("agentpreview").hidden = true;
  el("agentapply").hidden = true;
  el("agentback").hidden = true;
  el("agentpreviewbutton").hidden = false;
  el("agentediterr").textContent = "";
}

function cancelAgentEdit() {
  if (agentApplying) { return; }
  backAgentEdit();
  agentEdit = null;
  el("agenteditor").hidden = true;
}

function previewAgent() {
  if (!agentEdit || agentBusy) { return; }
  var name = el("agentname").value.trim();
  if (!name || Array.from(name).length > 80
      || /[\\x00-\\x1f\\x7f]/.test(name)) {
    el("agentediterr").textContent =
      "Use a display name of 1–80 characters without control characters.";
    return;
  }
  var changes = { repo: agentEdit.repo, participant: agentEdit.participant,
    display_name: name, role: el("agentrole").value, preview: true };
  var epoch = ++editorEpoch;
  setAgentBusy(true);
  el("agentediterr").textContent = "";
  api("/api/agents/configure", changes).then(function (res) {
    if (epoch !== editorEpoch || !agentEdit) { return; }
    setAgentBusy(false);
    if (!res.ok) {
      el("agentediterr").textContent = res.data.error || "Preview refused.";
      return;
    }
    agentEdit.changes = changes;
    agentEdit.preview = res.data;
    el("agentpreview").textContent = JSON.stringify({
      before: res.data.before, after: res.data.after,
      changed: res.data.changed, revision: res.data.revision
    }, null, 2);
    el("agentpreview").hidden = false;
    el("agentapply").hidden = false;
    el("agentback").hidden = false;
    el("agentpreviewbutton").hidden = true;
    el("agentname").disabled = true;
    el("agentrole").disabled = true;
  }).catch(function () {
    if (epoch === editorEpoch) {
      setAgentBusy(false);
      el("agentediterr").textContent =
        "Preview failed. Retry after checking the connection.";
    }
  });
}

function applyAgent() {
  if (!agentEdit || !agentEdit.preview || agentBusy) { return; }
  var epoch = ++editorEpoch;
  var changes = Object.assign({}, agentEdit.changes,
    { preview: false, expected_revision: agentEdit.preview.revision });
  agentApplying = true;
  ["agentback", "agentcancel", "dashboardtab", "agentstab"]
    .forEach(function (id) {
    el(id).disabled = true;
  });
  setAgentBusy(true);
  api("/api/agents/configure", changes).then(function (res) {
    if (epoch !== editorEpoch || !agentEdit) { return; }
    finishAgentApply();
    setAgentBusy(false);
    if (!res.ok) {
      backAgentEdit();
      el("agentediterr").textContent = res.data.error
        || "Apply refused. Preview again.";
      return;
    }
    cancelAgentEdit();
    refreshAgents();
  }).catch(function () {
    if (epoch === editorEpoch) {
      finishAgentApply();
      backAgentEdit();
      el("agentediterr").textContent =
        "Apply response unavailable. Refresh inventory before making another "
        + "preview; the change may have been recorded.";
      refreshAgents();
    }
  });
}

function finishAgentApply() {
  agentApplying = false;
  ["agentback", "agentcancel", "dashboardtab", "agentstab"]
    .forEach(function (id) {
    el(id).disabled = false;
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
el("dashboardtab").onclick = function () { selectTab("dashboard"); };
el("agentstab").onclick = function () { selectTab("agents"); };
el("agentpreviewbutton").onclick = previewAgent;
el("agentapply").onclick = applyAgent;
el("agentcancel").onclick = cancelAgentEdit;
el("agentback").onclick = backAgentEdit;
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
