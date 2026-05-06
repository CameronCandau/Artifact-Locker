from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifact_locker import cli
from artifact_locker.models import Artifact, Provenance, compute_sha256, load_manifest, next_artifact_id
from artifact_locker.oci import CHECKSUMS_TAG, MANIFEST_TAG, OrasError, OrasRunner, checksums_versioned_tag, manifest_versioned_tag
from artifact_locker.paths import init_repo, load_config
from artifact_locker.state import load_state, write_state


def write_sample_file(path: Path, content: bytes = b"sample") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def set_local_artifact_dir(repo: Path, directory: Path) -> None:
    config_path = repo / "config.json"
    config = json.loads(config_path.read_text())
    config["local_artifact_dir"] = str(directory)
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def invoke(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(args)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_creates_layout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    code, _, _ = invoke(["--catalog", str(repo), "init"], capsys)
    assert code == 0
    assert (repo / "catalog" / "artifacts.json").exists()
    assert (repo / "catalog" / "checksums.txt").exists()
    assert (repo / "staging" / "release-assets").is_dir()
    assert (repo / ".artifact-locker" / "state.json").exists()
    config = json.loads((repo / "config.json").read_text())
    assert config["local_artifact_dir"].endswith(".local/share/artifact-locker/artifacts")


def test_add_local_file_and_remove_by_filename(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = init_repo(tmp_path / "repo").root
    set_local_artifact_dir(repo, tmp_path / "managed-artifacts")
    source = write_sample_file(tmp_path / "SweetPotato.exe", b"abc123")
    code, _, _ = invoke(
        [
            "--catalog",
            str(repo),
            "add",
            str(source),
            "--platform",
            "windows",
            "--category",
            "bin",
            "--version",
            "v1.0.0",
            "--no-input",
        ],
        capsys,
    )
    assert code == 0
    manifest = load_manifest(repo / "catalog" / "artifacts.json")
    assert [item.artifact_id for item in manifest] == ["art-0001"]
    assert manifest[0].filename == "SweetPotato.exe"
    staged = repo / "staging" / "release-assets" / "art-0001"
    assert staged.read_bytes() == b"abc123"
    local_copy = tmp_path / "managed-artifacts" / "windows" / "bin" / "SweetPotato.exe"
    assert local_copy.read_bytes() == b"abc123"
    state = load_state(repo / ".artifact-locker" / "state.json")
    assert state["art-0001"].local_path == str(local_copy)
    checksums = (repo / "catalog" / "checksums.txt").read_text().strip()
    assert checksums.endswith("art-0001")

    code, _, _ = invoke(["--catalog", str(repo), "remove", "SweetPotato.exe"], capsys)
    assert code == 0
    assert load_manifest(repo / "catalog" / "artifacts.json") == []
    assert not staged.exists()


def test_add_url_only_artifact_with_interactive_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo").root
    set_local_artifact_dir(repo, tmp_path / "managed-artifacts")
    answers = iter([
        "linux",
        "bin",
        "",
        "",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(cli, "download_url_to_path", lambda uri, destination: destination.write_bytes(b"remote-bytes"))
    code, _, _ = invoke(
        [
            "--catalog",
            str(repo),
            "add",
            "https://example.test/tool.exe",
        ],
        capsys,
    )
    assert code == 0
    manifest = load_manifest(repo / "catalog" / "artifacts.json")
    artifact = manifest[0]
    assert artifact.artifact_id == "art-0001"
    assert artifact.filename == "tool.exe"
    assert artifact.sha256 == compute_sha256(write_sample_file(tmp_path / "expected.bin", b"remote-bytes"))
    assert artifact.provenance.uri == "https://example.test/tool.exe"
    assert (repo / "staging" / "release-assets" / "art-0001").read_bytes() == b"remote-bytes"
    assert (tmp_path / "managed-artifacts" / "linux" / "bin" / "tool.exe").read_bytes() == b"remote-bytes"
    assert "art-0001" in (repo / "catalog" / "checksums.txt").read_text()


def test_verify_catalog_rejects_invalid_manifest_and_orphans(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_paths = init_repo(tmp_path / "repo")
    bad = Artifact(
        artifact_id="art-0001",
        filename="bad.exe",
        platform="windows",
        category="bin",
        version="v1",
        sha256="deadbeef",
        staged_name="art-0001",
        active=True,
        provenance=Provenance(kind="built", repo="https://example.test/repo"),
    )
    repo_paths.manifest_path.write_text(json.dumps({"artifacts": [bad.to_dict(), bad.to_dict()]}, indent=2) + "\n")
    repo_paths.checksums_path.write_text("badchecksum  art-0001\n")
    write_sample_file(repo_paths.staging_dir / "orphan.bin", b"orphan")
    code, out, _ = invoke(["--catalog", str(repo_paths.root), "verify", "--catalog"], capsys)
    assert code == 1
    assert "duplicate artifact_id" in out or "catalog ok: no" in out


def test_verify_local_distinguishes_statuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_paths = init_repo(tmp_path / "repo")
    set_local_artifact_dir(repo_paths.root, tmp_path / "managed-artifacts")
    config = load_config(repo_paths)
    artifact_dir = repo_paths.artifact_dir(config)
    verified_artifact = Artifact.create(
        artifact_id="art-0001",
        filename="verified",
        platform="linux",
        category="bin",
        version="v1",
        sha256=compute_sha256(write_sample_file(tmp_path / "verified.bin", b"verified")),
        provenance=Provenance(kind="local"),
    )
    stale_artifact = Artifact.create(
        artifact_id="art-0002",
        filename="stale",
        platform="linux",
        category="bin",
        version="v1",
        sha256=compute_sha256(write_sample_file(tmp_path / "stale.bin", b"fresh")),
        provenance=Provenance(kind="local"),
    )
    present_artifact = Artifact.create(
        artifact_id="art-0003",
        filename="present",
        platform="linux",
        category="bin",
        version="v1",
        sha256=None,
        provenance=Provenance(kind="download", uri="https://example.test/present"),
    )
    missing_artifact = Artifact.create(
        artifact_id="art-0004",
        filename="missing",
        platform="linux",
        category="bin",
        version="v1",
        sha256=compute_sha256(write_sample_file(tmp_path / "missing.bin", b"missing")),
        provenance=Provenance(kind="local"),
    )
    artifacts = [missing_artifact, present_artifact, stale_artifact, verified_artifact]
    for artifact in artifacts:
        if artifact.staged_name:
            write_sample_file(repo_paths.staging_dir / artifact.staged_name, artifact.filename.encode())
    from artifact_locker.models import write_manifest, write_checksums

    write_manifest(repo_paths.manifest_path, artifacts)
    write_checksums(repo_paths.checksums_path, artifacts)

    verified_path = artifact_dir / "linux" / "bin" / "verified"
    present_path = artifact_dir / "linux" / "bin" / "present"
    stale_path = artifact_dir / "linux" / "bin" / "stale"
    write_sample_file(verified_path, b"verified")
    write_sample_file(present_path, b"present")
    write_sample_file(stale_path, b"wrong")
    state = {
        verified_artifact.artifact_id: cli.make_state_record(verified_artifact.artifact_id, verified_path, verified_artifact.sha256 or ""),
        stale_artifact.artifact_id: cli.make_state_record(stale_artifact.artifact_id, stale_path, stale_artifact.sha256 or ""),
    }
    write_state(repo_paths.state_path, state)
    code, out, _ = invoke(["--catalog", str(repo_paths.root), "verify", "--local", "--json"], capsys)
    assert code == 1
    payload = json.loads(out)
    statuses = {row["artifact_id"]: row["status"] for row in payload["local"]["artifacts"]}
    assert statuses[verified_artifact.artifact_id] == "verified"
    assert statuses[present_artifact.artifact_id] == "present"
    assert statuses[stale_artifact.artifact_id] == "stale"
    assert statuses[missing_artifact.artifact_id] == "missing"


class FakeOras:
    def __init__(self, remote_dir: Path):
        self.remote_dir = remote_dir
        self.commands: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def push_file(self, repository: str, tag: str, path: Path, media_type: str) -> None:
        self.commands.append(("push", f"{repository}:{tag}"))
        target = self.remote_dir / tag
        target.mkdir(parents=True, exist_ok=True)
        target.joinpath(path.name).write_bytes(path.read_bytes())

    def pull_to_dir(self, repository: str, tag: str, destination: Path) -> None:
        self.commands.append(("pull", f"{repository}:{tag}"))
        source_dir = self.remote_dir / tag
        destination.mkdir(parents=True, exist_ok=True)
        for item in source_dir.iterdir():
            destination.joinpath(item.name).write_bytes(item.read_bytes())

    def repo_tags(self, repository: str) -> None:
        self.commands.append(("tags", repository))


class EmptyRegistryOras(FakeOras):
    def pull_to_dir(self, repository: str, tag: str, destination: Path) -> None:
        raise OrasError(
            command=["oras", "pull", f"{repository}:{tag}"],
            returncode=1,
            stdout="",
            stderr="Error response from registry: manifest unknown",
        )


class UnauthorizedPushOras(FakeOras):
    def push_file(self, repository: str, tag: str, path: Path, media_type: str) -> None:
        raise OrasError(
            command=["oras", "push", f"{repository}:{tag}"],
            returncode=1,
            stdout="",
            stderr='Error response from registry: HEAD "https://public.ecr.aws/...": response status code 401: Unauthorized',
        )


def test_push_and_pull_use_expected_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    source_repo = init_repo(tmp_path / "source")
    target_repo = init_repo(tmp_path / "target")
    set_local_artifact_dir(source_repo.root, tmp_path / "source-artifacts")
    set_local_artifact_dir(target_repo.root, tmp_path / "target-artifacts")
    remote_dir = tmp_path / "remote"
    config = json.loads(source_repo.config_path.read_text())
    config["oci_repository"] = "example.test/catalog"
    source_repo.config_path.write_text(json.dumps(config, indent=2) + "\n")
    target_repo.config_path.write_text(json.dumps(config, indent=2) + "\n")
    src = write_sample_file(tmp_path / "tool.bin", b"artifact-bytes")
    invoke(
        [
            "--catalog",
            str(source_repo.root),
            "add",
            str(src),
            "--platform",
            "linux",
            "--category",
            "bin",
            "--version",
            "v1.2.3",
            "--no-input",
        ],
        capsys,
    )
    fake = FakeOras(remote_dir)
    monkeypatch.setattr(cli, "OrasRunner", lambda: fake)

    code, _, _ = invoke(["--catalog", str(source_repo.root), "push"], capsys)
    assert code == 0
    tags = [tag for action, tag in fake.commands if action == "push"]
    assert f"example.test/catalog:{MANIFEST_TAG}" in tags
    assert f"example.test/catalog:{manifest_versioned_tag(cli.default_push_tag())}" in tags
    assert f"example.test/catalog:{CHECKSUMS_TAG}" in tags
    assert f"example.test/catalog:{checksums_versioned_tag(cli.default_push_tag())}" in tags
    assert "example.test/catalog:art-0001" in tags

    fake.commands.clear()
    code, out, _ = invoke(["--catalog", str(target_repo.root), "pull", "--json"], capsys)
    assert code == 0
    downloaded = json.loads(out)
    assert downloaded[0]["artifact_id"] == "art-0001"
    state = load_state(target_repo.state_path)
    assert "art-0001" in state
    artifact_file = Path(state["art-0001"].local_path)
    assert artifact_file.read_bytes() == b"artifact-bytes"


def test_push_rewrites_stale_empty_checksums_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo")
    set_local_artifact_dir(repo.root, tmp_path / "managed-artifacts")
    config = json.loads(repo.config_path.read_text())
    config["oci_repository"] = "example.test/catalog"
    repo.config_path.write_text(json.dumps(config, indent=2) + "\n")
    source = write_sample_file(tmp_path / "tool.bin", b"artifact-bytes")
    invoke(
        [
            "--catalog",
            str(repo.root),
            "add",
            str(source),
            "--platform",
            "linux",
            "--category",
            "bin",
            "--version",
            "v1",
            "--no-input",
        ],
        capsys,
    )
    repo.checksums_path.write_text("")
    fake = FakeOras(tmp_path / "remote")
    monkeypatch.setattr(cli, "OrasRunner", lambda: fake)
    code, _, _ = invoke(["--catalog", str(repo.root), "push"], capsys)
    assert code == 0
    assert "art-0001" in repo.checksums_path.read_text()


def test_find_and_show_json_are_stable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = init_repo(tmp_path / "repo").root
    set_local_artifact_dir(repo, tmp_path / "managed-artifacts")
    source = write_sample_file(tmp_path / "tool.bin", b"artifact-bytes")
    invoke(
        [
            "--catalog",
            str(repo),
            "add",
            str(source),
            "--platform",
            "linux",
            "--category",
            "bin",
            "--version",
            "v1",
            "--no-input",
        ],
        capsys,
    )
    code, out, _ = invoke(["--catalog", str(repo), "find", "tool", "--json"], capsys)
    assert code == 0
    listing = json.loads(out)
    assert listing == [
        {
            "active": True,
            "artifact_id": "art-0001",
            "category": "bin",
            "filename": "tool.bin",
            "has_staged_asset": True,
            "local_status": "verified",
            "platform": "linux",
            "source_uri": None,
            "version": "v1",
        }
    ]
    code, out, _ = invoke(["--catalog", str(repo), "show", "tool.bin", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["artifact_id"] == "art-0001"


def test_next_artifact_id_is_incremental() -> None:
    assert next_artifact_id([]) == "art-0001"
    assert next_artifact_id(["art-0001", "art-0003"]) == "art-0004"


def test_managed_catalog_is_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    source = write_sample_file(tmp_path / "managed.bin", b"managed")
    code, _, _ = invoke(["init"], capsys)
    assert code == 0
    code, _, _ = invoke(["add", str(source), "--no-input"], capsys)
    assert code == 0
    managed_root = tmp_path / "xdg-data" / "artifact-locker"
    manifest = load_manifest(managed_root / "catalog" / "artifacts.json")
    assert [item.filename for item in manifest] == ["managed.bin"]


def test_pull_from_empty_registry_reports_clean_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo")
    config = json.loads(repo.config_path.read_text())
    config["oci_repository"] = "public.ecr.aws/o7l3z5i2/artifact-locker"
    repo.config_path.write_text(json.dumps(config, indent=2) + "\n")
    monkeypatch.setattr(cli, "OrasRunner", lambda: EmptyRegistryOras(tmp_path / "remote"))
    code, _, err = invoke(["--catalog", str(repo.root), "pull"], capsys)
    assert code == 1
    assert "remote catalog is empty or not initialized" in err
    assert MANIFEST_TAG in err


def test_push_to_public_ecr_reports_login_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo")
    set_local_artifact_dir(repo.root, tmp_path / "managed-artifacts")
    config = json.loads(repo.config_path.read_text())
    config["oci_repository"] = "public.ecr.aws/o7l3z5i2/artifact-locker"
    repo.config_path.write_text(json.dumps(config, indent=2) + "\n")
    source = write_sample_file(tmp_path / "tool.bin", b"artifact")
    invoke(["--catalog", str(repo.root), "add", str(source), "--no-input"], capsys)
    monkeypatch.setattr(cli, "OrasRunner", lambda: UnauthorizedPushOras(tmp_path / "remote"))
    code, _, err = invoke(["--catalog", str(repo.root), "push"], capsys)
    assert code == 1
    assert "registry auth is required for pushes" in err
    assert "aws ecr-public get-login-password --region us-east-1" in err
    assert "oras login -u AWS --password-stdin public.ecr.aws" in err


def test_oras_push_uses_relative_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(self: OrasRunner, args: list[str], cwd: Path | None = None):
        calls.append((args, cwd))
        return SimpleNamespace(args=args, stdout="", stderr="")

    monkeypatch.setattr(OrasRunner, "run", fake_run)
    payload = write_sample_file(tmp_path / "nested" / "artifact.json", b"{}")
    runner = OrasRunner()
    runner.push_file("example.test/catalog", "artifacts-catalog", payload, "application/json")
    assert calls == [
        (
            ["push", "example.test/catalog:artifacts-catalog", "artifact.json:application/json"],
            payload.parent.resolve(),
        )
    ]
