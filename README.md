# artifact-locker

`artifact-locker` is a Python CLI for maintaining a local catalog of curated
artifacts and publishing or pulling that catalog through OCI with `oras`.

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

## Development

```bash
python -m pytest
python -m build
```
