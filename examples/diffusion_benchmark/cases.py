from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


FOURIER_NUMBER = 0.4
GRID_RESOLUTIONS = (21, 41, 81)
GRID_CONVERGENCE_TIME = 0.08

CASE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "case_id": "zero-dirichlet-sine",
        "title": "Zero-temperature Dirichlet boundaries",
        "alpha": 0.5,
        "length": 1.0,
        "boundary": {
            "left": {"type": "dirichlet", "value": 0.0},
            "right": {"type": "dirichlet", "value": 0.0},
        },
        "transient": {
            "kind": "sine",
            "amplitude": 1.0,
            "mode": 1,
        },
        "steady": {"kind": "linear", "left": 0.0, "right": 0.0},
        "visible_nx": 41,
        "visible_final_time": 0.7,
        "convergence_tolerance": 0.05,
    },
    {
        "case_id": "offset-dirichlet-sine",
        "title": "Unequal fixed-temperature Dirichlet boundaries",
        "alpha": 0.35,
        "length": 1.0,
        "boundary": {
            "left": {"type": "dirichlet", "value": 1.0},
            "right": {"type": "dirichlet", "value": 0.2},
        },
        "transient": {
            "kind": "sine",
            "amplitude": 0.5,
            "mode": 1,
        },
        "steady": {"kind": "linear", "left": 1.0, "right": 0.2},
        "visible_nx": 41,
        "visible_final_time": 1.0,
        "convergence_tolerance": 0.03,
    },
    {
        "case_id": "insulated-neumann-cosine",
        "title": "Insulated zero-flux Neumann boundaries",
        "alpha": 0.25,
        "length": 1.0,
        "boundary": {
            "left": {"type": "neumann", "value": 0.0},
            "right": {"type": "neumann", "value": 0.0},
        },
        "transient": {
            "kind": "cosine",
            "amplitude": 0.5,
            "mode": 1,
        },
        "steady": {"kind": "constant", "value": 0.4},
        "visible_nx": 41,
        "visible_final_time": 1.3,
        "convergence_tolerance": 0.03,
    },
)


def case_by_id(case_id: str) -> dict[str, Any]:
    for spec in CASE_SPECS:
        if spec["case_id"] == case_id:
            return spec
    raise KeyError(case_id)


def grid(length: float, nx: int) -> list[float]:
    if nx < 3:
        raise ValueError("nx must be at least 3")
    return [length * index / (nx - 1) for index in range(nx)]


def steady_temperature(spec: dict[str, Any], x: float) -> float:
    steady = spec["steady"]
    if steady["kind"] == "linear":
        fraction = x / float(spec["length"])
        return float(steady["left"]) + fraction * (
            float(steady["right"]) - float(steady["left"])
        )
    if steady["kind"] == "constant":
        return float(steady["value"])
    raise ValueError(f"unknown steady profile {steady['kind']!r}")


def transient_temperature(
    spec: dict[str, Any],
    x: float,
    time_value: float,
) -> float:
    transient = spec["transient"]
    mode = int(transient["mode"])
    wave_number = mode * math.pi / float(spec["length"])
    decay = math.exp(
        -float(spec["alpha"]) * wave_number * wave_number * time_value
    )
    argument = wave_number * x
    if transient["kind"] == "sine":
        basis = math.sin(argument)
    elif transient["kind"] == "cosine":
        basis = math.cos(argument)
    else:
        raise ValueError(
            f"unknown transient profile {transient['kind']!r}"
        )
    return float(transient["amplitude"]) * basis * decay


def analytical_profile(
    spec: dict[str, Any],
    x_values: list[float],
    time_value: float,
) -> list[float]:
    return [
        steady_temperature(spec, x)
        + transient_temperature(spec, x, time_value)
        for x in x_values
    ]


def steady_profile(
    spec: dict[str, Any],
    x_values: list[float],
) -> list[float]:
    return [steady_temperature(spec, x) for x in x_values]


def expected_convergence_time(spec: dict[str, Any]) -> float:
    amplitude = abs(float(spec["transient"]["amplitude"]))
    tolerance = float(spec["convergence_tolerance"])
    mode = int(spec["transient"]["mode"])
    wave_number = mode * math.pi / float(spec["length"])
    decay_rate = float(spec["alpha"]) * wave_number * wave_number
    return math.log(amplitude / tolerance) / decay_rate


def build_request(
    spec: dict[str, Any],
    *,
    nx: int | None = None,
    t_final: float | None = None,
    record_every: int = 1,
) -> dict[str, Any]:
    resolved_nx = int(nx or spec["visible_nx"])
    length = float(spec["length"])
    alpha = float(spec["alpha"])
    x_values = grid(length, resolved_nx)
    dx = length / (resolved_nx - 1)
    return {
        "schema_version": "1.0",
        "case_id": str(spec["case_id"]),
        "alpha": alpha,
        "length": length,
        "nx": resolved_nx,
        "dt": FOURIER_NUMBER * dx * dx / alpha,
        "t_final": float(
            spec["visible_final_time"] if t_final is None else t_final
        ),
        "boundary": {
            side: {
                "type": str(value["type"]),
                "value": float(value["value"]),
            }
            for side, value in spec["boundary"].items()
        },
        "initial_temperature": analytical_profile(spec, x_values, 0.0),
        "record_every": record_every,
    }


def write_visible_cases(challenge_root: Path) -> tuple[Path, ...]:
    cases_root = challenge_root / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)
    written = []
    index = []
    for spec in CASE_SPECS:
        request = build_request(spec)
        path = cases_root / f"{spec['case_id']}.json"
        path.write_text(json.dumps(request, indent=2) + "\n")
        written.append(path)
        index.append(
            {
                "case_id": spec["case_id"],
                "title": spec["title"],
                "request": path.name,
                "convergence_tolerance": spec["convergence_tolerance"],
            }
        )
    index_path = cases_root / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "equation": "dT/dt = alpha * d2T/dx2",
                "cases": index,
            },
            indent=2,
        )
        + "\n"
    )
    return (*written, index_path)
