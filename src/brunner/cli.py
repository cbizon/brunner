from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Callable, Sequence

from brunner.archive import load_campaign_archive
from brunner.cluster import (
    ClusterCampaign,
    ClusterCampaignClient,
    apply_campaign_image_overrides,
    apply_definition_image_override,
    finalize_cluster_trial,
    prepare_cluster_campaign,
    run_cluster_controller,
)
from brunner.contract import load_output_contract, render_output_requirements
from brunner.dashboard import start_campaign_server
from brunner.definition import BenchmarkDefinition
from brunner.reference import (
    build_reference_manifest,
    validate_reference_manifest,
)
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
    definition = apply_definition_image_override(definition)
    definition.validate()
    return definition


def load_cluster_campaign(
    value: str,
    definition: BenchmarkDefinition,
    contract: Any,
) -> ClusterCampaign:
    module_name, separator, attribute_name = value.partition(":")
    if not separator:
        attribute_name = "build_campaign"
    module = importlib.import_module(module_name)
    selected = getattr(module, attribute_name)
    campaign = selected(definition, contract)
    if not isinstance(campaign, ClusterCampaign):
        raise TypeError(f"{value} did not provide a ClusterCampaign")
    campaign = apply_campaign_image_overrides(campaign)
    campaign.validate()
    return campaign


def build_parser(*, require_benchmark: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunner")
    parser.add_argument(
        "--benchmark",
        required=require_benchmark,
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

    reference = subparsers.add_parser("reference-build")
    reference.add_argument("--output", type=_path)

    subparsers.add_parser("reference-validate")

    campaign_submit = subparsers.add_parser("campaign-submit")
    campaign_submit.add_argument("campaign")
    campaign_submit.add_argument("--resume-from", type=_path)
    campaign_status = subparsers.add_parser("campaign-status")
    campaign_status.add_argument("campaign")
    campaign_continue = subparsers.add_parser("campaign-continue")
    campaign_continue.add_argument("campaign")
    campaign_continue.add_argument("test_id")
    campaign_continue.add_argument(
        "--additional-attempts",
        type=int,
        default=1,
    )
    campaign_continue.add_argument("--timeout-seconds", type=float)
    campaign_monitor = subparsers.add_parser("campaign-monitor")
    campaign_monitor.add_argument("archive", type=_path)
    campaign_monitor.add_argument("--local-port", type=int, default=8765)
    campaign_sync = subparsers.add_parser("campaign-sync")
    campaign_sync.add_argument("campaign")
    campaign_sync.add_argument("destination", type=_path)
    campaign_retire = subparsers.add_parser("campaign-retire")
    campaign_retire.add_argument("campaign")
    campaign_retire.add_argument("archive", type=_path)

    for name in (
        "controller-prepare",
        "controller-run",
        "controller-finalize",
    ):
        internal = subparsers.add_parser(name)
        internal.add_argument("campaign")
        internal.add_argument("--campaign-sha256", required=True)
        if name == "controller-finalize":
            internal.add_argument("--trial-relative", required=True)
            internal.add_argument("--output-relative", required=True)
            internal.add_argument(
                "--assessment-only",
                action="store_true",
            )
    return parser


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort")


def execute(
    definition: BenchmarkDefinition | None,
    args: argparse.Namespace,
) -> Any:
    if args.command == "campaign-monitor":
        if not 1 <= args.local_port <= 65535:
            raise ValueError("local monitor port is invalid")
        archive = load_campaign_archive(args.archive)
        server, url = start_campaign_server(
            archive["root"],
            port=args.local_port,
        )
        print(url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return None
        finally:
            server.server_close()
        return None
    if definition is None:
        raise ValueError(
            f"{args.command} requires --benchmark"
        )
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
    if args.command.startswith("campaign-") or args.command.startswith(
        "controller-"
    ):
        benchmark_ref = getattr(args, "benchmark", None)
        if not benchmark_ref:
            raise ValueError(
                "cluster campaign commands require --benchmark so the "
                "controller image can load the same definition"
            )
        campaign = load_cluster_campaign(
            args.campaign,
            definition,
            contract,
        )
        if args.command == "controller-prepare":
            return prepare_cluster_campaign(
                definition,
                contract,
                campaign,
                expected_sha256=args.campaign_sha256,
            )
        if args.command == "controller-run":
            run_cluster_controller(
                definition,
                contract,
                campaign,
                benchmark_ref=benchmark_ref,
                campaign_ref=args.campaign,
                expected_sha256=args.campaign_sha256,
            )
            return None
        if args.command == "controller-finalize":
            return finalize_cluster_trial(
                definition,
                contract,
                campaign,
                expected_sha256=args.campaign_sha256,
                trial_relative=args.trial_relative,
                output_relative=args.output_relative,
                assessment_only=args.assessment_only,
            )
        client = ClusterCampaignClient(
            definition,
            campaign,
            benchmark_ref=benchmark_ref,
            campaign_ref=args.campaign,
        )
        if args.command == "campaign-submit":
            return client.submit(resume_from=args.resume_from)
        if args.command == "campaign-status":
            return client.status()
        if args.command == "campaign-continue":
            return client.continue_trial(
                args.test_id,
                additional_attempts=args.additional_attempts,
                timeout_seconds=args.timeout_seconds,
            )
        if args.command == "campaign-sync":
            return client.sync(args.destination)
        if args.command == "campaign-retire":
            return client.retire(args.archive)
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
    parser = build_parser(require_benchmark=False)
    args = parser.parse_args()
    if args.command != "campaign-monitor" and not args.benchmark:
        parser.error(f"{args.command} requires --benchmark")
    definition = (
        load_definition(args.benchmark)
        if args.benchmark
        else None
    )
    result = execute(definition, args)
    if result is not None:
        _print(result)


if __name__ == "__main__":
    main()
