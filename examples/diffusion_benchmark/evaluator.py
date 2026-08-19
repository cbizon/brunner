from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

from brunner.evaluator import (
    EvaluationInput,
    load_evaluation_input,
    write_evaluation_result,
)

from examples.diffusion_benchmark.cases import (
    CASE_SPECS,
    GRID_CONVERGENCE_TIME,
    GRID_RESOLUTIONS,
    analytical_profile,
    build_request,
    case_by_id,
    expected_convergence_time,
    steady_profile,
)


REPORT_PATH = "evaluation/diffusion-report.html"
MAX_RMS_ERROR = 0.015
MAX_ABS_ERROR = 0.04
MIN_OBSERVED_ORDER = 1.0
MAX_REPRODUCIBILITY_DIFFERENCE = 1e-9
SOLVER_TIMEOUT_SECONDS = 30

HELD_OUT_SPECS: dict[str, dict[str, Any]] = {
    "zero-dirichlet-sine": {
        "case_id": "held-out-dirichlet-mode-two",
        "title": "Held-out unequal Dirichlet mode-two case",
        "alpha": 0.42,
        "length": 1.0,
        "boundary": {
            "left": {"type": "dirichlet", "value": 0.15},
            "right": {"type": "dirichlet", "value": -0.25},
        },
        "transient": {
            "kind": "sine",
            "amplitude": 0.35,
            "mode": 2,
        },
        "steady": {
            "kind": "linear",
            "left": 0.15,
            "right": -0.25,
        },
        "visible_nx": 37,
        "visible_final_time": 0.15,
        "convergence_tolerance": 0.04,
    },
    "offset-dirichlet-sine": {
        "case_id": "held-out-dirichlet-negative-mode-two",
        "title": "Held-out reversed Dirichlet mode-two case",
        "alpha": 0.28,
        "length": 1.0,
        "boundary": {
            "left": {"type": "dirichlet", "value": -0.1},
            "right": {"type": "dirichlet", "value": 0.8},
        },
        "transient": {
            "kind": "sine",
            "amplitude": -0.4,
            "mode": 2,
        },
        "steady": {
            "kind": "linear",
            "left": -0.1,
            "right": 0.8,
        },
        "visible_nx": 37,
        "visible_final_time": 0.2,
        "convergence_tolerance": 0.04,
    },
    "insulated-neumann-cosine": {
        "case_id": "held-out-neumann-mode-two",
        "title": "Held-out insulated mode-two case",
        "alpha": 0.3,
        "length": 1.0,
        "boundary": {
            "left": {"type": "neumann", "value": 0.0},
            "right": {"type": "neumann", "value": 0.0},
        },
        "transient": {
            "kind": "cosine",
            "amplitude": 0.3,
            "mode": 2,
        },
        "steady": {"kind": "constant", "value": 0.65},
        "visible_nx": 37,
        "visible_final_time": 0.2,
        "convergence_tolerance": 0.04,
    },
}


class CandidateSolverError(RuntimeError):
    pass


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CandidateSolverError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise CandidateSolverError(f"{label} must be a JSON object")
    return value


def _finite_numbers(value: object, *, label: str) -> list[float]:
    if not isinstance(value, list):
        raise CandidateSolverError(f"{label} must be an array")
    numbers = []
    for index, item in enumerate(value):
        if not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise CandidateSolverError(
                f"{label}[{index}] must be a finite number"
            )
        numbers.append(float(item))
    return numbers


