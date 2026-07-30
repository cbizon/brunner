from __future__ import annotations

import json
from pathlib import Path

import pytest

from brunner.contract import (
    load_output_contract,
    render_output_requirements,
    validate_json,
)
from brunner.errors import ContractError


ROOT = Path(__file__).parents[1]
CONTRACT_PATH = ROOT / "examples/text_benchmark/output-contract.json"


def test_contract_generates_final_response_schema() -> None:
    contract = load_output_contract(
        CONTRACT_PATH,
        expected_benchmark_id="text-uppercase",
    )

    schema = contract.final_response_schema()
    assert schema["properties"]["completed_units"]["items"]["enum"] == [
        "uppercase"
    ]
    validate_json(
        {
            "status": "complete",
            "submission_manifest": "submission/manifest.json",
            "completed_units": ["uppercase"],
            "limitations": [],
        },
        schema,
        label="final response",
    )


def test_contract_rendering_uses_machine_readable_definition() -> None:
    contract = load_output_contract(CONTRACT_PATH)

    rendered = render_output_requirements(contract)

    assert "Uppercase transformation" in rendered
    assert "`output`: string (required)" in rendered
    assert "`uppercase`" in rendered
    assert "manifest value at `/output`" in rendered
    assert contract.run_status_path in rendered


def test_contract_rejects_duplicate_work_units(tmp_path: Path) -> None:
    value = json.loads(CONTRACT_PATH.read_text())
    value["work_units"].append(value["work_units"][0])
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value))

    with pytest.raises(ContractError, match="work unit ids must be unique"):
        load_output_contract(path)


def test_contract_rejects_path_escape(tmp_path: Path) -> None:
    value = json.loads(CONTRACT_PATH.read_text())
    value["submission"]["manifest_path"] = "../manifest.json"
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value))

    with pytest.raises(ContractError, match="safe relative path"):
        load_output_contract(path)
