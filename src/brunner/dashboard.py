from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _relative_link(output: Path, value: str | None) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        relative = path.resolve().relative_to(output.parent.resolve())
    except ValueError:
        return ""
    return relative.as_posix()


def _seconds(value: object) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return f"{value:.1f}"


def write_campaign_dashboard(
    state: dict[str, Any],
    output: Path,
) -> Path:
    trials = state.get("trials", [])
    phases = Counter(str(trial.get("phase")) for trial in trials)
    outcomes = Counter(
        str(trial.get("outcome"))
        for trial in trials
        if trial.get("outcome")
    )
    cards = "".join(
        (
            '<div class="card">'
            f"<span>{html.escape(name)}</span>"
            f"<strong>{count}</strong>"
            "</div>"
        )
        for name, count in sorted(phases.items())
    )
    rows = []
    for trial in trials:
        evaluation = trial.get("evaluation", {})
        report = _relative_link(output, evaluation.get("report"))
        report_cell = (
            f'<a href="{html.escape(report)}">report</a>'
            if report
            else ""
        )
        assessment_links = []
        for assessment in evaluation.get("assessments", []):
            if not isinstance(assessment, dict):
                continue
            for assessment_report in assessment.get("reports", []):
                if not isinstance(assessment_report, dict):
                    continue
                href = _relative_link(
                    output,
                    str(
                        Path(trial.get("collected_trial", ""))
                        / str(assessment_report.get("path", ""))
                    ),
                )
                if not href:
                    continue
                assessment_links.append(
                    f'<a href="{html.escape(href)}">'
                    f"{html.escape(str(assessment.get('assessment_id', 'review')))}</a>"
                )
        snapshot = trial.get("backend_snapshot", {})
        warning = (
            trial.get("error")
            or trial.get("collection_error")
            or trial.get("cleanup_error")
            or ""
        )
        usage = trial.get("usage", {})
        timing = trial.get("timing", {})
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(trial.get('test_id', '')))}</td>"
            f"<td>{html.escape(str(trial.get('provider', '')))}</td>"
            f"<td>{html.escape(str(trial.get('model', '')))}</td>"
            f"<td>{html.escape(str(trial.get('effort') or 'default'))}</td>"
            f"<td>{html.escape(str(trial.get('phase', '')))}</td>"
            f"<td>{html.escape(str(trial.get('outcome') or ''))}</td>"
            f"<td>{html.escape(str(evaluation.get('assessment_status') or ''))}</td>"
            f"<td>{html.escape(str(usage.get('total_tokens') or ''))}</td>"
            f"<td>{_seconds(timing.get('wall_seconds'))}</td>"
            f"<td>{_seconds(timing.get('agent_active_seconds'))}</td>"
            f"<td>{_seconds(timing.get('external_wait_seconds'))}</td>"
            f"<td>{_seconds(timing.get('subscription_wait_seconds'))}</td>"
            f"<td>{html.escape(str(snapshot.get('node') or ''))}</td>"
            f"<td>{report_cell}</td>"
            f"<td>{' · '.join(assessment_links)}</td>"
            f"<td>{html.escape(str(warning))}</td>"
            "</tr>"
        )
    events = "\n".join(
        (
            f"{event.get('time', '')} "
            f"{event.get('test_id') or '-'} "
            f"{event.get('type', '')}: {event.get('message', '')}"
        )
        for event in state.get("events", [])[-50:]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(state.get("campaign_id", "brunner campaign")))}</title>
<style>
:root {{ --ink:#172019; --paper:#efe9dc; --panel:#fffdf7; --line:#bcb39e;
  --accent:#a83b24; --green:#176b55; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:
  linear-gradient(120deg,transparent 60%,#d9d2b8 60% 62%,transparent 62%),
  var(--paper); font:15px/1.4 "IBM Plex Mono","Courier New",monospace; }}
main {{ max-width:1400px; margin:auto; padding:40px 24px; }}
h1 {{ max-width:900px; font:700 clamp(2.5rem,7vw,6rem)/.88 Georgia,serif;
  margin:0 0 20px; }}
.status {{ color:var(--accent); font-size:1.2rem; font-weight:bold; }}
.cards {{ display:flex; flex-wrap:wrap; gap:10px; margin:24px 0; }}
.card {{ min-width:130px; padding:14px; background:var(--panel);
  border:1px solid var(--line); }}
.card span {{ display:block; }}
.card strong {{ font:700 2rem/1 Georgia,serif; }}
.table {{ overflow:auto; border:1px solid var(--line); }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); }}
th,td {{ padding:9px; border-bottom:1px solid var(--line);
  text-align:left; vertical-align:top; white-space:nowrap; }}
td:last-child {{ white-space:normal; min-width:240px; }}
pre {{ padding:18px; background:#1e261f; color:#f5f0df; overflow:auto; }}
a {{ color:var(--green); font-weight:bold; }}
</style>
</head>
<body><main>
<h1>{html.escape(str(state.get("campaign_id", "campaign")))}</h1>
<div class="status">{html.escape(str(state.get("status", "")))}</div>
<p>{html.escape(str(state.get("benchmark_id", "")))} ·
{len(trials)} trials · outcomes {html.escape(json.dumps(outcomes))}</p>
<div class="cards">{cards}</div>
<div class="table"><table>
<thead><tr><th>Trial</th><th>Provider</th><th>Model</th><th>Effort</th>
<th>Phase</th><th>Outcome</th><th>Assessment</th><th>Tokens</th>
<th>Wall s</th><th>Agent s</th><th>External wait s</th>
<th>Subscription wait s</th><th>Node</th>
<th>Report</th><th>Assessment reports</th><th>Issue</th>
</tr></thead><tbody>{''.join(rows)}</tbody>
</table></div>
<h2>Recent events</h2>
<pre>{html.escape(events)}</pre>
</main></body></html>
"""
    )
    return output
