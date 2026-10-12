import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agent_parley import checkpoints, cli, roles
from agent_parley.state import BridgeError

SPEC = {
    "version": 1,
    "roles": {
        "review": {
            "write_paths": ["reviews/*"],
            "deny_paths": ["reviews/secret*"],
            "deny_commands": [r"\bgit\s+push\b"],
            "deny_tools": ["mcp__video__*"],
        },
        "readonly": {"write": False},
    },
    "assign": {
        "copilot": {"role": "review"},
        "opencode": {"role": "readonly", "disabled": ["shell"]},
    },
}


def call(tool, **tool_input):
    return {"tool_name": tool, "tool_input": tool_input}


def test_unassigned_lane_is_never_restricted(tmp_path):
    spec = roles.validate(SPEC)

    assert (
        roles.decide(spec, "claude", call("Write", path="x"), tmp_path) is None
    )


@pytest.mark.parametrize("tool", ["Write", "write", "edit", "apply_patch"])
def test_write_outside_allowed_paths_is_refused_for_every_adapter_name(
    tmp_path, tool
):
    spec = roles.validate(SPEC)

    reason = roles.decide(
        spec, "copilot", call(tool, path="src/a.py"), tmp_path
    )

    assert reason and "reviews/*" in reason


def test_opencode_camel_case_path_is_checked(tmp_path):
    spec = roles.validate(SPEC)

    assert roles.decide(
        spec, "copilot", call("write", filePath="src/a.py"), tmp_path
    )


def test_write_inside_allowed_paths_passes(tmp_path):
    spec = roles.validate(SPEC)

    assert (
        roles.decide(
            spec, "copilot", call("Write", path="reviews/a.md"), tmp_path
        )
        is None
    )


def test_deny_path_wins_over_allowed_path(tmp_path):
    spec = roles.validate(SPEC)

    reason = roles.decide(
        spec, "copilot", call("Write", path="reviews/secret.md"), tmp_path
    )

    assert reason and "may not write" in reason


def test_write_outside_worktree_and_traversal_are_refused(tmp_path):
    spec = roles.validate(SPEC)

    for target in ("/etc/passwd", "reviews/../src/a.py"):
        assert roles.decide(
            spec, "copilot", call("Write", path=target), tmp_path
        )


def test_patch_paths_are_checked(tmp_path):
    spec = roles.validate(SPEC)
    patch = "*** Begin Patch\n*** Update File: src/a.py\n*** End Patch"

    assert roles.decide(
        spec, "copilot", call("apply_patch", input=patch), tmp_path
    )


def test_shell_patterns_and_tool_globs_are_refused(tmp_path):
    spec = roles.validate(SPEC)

    assert roles.decide(
        spec, "copilot", call("Bash", command="git push"), tmp_path
    )
    assert roles.decide(spec, "copilot", call("mcp__video__render"), tmp_path)
    assert (
        roles.decide(
            spec, "copilot", call("Bash", command="git diff"), tmp_path
        )
        is None
    )


def test_disabled_capability_and_read_only_refuse(tmp_path):
    spec = roles.validate(SPEC)

    assert roles.decide(spec, "opencode", call("bash", command="ls"), tmp_path)
    assert roles.decide(spec, "opencode", call("write", path="a"), tmp_path)
    assert (
        roles.decide(spec, "opencode", call("read", path="a"), tmp_path) is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 2},
        {"version": 1, "roles": {"r": {"surprise": 1}}},
        {"version": 1, "roles": {"r": {"deny_commands": ["("]}}},
        {"version": 1, "roles": {}, "assign": {"a": {"role": "missing"}}},
        {
            "version": 1,
            "roles": {"r": {}},
            "assign": {"a": {"role": "r", "disabled": ["fly"]}},
        },
    ],
)
def test_invalid_documents_are_rejected(bad):
    with pytest.raises(BridgeError):
        roles.validate(bad)


def test_save_load_and_digest_round_trip(tmp_path):
    roles.save(tmp_path, SPEC)

    loaded = roles.load(tmp_path)

    assert loaded and roles.digest(loaded) == roles.digest(roles.validate(SPEC))
    assert roles.load(tmp_path / "missing") is None
    assert roles.digest(None) == ""


def test_checkpoint_refusal_follows_the_rules_file(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    payload = call("Write", path="src/a.py")

    assert checkpoints.role_refusal(tmp_path, "copilot", payload, lane) is None
    roles.save(tmp_path, SPEC)
    assert checkpoints.role_refusal(tmp_path, "copilot", payload, lane)
    assert checkpoints.role_refusal(tmp_path, "claude", payload, lane) is None


def test_unreadable_rules_refuse_mutations_but_not_reads(tmp_path):
    (tmp_path / "roles.json").write_text("{not json")
    lane = tmp_path

    assert checkpoints.role_refusal(
        tmp_path, "any", call("Bash", command="ls"), lane
    )
    assert checkpoints.role_refusal(
        tmp_path, "any", call("Write", path="a"), lane
    )
    assert (
        checkpoints.role_refusal(tmp_path, "any", call("Read", path="a"), lane)
        is None
    )


def run_role(bridge, repo, monkeypatch, capsys, *words):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "role",
            "--repo",
            str(repo),
        ]
        + list(words),
    )
    status = cli.main()
    return status, capsys.readouterr()


