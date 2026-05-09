from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_TAG = "artifacts-catalog"
CHECKSUMS_TAG = "artifacts-checksums"
MANIFEST_MEDIA_TYPE = "application/json"
CHECKSUMS_MEDIA_TYPE = "text/plain"
ARTIFACT_MEDIA_TYPE = "application/octet-stream"


def manifest_versioned_tag(tag: str) -> str:
    return f"{tag}-artifacts"


def checksums_versioned_tag(tag: str) -> str:
    return f"{tag}-checksums"


def ref_for(repository: str, tag: str) -> str:
    return f"{repository}:{tag}"


@dataclass(slots=True)
class CommandResult:
    args: list[str]
    stdout: str
    stderr: str


@dataclass(slots=True)
class OrasError(Exception):
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def __str__(self) -> str:
        detail = (
            self.stderr.strip()
            or self.stdout.strip()
            or f"oras exited with status {self.returncode}"
        )
        return detail


class OrasRunner:
    def __init__(self, executable: str = "oras"):
        self.executable = executable

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def run(self, args: list[str], cwd: Path | None = None) -> CommandResult:
        try:
            completed = subprocess.run(
                [self.executable, *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise OrasError(
                command=[self.executable, *args],
                returncode=exc.returncode,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            ) from exc
        return CommandResult(
            args=[self.executable, *args], stdout=completed.stdout, stderr=completed.stderr
        )

    def push_file(self, repository: str, tag: str, path: Path, media_type: str) -> CommandResult:
        resolved = path.resolve()
        return self.run(
            ["push", ref_for(repository, tag), f"{resolved.name}:{media_type}"],
            cwd=resolved.parent,
        )

    def pull_to_dir(self, repository: str, tag: str, destination: Path) -> CommandResult:
        destination.mkdir(parents=True, exist_ok=True)
        return self.run(["pull", ref_for(repository, tag), "--output", str(destination)])

    def repo_tags(self, repository: str) -> CommandResult:
        return self.run(["repo", "tags", repository])

    def delete_manifest(self, repository: str, tag: str) -> CommandResult:
        return self.run(["manifest", "delete", "--force", ref_for(repository, tag)])