def _validated_output(
    request: dict[str, Any],
    value: dict[str, Any],
) -> dict[str, Any]:
    case_id = str(request["case_id"])
    if value.get("schema_version") != "1.0":
        raise CandidateSolverError(f"{case_id}: unsupported output schema")
    if value.get("case_id") != case_id:
        raise CandidateSolverError(
            f"{case_id}: output case_id is {value.get('case_id')!r}"
        )
    if not isinstance(value.get("method"), str) or not value["method"].strip():
        raise CandidateSolverError(f"{case_id}: method must be non-empty")

    nx = int(request["nx"])
    length = float(request["length"])
    expected_x = [length * index / (nx - 1) for index in range(nx)]
    x_values = _finite_numbers(value.get("x"), label=f"{case_id}.x")
    if len(x_values) != nx:
        raise CandidateSolverError(f"{case_id}: x must contain nx values")
    if max(abs(actual - expected) for actual, expected in zip(x_values, expected_x)) > 1e-10:
        raise CandidateSolverError(f"{case_id}: x does not match the requested grid")

    profiles_value = value.get("profiles")
    if not isinstance(profiles_value, list) or len(profiles_value) < 2:
        raise CandidateSolverError(f"{case_id}: profiles must contain at least two records")
    profiles = []
    previous_step = -1
    previous_time = -1.0
    for index, profile in enumerate(profiles_value):
        if not isinstance(profile, dict):
            raise CandidateSolverError(f"{case_id}: profile {index} must be an object")
        step = profile.get("step")
        time_value = profile.get("time")
        if not isinstance(step, int) or step < 0:
            raise CandidateSolverError(f"{case_id}: profile {index} has invalid step")
        if not isinstance(time_value, (int, float)) or not math.isfinite(float(time_value)):
            raise CandidateSolverError(f"{case_id}: profile {index} has invalid time")
        resolved_time = float(time_value)
        if step <= previous_step or resolved_time <= previous_time:
            raise CandidateSolverError(
                f"{case_id}: profile steps and times must increase"
            )
        temperature = _finite_numbers(
            profile.get("temperature"),
            label=f"{case_id}.profiles[{index}].temperature",
        )
        if len(temperature) != nx:
            raise CandidateSolverError(
                f"{case_id}: profile {index} must contain nx temperatures"
            )
        profiles.append(
            {
                "step": step,
                "time": resolved_time,
                "temperature": temperature,
            }
        )
        previous_step = step
        previous_time = resolved_time

    if profiles[0]["step"] != 0 or abs(profiles[0]["time"]) > 1e-12:
        raise CandidateSolverError(f"{case_id}: first profile must be step 0 at time 0")
    initial = [float(item) for item in request["initial_temperature"]]
    if max(
        abs(actual - expected)
        for actual, expected in zip(profiles[0]["temperature"], initial)
    ) > 1e-10:
        raise CandidateSolverError(f"{case_id}: first profile differs from the request")

    final_time = float(request["t_final"])
    steps_completed = value.get("steps_completed")
    if not isinstance(steps_completed, int) or steps_completed < 1:
        raise CandidateSolverError(
            f"{case_id}: steps_completed must be a positive integer"
        )
    simulated_time = value.get("simulated_time")
    if not isinstance(simulated_time, (int, float)) or not math.isfinite(float(simulated_time)):
        raise CandidateSolverError(f"{case_id}: simulated_time must be finite")
    if abs(float(simulated_time) - final_time) > 1e-9:
        raise CandidateSolverError(f"{case_id}: simulated_time must equal t_final")
    if profiles[-1]["step"] != steps_completed:
        raise CandidateSolverError(
            f"{case_id}: final profile step differs from steps_completed"
        )
    if abs(profiles[-1]["time"] - final_time) > 1e-9:
        raise CandidateSolverError(f"{case_id}: final profile time is incomplete")

    record_every = int(request["record_every"])
    for profile in profiles[1:-1]:
        if profile["step"] % record_every:
            raise CandidateSolverError(
                f"{case_id}: profile step {profile['step']} violates record_every"
            )

    boundary = request["boundary"]
    for profile in profiles:
        temperature = profile["temperature"]
        if boundary["left"]["type"] == "dirichlet" and abs(
            temperature[0] - float(boundary["left"]["value"])
        ) > 1e-8:
            raise CandidateSolverError(f"{case_id}: left Dirichlet boundary drifted")
        if boundary["right"]["type"] == "dirichlet" and abs(
            temperature[-1] - float(boundary["right"]["value"])
        ) > 1e-8:
            raise CandidateSolverError(f"{case_id}: right Dirichlet boundary drifted")

    return {
        **value,
        "x": x_values,
        "profiles": profiles,
        "steps_completed": steps_completed,
        "simulated_time": float(simulated_time),
    }


