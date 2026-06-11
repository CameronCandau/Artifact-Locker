#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pyproject="$repo_root/pyproject.toml"
module_init="$repo_root/src/artifact_locker/__init__.py"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/release.sh patch
  ./scripts/release.sh minor
  ./scripts/release.sh major
  ./scripts/release.sh X.Y.Z

Updates version metadata locally and prints the git tag to create.
EOF
}

if [ $# -ne 1 ]; then
  usage >&2
  exit 1
fi

current_version="$(python3 - <<'PY' "$pyproject"
from pathlib import Path
import re
import sys
text = Path(sys.argv[1]).read_text()
match = re.search(r'^version = "([^"]+)"$', text, re.M)
if not match:
    raise SystemExit("version not found in pyproject.toml")
print(match.group(1))
PY
)"

next_version="$(python3 - <<'PY' "$current_version" "$1"
import re
import sys

current = sys.argv[1]
requested = sys.argv[2]
match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current)
if not match:
    raise SystemExit(f"unsupported current version: {current}")
major, minor, patch = map(int, match.groups())
if requested == "patch":
    patch += 1
elif requested == "minor":
    minor += 1
    patch = 0
elif requested == "major":
    major += 1
    minor = 0
    patch = 0
elif re.fullmatch(r"\d+\.\d+\.\d+", requested):
    print(requested)
    raise SystemExit(0)
else:
    raise SystemExit(f"unsupported release target: {requested}")
print(f"{major}.{minor}.{patch}")
PY
)"

python3 - <<'PY' "$pyproject" "$module_init" "$current_version" "$next_version"
from pathlib import Path
import sys

pyproject = Path(sys.argv[1])
module_init = Path(sys.argv[2])
current = sys.argv[3]
new = sys.argv[4]

pyproject.write_text(pyproject.read_text().replace(f'version = "{current}"', f'version = "{new}"', 1))
module_init.write_text(module_init.read_text().replace(f'__version__ = "{current}"', f'__version__ = "{new}"', 1))
PY

printf 'artifact-locker version updated: %s -> %s\n' "$current_version" "$next_version"
printf 'next tag: v%s\n' "$next_version"
printf 'run: ./scripts/test.sh\n'
