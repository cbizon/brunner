# Benchmark Integration

## Repository Shape

A benchmark package can be as small as:

```text
my_benchmark/
  definition.py
  output-contract.json
  evaluator.py
  challenge/
    prompt.md
    inputs.json
  reference/                 # optional
    manifest.json
    answers.json
```

The prompt template must contain `{{BRUNNER_OUTPUT_CONTRACT}}` exactly once.
Brunner replaces that marker during isolated staging.

## Definition

```python
from pathlib import Path
import sys

from brunner import (
    ArtifactPolicy,
    BenchmarkDefinition,
    ChallengeDefinition,
    EvaluationDefinition,
    ReferenceDefinition,
    RuntimeDefaults,
)

ROOT = Path(__file__).resolve().parent


def build_definition() -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="my-benchmark",
        version="1.0.0",
        root=ROOT,
        contract_path=ROOT / "output-contract.json",
        challenge=ChallengeDefinition(
            root=ROOT / "challenge",
            forbidden_names=("reference", "evaluator.py"),
        ),
        evaluation=EvaluationDefinition(
            command=(sys.executable, str(ROOT / "evaluator.py")),
            # image="my-evaluator:1.0",  # optional trusted container
        ),
        reference=ReferenceDefinition(
            root=ROOT / "reference",
        ),
        artifacts=ArtifactPolicy(
            groups={"debug": ("debug/**",)},
        ),
        runtime=RuntimeDefaults(
            timeout_seconds=6 * 60 * 60,
            finalization_seconds=15 * 60,
        ),
    )
```

`forbidden_names` is an additional isolation assertion, not an exclusion
mechanism. Do not put evaluator/reference files under the challenge root.

## Output Contract

Use the submission schema for manifest structure and artifact entries for
files the evaluator needs:

```json
{
  "schema_version": "1.0",
  "benchmark_id": "my-benchmark",
  "title": "Example output",
  "submission": {
    "manifest_path": "submission/manifest.json",
    "schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["schema_version", "result"],
      "properties": {
        "schema_version": {"const": "1.0"},
        "result": {"type": "string"}
      }
    }
  },
  "run_status_path": "submission/run-status.json",
  "work_units": [
    {"id": "solve", "description": "Produce the complete result."}
  ],
  "artifacts": [
    {
      "id": "result",
      "description": "Structured benchmark result.",
      "manifest_pointer": "/result",
      "media_type": "application/json",
      "json_schema": {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "number"}}
      }
    }
  ]
}
```

Use artifact `details` and contract `instructions` for constraints that must
be visible in the prompt but cannot be represented by JSON Schema. Keep
accuracy, tolerances, scientific comparison, and other domain scoring in the
evaluator.

## Evaluator

```python
import json

from brunner.evaluator import (
    load_evaluation_input,
    write_evaluation_result,
)


def main() -> int:
    evaluation_input = load_evaluation_input()
    observed = json.loads(
        evaluation_input.artifact("result").path.read_text()
    )
    passed = observed["value"] == 42
    write_evaluation_result(
        evaluation_input,
        status="complete" if passed else "failed",
        summary={"passed": passed},
        metrics={"score": 1.0 if passed else 0.0},
    )
    return 0 if passed else 1
```

The evaluator must write the path in `BRUNNER_EVALUATION_RESULTS`. Brunner
validates the result envelope and generates a general run report. Evaluators
may list additional report files in the result.

## References

Create or refresh the reference manifest after the contract is valid:

```sh
brunner --benchmark my_benchmark.definition reference-build
brunner --benchmark my_benchmark.definition reference-validate
```

The manifest records every reference file's size and SHA-256 plus benchmark
and contract identity. It excludes itself from the inventory. Evaluation
fails before scoring if reference content or contract identity has drifted.

## Campaigns

A campaign module supplies the runtime profile but reuses the loaded
definition and contract:

```python
from pathlib import Path

from brunner import CampaignPlan, CampaignRunner, CampaignTrial
from brunner.backends import LocalBackend


def build_campaign(definition, contract):
    plan = CampaignPlan(
        campaign_id="comparison-01",
        root=Path("campaigns/comparison-01"),
        trials=(
            CampaignTrial("codex", "MODEL_A", effort="high"),
            CampaignTrial("claude", "MODEL_B", effort="high"),
        ),
        max_parallel=2,
        included_artifact_groups=frozenset({"debug"}),
    )
    return CampaignRunner(
        definition,
        contract,
        plan,
        LocalBackend(max_parallel=2),
    )
```

```sh
brunner --benchmark my_benchmark.definition \
  campaign-init my_benchmark.campaign
brunner --benchmark my_benchmark.definition \
  campaign-step my_benchmark.campaign
brunner --benchmark my_benchmark.definition \
  campaign-run my_benchmark.campaign --poll-seconds 10
```

For OCI execution, use `ContainerBackend` and set `backend_image` on the plan.
For Kubernetes, construct `KubernetesBackend(KubernetesProfile(...))`. Agent
and artifact-reader images must contain Brunner; the agent image also needs
the selected provider CLI. The benchmark package, evaluator, and references
are not required in the agent image.

Provider secrets should be inherited from the local environment or referenced
through `KubernetesProfile.secret_environment`. Do not place secret values in
campaign objects.

## CLI

```text
contract-check       Validate contract and print digest
contract-render      Render the generated output-requirements prompt section
stage                Stage an isolated challenge
trial-create         Create a durable trial
trial-run            Run a trial with a loaded benchmark definition
trial-evaluate       Run trusted evaluation
local-run            Create, run, and evaluate locally
reference-build      Build a reference manifest
reference-validate   Verify a reference bundle
campaign-init        Create campaign state and trials
campaign-step        Reconcile one campaign iteration
campaign-run         Reconcile until complete, paused, or attention required
```

Remote backends invoke `brunner-agent`, which reads only staged trial metadata
and does not import benchmark code.
