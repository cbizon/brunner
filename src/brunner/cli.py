from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from brunner.contract import load_output_contract, render_output_requirements
from brunner.campaign import CampaignRunner
from brunner.definition import BenchmarkDefinition, RuntimeDefaults
from brunner.evaluation import evaluate_trial
from brunner.providers import ProviderSettings
from brunner.reference import (
    build_reference_manifest,
    validate_reference_manifest,
)
from brunner.runner import run_trial
from brunner.staging import stage_challenge
from brunner.trial import TrialIdentity, create_trial, new_test_id


DefinitionFactory = Callable[[], BenchmarkDefinition]


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _path(value: str) -> Path:
    return Path(value).expanduser()


def load_definition(value: str) -> BenchmarkDefinition:
    module_name, separator, attribute_name = value.partition(":")
    if not separator:
        attribute_name = "build_definition"
    module = importlib.import_module(module_name)
    selected = getattr(module, attribute_name)
    definition = selected() if callable(selected) else selected
    if not isinstance(definition, BenchmarkDefinition):
        raise TypeError(
            f"{value} did not provide a BenchmarkDefinition"
        )
    definition.validate()
    return definition


def load_campaign_runner(
    value: str,
    definition: BenchmarkDefinition,
    contract: Any,
) -> CampaignRunner:
    module_name, separator, attribute_name = value.partition(":")
    if not separator:
        attribute_name = "build_campaign"
    module = importlib.import_module(module_name)
    selected = getattr(module, attribute_name)
    runner = selected(definition, contract)
    if not isinstance(runner, CampaignRunner):
        raise TypeError(f"{value} did not provide a CampaignRunner")
    return runner


