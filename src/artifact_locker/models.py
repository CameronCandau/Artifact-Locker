from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID7_LOCK = threading.Lock()
_UUID7_LAST_MS = -1
_UUID7_COUNTER = 0


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def next_artifact_id(existing_ids: list[str]) -> str:
    del existing_ids
    return new_uuid7()


def new_uuid7() -> str:
    global _UUID7_LAST_MS, _UUID7_COUNTER

    with _UUID7_LOCK:
        timestamp_ms = time.time_ns() // 1_000_000
        if timestamp_ms > _UUID7_LAST_MS:
            _UUID7_LAST_MS = timestamp_ms
            _UUID7_COUNTER = secrets.randbits(12)
        else:
            timestamp_ms = _UUID7_LAST_MS
            _UUID7_COUNTER += 1
            if _UUID7_COUNTER > 0xFFF:
                _UUID7_LAST_MS += 1
                timestamp_ms = _UUID7_LAST_MS
                _UUID7_COUNTER = 0

        rand_b = secrets.randbits(62)
        value = (
            ((timestamp_ms & ((1 << 48) - 1)) << 80)
            | (0x7 << 76)
            | ((_UUID7_COUNTER & 0xFFF) << 64)
            | (0b10 << 62)
            | rand_b
        )
    return str(uuid.UUID(int=value))


@dataclass(slots=True)
class Artifact:
    artifact_id: str
    filename: str
    platform: str | None
    category: str | None
    sha256: str
    staged_name: str

    @classmethod
    def create(
        cls,
        *,
        artifact_id: str,
        filename: str,
        platform: str | None = None,
        category: str | None = None,
        sha256: str,
    ) -> Artifact:
        return cls(
            artifact_id=artifact_id,
            filename=filename,
            platform=platform or None,
            category=category or None,
            sha256=sha256,
            staged_name=artifact_id,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            artifact_id=data["artifact_id"],
            filename=data["filename"],
            platform=data.get("platform"),
            category=data.get("category"),
            sha256=data["sha256"],
            staged_name=data["staged_name"],
        )

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            issues.append(f"invalid filename for {self.artifact_id}")
        if not SHA256_RE.match(self.sha256):
            issues.append(f"invalid sha256 for {self.artifact_id}")
        if self.staged_name != self.artifact_id:
            issues.append(
                "staged_name mismatch for "
                f"{self.artifact_id}: expected {self.artifact_id}, got {self.staged_name}"
            )
        return issues

    def to_dict(self) -> dict[str, Any]:
        data = {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "platform": self.platform,
            "category": self.category,
            "sha256": self.sha256,
            "staged_name": self.staged_name,
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
        lines.append(f"{artifact.sha256}  {artifact.staged_name}")
    if not lines:
        lines.append("# artifact-locker checksums")
    path.write_text("\n".join(lines) + "\n")
