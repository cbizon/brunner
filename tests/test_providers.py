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
