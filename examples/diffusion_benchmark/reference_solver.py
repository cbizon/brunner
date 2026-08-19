from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _load_request(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("schema_version") != "1.0":
        raise ValueError("unsupported request schema")
    return value


def _advance(
    temperature: list[float],
    *,
    ratio: float,
    dx: float,
    boundary: dict[str, dict[str, Any]],
) -> list[float]:
    updated = temperature.copy()
    for index in range(1, len(temperature) - 1):
        updated[index] = temperature[index] + ratio * (
            temperature[index - 1]
            - 2.0 * temperature[index]
            + temperature[index + 1]
        )

    left = boundary["left"]
    if left["type"] == "dirichlet":
        updated[0] = float(left["value"])
    elif left["type"] == "neumann":
        gradient = float(left["value"])
        updated[0] = temperature[0] + 2.0 * ratio * (
            temperature[1] - temperature[0] - dx * gradient
        )
    else:
        raise ValueError(f"unsupported left boundary {left['type']!r}")

    right = boundary["right"]
    if right["type"] == "dirichlet":
        updated[-1] = float(right["value"])
    elif right["type"] == "neumann":
        gradient = float(right["value"])
        updated[-1] = temperature[-1] + 2.0 * ratio * (
            temperature[-2] - temperature[-1] + dx * gradient
        )
    else:
        raise ValueError(f"unsupported right boundary {right['type']!r}")
    return updated


def solve(request: dict[str, Any]) -> dict[str, Any]:
    nx = int(request["nx"])
    if nx < 3:
        raise ValueError("nx must be at least 3")
    length = float(request["length"])
    alpha = float(request["alpha"])
    requested_dt = float(request["dt"])
    final_time = float(request["t_final"])
    record_every = int(request["record_every"])
    if min(length, alpha, requested_dt, final_time, record_every) <= 0:
        raise ValueError("length, alpha, dt, t_final, and record_every must be positive")
    temperature = [float(value) for value in request["initial_temperature"]]
    if len(temperature) != nx or not all(math.isfinite(value) for value in temperature):
        raise ValueError("initial_temperature must contain nx finite values")
    dx = length / (nx - 1)
    if alpha * requested_dt / (dx * dx) > 0.5 + 1e-12:
        raise ValueError("explicit FTCS stability limit exceeded")

    x_values = [length * index / (nx - 1) for index in range(nx)]
    profiles = [
        {
            "step": 0,
            "time": 0.0,
            "temperature": temperature.copy(),
        }
    ]
    current_time = 0.0
    step = 0
    while current_time < final_time - 1e-15:
        dt = min(requested_dt, final_time - current_time)
        ratio = alpha * dt / (dx * dx)
        temperature = _advance(
            temperature,
            ratio=ratio,
            dx=dx,
            boundary=request["boundary"],
        )
        step += 1
        current_time += dt
        if step % record_every == 0 or current_time >= final_time - 1e-15:
            profiles.append(
                {
                    "step": step,
                    "time": current_time,
                    "temperature": temperature.copy(),
                }
            )

    return {
        "schema_version": "1.0",
        "case_id": request["case_id"],
        "method": "explicit-ftcs",
        "x": x_values,
        "dt_requested": requested_dt,
        "steps_completed": step,
        "simulated_time": current_time,
        "termination": "final_time",
        "profiles": profiles,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = solve(_load_request(args.request))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
