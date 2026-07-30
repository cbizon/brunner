from __future__ import annotations

import json

from brunner.evaluator import (
    load_evaluation_input,
    write_evaluation_result,
)


def main() -> int:
    evaluation_input = load_evaluation_input()
    if evaluation_input.reference_root is None:
        raise RuntimeError("numeric benchmark requires a reference bundle")
    observed = json.loads(
        evaluation_input.artifact("squared-values").path.read_text()
    )
    expected = json.loads(
        (
            evaluation_input.reference_root / "answers.json"
        ).read_text()
    )
    passed = observed == expected
    observed_values = observed["results"]
    expected_values = expected["results"]
    matching = sum(
        actual == wanted
        for actual, wanted in zip(observed_values, expected_values)
    )
    write_evaluation_result(
        evaluation_input,
        status="complete" if passed else "failed",
        summary={
            "passed": passed,
            "matching_values": matching,
            "total_values": len(expected_values),
        },
        metrics={
            "exact_match": 1.0 if passed else 0.0,
            "value_accuracy": matching / len(expected_values),
        },
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
