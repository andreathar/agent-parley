"""Checks host coordination across independent projects and state homes."""

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_parley import host_resources, store
from agent_parley.state import BridgeError


def identity(tmp_path, name="codex", project="one"):
    return {"name": name, "project": str(tmp_path / project)}


@pytest.mark.parametrize(
    "other_home,other_project", [(False, True), (True, False), (True, True)]
)
def test_resources_conflict_across_projects_and_installations(
    tmp_path, other_home, other_project
):
    first = identity(tmp_path)
    second = identity(tmp_path, project="two" if other_project else "one")
    state = tmp_path / "state"
    host_resources.reserve(state, first, ["gpu:0", "port:5432"])
    result = host_resources.reserve(
        tmp_path / "other" if other_home else state,
        second,
        ["gpu:0", "device:android"],
    )
    assert result["granted"] == []
    assert [v["resource"] for v in result["conflicts"]] == ["gpu:0"]
    assert {v["resource"] for v in host_resources.inspect()["holdings"]} == {
        "gpu:0",
        "port:5432",
    }


def test_repeat_is_stable_foreign_release_refuses_and_stale_release_is_safe(
    tmp_path,
):
    actor = identity(tmp_path)
    owner = host_resources.owner(tmp_path, actor)["owner"]
    first = host_resources.reserve(tmp_path, actor, ["gpu:0"])["granted"][0]
    assert (
        host_resources.reserve(tmp_path, actor, ["gpu:0"])["granted"][0]
        == first
    )
    with pytest.raises(BridgeError, match="only its own"):
        host_resources.release([first["lease_id"]], owner_id="someone-else")
    host_resources.release([first["lease_id"]], owner_id=owner)
    second = host_resources.reserve(tmp_path, actor, ["gpu:0"])["granted"][0]
    assert second["lease_id"] != first["lease_id"]
    assert host_resources.release([first["lease_id"]], owner_id=owner) == {
        "released": []
    }
    assert host_resources.inspect()["holdings"] == [second]


def test_time_and_process_restarts_do_not_release_holdings(
    tmp_path, monkeypatch
):
    first = identity(tmp_path)
    lease = host_resources.reserve(tmp_path, first, ["gpu:0"])
    monkeypatch.setattr(host_resources.time, "time", lambda: 999999999999)
    assert (
        host_resources.reserve(
            tmp_path, identity(tmp_path, "claude"), ["gpu:0"]
        )["conflicts"]
        == lease["granted"]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from agent_parley import host_resources; "
            "print(json.dumps(host_resources.inspect()))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["holdings"] == lease["granted"]


def test_simultaneous_processes_grant_exactly_one_owner(tmp_path):
    code = (
        "import json,sys; from pathlib import Path; "
        "from agent_parley import host_resources; "
        "print(json.dumps(host_resources.reserve(Path(sys.argv[1]), "
        "{'name':sys.argv[2], 'project':sys.argv[1]}, ['gpu:0'])))"
    )

    def compete(number):
        return json.loads(
            subprocess.run(
                [sys.executable, "-c", code, str(tmp_path), str(number)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(compete, range(4)))
    assert sum(bool(result["granted"]) for result in results) == 1
    assert sum(bool(result["conflicts"]) for result in results) == 3


def test_corrupt_registry_fails_closed(tmp_path):
    host_resources.reserve(tmp_path, identity(tmp_path), ["gpu:0"])
    path = host_resources.home() / "resources.json"
    path.write_text('{"version": 99, "holdings": {}}')
    with pytest.raises(BridgeError, match="Invalid host"):
        host_resources.reserve(
            tmp_path, identity(tmp_path, "claude"), ["gpu:0"]
        )
    assert json.loads(path.read_text())["version"] == 99


def test_host_registry_refuses_nonprivate_directory_without_chmod():
    directory = host_resources.home()
    directory.mkdir()
    directory.chmod(0o755)
    with pytest.raises(BridgeError, match="private"):
        host_resources.inspect()
    assert directory.stat().st_mode & 0o777 == 0o755
    assert not list(directory.iterdir())


def test_mixed_owner_release_is_atomic(tmp_path):
    first = identity(tmp_path)
    second = identity(tmp_path, "claude")
    own = host_resources.reserve(tmp_path, first, ["gpu:0"])["granted"][0]
    foreign = host_resources.reserve(tmp_path, second, ["port:5432"])[
        "granted"
    ][0]
    before = host_resources.inspect()
    with pytest.raises(BridgeError, match="only its own"):
        host_resources.release(
            [own["lease_id"], foreign["lease_id"]],
            owner_id=host_resources.owner(tmp_path, first)["owner"],
        )
    assert host_resources.inspect() == before


def test_host_tools_over_real_authenticated_mcp(bridge, repo, paired):
    bridge.up()
    codex = store.register(bridge.home, paired["root"], "codex")[
        "registration_token"
    ]
    claude = store.register(bridge.home, paired["root"], "claude")[
        "registration_token"
    ]

    async def call(token, tool, args):
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, trust_env=False
        ) as http:
            async with streamable_http_client(
                bridge.url + "/mcp/", http_client=http
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return await session.call_tool(tool, args)

    async def scenario():
        result = await call(
            codex, "reserve_host_resources", {"resources": ["gpu:0"]}
        )
        assert not result.is_error
        lease = json.loads(result.content[0].text)["granted"][0]
        conflict = await call(
            claude, "reserve_host_resources", {"resources": ["gpu:0"]}
        )
        assert json.loads(conflict.content[0].text)["conflicts"]
        refused = await call(
            claude, "release_host_resources", {"lease_ids": [lease["lease_id"]]}
        )
        assert refused.is_error
        released = await call(
            codex, "release_host_resources", {"lease_ids": [lease["lease_id"]]}
        )
        assert not released.is_error
        assert json.loads(released.content[0].text)["released"] == [lease]

    asyncio.run(scenario())
