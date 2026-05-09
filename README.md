# artifact-locker

`artifact-locker` stores a small local catalog of files and syncs that current
state through OCI with `oras`.

The installed CLI is available as both `artifact-locker` and the shorter
`artlock`.

The model is intentionally simple:
- every artifact is a real stored file
- the local catalog is the source of truth
- `push` makes the remote match local current state
- `pull` restores that current state on another machine

## Commands

- `artifact-locker init`
- `artifact-locker add [source-or-url]`
- `artifact-locker list [query]`
- `artifact-locker find <query>`
- `artifact-locker show <query>`
- `artifact-locker remove <query>`
- `artifact-locker verify --catalog|--local|--all`
- `artifact-locker push`
- `artifact-locker pull`

## Repo Layout

```text
.
├── catalog/
│   ├── artifacts.json
│   └── checksums.txt
├── config.json
└── staging/
    └── release-assets/
```

`config.json` stores the OCI repository and the local artifact directory. By
default the managed repo lives under `~/.local/share/artifact-locker/` and the
managed payload directory is `~/.local/share/artifact-locker/artifacts`.

Managed payloads are stored in a flat local tree by platform and filename:

```text
~/.local/share/artifact-locker/artifacts/<platform>/<filename>
```

Artifact IDs remain in the catalog and OCI tags. Older local trees that still
use per-artifact ID directories are tolerated and are migrated forward on write.
Category remains catalog metadata for filtering and notes, but it is no longer
part of the local serving path.

Registry authentication is external. For ECR Public:

```bash
aws ecr-public get-login-password --region us-east-1 | \
  oras login -u AWS --password-stdin public.ecr.aws
```

## Usage

```bash
artifact-locker init
artifact-locker add ./Seatbelt.exe --platform windows --category bin --no-input
artifact-locker add https://example.test/tool.zip --platform linux --category archive --no-input
artifact-locker find seatbelt
artifact-locker show Seatbelt.exe
artifact-locker remove seatbelt
artifact-locker push
artifact-locker pull
```

The OCI repository is treated as fully owned by `artifact-locker`. Any remote
tag not part of the current live state may be removed on `push`.

## Development

```bash
python3 -m pytest
python3 -m build
```
