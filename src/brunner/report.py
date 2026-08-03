from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _load_optional(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else None


def _json_block(value: object) -> str:
    return html.escape(json.dumps(value, indent=2, sort_keys=True))


def _label(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _display_value(value: object) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return str(value)


def _seconds(value: object) -> str:
    if not isinstance(value, (int, float)):
        return _display_value(value)
    seconds = float(value)
    if seconds < 60:
        return f"{seconds:,.1f}s".replace(".0s", "s")
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:02.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes):02d}m"


def _fact(label: str, value: object) -> str:
    return (
        '<div class="fact">'
        f"{html.escape(label)}"
        f"<strong>{html.escape(_display_value(value))}</strong>"
        "</div>"
    )


def _summary_facts(
    value: dict[str, Any] | None,
    fields: tuple[tuple[str, str, bool], ...],
) -> str:
    if not isinstance(value, dict):
        return '<p class="empty">No summary available.</p>'
    facts = []
    for key, label, seconds in fields:
        if key not in value:
            continue
        displayed = _seconds(value[key]) if seconds else value[key]
        facts.append(_fact(label, displayed))
    return (
        f'<div class="facts summary-facts">{"".join(facts)}</div>'
        if facts
        else '<p class="empty">No summary available.</p>'
    )


def _evaluation_facts(evaluation: dict[str, Any]) -> str:
    facts = []
    for group_name in ("summary", "metrics"):
        group = evaluation.get(group_name)
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                facts.append(_fact(_label(key), value))
    return (
        f'<div class="facts summary-facts">{"".join(facts)}</div>'
        if facts
        else '<p class="empty">No evaluation summary available.</p>'
    )


def _raw_details(label: str, value: object) -> str:
    return (
        "<details>"
        f"<summary>{html.escape(label)}</summary>"
        f"<pre>{_json_block(value)}</pre>"
        "</details>"
    )


def write_run_report(trial: Path, output: Path) -> Path:
    metadata = _load_optional(trial / "metadata/manifest.json") or {}
    status = _load_optional(trial / "status.json") or {}
    timing = _load_optional(trial / "timing/accounting.json")
    if timing is None:
        timing = _load_optional(trial / "timing/goal.json")
    usage = _load_optional(trial / "usage/usage.json")
    evaluation = _load_optional(trial / "evaluation/results.json") or {}
    attempts = status.get("attempts", [])
    assessments = [
        assessment
        for assessment in evaluation.get("assessments", [])
        if isinstance(assessment, dict)
    ]
    reports = list(evaluation.get("reports", []))
    for assessment in assessments:
        reports.extend(assessment.get("reports", []))
    links = []
    seen_reports = set()
    for report in reports:
        if not isinstance(report, dict) or not isinstance(
            report.get("path"), str
        ):
            continue
        if report["path"] in seen_reports:
            continue
        seen_reports.add(report["path"])
        label = report.get("title") or report["path"]
        relative = Path(report["path"])
        try:
            href = relative.relative_to(output.parent)
        except ValueError:
            href = Path(
                html.escape(
                    str((trial / relative).relative_to(output.parent))
                )
            )
        links.append(
            f'<li><a href="{html.escape(href.as_posix())}">'
            f"{html.escape(str(label))}</a></li>"
        )
    assessment_rows = []
    for assessment in assessments:
        method = assessment.get("method", {})
        method_label = method.get("kind", "")
        if method_label == "reviewer":
            method_label = (
                f"{method.get('provider', '')}/"
                f"{method.get('model', '')}"
            )
        assessment_links = []
        for report in assessment.get("reports", []):
            if not isinstance(report, dict) or not isinstance(
                report.get("path"), str
            ):
                continue
            report_path = trial / report["path"]
            try:
                href = report_path.relative_to(output.parent)
            except ValueError:
                continue
            assessment_links.append(
                f'<a href="{html.escape(href.as_posix())}">'
                f"{html.escape(str(report.get('title') or 'report'))}</a>"
            )
        error = assessment.get("error", {})
        assessment_rows.append(
            "<tr>"
            f"<td>{html.escape(str(assessment.get('assessment_id', '')))}</td>"
            f"<td>{html.escape(str(assessment.get('status', '')))}</td>"
            f"<td>{html.escape(str(method_label))}</td>"
            f"<td>{' · '.join(assessment_links)}</td>"
            f"<td>{html.escape(str(error.get('message', '')))}</td>"
            "</tr>"
        )
    attempt_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(attempt.get('number', '')))}</td>"
        f"<td>{html.escape(str(attempt.get('mode', '')))}</td>"
        f"<td>{html.escape(str(attempt.get('status', '')))}</td>"
        f"<td>{html.escape(str(attempt.get('return_code', '')))}</td>"
        f"<td>{html.escape(str(attempt.get('failure', '') or ''))}</td>"
        "</tr>"
        for attempt in attempts
        if isinstance(attempt, dict)
    )
    display_title = metadata.get("display_title")
    heading = (
        f"<h1>{html.escape(str(display_title))}</h1>"
        if isinstance(display_title, str) and display_title.strip()
        else ""
    )
    page_title = display_title or metadata.get("test_id") or "Brunner run"
    timing_summary = timing.get("summary") if isinstance(timing, dict) else None
    timing_facts = _summary_facts(
        timing_summary,
        (
            ("wall_seconds", "Wall time", True),
            ("agent_active_seconds", "Agent active", True),
            ("foreground_tool_seconds", "Foreground tools", True),
            ("background_job_seconds", "Background jobs", True),
            ("subscription_wait_seconds", "Subscription wait", True),
            ("runner_retry_wait_seconds", "Retry wait", True),
            ("external_wait_seconds", "External wait", True),
        ),
    )
    usage_facts = _summary_facts(
        usage,
        (
            ("total_tokens", "Total tokens", False),
            ("logical_input_tokens", "Input tokens", False),
            ("output_tokens", "Output tokens", False),
            ("cache_read_input_tokens", "Cache reads", False),
            ("cache_write_input_tokens", "Cache writes", False),
            ("reasoning_output_tokens", "Reasoning tokens", False),
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(page_title))}</title>
<style>
:root {{ --ink:#1c211d; --paper:#f3efe3; --panel:#fffdf7; --line:#c8c1ad;
  --green:#176b55; --red:#a33b2f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:
  radial-gradient(circle at 85% 5%,#d7dfc8 0,transparent 28rem),var(--paper);
  font:16px/1.45 Georgia,serif; }}
main {{ max-width:1100px; margin:auto; padding:48px 24px; }}
h1 {{ font-size:clamp(2.4rem,7vw,5rem); line-height:.9; margin:0 0 24px; }}
h2 {{ margin-top:36px; }}
.facts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:12px; }}
.summary-facts {{ margin-bottom:14px; }}
.fact,section {{ background:var(--panel); border:1px solid var(--line);
  padding:18px; }}
