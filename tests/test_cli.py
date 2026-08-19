from __future__ import annotations

import tomllib
from pathlib import Path

import brunner.backends as backends
import brunner.evaluation as evaluation

from brunner.cli import build_parser


ROOT = Path(__file__).parents[1]


def test_public_cli_does_not_expose_host_agent_execution() -> None:
    help_text = build_parser(require_benchmark=False).format_help()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert "local-run" not in help_text
    assert "trial-run" not in help_text
    assert "trial-evaluate" not in help_text
    assert "trial-assess" not in help_text
    assert "campaign-init" not in help_text
    assert "campaign-step" not in help_text
    assert "campaign-run" not in help_text
    assert "campaign-submit" in help_text
    assert "campaign-retrieve" in help_text
    assert "brunner-agent" not in project["project"]["scripts"]
    assert not hasattr(evaluation, "evaluate_trial")


def test_public_backends_are_container_isolated() -> None:
    assert not hasattr(backends, "LocalBackend")
    assert not hasattr(backends, "ContainerBackend")
    assert (
        backends.KubernetesBackend.agent_isolation
        == backends.CONTAINER_ISOLATION
    )
    assert backends.KubernetesBackend.trusted_evaluation == "kubernetes"
