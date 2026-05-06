from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from time import time
from typing import Any


@dataclass(slots=True)
class LocalStateRecord:
    artifact_id: str
    local_path: str
    sha256: str
    synced_at_epoch: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "local_path": self.local_path,
            "sha256": self.sha256,
            "synced_at_epoch": self.synced_at_epoch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LocalStateRecord":
        return cls(
            artifact_id=data["artifact_id"],
            local_path=data["local_path"],
            sha256=data["sha256"],
            synced_at_epoch=int(data["synced_at_epoch"]),
        )


def load_state(path: Path) -> dict[str, LocalStateRecord]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return {
        artifact_id: LocalStateRecord.from_dict(record)
        for artifact_id, record in payload.get("records", {}).items()
    }


def write_state(path: Path, records: dict[str, LocalStateRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "records": {
            artifact_id: records[artifact_id].to_dict() for artifact_id in sorted(records)
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def make_state_record(artifact_id: str, local_path: Path, sha256: str) -> LocalStateRecord:
    return LocalStateRecord(
        artifact_id=artifact_id,
        local_path=str(local_path),
        sha256=sha256,
        synced_at_epoch=int(time()),
    )

