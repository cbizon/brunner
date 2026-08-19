# Diffusion equation benchmark

This example exercises Brunner end to end:

- trusted cluster-side challenge materialization;
- Codex and Claude candidate runs;
- strict submission and solver-output contracts;
- trusted numerical evaluation in Sterling;
- analytical profile, performance, steady-convergence, and grid-convergence
  measurements;
- model-based qualitative review of the implementation and diagnostics;
- campaign dashboard generation and resumable retrieval.

The candidate implements a standard-library Python CLI for the one-dimensional
heat equation. Three visible requests cover zero and unequal Dirichlet
boundaries plus insulated Neumann boundaries. The evaluator reruns the solver
at `nx = 21, 41, 81`, independently times it, compares it with the analytical
solution, estimates convergence order, and reports the first recorded
steady-state convergence step and physical time. Trusted held-out
parameterizations with different case IDs prevent a case-specific lookup from
passing as a reusable solver.

`output-contract.json` is the only candidate-output schema source. Brunner
renders it into the prompt and generates
`schema/artifacts/case-results.schema.json`; submission validation and the
trusted evaluator consume that same contract.

## Build images

Build from the repository root. Pin provider CLI versions explicitly:

```sh
docker buildx build --platform linux/amd64 \
  --build-arg CODEX_VERSION=VERSION \
  --build-arg CLAUDE_CODE_VERSION=VERSION \
  -f examples/diffusion_benchmark/images/agent.Dockerfile \
  -t ghcr.io/cbizon/brunner-diffusion-agent:VERSION --push .

docker buildx build --platform linux/amd64 \
  --build-arg CODEX_VERSION=VERSION \
  --build-arg KUBECTL_VERSION=CLUSTER_VERSION \
  -f examples/diffusion_benchmark/images/controller.Dockerfile \
  -t ghcr.io/cbizon/brunner-diffusion-controller:VERSION --push .
```

Resolve the pushed image digests and replace the zero digests in `images.py`.
Also replace the Squid placeholder with the approved digest-pinned Squid image.
Brunner injects the submitted image identities into cluster-side campaign
loads, avoiding a self-referential controller-image digest.
The agent image intentionally excludes `examples/`, so candidate processes do
not receive evaluator or analytical-solution code. The controller image
contains the benchmark, evaluator, qualitative-review runtime, and `kubectl`.

## Credentials

Create or reuse these Secrets in namespace `bizon`:

- `codex-provider-credentials`, key `AZURE_OPENAI_API_KEY`;
- `claude-provider-credentials`, key `CLAUDE_CODE_OAUTH_TOKEN`.
- `registry-credentials`, a Docker registry Secret for the private
  `ghcr.io/cbizon/brunner-diffusion-*` images.

The local ignored file `examples/diffusion_benchmark/secrets.env` may hold the
two provider values plus `GHCR_USERNAME` and `GHCR_TOKEN` while preparing the
namespace. Keep it mode `0600`; never add it to an image or commit it.

The example uses the RENCI Azure OpenAI endpoint for `gpt-5.6-luna`. Change the
non-secret provider connection settings in `definition.py` and `campaign.py`
if another OpenAI-compatible deployment is used.

## Validate and run

```sh
UV_CACHE_DIR=.uv-cache uv run brunner \
  --benchmark examples.diffusion_benchmark.definition contract-check

UV_CACHE_DIR=.uv-cache uv run brunner \
  --benchmark examples.diffusion_benchmark.definition \
  campaign-submit examples.diffusion_benchmark.campaign

UV_CACHE_DIR=.uv-cache uv run brunner \
  --benchmark examples.diffusion_benchmark.definition \
  campaign-monitor examples.diffusion_benchmark.campaign --local-port 8765
```

The dashboard at `http://127.0.0.1:8765/` links and embeds each trial's
diffusion report, links the qualitative review, and shows campaign lifecycle,
usage, and timing fields.

Retrieve the finalized, checksum-verified result bundle:

```sh
UV_CACHE_DIR=.uv-cache uv run brunner \
  --benchmark examples.diffusion_benchmark.definition \
  campaign-retrieve examples.diffusion_benchmark.campaign \
  ./diffusion-equation-results
```
