from __future__ import annotations

from pathlib import Path
import sys

from brunner import (
    ArtifactPolicy,
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
    QualitativeReviewDefinition,
    RuntimeDefaults,
)
from brunner.providers import ProviderSettings

from examples.diffusion_benchmark.images import EVALUATOR_IMAGE


ROOT = Path(__file__).resolve().parent
AZURE_OPENAI_BASE_URL = (
    "https://renci-analytics.openai.azure.com/openai/v1/"
)


def build_definition() -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="diffusion-equation-solver",
        version="1.0.0",
        display_title="One-dimensional diffusion solver",
        root=ROOT,
        contract_path=ROOT / "output-contract.json",
        challenge=ChallengeDefinition(
            root=ROOT / "challenge",
            materialize_command=(
                sys.executable,
                str(ROOT / "materialize_challenge.py"),
            ),
            materialize_timeout_seconds=60,
            forbidden_names=("reference", "evaluator.py"),
        ),
        evaluation=EvaluationDefinition(
            command=(
                "python",
                "-m",
                "examples.diffusion_benchmark.evaluator",
            ),
            image=EVALUATOR_IMAGE,
            primary_report="evaluation/diffusion-report.html",
            timeout_seconds=3 * 60,
            cpu_request="500m",
            cpu_limit="2",
            memory_request="512Mi",
            memory_limit="2Gi",
            ephemeral_storage_request="256Mi",
            ephemeral_storage_limit="1Gi",
        ),
        qualitative_review=QualitativeReviewDefinition(
            reviewer=ProviderSettings(
                provider="codex",
                model="gpt-5.6-luna",
                effort="low",
                provider_id="azure",
                provider_name="RENCI Azure OpenAI",
                base_url=AZURE_OPENAI_BASE_URL,
                environment_key="AZURE_OPENAI_API_KEY",
            ),
            trial_evidence_paths=(
                "workspace/PROMPT.md",
                "workspace/schema/artifacts/case-results.schema.json",
                "workspace/cases",
                "workspace/submission",
                "evaluation/results.json",
                "evaluation/diffusion-report.html",
                "transcript",
                "timing",
                "usage",
                "status.json",
            ),
            timeout_seconds=10 * 60,
            max_attempts=2,
            retry_initial_seconds=10,
            retry_max_seconds=30,
        ),
        artifacts=ArtifactPolicy(
            collect_evaluated_artifacts=True,
            max_collection_bytes=100 * 1024 * 1024,
            max_diagnostic_collection_bytes=50 * 1024 * 1024,
        ),
        runtime=RuntimeDefaults(
            timeout_seconds=20 * 60,
            finalization_seconds=2 * 60,
            retry_initial_seconds=15,
            retry_max_seconds=2 * 60,
            provider_exit_grace_seconds=20,
            backend_shutdown_grace_seconds=60,
            max_attempts=8,
            submission_poll_seconds=1,
        ),
    )
