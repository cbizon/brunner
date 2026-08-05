# brunner

`brunner` is a contract-driven runner for isolated agent benchmarks. It owns
the reusable lifecycle:

```text
isolated agent execution -> submission -> verified collection
                         -> trusted evaluation -> qualitative review
                         -> optional domain assessments
                         -> campaign reporting
```

Each benchmark imports Brunner and supplies only its challenge, canonical
output contract, evaluator, optional reference bundle, artifact policy, and
runtime defaults. `granular_benchmark` remains an independent historical
benchmark; this repository does not modify or depend on it.

## What Is Generic

- Deterministic challenge staging and isolation checks
- Optional orchestrator-side challenge resource materialization
- Prompt/schema generation from one output contract
- Codex and Claude provider adapters
- Durable retries, session resume, finalization, and timeout handling
- Cross-provider token normalization and interval-based time accounting
- Local, OCI-container, and Kubernetes execution backends
- Resumable checksum-verified artifact collection
- Trusted host or evaluator-container execution
- Packaged evidence-bound qualitative review contract and HTML report
- Schema-bound command or model-based post-evaluation assessments
- Evidence dossiers, timing facts, assessment provenance, and report links
- Reference bundle manifests and integrity checks
- Append-only campaign task lists with caller-owned trial IDs
- Campaign capacity control, recovery, and static dashboards
- Independent Kubernetes CPU, memory, and ephemeral-storage requests and limits

## Benchmark Slots

- `BenchmarkDefinition`: identity and component wiring
- `challenge/`: prompt template and agent-visible inputs
- Optional challenge materialization command and timeout
- `output-contract.json`: submission, work units, artifacts, and JSON schemas
- Evaluator command and optional evaluator image
- Standard qualitative-review model configuration
- Optional domain assessment contracts, commands or reviewer models, and reports
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

Candidate agents run only through campaign backends that provide an outer
container isolation boundary. Brunner supports OCI containers and Kubernetes;
it does not provide host-process or local campaign execution.

Materialize a harmless candidate-visible example resource before staging:

```sh
UV_CACHE_DIR=.uv-cache uv run brunner \
  --benchmark examples.text_benchmark.definition:build_materialized_definition \
  stage ./materialized-workspace
```

Materializers run on a temporary challenge copy on the orchestrator before
hashing or backend submission. Their output is candidate-visible and included
in `challenge_sha256`; evaluator/reference materials are not staged or passed
through materializer-specific environment variables.

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
process groups are terminated before artifact collection begins. Successful
provider events do not start exit grace until current structured output is
valid, preventing premature termination while final artifacts are still being
written without allowing a nonfinal event to consume the reserved finalization
window.

A declared interval only defers the deadline while it is credibly still open.
Brunner releases an interval whose start belongs to an earlier attempt, whose
holding process has exited, or that has outlived
`RuntimeDefaults.max_activity_interval_seconds`. Without those rules a single
unmatched `start` would suppress finalization for the rest of the trial. The
`activity` context manager and `brunner-activity run` record the holding
process so it can be checked; a bare `brunner-activity start` cannot be
checked that way and is bounded only by the maximum interval.

When a provider exposes the model that produced a primary response, Brunner
verifies it against the requested model. A provider-side substitution, such
as a safety downgrade from Fable to Opus, terminates the attempt as
`provider_error` and is never scored as a result from the requested model.

The repository includes:

- `examples/text_benchmark`: minimal non-reference text benchmark
- `examples/numeric_benchmark`: reference-backed benchmark with a
  contract-defined artifact JSON Schema

See [architecture.md](docs/architecture.md) and
[integration.md](docs/integration.md) for the full interfaces and execution
model.
