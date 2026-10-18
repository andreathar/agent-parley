"""Exercises real sparse checkouts without LFS downloads or native clients."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from agent_parley import agents, cli, pool, roster, workspace
from agent_parley.cli import git
from agent_parley.state import BridgeError, write_json


@pytest.fixture
def shared(bridge, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    marker = tmp_path / "smudge-called"
    (repo / ".gitattributes").write_text("*.mp4 filter=lfs\n")
    (repo / "inputs").mkdir()
    (repo / "inputs/clip.mp4").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        + "oid sha256:"
        + "a" * 64
        + "\nsize 123\n"
    )
    (repo / "inputs/frame.png").write_bytes(b"synthetic image")
    (repo / "catalog.csv").write_text(
        "asset_id,path\nsynthetic,inputs/clip.mp4\n"
    )
    git(repo, "config", "filter.lfs.clean", "cat")
    git(
        repo,
        "config",
        "filter.lfs.smudge",
        f"touch {shlex.quote(str(marker))}; cat",
    )
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Synthetic media",
    )
    bridge.setup(repo)
    preview = workspace.configure(bridge.home, repo, "catalog.csv")
    workspace.configure(
        bridge.home,
        repo,
        "catalog.csv",
        preview=False,
        expected_revision=preview["revision"],
    )
    return bridge.project(repo)[1], marker


def test_first_checkout_excludes_media_before_any_smudge(bridge, repo, shared):
    directory, marker = shared
    data = bridge.add_participant(repo, "reader", "codex")
    lane = Path(data["participants"]["reader"]["lane"])
    assert not (lane / "inputs/clip.mp4").exists()
    assert not (lane / "inputs/frame.png").exists()
    assert (lane / "shared.txt").read_text() == "original\n"
    assert not marker.exists()
    assert git(lane, "status", "--porcelain") == ""
    assert git(lane, "config", "--worktree", "core.sparseCheckout") == "true"
    with pytest.raises(BridgeError):
        git(repo, "config", "--get", "core.sparseCheckout")
    assert (repo / "inputs/frame.png").read_bytes() == b"synthetic image"
    assert not (directory / "reader-identity.json").exists()


def test_readmit_retained_branch_uses_sparse_before_checkout(
    bridge, repo, shared
):
    directory, marker = shared
    data = bridge.add_participant(repo, "reader", "codex")
    entry = data["participants"]["reader"]
    git(repo, "worktree", "remove", entry["lane"])
    document = roster.read(directory)
    document["participants"]["reader"]["retired"] = 1.0
    write_json(directory / "project.json", document)
    data = bridge.add_participant(repo, "reader", "codex")
    assert not Path(
        data["participants"]["reader"]["lane"], "inputs/clip.mp4"
    ).exists()
    assert not marker.exists()


def test_readmit_refuses_a_missing_retained_workspace_profile(
    bridge, repo, shared
):
    directory, marker = shared
    data = bridge.add_participant(repo, "reader", "codex")
    lane = data["participants"]["reader"]["lane"]
    git(repo, "worktree", "remove", lane)
    document = roster.read(directory)
    document["participants"]["reader"]["retired"] = 1.0
    document.pop("workspace")
    write_json(directory / "project.json", document)

    with pytest.raises(
        BridgeError, match="restore the matching project profile"
    ):
        bridge.add_participant(repo, "reader", "codex")

    assert not Path(lane).exists()
    assert not marker.exists()
    participant = roster.read(directory)["participants"]["reader"]
    assert "workspace_profile" in participant


def test_checkout_failure_preserves_partial_lane_without_full_fallback(
    bridge, repo, shared, monkeypatch
):
    directory, marker = shared
    original = workspace.exclusions

    def fail(*args, **kwargs):
        raise BridgeError("Synthetic sparse failure")

    monkeypatch.setattr(workspace, "exclusions", fail)
    with pytest.raises(BridgeError, match="Synthetic sparse"):
        bridge.add_participant(repo, "reader", "codex")
    assert (directory / "reader/.git").exists()
    assert not (directory / "reader/inputs/clip.mp4").exists()
    assert not marker.exists()
    monkeypatch.setattr(workspace, "exclusions", original)


@pytest.mark.parametrize("adapter", sorted(workspace.ADAPTERS))
def test_supported_adapter_roots_are_references_not_native_proof(
    bridge, repo, shared, adapter
):
    manifest = bridge.add_participant(repo, "reader", "codex")
    entry = manifest["participants"]["reader"]
    env = workspace.launch_roots(manifest, entry, adapter, "review-1")
    assert env["AGENT_PARLEY_MEDIA_ROOT"] == str(repo)
    assert env["AGENT_PARLEY_OUTPUT_ROOT"].endswith("reader/outputs/review-1")
    assert not Path(env["AGENT_PARLEY_OUTPUT_ROOT"]).exists()
    capabilities = workspace.capability(adapter)
    assert capabilities["native_read_verified"] is None
    assert capabilities["readonly_enforced"] is False


def test_unknown_adapter_does_not_fall_back(bridge, repo, shared):
    manifest = bridge.add_participant(repo, "reader", "codex")
    with pytest.raises(BridgeError, match="cannot launch"):
        workspace.launch_roots(
            manifest, manifest["participants"]["reader"], "amp", "job"
        )


@pytest.mark.parametrize(
    "value",
    ["../media", "/tmp/media", "C:/media", "a\\b", "a//b", "a/./b", "a\nb"],
)
def test_traversal_and_platform_path_confusion_fail(tmp_path, value):
    with pytest.raises(BridgeError):
        workspace.resolve(tmp_path, value, existing=False)


def test_symlink_cannot_redirect_inputs_or_outputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BridgeError, match="symlink"):
        workspace.resolve(source, "escape/file", existing=False)


def test_profile_preview_cas_audit_and_existing_lanes_unchanged(
    paired, bridge, repo
):
    directory = bridge.project(repo)[1]
    path = directory / "project.json"
    (repo / "catalog.csv").write_text("asset_id,path\n")
    before = path.read_bytes()
    preview = workspace.configure(bridge.home, repo, "catalog.csv")
    assert path.read_bytes() == before
    document = json.loads(before)
    document["concurrent"] = "preserve"
    write_json(path, document)
    with pytest.raises(BridgeError, match="Configuration changed"):
        workspace.configure(
            bridge.home,
            repo,
            "catalog.csv",
            preview=False,
            expected_revision=preview["revision"],
        )
    preview = workspace.configure(bridge.home, repo, "catalog.csv")
    applied = workspace.configure(
        bridge.home,
        repo,
        "catalog.csv",
        preview=False,
        expected_revision=preview["revision"],
    )
    updated = path.read_bytes()
    workspace.configure(
        bridge.home,
        repo,
        "catalog.csv",
        preview=False,
        expected_revision=preview["revision"],
    )
    assert path.read_bytes() == updated
    manifest = roster.read(directory)
    assert json.loads(updated)["concurrent"] == "preserve"
    assert len(manifest["workspace_history"]) == 1
    assert applied["permissions_changed"] is False
    assert all(
        "workspace_profile" not in row
        for row in manifest["participants"].values()
    )
    with pytest.raises(BridgeError, match="another workspace profile"):
        workspace.launch_roots(
            manifest, manifest["participants"]["claude"], "claude", "job"
        )


@pytest.mark.parametrize(
    "setting", [{"pool": 1}, {"initialize": ["synthetic-init"]}]
)
def test_unreviewed_pool_or_initializer_is_refused(bridge, repo, setting):
    bridge.setup(repo)
    (repo / "catalog.csv").write_text("header\n")
    directory = bridge.project(repo)[1]
    manifest = roster.read(directory)
    manifest.update(setting)
    write_json(directory / "project.json", manifest)
    before = (directory / "project.json").read_bytes()
    with pytest.raises(BridgeError):
        workspace.configure(bridge.home, repo, "catalog.csv")
    assert (directory / "project.json").read_bytes() == before


def test_pool_does_not_create_a_spare_for_light_profile(bridge, repo, shared):
    directory, marker = shared
    assert pool.fill(repo, directory, roster.read(directory)) == []
    assert not (directory / "spares.json").exists()
    assert not marker.exists()


def test_agent_panel_paths_and_host_probe_are_distinct(bridge, repo, shared):
    bridge.add_participant(repo, "reader", "codex")
    result = workspace.status(bridge.home, repo, sample="inputs/clip.mp4")
    assert result["sample"]["lfs_pointer"] is True
    assert result["sample"]["host_read_verified"] is True
    assert result["native_read_verified"] is None
    assert result["warnings"]
    reading = agents.inventory(bridge.home)["projects"][0]["agents"][0]
    assert reading["workspace"]["media_root"] == str(repo)
    assert reading["workspace"]["profile_matches"] is True


def test_light_launch_requires_task_id_before_creating_a_lane(
    bridge, repo, shared
):
    directory, _ = shared
    with pytest.raises(BridgeError):
        bridge.launch("codex", repo, "Synthetic task")
    assert not (directory / "codex").exists()


def test_native_command_gets_same_canonical_root_and_own_task_outputs(
    bridge, repo, shared, monkeypatch
):
    from agent_parley import terminal

    captured = {}

    async def identity(*args):
        return {"registration_token": "synthetic-token"}

    def native(command, lane, env):
        captured.update(command=command, lane=lane, env=env)
        return 0

    monkeypatch.setattr(bridge, "up", lambda: None)
    monkeypatch.setattr(bridge, "identity", identity)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/true")
    monkeypatch.setattr(terminal, "call", native)
    assert (
        bridge.launch(
            "codex", repo, "Synthetic local task", output_task="review-1"
        )
        == 0
    )
    assert captured["env"]["AGENT_PARLEY_MEDIA_ROOT"] == str(repo)
    output = Path(captured["env"]["AGENT_PARLEY_OUTPUT_ROOT"])
    assert output.is_dir()
    assert output == captured["lane"] / "outputs/review-1"
    assert (repo / "inputs/frame.png").read_bytes() == b"synthetic image"


def test_status_json_does_not_start_state_and_doctor_reports_pointer(
    bridge, repo, shared, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "media",
            "doctor",
            "--repo",
            str(repo),
            "--sample",
            "inputs/clip.mp4",
            "--json",
        ],
    )
    assert cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "agent-parley/read/v1"
    assert result["sample"]["lfs_pointer"] is True
    assert result["native_read_verified"] is None


def test_disabling_profile_keeps_light_lane_and_refuses_full_launch(
    bridge, repo, shared
):
    data = bridge.add_participant(repo, "reader", "codex")
    preview = workspace.configure(bridge.home, repo, "", disable=True)
    workspace.configure(
        bridge.home,
        repo,
        "",
        disable=True,
        preview=False,
        expected_revision=preview["revision"],
    )
    document = roster.read(shared[0])
    with pytest.raises(BridgeError, match="retains"):
        workspace.launch_roots(
            document, data["participants"]["reader"], "codex", "review-1"
        )
    assert not Path(
        data["participants"]["reader"]["lane"], "inputs/clip.mp4"
    ).exists()


@pytest.mark.parametrize(
    "group,action",
    [
        ("workspace", "plan"),
        ("workspace", "config"),
        ("media", "status"),
        ("media", "doctor"),
    ],
)
def test_cli_help_and_json_contracts(group, action, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent-parley", group, action, "--help"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_doctor_does_not_probe_credentials_or_generic_files(
    bridge, repo, shared
):
    (repo / ".env").write_text("synthetic-private-placeholder")
    with pytest.raises(BridgeError, match="supported media"):
        workspace.status(bridge.home, repo, sample=".env")


def test_profile_rejects_boolean_version(bridge, repo, shared):
    data = roster.read(shared[0])
    data["workspace"]["version"] = True
    with pytest.raises(BridgeError, match="Invalid workspace"):
        workspace.profile(data)


def test_output_file_refuses_before_registration(bridge, repo, shared):
    data = bridge.add_participant(repo, "reader", "codex")
    entry = data["participants"]["reader"]
    lane = Path(entry["lane"])
    (lane / "outputs").mkdir()
    (lane / "outputs/job").write_text("synthetic prior output")
    with pytest.raises(BridgeError, match="path is a file"):
        workspace.launch_roots(data, entry, "codex", "job")


def test_doctor_host_read_is_not_a_native_access_pass(
    bridge, repo, shared, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "media",
            "doctor",
            "--repo",
            str(repo),
            "--sample",
            "inputs/frame.png",
            "--json",
        ],
    )
    assert cli.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["sample"]["host_read_verified"] is True
    assert result["native_read_verified"] is None
    assert "Native media access is unverified" in result["warnings"][-1]
