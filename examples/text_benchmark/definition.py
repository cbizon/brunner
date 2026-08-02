from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

from brunner import (
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
)


ROOT = Path(__file__).resolve().parent


def build_definition() -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="text-uppercase",
        version="1.0.0",
        root=ROOT,
        contract_path=ROOT / "output-contract.json",
        challenge=ChallengeDefinition(root=ROOT / "challenge"),
        evaluation=EvaluationDefinition(
            command=(sys.executable, str(ROOT / "evaluator.py")),
        ),
    )


def build_materialized_definition() -> BenchmarkDefinition:
    definition = build_definition()
    return replace(
        definition,
        challenge=replace(
            definition.challenge,
            materialize_command=(
                sys.executable,
                str(ROOT / "materialize_challenge.py"),
            ),
            materialize_timeout_seconds=60,
        ),
    )
