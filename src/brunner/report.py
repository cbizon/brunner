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


def write_run_report(trial: Path, output: Path) -> Path:
    metadata = _load_optional(trial / "metadata/manifest.json") or {}
    status = _load_optional(trial / "status.json") or {}
    timing = _load_optional(trial / "timing/goal.json")
    usage = _load_optional(trial / "usage/usage.json")
    evaluation = _load_optional(trial / "evaluation/results.json") or {}
    attempts = status.get("attempts", [])
    reports = evaluation.get("reports", [])
    links = []
    for report in reports:
        if not isinstance(report, dict) or not isinstance(
            report.get("path"), str
        ):
            continue
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(metadata.get("test_id", "brunner run")))}</title>
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
.fact,section {{ background:var(--panel); border:1px solid var(--line);
  padding:18px; }}
.fact strong {{ display:block; font-size:1.35rem; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); }}
th,td {{ padding:10px; border:1px solid var(--line); text-align:left;
  vertical-align:top; }}
pre {{ overflow:auto; background:#20251f; color:#f5f0df; padding:16px; }}
a {{ color:var(--green); }}
</style>
</head>
<body><main>
<h1>{html.escape(str(metadata.get("benchmark_id", "brunner")))}</h1>
<div class="facts">
<div class="fact">Run<strong>{html.escape(str(metadata.get("test_id", "")))}</strong></div>
<div class="fact">Provider<strong>{html.escape(str(metadata.get("provider", "")))}</strong></div>
<div class="fact">Model<strong>{html.escape(str(metadata.get("model", "")))}</strong></div>
<div class="fact">Status<strong>{html.escape(str(status.get("status", "")))}</strong></div>
<div class="fact">Evaluation<strong>{html.escape(str(evaluation.get("status", "")))}</strong></div>
</div>
<h2>Benchmark reports</h2>
<section><ul>{''.join(links) or '<li>No benchmark-specific reports.</li>'}</ul></section>
<h2>Attempts</h2>
<table><thead><tr><th>#</th><th>Mode</th><th>Status</th><th>Exit</th><th>Failure</th></tr></thead>
<tbody>{attempt_rows}</tbody></table>
<h2>Timing</h2><pre>{_json_block(timing)}</pre>
<h2>Usage</h2><pre>{_json_block(usage)}</pre>
<h2>Evaluation summary</h2><pre>{_json_block(evaluation.get("summary", {}))}</pre>
</main></body></html>
"""
    )
    return output