def build_parser(*, require_benchmark: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunner")
    if require_benchmark:
        parser.add_argument(
            "--benchmark",
            required=True,
            help="Python module and optional attribute, MODULE[:ATTRIBUTE]",
        )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("contract-check")
    subparsers.add_parser("contract-render")

    stage = subparsers.add_parser("stage")
    stage.add_argument("destination", type=_path)

    create = subparsers.add_parser("trial-create")
    create.add_argument("tests_root", type=_path)
    _add_provider_arguments(create)
    create.add_argument("--test-id")

    run = subparsers.add_parser("trial-run")
    run.add_argument("trial", type=_path)
    run.add_argument("--timeout-seconds", type=float)
    run.add_argument("--finalization-seconds", type=float)
    run.add_argument("--provider-executable")

    evaluation = subparsers.add_parser("trial-evaluate")
    evaluation.add_argument("trial", type=_path)

    assessment = subparsers.add_parser("trial-assess")
    assessment.add_argument("trial", type=_path)

    local = subparsers.add_parser("local-run")
    local.add_argument("tests_root", type=_path)
    _add_provider_arguments(local)
    local.add_argument("--test-id")
    local.add_argument("--timeout-seconds", type=float)
    local.add_argument("--finalization-seconds", type=float)
    local.add_argument("--provider-executable")

    reference = subparsers.add_parser("reference-build")
    reference.add_argument("--output", type=_path)

    subparsers.add_parser("reference-validate")

    campaign_init = subparsers.add_parser("campaign-init")
    campaign_init.add_argument("campaign")
    campaign_step = subparsers.add_parser("campaign-step")
    campaign_step.add_argument("campaign")
    campaign_run = subparsers.add_parser("campaign-run")
    campaign_run.add_argument("campaign")
    campaign_run.add_argument("--poll-seconds", type=float, default=5)
    return parser


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort")


def _runtime(
    definition: BenchmarkDefinition,
    args: argparse.Namespace,
) -> RuntimeDefaults:
    defaults = definition.runtime
    return RuntimeDefaults(
        timeout_seconds=(
            args.timeout_seconds
            if getattr(args, "timeout_seconds", None) is not None
            else defaults.timeout_seconds
        ),
        finalization_seconds=(
            args.finalization_seconds
            if getattr(args, "finalization_seconds", None) is not None
            else defaults.finalization_seconds
        ),
        retry_initial_seconds=defaults.retry_initial_seconds,
        retry_max_seconds=defaults.retry_max_seconds,
        provider_exit_grace_seconds=defaults.provider_exit_grace_seconds,
        backend_shutdown_grace_seconds=(
            defaults.backend_shutdown_grace_seconds
        ),
        max_attempts=defaults.max_attempts,
        max_activity_interval_seconds=(
            defaults.max_activity_interval_seconds
        ),
        submission_poll_seconds=defaults.submission_poll_seconds,
    )


def _settings_from_metadata(trial: Path) -> ProviderSettings:
    metadata = json.loads((trial / "metadata/manifest.json").read_text())
    return ProviderSettings(
        provider=str(metadata["provider"]),
        model=str(metadata["model"]),
        effort=metadata.get("effort"),
    )


def execute(
    definition: BenchmarkDefinition,
    args: argparse.Namespace,
) -> Any:
    contract = load_output_contract(
        definition.contract_path,
        expected_benchmark_id=definition.benchmark_id,
    )
    if args.command == "contract-check":
        return {
            "valid": True,
            "benchmark_id": contract.benchmark_id,
            "contract_sha256": contract.sha256,
        }
    if args.command == "contract-render":
        print(render_output_requirements(contract), end="")
        return None
    if args.command == "stage":
        return stage_challenge(definition, contract, args.destination).to_dict()
    if args.command == "trial-create":
        identity = TrialIdentity(
            test_id=args.test_id or new_test_id(args.provider),
            provider=args.provider,
            model=args.model,
            effort=args.effort,
        )
        return {
            "trial": create_trial(
                definition,
                contract,
                args.tests_root,
                identity,
            )
        }
    if args.command == "trial-run":
        return run_trial(
            definition,
            contract,
            args.trial,
            _settings_from_metadata(args.trial),
            runtime=_runtime(definition, args),
            executable=args.provider_executable,
        )
    if args.command == "trial-evaluate":
        return evaluate_trial(definition, contract, args.trial)
    if args.command == "trial-assess":
        from brunner.assessment import run_assessments
        from brunner.io import load_json_object, write_json_atomic
        from brunner.report import write_run_report

        results_path = (
            args.trial / definition.evaluation.results_path
        )
        if not results_path.is_file():
            raise FileNotFoundError(
                "deterministic evaluation result does not exist: "
                f"{results_path}"
            )
        evaluation_result = load_json_object(results_path)
        assessment_index = run_assessments(
            definition,
            contract,
            args.trial,
            evaluation_result,
        )
        evaluation_result["assessment_status"] = assessment_index[
            "status"
        ]
        evaluation_result["required_assessments_complete"] = (
            assessment_index["required_assessments_complete"]
        )
        evaluation_result["assessments"] = assessment_index[
            "assessments"
        ]
        write_json_atomic(results_path, evaluation_result)
        write_run_report(
            args.trial,
            results_path.with_name("run-report.html"),
        )
        return assessment_index
    if args.command == "local-run":
        identity = TrialIdentity(
            test_id=args.test_id or new_test_id(args.provider),
            provider=args.provider,
            model=args.model,
            effort=args.effort,
        )
        trial = create_trial(
            definition,
            contract,
            args.tests_root,
            identity,
        )
        state = run_trial(
            definition,
            contract,
            trial,
            ProviderSettings(
                provider=args.provider,
                model=args.model,
                effort=args.effort,
            ),
            runtime=_runtime(definition, args),
            executable=args.provider_executable,
        )
        evaluation = evaluate_trial(definition, contract, trial)
        return {
            "trial": trial,
            "status": state["status"],
            "evaluation": evaluation,
        }
    if args.command == "reference-build":
        if definition.reference is None:
            raise ValueError("benchmark does not define a reference bundle")
        output = args.output or (
            definition.reference.root
            / definition.reference.manifest_path
        )
        return build_reference_manifest(
            definition.reference.root,
            output,
            metadata={
                "benchmark_id": definition.benchmark_id,
                "benchmark_version": definition.version,
                "contract_sha256": contract.sha256,
            },
        )
    if args.command == "reference-validate":
        if definition.reference is None:
            raise ValueError("benchmark does not define a reference bundle")
        return validate_reference_manifest(
            definition.reference.root,
            definition.reference.root
            / definition.reference.manifest_path,
        )
    if args.command in {
        "campaign-init",
        "campaign-step",
        "campaign-run",
    }:
        runner = load_campaign_runner(
            args.campaign,
            definition,
            contract,
        )
        if args.command == "campaign-init":
            return runner.initialize()
        if args.command == "campaign-step":
            return runner.advance()
        return runner.run(poll_seconds=args.poll_seconds)
    raise AssertionError(args.command)


def run_cli(
    definition: BenchmarkDefinition,
    argv: Sequence[str] | None = None,
) -> None:
    args = build_parser(require_benchmark=False).parse_args(argv)
    result = execute(definition, args)
    if result is not None:
        _print(result)


def main() -> None:
    parser = build_parser(require_benchmark=True)
    args = parser.parse_args()
    definition = load_definition(args.benchmark)
    result = execute(definition, args)
    if result is not None:
        _print(result)


if __name__ == "__main__":
    main()
