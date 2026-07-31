# brunner

`brunner` is a contract-driven runner for isolated agent benchmarks. It owns
the reusable lifecycle:

```text
isolated agent execution -> submission -> verified collection
                         -> trusted evaluation -> optional assessments
                         -> campaign reporting
```

Each benchmark imports Brunner and supplies only its challenge, canonical
output contract, evaluator, optional reference bundle, artifact policy, and
runtime defaults. `granular_benchmark` remains an independent historical
benchmark; this repository does not modify or depend on it.

## What Is Generic

- Deterministic challenge staging and isolation checks
- Prompt/schema generation from one output contract
- Codex and Claude provider adapters
- Durable retries, session resume, finalization, and timeout handling
- Cross-provider token normalization and interval-based time accounting
- Local, OCI-container, and Kubernetes execution backends
- Resumable checksum-verified artifact collection
- Trusted host or evaluator-container execution
- Schema-bound command or model-based post-evaluation assessments
- Evidence dossiers, timing facts, assessment provenance, and report links
- Reference bundle manifests and integrity checks
- Append-only campaign task lists with caller-owned trial IDs
- Campaign capacity control, recovery, and static dashboards

## Benchmark Slots

- `BenchmarkDefinition`: identity and component wiring
- `challenge/`: prompt template and agent-visible inputs
- `output-contract.json`: submission, work units, artifacts, and JSON schemas
- Evaluator command and optional evaluator image
- Optional assessment contracts, commands or reviewer models, and reports
- Optional reference root and validation command
- Artifact exclusion/group policy
- Runtime defaults and campaign/backend profile

The output contract is authoritative for the output-facing boundary. Brunner
uses it to render the prompt, stage schemas, validate manifests and artifacts,
validate final status, and construct the evaluator's `EvaluationInput`. Domain
accuracy and scoring remain handwritten evaluator logic.

## Quick Start

```sh
UV_CACHE_DIR=.uv-cache uv sync --all-groups
UV_CACHE_DIR=.uv-cache uv run pytest

UV_CACHE_DIR=.uv-cache uv run brunner \
  --benchmark examples.text_benchmark.definition \
  contract-check
```

Run a benchmark locally with an installed provider CLI:

```sh
UV_CACHE_DIR=.uv-cache uv run brunner \
  --benchmark examples.text_benchmark.definition \
  local-run ./runs --provider codex --model MODEL
```

## Resource Accounting

Every completed trial writes:

- `usage/usage.json` with provider-neutral logical input, uncached input,
  cache-read input, cache-write input when exposed, output, reasoning output
  when exposed, and total tokens.
- `timing/events.jsonl` with local receipt timestamps for attempts, provider
  events, foreground tools, and retry waits.
- `timing/accounting.json` with an exclusive wall-time partition for agent
  activity, foreground tools, external waits, subscription waits, ordinary
  retry waits, runner overhead, and unclassified time.

`agent_active_seconds` is the portion of an attempt not assigned to an
observed tool or explicit wait. It includes model/API processing,
orchestration, and provider latency because those cannot be separated
generically.

Benchmarks can identify simulation runtime and actual idle waits explicitly:

```sh
brunner-activity run external_wait case-a -- python simulate.py

brunner-activity start background_job case-b
python simulate.py &
simulation_pid=$!
wait "$simulation_pid"
brunner-activity end background_job case-b
```

The runner supplies `BRUNNER_ACTIVITY_LOG` automatically. Python benchmark
code can use the same interface:

```python
from brunner import activity

with activity("external_wait", "case-a", label="particle simulation"):
    run_simulation()
```

`background_job_seconds` may overlap agent work and is reported separately
from the exclusive wall-time partition. Use `external_wait` only for time when
the agent is blocked waiting for an external process.

Declared tool, wait, and background intervals are allowed to drain across the
soft finalization boundary, up to the hard trial deadline. Undeclared orphan
process groups are terminated before artifact collection begins.

The repository includes:

- `examples/text_benchmark`: minimal non-reference text benchmark
- `examples/numeric_benchmark`: reference-backed benchmark with a
  contract-defined artifact JSON Schema

See [architecture.md](docs/architecture.md) and
[integration.md](docs/integration.md) for the full interfaces and execution
model.
