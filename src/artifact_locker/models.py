from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_PROVENANCE_KINDS = {"download", "built", "local"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def next_artifact_id(existing_ids: list[str]) -> str:
    numbers = []
    for artifact_id in existing_ids:
        if artifact_id.startswith("art-"):
            suffix = artifact_id.removeprefix("art-")
            if suffix.isdigit():
                numbers.append(int(suffix))
    next_number = max(numbers, default=0) + 1
    return f"art-{next_number:04d}"


@dataclass(slots=True)
class Provenance:
    kind: str
    uri: str | None = None
    repo: str | None = None
    tag: str | None = None
    commit: str | None = None
    archive_path: str | None = None
    build_method: str | None = None
    notes: str | None = None

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.kind not in VALID_PROVENANCE_KINDS:
            issues.append(f"invalid provenance kind: {self.kind}")
        return issues

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind,
            "uri": self.uri,
            "repo": self.repo,
            "tag": self.tag,
            "commit": self.commit,
            "archive_path": self.archive_path,
            "build_method": self.build_method,
            "notes": self.notes,
        }
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Provenance:
        return cls(
            kind=data.get("kind", "local"),
            uri=data.get("uri"),
            repo=data.get("repo"),
            tag=data.get("tag"),
            commit=data.get("commit"),
            archive_path=data.get("archive_path"),
            build_method=data.get("build_method"),
            notes=data.get("notes"),
        )


@dataclass(slots=True)
class Artifact:
    artifact_id: str
    filename: str
    platform: str | None
    category: str | None
    version: str | None
    sha256: str | None
    staged_name: str | None
    active: bool
    provenance: Provenance

    @classmethod
    def create(
        cls,
        *,
        artifact_id: str,
        filename: str,
        provenance: Provenance,
        platform: str | None = None,
        category: str | None = None,
        version: str | None = None,
        sha256: str | None = None,
        active: bool = True,
    ) -> Artifact:
        return cls(
            artifact_id=artifact_id,
            filename=filename,
            platform=platform or None,
            category=category or None,
            version=version or None,
            sha256=sha256 or None,
            staged_name=artifact_id if sha256 else None,
            active=active,
            provenance=provenance,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            artifact_id=data["artifact_id"],
            filename=data["filename"],
            platform=data.get("platform"),
            category=data.get("category"),
            version=data.get("version"),
            sha256=data.get("sha256"),
            staged_name=data.get("staged_name"),
            active=bool(data.get("active", True)),
            provenance=Provenance.from_dict(data.get("provenance", {})),
        )

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            issues.append(f"invalid filename for {self.artifact_id}")
        if self.sha256 is not None and not SHA256_RE.match(self.sha256):
            issues.append(f"invalid sha256 for {self.artifact_id}")
        if self.sha256 and self.staged_name != self.artifact_id:
            issues.append(
                "staged_name mismatch for "
                f"{self.artifact_id}: expected {self.artifact_id}, got {self.staged_name}"
            )
        if not self.sha256 and self.staged_name is not None:
            issues.append(
                f"staged_name must be omitted when sha256 is absent for {self.artifact_id}"
            )
        if not any([self.sha256, self.provenance.uri, self.provenance.repo]):
            issues.append(
                f"artifact {self.artifact_id} must have a local file, uri, or repo reference"
            )
        issues.extend(self.provenance.validate())
        return issues

    def to_dict(self) -> dict[str, Any]:
        data = {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "platform": self.platform,
            "category": self.category,
            "version": self.version,
            "sha256": self.sha256,
            "staged_name": self.staged_name,
            "active": self.active,
            "provenance": self.provenance.to_dict(),
        }
        return {key: value for key, value in data.items() if value is not None}


def load_manifest(path: Path) -> list[Artifact]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    artifacts = [Artifact.from_dict(item) for item in payload.get("artifacts", [])]
    return sorted(artifacts, key=lambda item: item.artifact_id)


def write_manifest(path: Path, artifacts: list[Artifact]) -> None:
    data = {
        "artifacts": [
            artifact.to_dict() for artifact in sorted(artifacts, key=lambda item: item.artifact_id)
        ]
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_checksums(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        checksum, name = line.split(maxsplit=1)
        result[name.strip()] = checksum
    return result


def write_checksums(path: Path, artifacts: list[Artifact]) -> None:
    lines = []
    for artifact in sorted(artifacts, key=lambda item: item.artifact_id):
        if artifact.sha256 and artifact.staged_name:
            lines.append(f"{artifact.sha256}  {artifact.staged_name}")
    if not lines:
        lines.append("# artifact-locker checksums")
    path.write_text("\n".join(lines) + "\n")
