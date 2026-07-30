from __future__ import annotations

from brunner.evaluator import (
    load_evaluation_input,
    write_evaluation_result,
)


def main() -> int:
    evaluation_input = load_evaluation_input()
    observed = evaluation_input.artifact("transformed-text").path.read_text()
    expected = (
        evaluation_input.workspace / "input.txt"
    ).read_text().upper()
    passed = observed == expected
    write_evaluation_result(
        evaluation_input,
        status="complete" if passed else "failed",
        summary={
            "passed": passed,
            "expected_characters": len(expected),
            "observed_characters": len(observed),
        },
        metrics={"exact_match": 1.0 if passed else 0.0},
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
