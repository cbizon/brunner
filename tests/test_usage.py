from __future__ import annotations

from brunner.providers import ClaudeAdapter, CodexAdapter


def test_provider_usage_maps_equivalent_work_to_same_canonical_totals() -> None:
    codex = CodexAdapter().parse_usage(
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 70,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
            }
        ]
    )
    claude = ClaudeAdapter().parse_usage(
        [
            {
                "type": "result",
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 70,
                    "output_tokens": 20,
                },
            }
        ]
    )

    comparable_keys = (
        "logical_input_tokens",
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "total_tokens",
    )
    assert {key: codex[key] for key in comparable_keys} == {
        key: claude[key] for key in comparable_keys
    }
    assert codex["cache_write_input_tokens"] is None
    assert claude["cache_write_input_tokens"] == 10
    assert codex["reasoning_output_tokens"] == 5
    assert claude["reasoning_output_tokens"] is None
    assert codex["provider_fields"]["input_tokens"] == 100
    assert claude["provider_fields"]["input_tokens"] == 20


def test_codex_usage_reads_nested_cache_and_reasoning_details() -> None:
    usage = CodexAdapter().parse_usage(
        [
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 50,
                    "input_tokens_details": {"cached_tokens": 30},
                    "output_tokens": 12,
                    "output_tokens_details": {"reasoning_tokens": 4},
                },
            }
        ]
    )

    assert usage["logical_input_tokens"] == 50
    assert usage["uncached_input_tokens"] == 20
    assert usage["cache_read_input_tokens"] == 30
    assert usage["reasoning_output_tokens"] == 4
    assert usage["total_tokens"] == 62
    assert (
        usage["provider_fields"]["input_tokens_details.cached_tokens"]
        == 30
    )