def _solver_command(
    solver: Path,
    request_path: Path,
    output_path: Path,
) -> list[str]:
    bootstrap = (
        "import runpy,sys;"
        "root=sys.argv[1];script=sys.argv[2];"
        "sys.path.insert(0,root);"
        "sys.argv=[script,*sys.argv[3:]];"
        "runpy.run_path(script,run_name='__main__')"
    )
    return [
        sys.executable,
        "-I",
        "-S",
        "-c",
        bootstrap,
        str(solver.parent),
        str(solver),
        "--request",
        str(request_path),
        "--output",
        str(output_path),
    ]


def _run_solver(
    solver: Path,
    request: dict[str, Any],
    work_root: Path,
    run_id: str,
) -> tuple[dict[str, Any], float]:
    run_root = work_root / run_id
    run_root.mkdir(parents=True)
    request_path = run_root / "request.json"
    output_path = run_root / "output.json"
    request_path.write_text(json.dumps(request, indent=2) + "\n")
    environment = {
        "HOME": str(run_root / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": str(Path(sys.executable).parent),
        "PYTHONHASHSEED": "0",
    }
    Path(environment["HOME"]).mkdir()
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            _solver_command(solver, request_path, output_path),
            cwd=solver.parent,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SOLVER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CandidateSolverError(
            f"{request['case_id']}: solver exceeded {SOLVER_TIMEOUT_SECONDS} seconds"
        ) from error
    elapsed = time.perf_counter() - started
    (run_root / "stdout.log").write_text(completed.stdout)
    (run_root / "stderr.log").write_text(completed.stderr)
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip()
        if len(diagnostic) > 2000:
            diagnostic = diagnostic[-2000:]
        raise CandidateSolverError(
            f"{request['case_id']}: solver exited {completed.returncode}: {diagnostic}"
        )
    if not output_path.is_file():
        raise CandidateSolverError(
            f"{request['case_id']}: solver did not create its output"
        )
    return _validated_output(
        request,
        _load_object(output_path, label=f"{request['case_id']} rerun output"),
    ), elapsed


def _errors(
    observed: list[float],
    expected: list[float],
) -> tuple[float, float]:
    differences = [
        actual - wanted for actual, wanted in zip(observed, expected)
    ]
    rms = math.sqrt(
        sum(difference * difference for difference in differences)
        / len(differences)
    )
    return rms, max(abs(difference) for difference in differences)


def _final_errors(
    spec: dict[str, Any],
    output: dict[str, Any],
) -> tuple[float, float]:
    final = output["profiles"][-1]
    expected = analytical_profile(spec, output["x"], final["time"])
    return _errors(final["temperature"], expected)


def _convergence(
    spec: dict[str, Any],
    output: dict[str, Any],
) -> dict[str, Any]:
    steady = steady_profile(spec, output["x"])
    tolerance = float(spec["convergence_tolerance"])
    observed = None
    for profile in output["profiles"]:
        distance = max(
            abs(actual - wanted)
            for actual, wanted in zip(profile["temperature"], steady)
        )
        if distance <= tolerance:
            observed = {
                "step": profile["step"],
                "time": profile["time"],
                "distance": distance,
            }
            break
    expected_time = expected_convergence_time(spec)
    return {
        "tolerance": tolerance,
        "expected_time": expected_time,
        "observed": observed,
        "time_error": (
            abs(float(observed["time"]) - expected_time)
            if observed is not None
            else None
        ),
    }


def _observed_order(errors: list[dict[str, float]]) -> list[float]:
    orders = []
    for coarse, fine in zip(errors, errors[1:]):
        coarse_error = coarse["rms_error"]
        fine_error = fine["rms_error"]
        if coarse_error <= 1e-14 or fine_error <= 1e-14:
            continue
        ratio = coarse["dx"] / fine["dx"]
        orders.append(math.log(coarse_error / fine_error) / math.log(ratio))
    return orders


def _submitted_outputs(
    evaluation_input: EvaluationInput,
) -> dict[str, dict[str, Any]]:
    outputs = {}
    for artifact in evaluation_input.artifacts("case-results"):
        value = _load_object(artifact.path, label="submitted case output")
        case_id = value.get("case_id")
        if not isinstance(case_id, str):
            raise CandidateSolverError(
                f"submitted output {artifact.path.name} has no case_id"
            )
        if case_id in outputs:
            raise CandidateSolverError(
                f"duplicate submitted output for {case_id}"
            )
        spec = case_by_id(case_id)
        request = build_request(spec)
        outputs[case_id] = _validated_output(request, value)
    expected = {str(spec["case_id"]) for spec in CASE_SPECS}
    if set(outputs) != expected:
        raise CandidateSolverError(
            "submitted case IDs differ from the required cases: "
            f"{sorted(outputs)} != {sorted(expected)}"
        )
    return outputs


def _evaluate_case(
    solver: Path,
    spec: dict[str, Any],
    submitted: dict[str, Any],
    work_root: Path,
) -> dict[str, Any]:
    case_id = str(spec["case_id"])
    request = build_request(spec)
    rerun, wall_seconds = _run_solver(
        solver,
        request,
        work_root,
        f"{case_id}-base",
    )
    rms_error, max_error = _final_errors(spec, rerun)
    submitted_rms, submitted_max = _final_errors(spec, submitted)
    reproducibility_difference = max(
        abs(first - second)
        for first, second in zip(
            submitted["profiles"][-1]["temperature"],
            rerun["profiles"][-1]["temperature"],
        )
    )
    convergence = _convergence(spec, rerun)
    held_out_spec = HELD_OUT_SPECS[case_id]
    held_out_request = build_request(held_out_spec)
    held_out, held_out_wall_seconds = _run_solver(
        solver,
        held_out_request,
        work_root,
        f"{case_id}-held-out",
    )
    held_out_rms, held_out_max = _final_errors(
        held_out_spec,
        held_out,
    )

    grid_errors = []
    grid_wall_seconds = 0.0
    for nx in GRID_RESOLUTIONS:
        grid_request = build_request(
            spec,
            nx=nx,
            t_final=GRID_CONVERGENCE_TIME,
            record_every=10**9,
        )
        grid_output, elapsed = _run_solver(
            solver,
            grid_request,
            work_root,
            f"{case_id}-grid-{nx}",
        )
        grid_wall_seconds += elapsed
        grid_rms, grid_max = _final_errors(spec, grid_output)
        grid_errors.append(
            {
                "nx": nx,
                "dx": float(spec["length"]) / (nx - 1),
                "steps": grid_output["steps_completed"],
                "rms_error": grid_rms,
                "max_error": grid_max,
                "wall_seconds": elapsed,
            }
        )
    orders = _observed_order(grid_errors)
    minimum_order = min(orders) if orders else None
    cell_updates = int(rerun["steps_completed"]) * int(request["nx"])
    passed = (
        rms_error <= MAX_RMS_ERROR
        and max_error <= MAX_ABS_ERROR
        and submitted_rms <= MAX_RMS_ERROR
        and submitted_max <= MAX_ABS_ERROR
        and held_out_rms <= MAX_RMS_ERROR
        and held_out_max <= MAX_ABS_ERROR
        and reproducibility_difference <= MAX_REPRODUCIBILITY_DIFFERENCE
        and convergence["observed"] is not None
        and (
            minimum_order is None
            or minimum_order >= MIN_OBSERVED_ORDER
        )
    )
    return {
        "case_id": case_id,
        "title": spec["title"],
        "method": rerun["method"],
        "passed": passed,
        "request": request,
        "output": rerun,
        "submitted": {
            "rms_error": submitted_rms,
            "max_error": submitted_max,
        },
        "accuracy": {
            "rms_error": rms_error,
            "max_error": max_error,
        },
        "reproducibility_difference": reproducibility_difference,
        "held_out": {
            "case_id": held_out_spec["case_id"],
            "rms_error": held_out_rms,
            "max_error": held_out_max,
            "wall_seconds": held_out_wall_seconds,
        },
        "convergence": convergence,
        "grid_convergence": {
            "runs": grid_errors,
            "orders": orders,
            "minimum_order": minimum_order,
        },
        "performance": {
            "base_wall_seconds": wall_seconds,
            "held_out_wall_seconds": held_out_wall_seconds,
            "grid_wall_seconds": grid_wall_seconds,
            "steps_per_second": (
                rerun["steps_completed"] / wall_seconds
                if wall_seconds > 0
                else None
            ),
            "cell_updates_per_second": (
                cell_updates / wall_seconds if wall_seconds > 0 else None
            ),
        },
    }


def _curve_path(
    x_values: list[float],
    y_values: list[float],
    *,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> str:
    width = 600.0
    height = 220.0
    x_span = max(x_max - x_min, 1e-12)
    y_span = max(y_max - y_min, 1e-12)
    points = []
    for x_value, y_value in zip(x_values, y_values):
        x_pixel = 42.0 + (x_value - x_min) / x_span * width
        y_pixel = 18.0 + (y_max - y_value) / y_span * height
        points.append(f"{x_pixel:.2f},{y_pixel:.2f}")
    return " ".join(points)


def _profile_chart(case: dict[str, Any]) -> str:
    output = case["output"]
    spec = case_by_id(case["case_id"])
    profiles = output["profiles"]
    selected = [
        profiles[0],
        profiles[len(profiles) // 2],
        profiles[-1],
    ]
    curves = []
    for index, profile in enumerate(selected):
        curves.append(
            (
                f"numerical t={profile['time']:.3g}",
                profile["temperature"],
                ("#c7432b", "#d99520", "#176653")[index],
                False,
            )
        )
        curves.append(
            (
                f"analytical t={profile['time']:.3g}",
                analytical_profile(spec, output["x"], profile["time"]),
                ("#7f2315", "#7b530b", "#0b3d31")[index],
                True,
            )
        )
    all_values = [value for _, values, _, _ in curves for value in values]
    y_min = min(all_values)
    y_max = max(all_values)
    padding = max((y_max - y_min) * 0.08, 0.05)
    y_min -= padding
    y_max += padding
    polylines = []
    legend = []
    for index, (label, values, color, dashed) in enumerate(curves):
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        polylines.append(
            '<polyline fill="none" '
            f'stroke="{color}" stroke-width="2"{dash} points="'
            + _curve_path(
                output["x"],
                values,
                x_min=min(output["x"]),
                x_max=max(output["x"]),
                y_min=y_min,
                y_max=y_max,
            )
            + '"/>'
        )
        legend.append(
            f'<span style="--swatch:{color}">'
            f"{html.escape(label)}{' dashed' if dashed else ''}</span>"
        )
    return (
        '<div class="chart">'
        '<svg viewBox="0 0 690 265" role="img" '
        f'aria-label="{html.escape(case["title"])} temperature profiles">'
        '<rect x="42" y="18" width="600" height="220" class="plot-bg"/>'
        '<line x1="42" y1="238" x2="642" y2="238" class="axis"/>'
        '<line x1="42" y1="18" x2="42" y2="238" class="axis"/>'
        + "".join(polylines)
        + "</svg><div class=\"legend\">"
        + "".join(legend)
        + "</div></div>"
    )


def _format_number(value: object) -> str:
    if value is None:
        return "not available"
    if isinstance(value, float):
        if value == 0:
            return "0"
        if abs(value) < 0.001 or abs(value) >= 10000:
            return f"{value:.3e}"
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _render_report(
    cases: list[dict[str, Any]],
    failures: list[dict[str, str]],
    output_path: Path,
) -> None:
    passed = len(cases) == len(CASE_SPECS) and all(
        case["passed"] for case in cases
    )
    sections = []
    for case in cases:
        convergence = case["convergence"]
        observed = convergence["observed"] or {}
        sections.append(
            "<section>"
            f"<h2>{html.escape(case['title'])}</h2>"
            f"<p class=\"status {'pass' if case['passed'] else 'fail'}\">"
            f"{'PASS' if case['passed'] else 'FAIL'}"
            f" · {html.escape(case['method'])}</p>"
            + _profile_chart(case)
            + "<div class=\"facts\">"
            f"<div><span>RMS error</span><strong>{_format_number(case['accuracy']['rms_error'])}</strong></div>"
            f"<div><span>Max error</span><strong>{_format_number(case['accuracy']['max_error'])}</strong></div>"
            f"<div><span>Held-out RMS error</span><strong>{_format_number(case['held_out']['rms_error'])}</strong></div>"
            f"<div><span>Held-out max error</span><strong>{_format_number(case['held_out']['max_error'])}</strong></div>"
            f"<div><span>Observed order</span><strong>{_format_number(case['grid_convergence']['minimum_order'])}</strong></div>"
            f"<div><span>Base runtime</span><strong>{_format_number(case['performance']['base_wall_seconds'])} s</strong></div>"
            f"<div><span>Steps / second</span><strong>{_format_number(case['performance']['steps_per_second'])}</strong></div>"
            f"<div><span>Cell updates / second</span><strong>{_format_number(case['performance']['cell_updates_per_second'])}</strong></div>"
            f"<div><span>Convergence step</span><strong>{_format_number(observed.get('step'))}</strong></div>"
            f"<div><span>Convergence time</span><strong>{_format_number(observed.get('time'))}</strong></div>"
            f"<div><span>Analytical convergence</span><strong>{_format_number(convergence['expected_time'])}</strong></div>"
            f"<div><span>Submitted/rerun difference</span><strong>{_format_number(case['reproducibility_difference'])}</strong></div>"
            "</div></section>"
        )
    failure_html = "".join(
        "<li><strong>"
        + html.escape(failure["case_id"])
        + ":</strong> "
        + html.escape(failure["message"])
        + "</li>"
        for failure in failures
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diffusion solver evaluation</title>
<style>
:root { --ink:#1c2622; --paper:#eef1e7; --panel:#fffdf7; --line:#b8c0b5;
  --hot:#c7432b; --cool:#176653; --gold:#d99520; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:
  radial-gradient(circle at 85% 0,#c9ddd2 0,transparent 28rem),
  linear-gradient(125deg,transparent 72%,#e3d8ba 72% 73%,transparent 73%),
  var(--paper); font:16px/1.45 Georgia,serif; }
main { max-width:1180px; margin:auto; padding:42px 22px 80px; }
h1 { max-width:850px; margin:0; font-size:clamp(3rem,8vw,6.8rem);
  line-height:.86; letter-spacing:-.055em; }
.lede { max-width:780px; font-size:1.15rem; }
section { margin-top:28px; padding:22px; border:1px solid var(--line);
  background:rgba(255,253,247,.94); }
.status { display:inline-block; padding:5px 9px; font:bold 12px monospace;
  letter-spacing:.08em; }
.pass { color:white; background:var(--cool); }
.fail { color:white; background:var(--hot); }
.facts { display:grid; grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
  gap:10px; margin-top:18px; }
.facts div { padding:12px; border-top:3px solid var(--gold); background:#f5f1e5; }
.facts span { display:block; color:#59625c; font:11px/1.2 monospace;
  text-transform:uppercase; letter-spacing:.07em; }
.facts strong { display:block; margin-top:7px; overflow-wrap:anywhere; }
.chart { overflow-x:auto; }
svg { min-width:650px; width:100%; height:auto; }
.plot-bg { fill:#f8f5ec; stroke:var(--line); }
.axis { stroke:#59625c; stroke-width:1; }
.legend { display:flex; flex-wrap:wrap; gap:8px 16px; font:12px monospace; }
.legend span::before { content:""; display:inline-block; width:18px;
  border-top:3px solid var(--swatch); margin-right:6px; vertical-align:middle; }
@media (max-width:650px) { main { padding:28px 12px 55px; } section { padding:14px; } }
</style></head><body><main>
<h1>Diffusion solver evaluation</h1>
<p class="lede">Numerical profiles are compared with analytical solutions for
three boundary-condition families. Runtime, steady-state convergence, and
grid-convergence behavior are measured by rerunning the submitted solver.</p>
"""
        + (
            '<p class="status pass">OVERALL PASS</p>'
            if passed
            else '<p class="status fail">OVERALL FAIL</p>'
        )
        + (
            "<section><h2>Execution failures</h2><ul>"
            + failure_html
            + "</ul></section>"
            if failures
            else ""
        )
        + "".join(sections)
        + "</main></body></html>\n"
    )


def _aggregate(
    cases: list[dict[str, Any]],
    failures: list[dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    passed_cases = sum(bool(case["passed"]) for case in cases)
    complete = len(cases) == len(CASE_SPECS) and not failures
    passed = complete and passed_cases == len(CASE_SPECS)
    rms_errors = [case["accuracy"]["rms_error"] for case in cases]
    max_errors = [case["accuracy"]["max_error"] for case in cases]
    held_out_rms_errors = [
        case["held_out"]["rms_error"] for case in cases
    ]
    held_out_max_errors = [
        case["held_out"]["max_error"] for case in cases
    ]
    orders = [
        case["grid_convergence"]["minimum_order"]
        for case in cases
        if case["grid_convergence"]["minimum_order"] is not None
    ]
    wall_seconds = sum(
        case["performance"]["base_wall_seconds"]
        + case["performance"]["held_out_wall_seconds"]
        + case["performance"]["grid_wall_seconds"]
        for case in cases
    )
    summary = {
        "passed": passed,
        "cases_passed": passed_cases,
        "cases_total": len(CASE_SPECS),
        "max_rms_error": max(rms_errors) if rms_errors else None,
        "max_abs_error": max(max_errors) if max_errors else None,
        "max_held_out_rms_error": (
            max(held_out_rms_errors) if held_out_rms_errors else None
        ),
        "max_held_out_abs_error": (
            max(held_out_max_errors) if held_out_max_errors else None
        ),
        "minimum_observed_order": min(orders) if orders else None,
        "solver_wall_seconds": wall_seconds,
        "execution_failures": len(failures),
    }
    metrics = {
        "thresholds": {
            "max_rms_error": MAX_RMS_ERROR,
            "max_abs_error": MAX_ABS_ERROR,
            "minimum_observed_order": MIN_OBSERVED_ORDER,
            "max_reproducibility_difference": (
                MAX_REPRODUCIBILITY_DIFFERENCE
            ),
        },
        "cases": [
            {
                key: value
                for key, value in case.items()
                if key not in {"output", "request"}
            }
            for case in cases
        ],
        "failures": failures,
    }
    return summary, metrics, passed


def main() -> int:
    evaluation_input = load_evaluation_input()
    solver = evaluation_input.artifact("solver-source").path
    cases = []
    failures = []
    try:
        submitted = _submitted_outputs(evaluation_input)
    except CandidateSolverError as error:
        failures.append(
            {"case_id": "submitted-outputs", "message": str(error)}
        )
    else:
        with tempfile.TemporaryDirectory(
            prefix="diffusion-evaluator-"
        ) as temporary:
            work_root = Path(temporary)
            for spec in CASE_SPECS:
                case_id = str(spec["case_id"])
                try:
                    cases.append(
                        _evaluate_case(
                            solver,
                            spec,
                            submitted[case_id],
                            work_root,
                        )
                    )
                except CandidateSolverError as error:
                    failures.append(
                        {"case_id": case_id, "message": str(error)}
                    )
    report_path = evaluation_input.trial_root / REPORT_PATH
    _render_report(cases, failures, report_path)
    summary, metrics, passed = _aggregate(cases, failures)
    write_evaluation_result(
        evaluation_input,
        status="complete" if passed else "failed",
        summary=summary,
        metrics=metrics,
        reports=[
            {
                "path": REPORT_PATH,
                "media_type": "text/html",
                "title": "Diffusion solver profiles and convergence",
                "primary": True,
            }
        ],
        error=(
            {
                "type": "CandidateSolverError",
                "message": "; ".join(
                    f"{item['case_id']}: {item['message']}"
                    for item in failures
                )
                or "one or more numerical acceptance criteria failed",
            }
            if not passed
            else None
        ),
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
