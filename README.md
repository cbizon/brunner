# brunner

`brunner` is a contract-driven runner for isolated agent benchmarks. It owns
the reusable lifecycle:

```text
isolated agent execution -> submission -> trusted evaluation
                         -> verified collection -> campaign reporting
```

Each benchmark imports Brunner and supplies only its challenge, canonical
output contract, evaluator, optional reference bundle, artifact policy, and
runtime defaults. `granular_benchmark` remains an independent historical
benchmark; this repository does not modify or depend on it.

## What Is Generic

- Deterministic challenge staging and isolation checks
- Prompt/schema generation from one output contract
- Codex and Claude provider adapters
- Durable retries, session resume, finalization, timeout, and usage capture
- Local, OCI-container, and Kubernetes execution backends
- Resumable checksum-verified artifact collection
- Trusted host or evaluator-container execution
- Reference bundle manifests and integrity checks
- Campaign state, capacity control, recovery, and static dashboards

## Benchmark Slots

- `BenchmarkDefinition`: identity and component wiring
- `challenge/`: prompt template and agent-visible inputs
- `output-contract.json`: submission, work units, artifacts, and JSON schemas
- Evaluator command and optional evaluator image
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

The repository includes:

- `examples/text_benchmark`: minimal non-reference text benchmark
- `examples/numeric_benchmark`: reference-backed benchmark with a
  contract-defined artifact JSON Schema

See [architecture.md](docs/architecture.md) and
[integration.md](docs/integration.md) for the full interfaces and execution
model.
