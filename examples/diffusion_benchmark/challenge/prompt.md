# One-dimensional diffusion solver benchmark

Design and implement a reusable numerical solver for the one-dimensional heat
equation

```text
dT/dt = alpha * d2T/dx2
```

on `0 <= x <= length`.

## Required solver

Write the solver entry point to `submission/solver.py`. It must use only the
Python standard library and accept this command line:

```text
python submission/solver.py --request REQUEST.json --output OUTPUT.json
```

Each request supplies:

- `alpha`, `length`, `nx`, `dt`, and `t_final`;
- an `initial_temperature` array with exactly `nx` values;
- independent left and right boundary specifications;
- `record_every`, which requests a profile every N completed steps; and
- a caller-provided `case_id` that must be copied to the output.

Support both boundary types:

- `{"type": "dirichlet", "value": T}` fixes the endpoint temperature.
- `{"type": "neumann", "value": G}` fixes `dT/dx = G` at the endpoint.

The generated requests use stable timesteps for a correct explicit method, but
you may implement another defensible finite-difference method. Do not
special-case the supplied case IDs, hard-code analytical profiles, or return
prescribed outputs instead of running the submitted solver.

The output must conform to
`schema/artifacts/case-results.schema.json`, which Brunner generates from the
canonical output contract. In particular:

- include the initial profile at step 0 and time 0;
- include each requested recorded profile and the final profile;
- preserve monotonically increasing step and time values;
- advance exactly to `t_final`; if `dt` does not evenly divide the requested
  interval, use a shorter final timestep rather than stopping early or
  overshooting;
- report `simulated_time` and the final profile time equal to `t_final` within
  ordinary floating-point precision while keeping `dt_requested` equal to the
  request's original `dt`;
- report the actual completed step count and simulated time; and
- produce only finite numeric values.

## Cases

`cases/index.json` lists three materialized requests:

1. A sine perturbation with zero-temperature Dirichlet boundaries.
2. A sine perturbation around a linear steady profile with unequal Dirichlet
   boundary temperatures.
3. A cosine perturbation with insulated zero-flux Neumann boundaries.

Run your solver for every request and write the outputs beneath `submission/`.
Paths inside `submission/manifest.json` are relative to the manifest's
`submission/` directory, so use values such as `solver.py` and
`zero-dirichlet-sine.json`, not paths beginning with `submission/`.
The evaluator will rerun the submitted implementation at additional grid
resolutions. It will compare numerical and analytical temperature profiles,
measure error and observed convergence order, independently time execution,
find the first recorded step and simulated time that reach the supplied
steady-state tolerance, and exercise held-out parameters and case IDs that are
not present in the candidate workspace.

Include focused tests in `submission/test_solver.py` if practical. Tests should
cover boundary handling, output shape, and at least one simple invariant or
known solution.

Work only in this directory and do not use network tools.

{{BRUNNER_OUTPUT_CONTRACT}}
