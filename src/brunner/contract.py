from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator

from brunner.errors import ContractError
from brunner.hashing import sha256_bytes


def _safe_relative_path(value: str, *, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{label} must be a safe relative path: {value!r}")
    return path.as_posix()


def _validation_errors(schema: dict[str, Any], value: Any) -> list[str]:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    return [
        (
            f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: "
            f"{error.message}"
        )
        for error in errors
    ]


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


@dataclass(frozen=True)
class OutputContract:
    path: Path
    data: dict[str, Any]
    sha256: str

    @property
    def benchmark_id(self) -> str:
        return str(self.data["benchmark_id"])

    @property
    def submission_manifest(self) -> str:
        return str(self.data["submission"]["manifest_path"])

    @property
    def run_status_path(self) -> str:
        return str(self.data["run_status_path"])

    @property
    def work_unit_ids(self) -> tuple[str, ...]:
        return tuple(
            str(unit["id"]) for unit in self.data.get("work_units", ())
        )

    @property
    def submission_schema(self) -> dict[str, Any]:
        return dict(self.data["submission"]["schema"])

    def final_response_schema(self) -> dict[str, Any]:
        completed_units: dict[str, Any] = {
            "type": "array",
            "items": {"type": "string"},
        }
        if self.work_unit_ids:
            completed_units["items"]["enum"] = list(self.work_unit_ids)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": [
                "status",
                "submission_manifest",
                "completed_units",
                "limitations",
            ],
            "properties": {
                "status": {
                    "enum": ["complete", "partial", "failed"],
                },
                "submission_manifest": {
                    "const": self.submission_manifest,
                },
                "completed_units": completed_units,
                "limitations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "details": {
                    "type": "object",
                },
            },
        }


def load_output_contract(
    path: Path,
    *,
    expected_benchmark_id: str | None = None,
) -> OutputContract:
    path = path.resolve()
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ContractError(f"invalid output contract JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError("output contract must be a JSON object")

    schema_path = files("brunner.schemas").joinpath(
        "output-contract.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    errors = _validation_errors(schema, value)
    if errors:
        raise ContractError("invalid output contract: " + "; ".join(errors))

    if (
        expected_benchmark_id is not None
        and value["benchmark_id"] != expected_benchmark_id
    ):
        raise ContractError(
            "output contract benchmark_id does not match definition: "
            f"{value['benchmark_id']!r} != {expected_benchmark_id!r}"
        )

    _safe_relative_path(
        value["submission"]["manifest_path"],
        label="submission manifest_path",
    )
    _safe_relative_path(value["run_status_path"], label="run_status_path")

    work_units = [unit["id"] for unit in value.get("work_units", ())]
    if len(work_units) != len(set(work_units)):
        raise ContractError("work unit ids must be unique")
    artifacts = [artifact["id"] for artifact in value.get("artifacts", ())]
    if len(artifacts) != len(set(artifacts)):
        raise ContractError("artifact ids must be unique")
    for artifact in value.get("artifacts", ()):
        if "path" in artifact:
            _safe_relative_path(
                artifact["path"],
                label=f"artifact {artifact['id']!r} path",
            )
        minimum = artifact.get("minimum_bytes")
        maximum = artifact.get("maximum_bytes")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ContractError(
                f"artifact {artifact['id']!r} minimum_bytes exceeds maximum_bytes"
            )
        artifact_schema = artifact.get("json_schema")
        if artifact_schema is not None:
            Draft202012Validator.check_schema(artifact_schema)

    submission_schema = value["submission"]["schema"]
    Draft202012Validator.check_schema(submission_schema)
    return OutputContract(
        path=path,
        data=value,
        sha256=sha256_bytes(_canonical_bytes(value)),
    )


def validate_json(
    value: Any,
    schema: dict[str, Any],
    *,
    label: str,
) -> None:
    errors = _validation_errors(schema, value)
    if errors:
        raise ContractError(f"invalid {label}: " + "; ".join(errors))


def validate_final_response(
    value: Any,
    contract: OutputContract,
    *,
    label: str,
) -> None:
    validate_json(
        value,
        contract.final_response_schema(),
        label=label,
    )
    assert isinstance(value, dict)
    completed_units = value["completed_units"]
    if len(completed_units) != len(set(completed_units)):
        raise ContractError(
            f"invalid {label}: completed_units contains duplicates"
        )
    if value["status"] == "complete":
        expected = set(contract.work_unit_ids)
        actual = set(completed_units)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected))
            raise ContractError(
                f"invalid {label}: complete status must include every work "
                f"unit ({'; '.join(details)})"
            )


def _schema_properties(
    schema: dict[str, Any],
) -> Iterator[tuple[str, bool, dict[str, Any]]]:
    required = set(schema.get("required", ()))
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return
    for name, child in properties.items():
        if isinstance(child, dict):
            yield name, name in required, child


def _schema_type(schema: dict[str, Any]) -> str:
    if "const" in schema:
        return f"constant `{schema['const']}`"
    if "enum" in schema:
        return "one of " + ", ".join(f"`{item}`" for item in schema["enum"])
    value = schema.get("type")
    if isinstance(value, list):
        return " or ".join(str(item) for item in value)
    return str(value or "value")


def render_output_requirements(contract: OutputContract) -> str:
    data = contract.data
    lines = [f"## Required output: {data['title']}", ""]
    if data.get("description"):
        lines.extend((str(data["description"]), ""))

    lines.extend(
        (
            "Write the submission manifest to "
            f"`{contract.submission_manifest}`. It must conform to "
            "`schema/submission.schema.json`.",
            "",
        )
    )
    properties = list(_schema_properties(contract.submission_schema))
    if properties:
        lines.extend(("### Submission manifest fields", ""))
        for name, required, schema in properties:
            requirement = "required" if required else "optional"
            description = schema.get("description")
            suffix = f" {description}" if description else ""
            lines.append(
                f"- `{name}`: {_schema_type(schema)} ({requirement}).{suffix}"
            )
        lines.append("")

    if data.get("work_units"):
        lines.extend(("### Work units", ""))
        for unit in data["work_units"]:
            lines.append(f"- `{unit['id']}`: {unit['description']}")
        lines.append("")

    if data.get("artifacts"):
        lines.extend(("### Required artifacts", ""))
        for artifact in data["artifacts"]:
            location = (
                f"`{artifact['path']}`"
                if "path" in artifact
                else f"manifest value at `{artifact['manifest_pointer']}`"
            )
            optional = "" if artifact.get("required", True) else " Optional."
            media = (
                f" Media type: `{artifact['media_type']}`."
                if artifact.get("media_type")
                else ""
            )
            lines.append(
                f"- `{artifact['id']}` at {location}: "
                f"{artifact['description']}{optional}{media}"
            )
            if "json_schema" in artifact:
                lines.append(
                    "  - The artifact must be valid JSON conforming to "
                    f"`schema/artifacts/{artifact['id']}.schema.json`."
                )
            for detail in artifact.get("details", ()):
                lines.append(f"  - {detail}")
        lines.append("")

    if data.get("instructions"):
        lines.extend(("### Output constraints", ""))
        lines.extend(f"- {instruction}" for instruction in data["instructions"])
        lines.append("")

    lines.extend(
        (
            f"Before finishing, write `{contract.run_status_path}` conforming "
            "to `schema/final-response.schema.json`.",
            "",
            "The run status contains `status`, `submission_manifest`, "
            "`completed_units`, and `limitations`. Use `complete` only when "
            "every required work unit and artifact is valid.",
        )
    )
    return "\n".join(lines).rstrip() + "\n"
