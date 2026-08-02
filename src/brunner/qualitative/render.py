from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any


def _text(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _list(items: object, empty: str) -> str:
    if not isinstance(items, list) or not items:
        return f"<li>{html.escape(empty)}</li>"
    return "".join(f"<li>{_text(item)}</li>" for item in items)


def _evidence_count(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    evidence = value.get("evidence")
    return len(evidence) if isinstance(evidence, list) else 0


def render_review(review: dict[str, Any]) -> str:
    approach = review.get("approach", {})
    criteria = review.get("criteria", {})
    transcript = review.get("transcript_review", {})
    overall = review.get("overall", {})
    time_accounting = (
        transcript.get("time_accounting", {})
        if isinstance(transcript, dict)
        else {}
    )
    criterion_rows = []
    if isinstance(criteria, dict):
        for name, criterion in criteria.items():
            selected = criterion if isinstance(criterion, dict) else {}
            criterion_rows.append(
                "<tr>"
                f"<th>{_text(name.replace('_', ' ').title())}</th>"
                f"<td><span class=\"rating\">"
                f"{_text(selected.get('rating'))}</span></td>"
                f"<td>{_text(selected.get('confidence'))}</td>"
                f"<td>{_text(selected.get('summary'))}</td>"
                f"<td>{_evidence_count(selected)}</td>"
                "</tr>"
            )
    timing_rows = []
    if isinstance(time_accounting, dict):
        for key in (
            "wall_seconds",
            "agent_active_seconds",
            "foreground_tool_seconds",
            "external_wait_seconds",
            "subscription_wait_seconds",
            "runner_retry_wait_seconds",
            "runner_overhead_seconds",
            "unclassified_seconds",
            "background_job_seconds",
        ):
            timing_rows.append(
                "<tr>"
                f"<th>{_text(key.replace('_', ' '))}</th>"
                f"<td>{_text(time_accounting.get(key))}</td>"
                "</tr>"
            )
    milestones = transcript.get("milestones", []) if isinstance(
        transcript, dict
    ) else []
    milestone_items = []
    if isinstance(milestones, list):
        for milestone in milestones:
            selected = milestone if isinstance(milestone, dict) else {}
            milestone_items.append(
                "<li>"
                f"<strong>{_text(selected.get('phase'))}</strong>"
                f"<span>{_text(selected.get('summary'))}</span>"
                "</li>"
            )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Qualitative review</title>
<style>
:root {{ --ink:#17221c; --paper:#f1ecdd; --panel:#fffdf7; --line:#bcb49f;
  --accent:#b44827; --green:#176653; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:
  radial-gradient(circle at 88% 2%,#d5dfc5 0,transparent 27rem),
  linear-gradient(120deg,transparent 68%,#ddd4ba 68% 69%,transparent 69%),
  var(--paper); font:16px/1.45 Georgia,serif; }}
main {{ max-width:1180px; margin:auto; padding:48px 24px 80px; }}
h1 {{ max-width:800px; margin:0 0 24px;
  font-size:clamp(3rem,8vw,6.5rem); line-height:.86; letter-spacing:-.05em; }}
h2 {{ margin-top:40px; }}
.lede {{ max-width:820px; font-size:1.2rem; }}
.facts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:12px; margin:28px 0; }}
.fact,section {{ background:var(--panel); border:1px solid var(--line);
  padding:18px; }}
.fact span {{ display:block; color:#5a5d53; text-transform:uppercase;
  font:12px/1.2 "Courier New",monospace; letter-spacing:.08em; }}
.fact strong {{ display:block; margin-top:8px; font-size:1.35rem; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); }}
th,td {{ padding:11px; border:1px solid var(--line); text-align:left;
  vertical-align:top; }}
.rating {{ color:var(--accent); font-weight:bold; }}
.milestones {{ display:grid; gap:8px; padding:0; list-style:none; }}
.milestones li {{ display:grid; grid-template-columns:150px 1fr; gap:12px;
  background:var(--panel); border-left:5px solid var(--green); padding:12px; }}
.milestones span {{ display:block; }}
code {{ font-family:"Courier New",monospace; }}
@media (max-width:650px) {{
  main {{ padding:32px 14px 60px; }}
  .milestones li {{ grid-template-columns:1fr; }}
  table {{ font-size:.88rem; }}
}}
</style>
</head>
<body><main>
<h1>Qualitative review</h1>
<p class="lede">{_text(review.get("task_summary"))}</p>
<div class="facts">
<div class="fact"><span>Overall</span><strong>{_text(overall.get("rating"))}</strong></div>
<div class="fact"><span>Confidence</span><strong>{_text(overall.get("confidence"))}</strong></div>
<div class="fact"><span>Approach</span><strong>{_text(approach.get("primary_classification"))}</strong></div>
<div class="fact"><span>Output provenance</span><strong>{_text(approach.get("output_provenance"))}</strong></div>
</div>
<section><strong>Bottom line</strong><p>{_text(overall.get("bottom_line"))}</p></section>
<h2>Criteria</h2>
<table><thead><tr><th>Criterion</th><th>Rating</th><th>Confidence</th>
<th>Summary</th><th>Evidence</th></tr></thead>
<tbody>{''.join(criterion_rows)}</tbody></table>
<h2>Strengths and failures</h2>
<div class="facts">
<section><strong>Strengths</strong><ul>{_list(overall.get("strengths"), "None recorded.")}</ul></section>
<section><strong>Major failures</strong><ul>{_list(overall.get("major_failures"), "None recorded.")}</ul></section>
</div>
<h2>Transcript narrative</h2>
<section><p>{_text(transcript.get("narrative"))}</p></section>
<ul class="milestones">{''.join(milestone_items) or '<li>No milestones recorded.</li>'}</ul>
<h2>Time accounting</h2>
<section><p>{_text(time_accounting.get("summary"))}</p></section>
<table><tbody>{''.join(timing_rows)}</tbody></table>
<h2>Review limitations</h2>
<section><ul>{_list(review.get("review_limitations"), "None recorded.")}</ul></section>
</main></body></html>
"""


def main() -> None:
    review_path = Path(os.environ["BRUNNER_ASSESSMENT_OUTPUT"])
    trial = Path(os.environ["BRUNNER_TRIAL_ROOT"])
    review = json.loads(review_path.read_text())
    if not isinstance(review, dict):
        raise TypeError("qualitative review output must be an object")
    output = trial / "evaluation/qualitative-review.html"
    output.write_text(render_review(review))


if __name__ == "__main__":
    main()
