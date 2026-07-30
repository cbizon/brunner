from __future__ import annotations

import sys
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
            command=(sys.executable, str(ROOT / "evaluator.py")),
        ),
        reference=ReferenceDefinition(root=ROOT / "reference"),
    )
