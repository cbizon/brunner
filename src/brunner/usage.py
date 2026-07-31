from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


CANONICAL_TOKEN_KEYS = (
    "logical_input_tokens",
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _integer(value: object) -> int:
    return int(value or 0)


def _nested_integer(
    value: dict[str, Any],
    container: str,
    key: str,
) -> int | None:
    nested = value.get(container)
    if not isinstance(nested, dict) or key not in nested:
        return None
    return _integer(nested[key])


def _provider_token_fields(value: dict[str, Any]) -> dict[str, int]:
    return {
        key: _integer(raw)
        for key, raw in value.items()
        if key.endswith("_tokens") and isinstance(raw, (int, float))
    }


def normalize_codex_usage(value: dict[str, Any]) -> dict[str, Any]:
    logical_input = _integer(value.get("input_tokens"))
    cache_read = (
        _integer(value["cached_input_tokens"])
        if "cached_input_tokens" in value
        else _nested_integer(
            value,
            "input_tokens_details",
            "cached_tokens",
        )
        or 0
    )
    output = _integer(value.get("output_tokens"))
    reasoning = (
        _integer(value["reasoning_output_tokens"])
        if "reasoning_output_tokens" in value
        else _nested_integer(
            value,
            "output_tokens_details",
            "reasoning_tokens",
        )
    )
    reported_total = (
        _integer(value["total_tokens"])
        if "total_tokens" in value
        else None
    )
    provider_fields = _provider_token_fields(value)
    nested_cache_read = _nested_integer(
        value,
        "input_tokens_details",
        "cached_tokens",
    )
    nested_reasoning = _nested_integer(
        value,
        "output_tokens_details",
        "reasoning_tokens",
    )
    if nested_cache_read is not None:
        provider_fields["input_tokens_details.cached_tokens"] = (
            nested_cache_read
        )
    if nested_reasoning is not None:
        provider_fields["output_tokens_details.reasoning_tokens"] = (
            nested_reasoning
        )
    return {
        "logical_input_tokens": logical_input,
        "uncached_input_tokens": max(0, logical_input - cache_read),
        "cache_read_input_tokens": cache_read,
        "cache_write_input_tokens": None,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": logical_input + output,
        "provider_reported_total_tokens": reported_total,
        "provider_fields": provider_fields,
    }


def normalize_claude_usage(value: dict[str, Any]) -> dict[str, Any]:
    ordinary_input = _integer(value.get("input_tokens"))
    cache_write = _integer(value.get("cache_creation_input_tokens"))
    cache_read = _integer(value.get("cache_read_input_tokens"))
    logical_input = ordinary_input + cache_write + cache_read
    output = _integer(value.get("output_tokens"))
    reasoning = (
        _integer(value["reasoning_output_tokens"])
        if "reasoning_output_tokens" in value
        else None
    )
    reported_total = (
        _integer(value["total_tokens"])
        if "total_tokens" in value
        else None
    )
    return {
        "logical_input_tokens": logical_input,
        "uncached_input_tokens": ordinary_input + cache_write,
        "cache_read_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": logical_input + output,
        "provider_reported_total_tokens": reported_total,
        "provider_fields": _provider_token_fields(value),
    }


def _sum_nullable(
    values: list[dict[str, Any]],
    key: str,
) -> int | None:
    selected = [value.get(key) for value in values]
    if not selected or any(value is None for value in selected):
        return None
    return sum(_integer(value) for value in selected)


def sum_usage(
    values: Iterable[dict[str, Any]],
    *,
    provider: str,
) -> dict[str, Any]:
    selected = list(values)
    result = {
        "schema_version": "2.0",
        "provider": provider,
        "usage_record_count": len(selected),
        **{
            key: sum(_integer(item.get(key)) for item in selected)
            for key in CANONICAL_TOKEN_KEYS
            if key not in {
                "cache_write_input_tokens",
                "reasoning_output_tokens",
            }
        },
        "cache_write_input_tokens": _sum_nullable(
            selected,
            "cache_write_input_tokens",
        ),
        "reasoning_output_tokens": _sum_nullable(
            selected,
            "reasoning_output_tokens",
        ),
        "provider_reported_total_tokens": _sum_nullable(
            selected,
            "provider_reported_total_tokens",
        ),
    }
    provider_fields = {
        key: sum(
            _integer(item.get("provider_fields", {}).get(key))
            for item in selected
        )
        for key in {
            key
            for item in selected
            for key in item.get("provider_fields", {})
        }
    }
    result["provider_fields"] = provider_fields
    limitations = [
        (
            "logical_input_tokens counts all input context processed, "
            "including cache reads and writes; total_tokens is "
            "logical_input_tokens plus output_tokens."
        )
    ]
    if result["cache_write_input_tokens"] is None:
        limitations.append(
            f"{provider} does not expose cache-write tokens in this usage "
            "stream."
        )
    if result["reasoning_output_tokens"] is None:
        limitations.append(
            f"{provider} does not expose reasoning tokens separately in this "
            "usage stream."
        )
    result["limitations"] = limitations
    _validate_canonical_usage(result)
    return result


def _validate_canonical_usage(value: dict[str, Any]) -> None:
    if value["total_tokens"] != (
        value["logical_input_tokens"] + value["output_tokens"]
    ):
        raise ValueError("canonical total_tokens is inconsistent")
    if value["logical_input_tokens"] != (
        value["uncached_input_tokens"]
        + value["cache_read_input_tokens"]
    ):
        raise ValueError("canonical input token partition is inconsistent")


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
