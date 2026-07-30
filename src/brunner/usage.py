from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def normalized_usage(value: dict[str, Any]) -> dict[str, int]:
    result = {key: int(value.get(key, 0) or 0) for key in TOKEN_KEYS}
    result["total_tokens"] = int(
        value.get(
            "total_tokens",
            result["input_tokens"] + result["output_tokens"],
        )
        or 0
    )
    return result


def read_json_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(errors="replace").strip()
    if not text:
        return []
    if text.startswith("["):
        value = json.loads(text)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return [value] if isinstance(value, dict) else []
    records = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def sum_usage(values: Iterable[dict[str, int]]) -> dict[str, int]:
    selected = list(values)
    return {
        key: sum(item.get(key, 0) for item in selected)
        for key in (*TOKEN_KEYS, "total_tokens")
    }
