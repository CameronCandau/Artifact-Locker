# artifact-locker

`artifact-locker` is a Python CLI for maintaining a local catalog of curated
artifacts and publishing or pulling that catalog through OCI with `oras`.

New artifacts use raw UUIDv7 `artifact_id` values. Treat those IDs as opaque
strings everywhere.

The CLI is designed to be operator-friendly:
- `add` prompts for missing fields instead of forcing you to remember flags
- most metadata fields are optional
- artifacts can be staged from a local file or tracked as a URL-only reference
- `find`, `show`, and `remove` work from filename or free-text query, not just an opaque ID

## Commands

- `artifact-locker init`
- `artifact-locker add [source-or-url]`
- `artifact-locker list [query]`
- `artifact-locker find <query>`
- `artifact-locker show <query>`
- `artifact-locker remove <query>`
- `artifact-locker verify --catalog|--local|--all`
- `artifact-locker push [tag]`
- `artifact-locker pull`
- `artifact-locker doctor`

## Repo Layout

```text
.
├── catalog/
│   ├── artifacts.json
│   └── checksums.txt
├── config.json
├── staging/
│   └── release-assets/
└── .artifact-locker/
    └── state.json
```

`config.json` stores non-secret defaults such as the OCI repository name and
the managed local artifact directory. The default managed path is:

```text
~/.local/share/artifact-locker/artifacts
```

Managed payloads are stored by platform, category, and artifact ID to avoid
filename collisions across versions:

```text
~/.local/share/artifact-locker/artifacts/<platform>/<category>/<artifact_id>/<filename>
```

Registry authentication is intentionally external to the application; use
`oras login` when pushes require credentials.

For ECR Public repositories, a typical login flow is:

```bash
aws ecr-public get-login-password --region us-east-1 | \
  oras login -u AWS --password-stdin public.ecr.aws
```

By default, commands use the managed catalog under
`~/.local/share/artifact-locker/`. Use `--catalog /path/to/dir` only when you
want an alternate catalog location.

## Usage

Interactive add:

```bash
artifact-locker add
artifact-locker add ./Seatbelt.exe
artifact-locker add https://example.test/tool.zip
```

Non-interactive add:

```bash
artifact-locker add ./Seatbelt.exe \
  --platform windows \
  --category bin \
  --version v1.0.0 \
  --no-input
```

Find and manage artifacts without remembering the generated ID:

```bash
artifact-locker find seatbelt
artifact-locker show Seatbelt.exe
artifact-locker remove seatbelt
```

Push with an automatic date tag:

```bash
artifact-locker push
```

When omitted, the push tag defaults to the current date in `vYYYY-MM-DD`
format.

`push` also prunes stale remote per-artifact tags that are no longer present in
the current manifest. It preserves the catalog tags
(`artifacts-catalog`/`artifacts-checksums`) and dated snapshot tags like
`v2026-05-08-artifacts` and `v2026-05-08-checksums`.

The OCI repository should be treated as owned by `artifact-locker`. Extra
non-catalog tags in that repository may be removed on `push`.

## Development

```bash
python3 -m pytest
python3 -m build
```

For local commit-time auto-formatting, install the repo-managed pre-commit hook
once:

```bash
pip install -e .[dev]
ln -sf ../../scripts/pre-commit .git/hooks/pre-commit
```

Then before each commit, the hook will run `ruff check --fix` and
`ruff format` on staged Python files and re-stage the results automatically.
You should not need to remember formatter commands for normal use.

If you want to run the same tools manually:

```bash
ruff check --fix .
ruff format .
```

For local push-time test gating, install the repo pre-push hook:

```bash
ln -sf ../../scripts/pre-push .git/hooks/pre-push
```

That hook runs `pytest` from `venv/bin/pytest` when available.