def test_role_commands_apply_a_template_and_toggle_capabilities(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]

    status, out = run_role(bridge, repo, monkeypatch, capsys, "show")
    assert status == 0 and "no lane is restricted" in out.out

    status, out = run_role(
        bridge, repo, monkeypatch, capsys, "apply", "review", "--to", "codex"
    )
    assert status == 0 and "Applied review to codex" in out.out
    spec = roles.load(directory)
    assert spec["assign"]["codex"] == {"role": "review"}

    run_role(bridge, repo, monkeypatch, capsys, "disable", "codex", "shell")
    assert roles.load(directory)["assign"]["codex"]["disabled"] == ["shell"]
    run_role(bridge, repo, monkeypatch, capsys, "enable", "codex", "shell")
    assert "disabled" not in roles.load(directory)["assign"]["codex"]

    status, out = run_role(
        bridge, repo, monkeypatch, capsys, "remove", "review"
    )
    assert status != 0 or "still assigned" in out.err + out.out

    status, out = run_role(
        bridge, repo, monkeypatch, capsys, "unassign", "codex"
    )
    assert status == 0 and roles.load(directory)["assign"] == {}


def test_role_define_stores_typed_rules(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]

    status, _ = run_role(
        bridge,
        repo,
        monkeypatch,
        capsys,
        "define",
        "scribe",
        "--read-only",
        "--deny-command",
        "curl",
    )

    assert status == 0
    assert roles.load(directory)["roles"]["scribe"] == {
        "write": False,
        "deny_commands": ["curl"],
    }


MOVIES = Path(__file__).resolve().parents[1] / "docs/examples/movies-roles.json"
HOOKS = Path("/mnt/data/Projects_SSD/Movies_Projects/.claude/hooks")
COMMANDS = [
    "git clean -fd",
    "git reset --hard HEAD",
    "rm -rf projects/film/07_generations",
    "rm -rf /tmp/scratch",
    "ls projects/film && rm notes.txt",
    "mv projects/film/inbox/a.mp4 /tmp",
    "git restore projects/film/00_brief/x",
    "git restore --staged projects/film/00_brief/x",
    "find projects -name '*.tmp' -delete",
    "rsync -a --delete a/ projects/film/",
    "git status && echo done",
    "cat projects/film/manifest.json",
]


def test_movies_example_validates_and_imports(
    bridge, repo, paired, monkeypatch, capsys
):
    directory = bridge.project(repo)[1]

    status, _ = run_role(
        bridge, repo, monkeypatch, capsys, "import", str(MOVIES)
    )
    run_role(
        bridge, repo, monkeypatch, capsys, "assign", "codex", "movies-lane"
    )

    assert status == 0
    assert roles.load(directory)["assign"]["codex"] == {"role": "movies-lane"}


@pytest.mark.skipif(not HOOKS.is_dir(), reason="Movies hooks not on this host")
def test_movies_media_guard_parity(tmp_path):
    sys.path.insert(0, str(HOOKS))
    spec = importlib.util.spec_from_file_location(
        "media_guard", HOOKS / "media-guard.py"
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    document = json.loads(MOVIES.read_text())
    document["assign"] = {"codex": {"role": "movies-lane"}}
    rules = roles.validate(document)

    for command in COMMANDS:
        hook = any(
            guard.destructive(segment)
            for segment in guard.SEPARATOR.split(command)
        )
        ours = bool(
            roles.decide(
                rules, "codex", call("Bash", command=command), tmp_path
            )
        )
        assert ours == hook, command


def test_movies_role_blocks_canonical_records_and_paid_calls(tmp_path):
    document = json.loads(MOVIES.read_text())
    document["assign"] = {"codex": {"role": "movies-lane"}}
    rules = roles.validate(document)

    for path in ("projects/f/manifest.json", "AGENTS.md"):
        assert roles.decide(rules, "codex", call("Write", path=path), tmp_path)
    assert (
        roles.decide(
            rules, "codex", call("Write", path="projects/f/notes.md"), tmp_path
        )
        is None
    )
    for command in (
        "python scripts/kling_batch.py --spec a.json",
        "kling image_to_video --prompt x",
    ):
        assert roles.decide(
            rules, "codex", call("Bash", command=command), tmp_path
        )
    assert (
        roles.decide(
            rules, "codex", call("Bash", command="kling balance"), tmp_path
        )
        is None
    )
