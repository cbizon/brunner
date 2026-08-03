from __future__ import annotations

import json
from pathlib import Path

from brunner.report import write_run_report


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _report_fixture(tmp_path: Path, *, display_title: str | None = None) -> str:
    trial = tmp_path / "trial"
    output = trial / "evaluation/run-report.html"
    metadata = {
        "test_id": "monkey-canary-1",
        "benchmark_id": "internal-benchmark-key",
        "provider": "claude",
        "model": "claude-opus-test",
        "effort": "high",
    }
    if display_title is not None:
        metadata["display_title"] = display_title
    _write_json(trial / "metadata/manifest.json", metadata)
    _write_json(
        trial / "status.json",
        {
            "status": "complete",
            "attempts": [
                {
                    "number": 1,
                    "mode": "initial",
                    "status": "complete",
                    "return_code": 0,
                }
            ],
        },
    )
    _write_json(
        trial / "timing/accounting.json",
        {
            "summary": {
                "wall_seconds": 125,
                "agent_active_seconds": 60,
                "foreground_tool_seconds": 20,
                "background_job_seconds": 40,
                "subscription_wait_seconds": 5,
                "runner_retry_wait_seconds": 0,
            },
            "intervals": [{"category": "<timing>"}],
        },
    )
    _write_json(
        trial / "usage/usage.json",
        {
            "total_tokens": 12345,
            "logical_input_tokens": 10000,
            "output_tokens": 2345,
            "cache_read_input_tokens": 8000,
            "cache_write_input_tokens": 100,
            "reasoning_output_tokens": None,
        },
    )
    _write_json(
        trial / "evaluation/results.json",
        {
            "status": "complete",
            "assessment_status": "complete",
            "summary": {"message": "<evaluation complete>"},
            "metrics": {"exact_match": 1.0},
            "reports": [
                {
                    "path": "evaluation/benchmark-report.html",
                    "title": "A & B",
                }
            ],
            "assessments": [
                {
                    "assessment_id": "qualitative",
                    "status": "complete",
                    "required": True,
                    "method": {
                        "kind": "reviewer",
                        "provider": "codex",
                        "model": "reviewer-model",
                    },
                    "reports": [
                        {
                            "path": "evaluation/qualitative.html",
                            "title": "Qualitative <review>",
                        }
                    ],
                }
            ],
        },
    )

    write_run_report(trial, output)
    return output.read_text()


def test_run_report_uses_summary_facts_and_collapsed_raw_json(
    tmp_path: Path,
) -> None:
    report = _report_fixture(tmp_path)

    assert "<h1>" not in report
    assert "internal-benchmark-key" not in report
    assert "Run ID<strong>monkey-canary-1</strong>" in report
    assert "Effort<strong>high</strong>" in report
    assert "Evaluation<strong>complete</strong>" in report
    assert "Assessments<strong>complete</strong>" in report
    assert "<th>Required</th>" not in report
    assert "<th>ID</th><th>Status</th>" in report
    assert "Wall time<strong>2m 05s</strong>" in report
    assert "Total tokens<strong>12,345</strong>" in report
    assert "Exact Match<strong>1</strong>" in report
    assert "<details>" in report
    assert "<details open" not in report
    assert "Raw timing JSON" in report
    assert "Raw usage JSON" in report
    assert "Raw evaluation JSON" in report
    assert "&lt;evaluation complete&gt;" in report
    assert "A &amp; B" in report
    assert "Qualitative &lt;review&gt;" in report


def test_run_report_renders_optional_display_title(tmp_path: Path) -> None:
    report = _report_fixture(
        tmp_path,
        display_title="Monkeybench <Canary>",
    )

    assert "<h1>Monkeybench &lt;Canary&gt;</h1>" in report
    assert "<title>Monkeybench &lt;Canary&gt;</title>" in report
