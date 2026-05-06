from __future__ import annotations

import json
from typing import Any


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def print_json(data: Any) -> None:
    print(dump_json(data))
