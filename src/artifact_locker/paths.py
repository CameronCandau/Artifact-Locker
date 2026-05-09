from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_NAME = "artifact-locker"


def xdg_data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    if value:
        return Path(value)
    return Path.home() / ".local" / "share"


def default_catalog_root() -> Path:
    return xdg_data_home() / APP_NAME


def default_local_artifact_dir() -> str:
    return str(xdg_data_home() / APP_NAME / "artifacts")


def default_config() -> dict[str, Any]:
    return {
        "oci_repository": None,
        "local_artifact_dir": default_local_artifact_dir(),
    }


@dataclass(slots=True)
class RepoPaths:
    root: Path
    config_path: Path
    catalog_dir: Path
    manifest_path: Path
    checksums_path: Path
    staging_dir: Path

    def artifact_dir(self, config: dict[str, Any]) -> Path:
        candidate = Path(config["local_artifact_dir"]).expanduser()
        if candidate.is_absolute():
            return candidate
        return (self.root / candidate).resolve()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    payload = json.loads(path.read_text())
    merged = dict(default)
    merged.update(payload)
    return merged


def find_repo_root(start: Path) -> Path | None:
    current = start.resolve()
    while True:
        if (current / "config.json").exists() and (current / "catalog").is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent


def resolve_repo_paths(root: Path) -> RepoPaths:
    return RepoPaths(
        root=root,
        config_path=root / "config.json",
        catalog_dir=root / "catalog",
        manifest_path=root / "catalog" / "artifacts.json",
        checksums_path=root / "catalog" / "checksums.txt",
        staging_dir=root / "staging" / "release-assets",
    )


def catalog_exists(paths: RepoPaths) -> bool:
    return paths.config_path.exists() and paths.catalog_dir.is_dir()


def discover_repo_paths(explicit_catalog: str | None = None, cwd: Path | None = None) -> RepoPaths:
    if explicit_catalog:
        return resolve_repo_paths(Path(explicit_catalog).resolve())
    repo_root = find_repo_root(cwd or Path.cwd())
    if repo_root is not None:
        return resolve_repo_paths(repo_root)
    return resolve_repo_paths(default_catalog_root())


def load_config(paths: RepoPaths) -> dict[str, Any]:
    config = load_json(paths.config_path, default_config())
    config.setdefault("local_artifact_dir", default_local_artifact_dir())
    return config


def init_repo(root: Path) -> RepoPaths:
    paths = resolve_repo_paths(root.resolve())
    paths.catalog_dir.mkdir(parents=True, exist_ok=True)
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    if not paths.manifest_path.exists():
        paths.manifest_path.write_text('{\n  "artifacts": []\n}\n')
    if not paths.checksums_path.exists():
        paths.checksums_path.write_text("# artifact-locker checksums\n")
    if not paths.config_path.exists():
        paths.config_path.write_text(json.dumps(default_config(), indent=2, sort_keys=True) + "\n")
    return paths
