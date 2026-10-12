"""Checks that running lanes are bound to the service, port and rules.

The reading is observational: it opens no state, writes nothing and never
prints a token or a header. Process evidence comes from ``/proc`` and is
reported as unknown where the platform or permissions do not provide it. A
passing reading shows the recorded wiring is consistent, not that a model
obeyed it.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from agent_parley import start
from agent_parley.preflight import _document

URL_PATTERN = re.compile(r'mcp_servers\.agent_parley\.url="([^"]+)"')


def _check(scope: str, name: str, state: str, detail: str) -> dict:
    """Builds one finding.

    Args:
        scope: Project, participant or service the finding concerns.
        name: Short name of what was checked.
        state: One of ``pass``, ``warn``, ``fail`` or ``unknown``.
        detail: Plain explanation without secrets.

    Returns:
        The finding as a JSON-ready mapping.
    """
    return {"scope": scope, "name": name, "state": state, "detail": detail}


def _port(home: Path) -> int | None:
    """Reads the saved service port.

    Args:
        home: State home holding ``config.json``.

    Returns:
        The saved port, or None when absent or invalid.
    """
    port = _document(home / "config.json").get("port")
    return port if type(port) is int and 1024 <= port <= 65535 else None


def _unauthenticated(port: int) -> int | None:
    """Sends one tokenless MCP request to confirm authentication is enforced.

    Args:
        port: Loopback service port.

    Returns:
        The HTTP status, or None when the service did not answer.
    """
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("POST", "/mcp/", body=b"{}")
        status = connection.getresponse().status
        connection.close()
        return status
    except (OSError, http.client.HTTPException):
        return None


def _processes(lane: Path) -> list[dict]:
    """Finds readable processes working inside a lane directory.

    Args:
        lane: Lane worktree path.

    Returns:
        One record per process with its arguments and environment keys.
    """
    found: list[dict] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve()
            if cwd != lane and lane not in cwd.parents:
                continue
            arguments = (
                (entry / "cmdline")
                .read_bytes()
                .decode(errors="replace")
                .split("\0")
            )
            environment = dict(
                item.split("=", 1)
                for item in (entry / "environ")
                .read_bytes()
                .decode(errors="replace")
                .split("\0")
                if "=" in item
            )
        except OSError:
            continue
        found.append(
            {"pid": int(entry.name), "arguments": arguments, "env": environment}
        )
    return found


def _bound_url(arguments: list[str]) -> str | None:
    """Extracts the service URL a native client was launched with.

    Args:
        arguments: Process arguments.

    Returns:
        The MCP URL, or None when the process carries none.
    """
    for argument in arguments:
        match = URL_PATTERN.search(argument)
        if match:
            return match.group(1)
    if "--mcp-config" in arguments:
        index = arguments.index("--mcp-config") + 1
        if index < len(arguments):
            servers = _document(Path(arguments[index])).get("mcpServers", {})
            url = servers.get("agent_parley", {}).get("url")
            return url if isinstance(url, str) else None
    return None


def _participant(
    home: Path,
    name: str,
    record: dict,
    port: int | None,
    expected: list[str],
) -> list[dict]:
    """Checks one lane's process binding, identity and required variables.

    Args:
        home: State home the lane must be bound to.
        name: Participant name.
        record: Participant entry of the project manifest.
        port: Service port the lane must use.
        expected: Environment variable names the lane process must export.

    Returns:
        Findings for the lane.
    """
    scope = f"participant {name}"
    lane = Path(record.get("lane", "")).resolve()
    if not lane.is_dir():
        return [_check(scope, "worktree", "fail", f"{lane} is missing.")]
    results = [_check(scope, "worktree", "pass", str(lane))]
    live = [
        item
        for item in _processes(lane)
        if "AGENT_PARLEY_TOKEN" in item["env"]
        and _bound_url(item["arguments"]) is not None
    ]
    if not live:
        return results + [
            _check(scope, "process", "warn", "No running lane client found.")
        ]
    for item in live:
        label = f"pid {item['pid']}"
        url = _bound_url(item["arguments"])
        if url is None:
            results.append(
                _check(
                    scope, f"{label} mcp url", "unknown", "Not in arguments."
                )
            )
        else:
            parsed = urlparse(url)
            loopback = parsed.hostname == "127.0.0.1"
            state = "pass" if loopback and parsed.port == port else "fail"
            results.append(
                _check(
                    scope,
                    f"{label} mcp url",
                    state,
                    f"{parsed.hostname}:{parsed.port}, "
                    f"expected 127.0.0.1:{port}",
                )
            )
        bound = item["env"].get("AGENT_PARLEY_HOME")
        results.append(
            _check(
                scope,
                f"{label} state home",
                "pass" if bound == str(home) else "fail",
                f"bound to {bound}",
            )
        )
        missing = [key for key in expected if key not in item["env"]]
        if expected:
            results.append(
                _check(
                    scope,
                    f"{label} required environment",
                    "fail" if missing else "pass",
                    f"missing {', '.join(missing)}" if missing else "present",
                )
            )
    return results


def inspect(home: Path, repo: Path, expected: list[str] | None = None) -> dict:
    """Reports whether a registered project's lanes are correctly wired.

    Args:
        home: State home to inspect.
        repo: Repository root registered in that home.
        expected: Environment variable names each lane process must export.

    Returns:
        A mapping with the findings and an overall ``ok`` flag.
    """
    home, repo = home.expanduser().resolve(), repo.expanduser().resolve()
    expected = expected or []
    port = _port(home)
    results = []
    if port is None:
        results.append(_check("service", "port", "fail", "No saved port."))
    else:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            listening = probe.connect_ex(("127.0.0.1", port)) == 0
        results.append(
            _check(
                "service",
                "listening",
                "pass" if listening and start.serving(home) else "fail",
                f"127.0.0.1:{port}",
            )
        )
        status = _unauthenticated(port)
        results.append(
            _check(
                "service",
                "authentication enforced",
                "pass" if status == 401 else "fail",
                f"tokenless request answered {status}",
            )
        )
    manifest = {}
    for path in (home / "projects").glob("*/project.json"):
        data = _document(path)
        if data.get("root") == str(repo):
            manifest = data
            break
    if not manifest:
        results.append(_check("project", "registered", "fail", str(repo)))
    else:
        results.append(_check("project", "registered", "pass", str(repo)))
        for name, record in sorted(manifest.get("participants", {}).items()):
            results.extend(_participant(home, name, record, port, expected))
    return {
        "read_only": True,
        "state_home": str(home),
        "port": port,
        "ok": all(item["state"] != "fail" for item in results),
        "checks": results,
        "evidence_limit": (
            "Local files, a tokenless HTTP probe and /proc observations; "
            "no model session or tool call was exercised."
        ),
    }


def render(report: dict) -> str:
    """Formats a report for a terminal.

    Args:
        report: Mapping returned by `inspect`.

    Returns:
        One line per finding plus the verdict.
    """
    lines = [
        f"{item['state']:<7} {item['scope']}: {item['name']} - {item['detail']}"
        for item in report["checks"]
    ]
    lines.append("wired" if report["ok"] else "NOT wired")
    return "\n".join(lines)


def main(home: Path, repo: Path, expected: list[str], as_json: bool) -> int:
    """Prints the report and returns a process status.

    Args:
        home: State home to inspect.
        repo: Registered repository root.
        expected: Environment variable names lanes must export.
        as_json: Print JSON instead of text.

    Returns:
        Zero when no check failed, otherwise one.
    """
    report = inspect(home, repo, expected)
    print(json.dumps(report, indent=2) if as_json else render(report))
    return 0 if report["ok"] else 1


__all__ = ["inspect", "main", "render"]
