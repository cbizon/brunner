from __future__ import annotations

import tomllib
from pathlib import Path

import brunner.backends as backends

from brunner.cli import build_parser


ROOT = Path(__file__).parents[1]


def test_public_cli_does_not_expose_host_agent_execution() -> None:
    help_text = build_parser(require_benchmark=False).format_help()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert "local-run" not in help_text
    assert "trial-run" not in help_text
    assert "brunner-agent" not in project["project"]["scripts"]


def test_public_backends_are_container_isolated() -> None:
    assert not hasattr(backends, "LocalBackend")
    assert (
        backends.ContainerBackend.agent_isolation
        == backends.CONTAINER_ISOLATION
    )
    assert (
        backends.KubernetesBackend.agent_isolation
        == backends.CONTAINER_ISOLATION
    )
