from __future__ import annotations

from pathlib import Path

from brunner import (
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
    ReferenceDefinition,
)


ROOT = Path(__file__).resolve().parent


def build_definition() -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="numeric-square",
        version="1.0.0",
        root=ROOT,
        contract_path=ROOT / "output-contract.json",
        challenge=ChallengeDefinition(root=ROOT / "challenge"),
        evaluation=EvaluationDefinition(
            command=(
                "python",
                "-m",
                "examples.numeric_benchmark.evaluator",
            ),
            image=(
                "registry.example/brunner-numeric-evaluator@sha256:"
                + "0" * 64
            ),
        ),
        reference=ReferenceDefinition(root=ROOT / "reference"),
    )
