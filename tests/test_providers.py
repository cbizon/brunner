from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brunner.errors import ConfigurationError
from brunner.providers import (
    ClaudeAdapter,
    CodexAdapter,
    ProviderRunContext,
    ProviderSettings,
)


def context(tmp_path: Path) -> ProviderRunContext:
    workspace = tmp_path / "workspace"
    schema = workspace / "schema/final-response.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text('{"type":"object"}')
    transcript = tmp_path / "transcript"
    transcript.mkdir()
    return ProviderRunContext(
        workspace=workspace,
        transcript_dir=transcript,
        final_schema_path=schema,
        final_output_path=transcript / "final.json",
        persist_session=False,
        resume_session=False,
        session_id=None,
    )


def test_codex_adapter_disables_external_tools(tmp_path: Path) -> None:
    command = CodexAdapter().build_command(
        ProviderSettings(
            provider="codex",
            model="test-model",
            effort="high",
        ),
        context(tmp_path),
    ).command

    assert "--strict-config" in command
    assert 'web_search="disabled"' in command
    assert "allow_login_shell=false" in command
    assert (
        f"shell_environment_policy.set.PATH={json.dumps(os.environ['PATH'])}"
        in command
    )
    assert 'model_reasoning_effort="high"' in command
    assert "--ephemeral" in command
    sandbox_index = command.index("--sandbox")
    assert command[sandbox_index + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_codex_adapter_can_run_in_read_only_mode(tmp_path: Path) -> None:
    selected = context(tmp_path)
    selected = ProviderRunContext(
        **{
            **selected.__dict__,
            "read_only": True,
        }
    )

    command = CodexAdapter().build_command(
        ProviderSettings(provider="codex", model="test-model"),
        selected,
    ).command

    assert "--sandbox" in command
    assert "read-only" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_codex_adapter_resume_omits_initial_only_options(
    tmp_path: Path,
) -> None:
    selected = context(tmp_path)
    selected = ProviderRunContext(
        **{
            **selected.__dict__,
            "persist_session": True,
            "resume_session": True,
        }
    )

    command = CodexAdapter().build_command(
        ProviderSettings(provider="codex", model="test-model"),
        selected,
    ).command

    assert command[:3] == ("codex", "exec", "resume")
    assert command[-2:] == ("--last", "-")
    assert "--sandbox" not in command
    assert "--cd" not in command
    assert "--output-schema" in command
    assert "--output-last-message" in command
    assert "--json" in command


def test_claude_adapter_disables_external_tools(tmp_path: Path) -> None:
    command = ClaudeAdapter().build_command(
        ProviderSettings(
            provider="claude",
            model="claude-test",
            effort="high",
        ),
        context(tmp_path),
    ).command

    assert "--disallowedTools" in command
    assert "WebSearch,WebFetch" in command
    assert "--no-chrome" in command
    assert "--no-session-persistence" in command
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in command
    assert command[command.index("--mcp-config") + 1] == (
        '{"mcpServers":{}}'
    )
    assert "--dangerously-skip-permissions" in command
    assert "--settings" not in command
    assert "--permission-mode" not in command
    assert "--tools" not in command
    assert "--allowedTools" not in command


def test_claude_adapter_limits_read_only_reviewer_tools(
    tmp_path: Path,
) -> None:
    selected = context(tmp_path)
    selected = ProviderRunContext(
        **{
            **selected.__dict__,
            "read_only": True,
        }
    )

    command = ClaudeAdapter().build_command(
        ProviderSettings(provider="claude", model="claude-test"),
        selected,
    ).command

    assert "--permission-mode" in command
    assert "dontAsk" in command
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    assert command[command.index("--allowedTools") + 1] == "Read,Glob,Grep"
    assert "--dangerously-skip-permissions" not in command
    assert "--settings" not in command


@pytest.mark.parametrize(
    ("records", "stderr"),
    [
        (
            [],
            "unshare: unshare failed: Operation not permitted",
        ),
        (
            [
                {
                    "type": "result",
                    "is_error": True,
                    "result": (
                        "Claude requested permissions to use Write, "
                        "but you haven't granted it yet."
                    ),
                }
            ],
            "",
        ),
    ],
)
def test_claude_harness_configuration_failures_are_terminal(
    records: list[dict[str, object]],
    stderr: str,
) -> None:
    failure = ClaudeAdapter().classify_failure(records, stderr)

    assert failure is not None
    assert failure.terminal is True
    assert failure.reason == "harness_configuration_error"


def test_claude_command_permission_error_is_not_a_harness_failure() -> None:
    failure = ClaudeAdapter().classify_failure(
        [
            {
                "type": "result",
                "is_error": True,
                "result": "Bash exited with code 1: permission denied",
            }
        ],
        "",
    )

    assert failure is not None
    assert failure.terminal is False


def test_provider_effort_can_be_limited_by_runtime_configuration(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="does not support"):
        CodexAdapter().build_command(
            ProviderSettings(
                provider="codex",
                model="deployment",
                effort="max",
                allowed_efforts=("low", "high"),
            ),
            context(tmp_path),
        )


def test_claude_credit_failure_is_terminal_but_rate_limit_is_retryable() -> None:
    adapter = ClaudeAdapter()
    terminal = adapter.classify_failure(
        [
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 429,
                "rate_limit_info": {
                    "overageDisabledReason": "org_level_disabled_until"
                },
                "result": "Usage credits are required for this model.",
            }
        ],
        "",
    )
    retryable = adapter.classify_failure(
        [
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 429,
                "result": "Rate limited. Retry later.",
            }
        ],
        "",
    )

    assert terminal is not None and terminal.terminal is True
    assert retryable is not None and retryable.terminal is False


