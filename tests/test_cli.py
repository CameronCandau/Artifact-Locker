from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifact_locker import cli
from artifact_locker.models import load_manifest, next_artifact_id
from artifact_locker.oci import CHECKSUMS_TAG, MANIFEST_TAG, CommandResult, OrasError, OrasRunner
from artifact_locker.paths import init_repo

TEST_UUID_1 = "0196f3d4-7b2a-7c91-9c5c-2a4b7d8e9f10"
TEST_UUID_3 = "0196f3d4-7b2c-7d44-a123-456789abcdef"


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


def assert_is_uuid7(value: str) -> None:
    parsed = uuid.UUID(value)
    assert str(parsed) == value
    assert parsed.version == 7


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

    def repo_tags(self, repository: str) -> CommandResult:
        self.commands.append(("tags", repository))
        tags = "\n".join(sorted(item.name for item in self.remote_dir.iterdir() if item.is_dir()))
        return CommandResult(args=["oras", "repo", "tags", repository], stdout=tags, stderr="")

    def delete_manifest(self, repository: str, tag: str) -> None:
        self.commands.append(("delete", f"{repository}:{tag}"))
        shutil.rmtree(self.remote_dir / tag, ignore_errors=True)


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
            stderr=(
                'Error response from registry: HEAD "https://public.ecr.aws/...": '
                "response status code 401: Unauthorized"
            ),
        )


def test_init_creates_layout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    code, _, _ = invoke(["--catalog", str(repo), "init"], capsys)
    assert code == 0
    assert (repo / "catalog" / "artifacts.json").exists()
    assert (repo / "catalog" / "checksums.txt").exists()
    assert (repo / "staging" / "release-assets").is_dir()
    assert not (repo / ".artifact-locker").exists()


def test_add_local_file_and_remove_by_filename(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
            "--no-input",
        ],
        capsys,
    )
    assert code == 0
    manifest = load_manifest(repo / "catalog" / "artifacts.json")
    artifact_id = manifest[0].artifact_id
    assert_is_uuid7(artifact_id)
    staged = repo / "staging" / "release-assets" / artifact_id
    assert staged.read_bytes() == b"abc123"
    local_copy = (
        tmp_path / "managed-artifacts" / "windows" / "bin" / artifact_id / "SweetPotato.exe"
    )
    assert local_copy.read_bytes() == b"abc123"

    code, _, _ = invoke(["--catalog", str(repo), "remove", "SweetPotato.exe"], capsys)
    assert code == 0
    assert load_manifest(repo / "catalog" / "artifacts.json") == []
    assert not staged.exists()
    assert not local_copy.exists()


def test_add_url_downloads_and_stores_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo").root
    set_local_artifact_dir(repo, tmp_path / "managed-artifacts")
    answers = iter(["linux", "bin"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        cli,
        "download_url_to_path",
        lambda uri, destination: destination.write_bytes(b"remote-bytes"),
    )
    code, _, _ = invoke(["--catalog", str(repo), "add", "https://example.test/tool.exe"], capsys)
    assert code == 0
    artifact = load_manifest(repo / "catalog" / "artifacts.json")[0]
    assert artifact.filename == "tool.exe"
    assert (
        tmp_path / "managed-artifacts" / "linux" / "bin" / artifact.artifact_id / "tool.exe"
    ).read_bytes() == b"remote-bytes"


def test_verify_catalog_and_local_statuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    set_local_artifact_dir(repo.root, tmp_path / "managed-artifacts")
    source = write_sample_file(tmp_path / "tool.bin", b"artifact")
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
            "--no-input",
        ],
        capsys,
    )
    code, out, _ = invoke(["--catalog", str(repo.root), "verify", "--all"], capsys)
    assert code == 0
    assert "catalog ok: yes" in out
    assert "local ok: yes" in out

    artifact = load_manifest(repo.manifest_path)[0]
    payload_path = (
        tmp_path / "managed-artifacts" / "linux" / "bin" / artifact.artifact_id / "tool.bin"
    )
    payload_path.write_bytes(b"wrong")
    code, out, _ = invoke(["--catalog", str(repo.root), "verify", "--local"], capsys)
    assert code == 1
    assert f"- {artifact.artifact_id}: stale" in out


def test_push_and_pull_use_expected_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_repo = init_repo(tmp_path / "source")
    target_repo = init_repo(tmp_path / "target")
    set_local_artifact_dir(source_repo.root, tmp_path / "source-artifacts")
    set_local_artifact_dir(target_repo.root, tmp_path / "target-artifacts")
    remote_dir = tmp_path / "remote"
    source_config = json.loads(source_repo.config_path.read_text())
    source_config["oci_repository"] = "example.test/catalog"
    source_repo.config_path.write_text(json.dumps(source_config, indent=2) + "\n")
    target_config = json.loads(target_repo.config_path.read_text())
    target_config["oci_repository"] = "example.test/catalog"
    target_repo.config_path.write_text(json.dumps(target_config, indent=2) + "\n")
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
            "--no-input",
        ],
        capsys,
    )
    fake = FakeOras(remote_dir)
    monkeypatch.setattr(cli, "OrasRunner", lambda: fake)

    code, _, _ = invoke(["--catalog", str(source_repo.root), "push"], capsys)
    assert code == 0
    artifact_id = load_manifest(source_repo.manifest_path)[0].artifact_id
    tags = [tag for action, tag in fake.commands if action == "push"]
    assert f"example.test/catalog:{MANIFEST_TAG}" in tags
    assert f"example.test/catalog:{CHECKSUMS_TAG}" in tags
    assert f"example.test/catalog:{artifact_id}" in tags

    fake.commands.clear()
    code, out, _ = invoke(["--catalog", str(target_repo.root), "pull", "--json"], capsys)
    assert code == 0
    downloaded = json.loads(out)
    assert downloaded[0]["artifact_id"] == artifact_id
    artifact_file = tmp_path / "target-artifacts" / "linux" / "bin" / artifact_id / "tool.bin"
    assert artifact_file.read_bytes() == b"artifact-bytes"