.fact strong {{ display:block; font-size:1.35rem; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); }}
th,td {{ padding:10px; border:1px solid var(--line); text-align:left;
  vertical-align:top; }}
pre {{ overflow:auto; background:#20251f; color:#f5f0df; padding:16px; }}
a {{ color:var(--green); }}
details {{ margin-top:12px; background:var(--panel); border:1px solid var(--line); }}
summary {{ cursor:pointer; padding:12px 16px; color:var(--green); font-weight:bold; }}
details pre {{ margin:0; border-top:1px solid var(--line); }}
.empty {{ color:#686457; font-style:italic; }}
</style>
</head>
<body><main>
{heading}
<div class="facts">
<div class="fact">Run ID<strong>{html.escape(str(metadata.get("test_id", "")))}</strong></div>
<div class="fact">Provider<strong>{html.escape(str(metadata.get("provider", "")))}</strong></div>
<div class="fact">Model<strong>{html.escape(str(metadata.get("model", "")))}</strong></div>
<div class="fact">Effort<strong>{html.escape(str(metadata.get("effort") or "default"))}</strong></div>
<div class="fact">Status<strong>{html.escape(str(status.get("status", "")))}</strong></div>
<div class="fact">Evaluation<strong>{html.escape(str(evaluation.get("status", "")))}</strong></div>
<div class="fact">Assessments<strong>{html.escape(str(evaluation.get("assessment_status", "not_configured")))}</strong></div>
</div>
<h2>Benchmark reports</h2>
<section><ul>{''.join(links) or '<li>No benchmark-specific reports.</li>'}</ul></section>
<h2>Assessments</h2>
<table><thead><tr><th>ID</th><th>Status</th>
<th>Method</th><th>Reports</th><th>Issue</th></tr></thead>
<tbody>{''.join(assessment_rows) or '<tr><td colspan="5">No assessments configured.</td></tr>'}</tbody></table>
<h2>Attempts</h2>
<table><thead><tr><th>#</th><th>Mode</th><th>Status</th><th>Exit</th><th>Failure</th></tr></thead>
<tbody>{attempt_rows}</tbody></table>
<h2>Timing</h2>{timing_facts}
{_raw_details("Raw timing JSON", timing)}
<h2>Usage</h2>{usage_facts}
{_raw_details("Raw usage JSON", usage)}
<h2>Evaluation summary</h2>{_evaluation_facts(evaluation)}
{_raw_details("Raw evaluation JSON", evaluation)}
</main></body></html>
"""
    )
    return output