def test_claude_subscription_boundary_exposes_reset_time() -> None:
    failure = ClaudeAdapter().classify_failure(
        [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": 1785616800,
                    "rateLimitType": "five_hour",
                    "overageStatus": "rejected",
                    "overageDisabledReason": "org_level_disabled",
                    "isUsingOverage": False,
                },
            },
            {
                "type": "assistant",
                "message": {
                    "model": "<synthetic>",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You've hit your limit · "
                                "resets 8:40pm (UTC)"
                            ),
                        }
                    ],
                },
                "parent_tool_use_id": None,
                "error": "rate_limit",
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 429,
                "result": (
                    "You've hit your limit · resets 8:40pm (UTC)"
                ),
            },
        ],
        "",
    )

    assert failure is not None
    assert failure.terminal is False
    assert failure.summary == (
        "You've hit your limit · resets 8:40pm (UTC)"
    )
    assert failure.api_status == 429
    assert failure.reason == "org_level_disabled"
    assert failure.wait_category == "subscription_wait"
    assert failure.retry_at_epoch == 1785616800


def test_provider_activity_observations_normalize_tool_lifecycles() -> None:
    codex_start = CodexAdapter().activity_observations(
        {
            "type": "item.started",
            "item": {
                "id": "item-1",
                "type": "command_execution",
                "command": "python simulate.py",
            },
        }
    )
    claude_end = ClaudeAdapter().activity_observations(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                    }
                ]
            },
        }
    )

    assert codex_start[0].phase == "start"
    assert codex_start[0].category == "foreground_tool"
    assert codex_start[0].activity_id == "item-1"
    assert claude_end[0].phase == "end"
    assert claude_end[0].activity_id == "tool-1"


def test_claude_model_identity_uses_primary_assistant_records() -> None:
    adapter = ClaudeAdapter()

    assistant = adapter.model_observations(
        {
            "type": "assistant",
            "message": {"model": "claude-fable-5"},
        }
    )
    system = adapter.model_observations(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-fable-5",
        }
    )
    subagent = adapter.model_observations(
        {
            "type": "assistant",
            "parent_tool_use_id": "tool-1",
            "message": {"model": "claude-haiku-4-5"},
        }
    )
    usage = adapter.model_observations(
        {
            "type": "result",
            "modelUsage": {
                "claude-fable-5": {},
                "claude-haiku-4-5": {},
            },
        }
    )
    synthetic = adapter.model_observations(
        {
            "type": "assistant",
            "message": {"model": "<synthetic>"},
            "error": "rate_limit",
        }
    )

    assert assistant[0].model == "claude-fable-5"
    assert assistant[0].source == "assistant.message.model"
    assert system == ()
    assert subagent == ()
    assert usage == ()
    assert synthetic == ()
    assert adapter.models_match("fable", "claude-fable-5")
    assert adapter.models_match(
        "claude-fable-5",
        "claude-fable-5-20260731",
    )
    assert not adapter.models_match(
        "claude-fable-5",
        "claude-opus-5",
    )


@pytest.mark.parametrize("adapter", [CodexAdapter(), ClaudeAdapter()])
def test_resume_unavailable_is_detected_in_json_records(adapter) -> None:
    assert adapter.resume_is_unavailable(
        [
            {
                "type": "error",
                "error": "No session found for requested identifier",
            }
        ],
        "",
    )
