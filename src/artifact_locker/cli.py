from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import (
    Artifact,
    Provenance,
    compute_sha256,
    load_checksums,
    load_manifest,
    next_artifact_id,
    write_checksums,
    write_manifest,
)
from .oci import (
    ARTIFACT_MEDIA_TYPE,
    CHECKSUMS_MEDIA_TYPE,
    CHECKSUMS_TAG,
    MANIFEST_MEDIA_TYPE,
    MANIFEST_TAG,
    OrasError,
    OrasRunner,
    checksums_versioned_tag,
    manifest_versioned_tag,
)
from .output import print_json
from .paths import RepoPaths, catalog_exists, discover_repo_paths, init_repo, load_config
from .state import LocalStateRecord, load_state, make_state_record, write_state

PLATFORM_CHOICES = ["windows", "linux", "macos", "cross-platform"]
CATEGORY_CHOICES = ["bin", "script", "archive", "doc", "source", "other"]
VERSIONED_CATALOG_TAG_RE = re.compile(r"^v\d{4}-\d{2}-\d{2}-(artifacts|checksums)$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artifact-locker")
    parser.add_argument(
        "--catalog", dest="catalog_path", help="Path to an alternate catalog directory"
    )
    parser.add_argument("--root", dest="catalog_legacy", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")

    add = subparsers.add_parser("add")
    add.add_argument("source", nargs="?")
    add.add_argument("--platform")
    add.add_argument("--category")
    add.add_argument("--filename")
    add.add_argument("--version")
    add.add_argument("--provenance-kind", choices=["download", "built", "local"])
    add.add_argument("--uri")
    add.add_argument("--source-repo")
    add.add_argument("--source-tag")
    add.add_argument("--source-commit")
    add.add_argument("--archive-path")
    add.add_argument("--build-method")
    add.add_argument("--notes")
    add.add_argument("--inactive", action="store_true")
    add.add_argument("--no-input", action="store_true")

    remove = subparsers.add_parser("remove")
    remove.add_argument("query")

    listing = subparsers.add_parser("list")
    listing.add_argument("query", nargs="?")
    listing.add_argument("--platform")
    listing.add_argument("--category")
    listing.add_argument("--active", choices=["true", "false"])
    listing.add_argument("--json", action="store_true")

    find = subparsers.add_parser("find")
    find.add_argument("query")
    find.add_argument("--platform")
    find.add_argument("--category")
    find.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show")
    show.add_argument("query")
    show.add_argument("--json", action="store_true")

    verify = subparsers.add_parser("verify")
    mode = verify.add_mutually_exclusive_group()
    mode.add_argument("--catalog", action="store_true")
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--all", action="store_true")
    verify.add_argument("--json", action="store_true")

    push = subparsers.add_parser("push")
    push.add_argument("tag", nargs="?")

    pull = subparsers.add_parser("pull")
    pull.add_argument("--platform")
    pull.add_argument("--category")
    pull.add_argument("--json", action="store_true")

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")

    return parser


def human_bool(value: bool) -> str:
    return "yes" if value else "no"


def is_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def prompt_text(label: str, default: str | None = None, allow_empty: bool = True) -> str | None:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    if not raw:
        if default is not None:
            return default
        return None if allow_empty else prompt_text(label, default=default, allow_empty=allow_empty)
    return raw


def prompt_choice(label: str, choices: list[str], default: str | None = None) -> str | None:
    print(f"{label} options: {', '.join(choices)}")
    return prompt_text(label, default=default, allow_empty=True)


def infer_filename(source: str | None, uri: str | None) -> str | None:
    if source and not is_url(source):
        return Path(source).name
    if uri:
        parsed = urlparse(uri)
        candidate = Path(parsed.path).name
        return candidate or None
    return None


def infer_provenance_kind(source: str | None, uri: str | None, args: argparse.Namespace) -> str:
    if args.provenance_kind:
        return args.provenance_kind
    if args.source_repo or args.source_commit or args.build_method:
        return "built"
    if uri or args.uri or is_url(source):
        return "download"
    return "local"


def build_provenance(kind: str, args: argparse.Namespace, uri: str | None) -> Provenance:
    return Provenance(
        kind=kind,
        uri=uri,
        repo=args.source_repo,
        tag=args.source_tag,
        commit=args.source_commit,
        archive_path=args.archive_path,
        build_method=args.build_method,
        notes=args.notes,
    )


def staged_path(paths: RepoPaths, artifact: Artifact) -> Path:
    if not artifact.staged_name:
        raise ValueError(f"{artifact.artifact_id} has no staged asset")
    return paths.staging_dir / artifact.staged_name


def local_artifact_path(paths: RepoPaths, config: dict[str, Any], artifact: Artifact) -> Path:
    platform = artifact.platform or "unclassified"
    category = artifact.category or "misc"
    return (
        paths.artifact_dir(config) / platform / category / artifact.artifact_id / artifact.filename
    )


def artifact_exists(artifacts: list[Artifact], candidate: Artifact) -> bool:
    return any(
        existing.filename == candidate.filename
        and existing.version == candidate.version
        and existing.provenance.uri == candidate.provenance.uri
        and existing.provenance.repo == candidate.provenance.repo
        and existing.provenance.commit == candidate.provenance.commit
        for existing in artifacts
    )


def download_url_to_path(uri: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(uri, headers={"User-Agent": "artifact-locker/0.1.0"})
    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except HTTPError as exc:
        raise SystemExit(f"download failed for {uri}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise SystemExit(f"download failed for {uri}: {exc.reason}") from exc


def materialize_artifact_bytes(
    paths: RepoPaths,
    config: dict[str, Any],
    state: dict[str, LocalStateRecord],
    artifact: Artifact,
    source_path: Path,
) -> None:
    destination = staged_path(paths, artifact)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)
    payload_destination = local_artifact_path(paths, config, artifact)
    payload_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, payload_destination)
    if artifact.sha256:
        state[artifact.artifact_id] = make_state_record(
            artifact.artifact_id, payload_destination, artifact.sha256
        )


def sync_checksums(paths: RepoPaths, artifacts: list[Artifact]) -> None:
    write_manifest(paths.manifest_path, artifacts)
    write_checksums(paths.checksums_path, artifacts)


def load_repo(
    paths: RepoPaths,
) -> tuple[dict[str, Any], list[Artifact], dict[str, str], dict[str, LocalStateRecord]]:
    if not catalog_exists(paths):
        init_repo(paths.root)
    config = load_config(paths)
    artifacts = load_manifest(paths.manifest_path)
    checksums = load_checksums(paths.checksums_path)
    state = load_state(paths.state_path)
    return config, artifacts, checksums, state


def verify_catalog(
    paths: RepoPaths, artifacts: list[Artifact], checksums: dict[str, str]
) -> list[str]:
    issues: list[str] = []
    seen_ids: set[str] = set()
    staged_names = {artifact.staged_name for artifact in artifacts if artifact.staged_name}
    for artifact in artifacts:
        if artifact.artifact_id in seen_ids:
            issues.append(f"duplicate artifact_id: {artifact.artifact_id}")
        seen_ids.add(artifact.artifact_id)
        issues.extend(artifact.validate())
        if artifact.sha256 and artifact.staged_name:
            checksum = checksums.get(artifact.staged_name)
            if checksum != artifact.sha256:
                issues.append(f"checksum entry mismatch for {artifact.artifact_id}")
            staged_file = staged_path(paths, artifact)
            if not staged_file.exists():
                issues.append(f"missing staged asset for {artifact.artifact_id}")
                continue
            actual = compute_sha256(staged_file)
            if actual != artifact.sha256:
                issues.append(f"staged asset checksum drift for {artifact.artifact_id}")
        elif artifact.staged_name or artifact.sha256:
            issues.append(f"incomplete staged metadata for {artifact.artifact_id}")
    for candidate in sorted(paths.staging_dir.iterdir()) if paths.staging_dir.exists() else []:
        if candidate.is_file() and candidate.name not in staged_names:
            issues.append(f"orphaned staged asset: {candidate.name}")
    for name in sorted(checksums):
        if name not in staged_names:
            issues.append(f"orphaned checksum entry: {name}")
    return issues


def verify_remote_catalog(artifacts: list[Artifact], checksums: dict[str, str]) -> list[str]:
    issues: list[str] = []
    seen_ids: set[str] = set()
    staged_names = {artifact.staged_name for artifact in artifacts if artifact.staged_name}
    for artifact in artifacts:
        if artifact.artifact_id in seen_ids:
            issues.append(f"duplicate artifact_id: {artifact.artifact_id}")
        seen_ids.add(artifact.artifact_id)
        issues.extend(artifact.validate())
        if (
            artifact.sha256
            and artifact.staged_name
            and checksums.get(artifact.staged_name) != artifact.sha256
        ):
            issues.append(f"checksum entry mismatch for {artifact.artifact_id}")
    for name in sorted(checksums):
        if name not in staged_names:
            issues.append(f"orphaned checksum entry: {name}")
    return issues


def local_statuses(
    paths: RepoPaths,
    config: dict[str, Any],
    artifacts: list[Artifact],
    state: dict[str, LocalStateRecord],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        record = state.get(artifact.artifact_id)
        expected = local_artifact_path(paths, config, artifact)
        candidate = Path(record.local_path) if record else expected
        if not candidate.exists():
            status = "missing"
        elif artifact.sha256 is None:
            status = "present"
        else:
            actual = compute_sha256(candidate)
            if actual == artifact.sha256:
                status = "verified" if record else "present"
            else:
                status = "stale"
        rows.append(
            {
                "artifact_id": artifact.artifact_id,
                "expected_path": str(expected),
                "recorded_path": record.local_path if record else None,
                "status": status,
            }
        )
    return rows


def artifact_matches_query(artifact: Artifact, query: str) -> bool:
    lowered = query.lower()
    fields = [
        artifact.artifact_id,
        artifact.filename,
        artifact.platform or "",
        artifact.category or "",
        artifact.version or "",
        artifact.provenance.uri or "",
        artifact.provenance.repo or "",
        artifact.provenance.notes or "",
    ]
    return any(lowered in field.lower() for field in fields)


def filter_artifacts(
    artifacts: list[Artifact],
    query: str | None = None,
    platform: str | None = None,
    category: str | None = None,
    active: bool | None = None,
) -> list[Artifact]:
    matches = []
    for artifact in artifacts:
        if platform and artifact.platform != platform:
            continue
        if category and artifact.category != category:
            continue
        if active is not None and artifact.active != active:
            continue
        if query and not artifact_matches_query(artifact, query):
            continue
        matches.append(artifact)
    return matches


def choose_artifact(matches: list[Artifact], query: str) -> Artifact:
    if not matches:
        raise SystemExit(f"artifact not found: {query}")
    if len(matches) == 1:
        return matches[0]
    print(f"Multiple artifacts match {query!r}:")
    for index, artifact in enumerate(matches, start=1):
        print(f"{index}. {artifact.artifact_id}  {artifact.filename}")
    raw = prompt_text("Select artifact number", allow_empty=False)
    selection = int(raw or "0")
    if not 1 <= selection <= len(matches):
        raise SystemExit("invalid selection")
    return matches[selection - 1]


def resolve_artifact(artifacts: list[Artifact], query: str) -> Artifact:
    exact_id = [artifact for artifact in artifacts if artifact.artifact_id == query]
    if exact_id:
        return exact_id[0]
    exact_name = [artifact for artifact in artifacts if artifact.filename == query]
    if len(exact_name) == 1:
        return exact_name[0]
    return choose_artifact(filter_artifacts(artifacts, query=query), query)


def command_init(args: argparse.Namespace) -> int:
    catalog_arg = args.catalog_path or args.catalog_legacy
    root = Path(catalog_arg).resolve() if catalog_arg else discover_repo_paths().root
    paths = init_repo(root)
    print(f"initialized repo at {paths.root}")
    return 0


def resolve_add_inputs(
    args: argparse.Namespace, artifacts: list[Artifact]
) -> tuple[Path | None, str | None, str, dict[str, Any]]:
    source = args.source
    uri = args.uri
    local_source: Path | None = None
    if source and is_url(source):
        uri = source
        source = None
    elif source:
        local_source = Path(source).expanduser().resolve()
        if not local_source.is_file():
            raise SystemExit(f"source file not found: {local_source}")

    interactive = not args.no_input
    if interactive and source is None and uri is None and not args.source_repo:
        source_or_link = prompt_text("Source file path or URL", allow_empty=True)
        if source_or_link:
            if is_url(source_or_link):
                uri = source_or_link
            else:
                local_source = Path(source_or_link).expanduser().resolve()
                if not local_source.is_file():
                    raise SystemExit(f"source file not found: {local_source}")

    default_filename = args.filename or infer_filename(
        str(local_source) if local_source else source, uri
    )
    filename = default_filename
    if interactive and not filename:
        filename = prompt_text("Filename", allow_empty=False)
    if not filename:
        raise SystemExit("filename is required")

    platform = args.platform
    category = args.category
    version = args.version
    if interactive and platform is None:
        platform = prompt_choice("Platform", PLATFORM_CHOICES)
    if interactive and category is None:
        category = prompt_choice("Category", CATEGORY_CHOICES)
    if interactive and version is None:
        version = prompt_text("Version", allow_empty=True)

    kind = infer_provenance_kind(str(local_source) if local_source else source, uri, args)
    if interactive and args.provenance_kind is None:
        prompted_kind = prompt_choice(
            "Provenance kind", ["local", "download", "built"], default=kind
        )
        if prompted_kind:
            kind = prompted_kind
    if interactive and uri is None and kind == "download":
        uri = prompt_text("Download URL", allow_empty=True)
    if interactive and args.source_repo is None and kind == "built":
        args.source_repo = prompt_text("Source repo", allow_empty=True)
    if interactive and args.source_commit is None and kind == "built":
        args.source_commit = prompt_text("Source commit", allow_empty=True)
    if interactive and args.build_method is None and kind == "built":
        args.build_method = prompt_text("Build method", allow_empty=True)
    provenance = build_provenance(kind, args, uri)

    artifact_fields = {
        "artifact_id": next_artifact_id([artifact.artifact_id for artifact in artifacts]),
        "filename": filename,
        "provenance": provenance,
        "platform": platform,
        "category": category,
        "version": version,
        "active": not args.inactive,
    }
    return local_source, uri, kind, artifact_fields


def command_add(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, _, state = load_repo(paths)
    local_source, uri, kind, artifact_fields = resolve_add_inputs(args, artifacts)
    materialized_source: Path | None = local_source
    if materialized_source is None and kind == "download" and uri:
        with tempfile.TemporaryDirectory() as tmp_dir:
            downloaded_path = Path(tmp_dir) / artifact_fields["filename"]
            download_url_to_path(uri, downloaded_path)
            artifact = Artifact.create(
                **artifact_fields,
                sha256=compute_sha256(downloaded_path),
            )
            issues = artifact.validate()
            if issues:
                raise SystemExit("; ".join(issues))
            if artifact_exists(artifacts, artifact):
                raise SystemExit(f"artifact already exists: {artifact.filename}")
            materialize_artifact_bytes(paths, config, state, artifact, downloaded_path)
            artifacts.append(artifact)
            sync_checksums(paths, artifacts)
            write_state(paths.state_path, state)
            print(f"added {artifact.artifact_id} ({artifact.filename})")
            return 0
    sha256 = compute_sha256(materialized_source) if materialized_source else None
    artifact = Artifact.create(
        **artifact_fields,
        sha256=sha256,
    )
    issues = artifact.validate()
    if issues:
        raise SystemExit("; ".join(issues))
    if artifact_exists(artifacts, artifact):
        raise SystemExit(f"artifact already exists: {artifact.filename}")
    if materialized_source and artifact.staged_name:
        materialize_artifact_bytes(paths, config, state, artifact, materialized_source)
    artifacts.append(artifact)
    sync_checksums(paths, artifacts)
    write_state(paths.state_path, state)
    print(f"added {artifact.artifact_id} ({artifact.filename})")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, _, state = load_repo(paths)
    artifact = resolve_artifact(artifacts, args.query)
    artifacts = [item for item in artifacts if item.artifact_id != artifact.artifact_id]
    if artifact.staged_name:
        staged = staged_path(paths, artifact)
        if staged.exists():
            staged.unlink()
    record = state.get(artifact.artifact_id)
    payload_path = (
        Path(record.local_path) if record else local_artifact_path(paths, config, artifact)
    )
    if payload_path.exists():
        payload_path.unlink()
        parent = payload_path.parent
        while parent != paths.artifact_dir(config) and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    state.pop(artifact.artifact_id, None)
    sync_checksums(paths, artifacts)
    write_state(paths.state_path, state)
    print(f"removed {artifact.artifact_id}")
    return 0


def present_artifact_row(status_by_id: dict[str, str], artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "filename": artifact.filename,
        "platform": artifact.platform,
        "category": artifact.category,
        "version": artifact.version,
        "active": artifact.active,
        "has_staged_asset": artifact.sha256 is not None,
        "source_uri": artifact.provenance.uri,
        "local_status": status_by_id[artifact.artifact_id],
    }


def command_list(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, _, state = load_repo(paths)
    rows = local_statuses(paths, config, artifacts, state)
    status_by_id = {row["artifact_id"]: row["status"] for row in rows}
    active_filter = None if args.active is None else args.active == "true"
    filtered = [
        present_artifact_row(status_by_id, artifact)
        for artifact in filter_artifacts(
            artifacts,
            query=args.query,
            platform=args.platform,
            category=args.category,
            active=active_filter,
        )
    ]
    if args.json:
        print_json(filtered)
    else:
        for row in filtered:
            print(
                f"{row['artifact_id']}\t{row['filename']}\tplatform={row['platform'] or '-'}"
                f"\tcategory={row['category'] or '-'}\tlocal={row['local_status']}"
            )
    return 0


def command_find(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, _, state = load_repo(paths)
    rows = local_statuses(paths, config, artifacts, state)
    status_by_id = {row["artifact_id"]: row["status"] for row in rows}
    matches = [
        present_artifact_row(status_by_id, artifact)
        for artifact in filter_artifacts(
            artifacts, query=args.query, platform=args.platform, category=args.category
        )
    ]
    if args.json:
        print_json(matches)
    else:
        for row in matches:
            print(f"{row['artifact_id']}\t{row['filename']}\t{row['local_status']}")
    return 0


def command_show(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, _, state = load_repo(paths)
    artifact = resolve_artifact(artifacts, args.query)
    status = next(
        row
        for row in local_statuses(paths, config, [artifact], state)
        if row["artifact_id"] == artifact.artifact_id
    )
    payload = artifact.to_dict() | {
        "local_status": status["status"],
        "expected_path": status["expected_path"],
    }
    if args.json:
        print_json(payload)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def verify_payload(
    paths: RepoPaths,
    config: dict[str, Any],
    artifacts: list[Artifact],
    checksums: dict[str, str],
    state: dict[str, LocalStateRecord],
    mode: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if mode in {"catalog", "all"}:
        issues = verify_catalog(paths, artifacts, checksums)
        result["catalog"] = {"ok": not issues, "issues": issues}
    if mode in {"local", "all"}:
        rows = local_statuses(paths, config, artifacts, state)
        result["local"] = {
            "ok": all(row["status"] in {"verified", "present"} for row in rows),
            "artifacts": rows,
        }
    return result


def command_verify(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, checksums, state = load_repo(paths)
    mode = "catalog" if args.catalog else "local" if args.local else "all"
    result = verify_payload(paths, config, artifacts, checksums, state, mode)
    if args.json:
        print_json(result)
    else:
        if "catalog" in result:
            print(f"catalog ok: {human_bool(result['catalog']['ok'])}")
            for issue in result["catalog"]["issues"]:
                print(f"- {issue}")
        if "local" in result:
            print(f"local ok: {human_bool(result['local']['ok'])}")
            for row in result["local"]["artifacts"]:
                print(f"- {row['artifact_id']}: {row['status']}")
    return 0 if all(section["ok"] for section in result.values()) else 1


def require_repository(config: dict[str, Any]) -> str:
    repository = config.get("oci_repository")
    if not repository:
        raise SystemExit("config missing `oci_repository`")
    return repository


def describe_registry_pull_error(repository: str, tag: str, error: OrasError) -> str:
    text = f"{error.stdout}\n{error.stderr}".lower()
    if any(token in text for token in ["not found", "404", "no such manifest", "manifest unknown"]):
        if tag in {MANIFEST_TAG, CHECKSUMS_TAG}:
            return (
                f"remote catalog is empty or not initialized at {repository}. "
                f"Missing required tag: {tag}"
            )
        return f"artifact tag {tag} is missing from {repository}"
    return f"oras pull failed for {repository}:{tag}: {error}"


def describe_registry_push_error(repository: str, tag: str, error: OrasError) -> str:
    return describe_registry_write_error("push", repository, tag, error)


def describe_registry_delete_error(repository: str, tag: str, error: OrasError) -> str:
    return describe_registry_write_error("delete", repository, tag, error)


def describe_registry_write_error(
    operation: str, repository: str, tag: str, error: OrasError
) -> str:
    text = f"{error.stdout}\n{error.stderr}".lower()
    auth_suffix = "for pushes" if operation == "push" else f"for {operation}s"
    if "unauthorized" in text or "authorization token has expired" in text or "denied" in text:
        if repository.startswith("public.ecr.aws/"):
            return (
                f"oras {operation} failed for {repository}:{tag}: "
                f"registry auth is required {auth_suffix}.\n"
                "Login to ECR Public first:\n"
                "aws ecr-public get-login-password --region us-east-1 | "
                "oras login -u AWS --password-stdin public.ecr.aws"
            )
        return (
            f"oras {operation} failed for {repository}:{tag}: "
            f"registry auth is required {auth_suffix}.\n"
            "Authenticate with `oras login` for that registry, then retry."
        )
    return f"oras {operation} failed for {repository}:{tag}: {error}"


def parse_repo_tags(result: Any) -> set[str]:
    stdout = getattr(result, "stdout", "")
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def reserved_catalog_tags(tag: str) -> set[str]:
    return {
        MANIFEST_TAG,
        manifest_versioned_tag(tag),
        CHECKSUMS_TAG,
        checksums_versioned_tag(tag),
    }


def stale_remote_artifact_tags(remote_tags: set[str], current_artifact_tags: set[str]) -> list[str]:
    return sorted(
        tag
        for tag in remote_tags
        if tag not in current_artifact_tags
        and tag not in {MANIFEST_TAG, CHECKSUMS_TAG}
        and not VERSIONED_CATALOG_TAG_RE.match(tag)
    )


def default_push_tag() -> str:
    return datetime.now().strftime("v%Y-%m-%d")


def command_push(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, checksums, state = load_repo(paths)
    sync_checksums(paths, artifacts)
    checksums = load_checksums(paths.checksums_path)
    verification = verify_payload(paths, config, artifacts, checksums, state, "catalog")
    if not verification["catalog"]["ok"]:
        raise SystemExit("catalog verification failed")
    repository = require_repository(config)
    runner = OrasRunner()
    if not runner.available():
        raise SystemExit("oras not found on PATH")
    tag = args.tag or default_push_tag()
    current_artifact_tags = {
        artifact.artifact_id for artifact in artifacts if artifact.sha256 and artifact.staged_name
    }
    try:
        runner.push_file(repository, MANIFEST_TAG, paths.manifest_path, MANIFEST_MEDIA_TYPE)
        runner.push_file(
            repository, manifest_versioned_tag(tag), paths.manifest_path, MANIFEST_MEDIA_TYPE
        )
        runner.push_file(repository, CHECKSUMS_TAG, paths.checksums_path, CHECKSUMS_MEDIA_TYPE)
        runner.push_file(
            repository, checksums_versioned_tag(tag), paths.checksums_path, CHECKSUMS_MEDIA_TYPE
        )
        for artifact in artifacts:
            if artifact.sha256 and artifact.staged_name:
                runner.push_file(
                    repository,
                    artifact.artifact_id,
                    staged_path(paths, artifact),
                    ARTIFACT_MEDIA_TYPE,
                )
        remote_tags = parse_repo_tags(runner.repo_tags(repository))
        for stale_tag in stale_remote_artifact_tags(remote_tags, current_artifact_tags):
            runner.delete_manifest(repository, stale_tag)
    except OrasError as error:
        failed_tag = tag
        command_text = " ".join(error.command)
        for candidate in [
            *reserved_catalog_tags(tag),
            *[artifact.artifact_id for artifact in artifacts],
        ]:
            if candidate in command_text:
                failed_tag = candidate
                break
        if "manifest delete" in command_text:
            raise SystemExit(
                describe_registry_delete_error(repository, failed_tag, error)
            ) from error
        raise SystemExit(describe_registry_push_error(repository, failed_tag, error)) from error
    print(f"pushed catalog to {repository} with tag {tag}")
    return 0


def find_downloaded_file(directory: Path, preferred_name: str | None = None) -> Path:
    if preferred_name:
        candidate = directory / preferred_name
        if candidate.exists():
            return candidate
    files = [item for item in directory.iterdir() if item.is_file()]
    if len(files) != 1:
        raise FileNotFoundError(f"expected exactly one file in {directory}")
    return files[0]


def command_pull(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config = load_config(paths)
    repository = require_repository(config)
    runner = OrasRunner()
    if not runner.available():
        raise SystemExit("oras not found on PATH")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        manifest_dir = tmp / "manifest"
        checksums_dir = tmp / "checksums"
        try:
            runner.pull_to_dir(repository, MANIFEST_TAG, manifest_dir)
        except OrasError as error:
            raise SystemExit(
                describe_registry_pull_error(repository, MANIFEST_TAG, error)
            ) from error
        try:
            runner.pull_to_dir(repository, CHECKSUMS_TAG, checksums_dir)
        except OrasError as error:
            raise SystemExit(
                describe_registry_pull_error(repository, CHECKSUMS_TAG, error)
            ) from error
        manifest_file = find_downloaded_file(manifest_dir, "artifacts.json")
        checksums_file = find_downloaded_file(checksums_dir, "checksums.txt")
        artifacts = load_manifest(manifest_file)
        checksums = load_checksums(checksums_file)
        schema_issues = verify_remote_catalog(artifacts, checksums)
        if schema_issues:
            raise SystemExit("; ".join(schema_issues))
        shutil.copy2(manifest_file, paths.manifest_path)
        shutil.copy2(checksums_file, paths.checksums_path)

        state = load_state(paths.state_path)
        downloaded: list[dict[str, Any]] = []
        for artifact in artifacts:
            if not artifact.active or not artifact.sha256:
                continue
            if args.platform and artifact.platform != args.platform:
                continue
            if args.category and artifact.category != args.category:
                continue
            asset_dir = tmp / artifact.artifact_id
            try:
                runner.pull_to_dir(repository, artifact.artifact_id, asset_dir)
            except OrasError as error:
                raise SystemExit(
                    describe_registry_pull_error(repository, artifact.artifact_id, error)
                ) from error
            fetched = find_downloaded_file(asset_dir)
            actual = compute_sha256(fetched)
            if actual != artifact.sha256:
                raise SystemExit(f"downloaded checksum mismatch for {artifact.artifact_id}")
            staged_destination = staged_path(paths, artifact)
            staged_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fetched, staged_destination)
            payload_destination = local_artifact_path(paths, config, artifact)
            payload_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fetched, payload_destination)
            state[artifact.artifact_id] = make_state_record(
                artifact.artifact_id, payload_destination, artifact.sha256
            )
            downloaded.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_path": str(payload_destination),
                    "staged_path": str(staged_destination),
                }
            )
        write_state(paths.state_path, state)
    if args.json:
        print_json(downloaded)
    else:
        for item in downloaded:
            print(f"{item['artifact_id']}\t{item['artifact_path']}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    paths = discover_repo_paths(args.catalog_path or args.catalog_legacy)
    config, artifacts, checksums, state = load_repo(paths)
    runner = OrasRunner()
    repository = config.get("oci_repository")
    reachability = None
    if repository and runner.available():
        try:
            runner.repo_tags(repository)
            reachability = True
        except Exception:
            reachability = False
    verification = verify_payload(paths, config, artifacts, checksums, state, "all")
    payload = {
        "root": str(paths.root),
        "config_path": str(paths.config_path),
        "local_artifact_dir": config.get("local_artifact_dir"),
        "oras_available": runner.available(),
        "oci_repository": repository,
        "oci_reachable": reachability,
        "push_auth_expectation": "configure registry auth externally with `oras login`",
        "catalog_ok": verification["catalog"]["ok"],
        "local_ok": verification["local"]["ok"],
    }
    if args.json:
        print_json(payload)
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "init": command_init,
        "add": command_add,
        "remove": command_remove,
        "list": command_list,
        "find": command_find,
        "show": command_show,
        "verify": command_verify,
        "push": command_push,
        "pull": command_pull,
        "doctor": command_doctor,
    }
    try:
        return commands[args.command](args)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OrasError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