def test_push_prunes_removed_and_legacy_remote_tags(
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
            "--no-input",
        ],
        capsys,
    )
    artifact_id = load_manifest(repo.manifest_path)[0].artifact_id
    fake = FakeOras(tmp_path / "remote")
    legacy_dir = fake.remote_dir / "art-0001"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_dir.joinpath("artifact.bin").write_bytes(b"legacy")
    monkeypatch.setattr(cli, "OrasRunner", lambda: fake)

    code, _, _ = invoke(["--catalog", str(repo.root), "push"], capsys)
    assert code == 0
    assert not legacy_dir.exists()

    code, _, _ = invoke(["--catalog", str(repo.root), "remove", "tool.bin"], capsys)
    assert code == 0
    code, out, _ = invoke(["--catalog", str(repo.root), "push"], capsys)
    assert code == 0
    delete_tags = [tag for action, tag in fake.commands if action == "delete"]
    assert f"example.test/catalog:{artifact_id}" in delete_tags
    assert "example.test/catalog:art-0001" in delete_tags
    assert "remote tags seen:" in out
    assert "remote tags to delete:" in out


def test_find_and_show_json_are_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
            "--no-input",
        ],
        capsys,
    )
    code, out, _ = invoke(["--catalog", str(repo), "find", "tool", "--json"], capsys)
    assert code == 0
    listing = json.loads(out)
    artifact_id = listing[0]["artifact_id"]
    assert listing == [
        {
            "artifact_id": artifact_id,
            "category": "bin",
            "filename": "tool.bin",
            "has_staged_asset": True,
            "local_status": "verified",
            "platform": "linux",
        }
    ]
    code, out, _ = invoke(["--catalog", str(repo), "show", "tool.bin", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["artifact_id"] == artifact_id


def test_managed_catalog_is_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_pull_does_not_overwrite_local_catalog_when_remote_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo")
    config = json.loads(repo.config_path.read_text())
    config["oci_repository"] = "example.test/catalog"
    repo.config_path.write_text(json.dumps(config, indent=2) + "\n")
    original_manifest = {
        "artifacts": [
            {
                "artifact_id": TEST_UUID_1,
                "filename": "local.bin",
                "platform": "linux",
                "category": "bin",
                "sha256": "d2dbf006f96dd05044a8f63d8f118f23925ba4cc5750f8b6c8e287fd506c8188",
                "staged_name": TEST_UUID_1,
            }
        ]
    }
    repo.manifest_path.write_text(json.dumps(original_manifest, indent=2) + "\n")
    repo.checksums_path.write_text("# artifact-locker checksums\n")

    class InvalidRemoteOras(FakeOras):
        def pull_to_dir(self, repository: str, tag: str, destination: Path):
            destination.mkdir(parents=True, exist_ok=True)
            if tag == MANIFEST_TAG:
                (destination / "artifacts.json").write_text(
                    json.dumps(
                        {
                            "artifacts": [
                                {
                                    "artifact_id": TEST_UUID_3,
                                    "filename": "broken.bin",
                                    "platform": "linux",
                                    "category": "bin",
                                    "sha256": "deadbeef",
                                    "staged_name": TEST_UUID_3,
                                }
                            ]
                        }
                    )
                )
                return None
            if tag == CHECKSUMS_TAG:
                (destination / "checksums.txt").write_text(f"badchecksum  {TEST_UUID_3}\n")
                return None
            return super().pull_to_dir(repository, tag, destination)

    monkeypatch.setattr(cli, "OrasRunner", lambda: InvalidRemoteOras(tmp_path / "remote"))
    code, _, err = invoke(["--catalog", str(repo.root), "pull"], capsys)
    assert code == 1
    assert f"invalid sha256 for {TEST_UUID_3}" in err
    assert json.loads(repo.manifest_path.read_text()) == original_manifest


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


def test_oras_push_uses_relative_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(self: OrasRunner, args: list[str], cwd: Path | None = None):
        calls.append((args, cwd))
        return SimpleNamespace(args=args, stdout="", stderr="")

    monkeypatch.setattr(OrasRunner, "run", fake_run)
    payload = write_sample_file(tmp_path / "nested" / "artifact.json", b"{}")
    runner = OrasRunner()
    runner.push_file(
        "example.test/catalog",
        "artifacts-catalog",
        payload,
        "application/json",
    )
    assert calls == [
        (
            [
                "push",
                "example.test/catalog:artifacts-catalog",
                "artifact.json:application/json",
            ],
            payload.parent.resolve(),
        )
    ]


def test_next_artifact_id_returns_monotonic_uuid7_strings() -> None:
    generated = [next_artifact_id([]), next_artifact_id([TEST_UUID_1]), next_artifact_id([])]
    assert len(set(generated)) == len(generated)
    assert generated == sorted(generated)
    for artifact_id in generated:
        assert_is_uuid7(artifact_id)
