# Benchmark Integration

## Repository Shape

A benchmark package can be as small as:

```text
my_benchmark/
  definition.py
  output-contract.json
  evaluator.py
  assessment/                # optional trusted material
    prompt.md
    rubric.md
    review.schema.json
    render.py
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
    QualitativeReviewDefinition,
    ReferenceDefinition,
    RuntimeDefaults,
    ProviderSettings,
)

ROOT = Path(__file__).resolve().parent


def build_definition() -> BenchmarkDefinition:
    return BenchmarkDefinition(
        benchmark_id="my-benchmark",
        version="1.0.0",
        display_title="My benchmark",  # optional run-report heading
        root=ROOT,
        contract_path=ROOT / "output-contract.json",
        challenge=ChallengeDefinition(
            root=ROOT / "challenge",
            forbidden_names=("reference", "evaluator.py"),
            materialize_command=(
                sys.executable,
                "-m",
                "my_benchmark.materialize_challenge",
            ),
            materialize_timeout_seconds=60 * 60,
        ),
        evaluation=EvaluationDefinition(
            command=(sys.executable, str(ROOT / "evaluator.py")),
            # image="my-evaluator:1.0",  # optional trusted container
        ),
        qualitative_review=QualitativeReviewDefinition(
            reviewer=ProviderSettings(
                provider="codex",
                model="REVIEWER_MODEL",
                effort="high",
            ),
            required=False,
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
            backend_shutdown_grace_seconds=2 * 60,
            max_attempts=50,
            max_activity_interval_seconds=6 * 60 * 60,
        ),
    )
```

`benchmark_id` is the stable machine identity. Set `display_title` only when the
generic run report should show a human-facing heading; omitting it leaves the
report untitled while retaining the run ID, provider, model, effort, and status
facts.

`forbidden_names` is an additional isolation assertion, not an exclusion
mechanism. Do not put evaluator/reference files under the challenge root.

## Challenge Materialization

Use `materialize_command` for candidate-visible resources that should not be
committed to the challenge or built into the agent image. Brunner:

1. copies the source challenge into a fresh temporary directory;
2. runs the command on the orchestrator with that directory as its working
   directory;
3. rejects command-created symlinks and configured forbidden names;
4. renders the prompt and generated schemas from the materialized copy;
5. stages and hashes every candidate-visible materialized file; and
6. submits only the completed trial to the selected backend.

The command receives:

```text
BRUNNER_CHALLENGE_ROOT
BRUNNER_RESOURCE_CACHE     # only when externally configured
```

Other inherited `BRUNNER_*` variables are removed. Brunner does not provide
the command with the trial, reference bundle, evaluator, assessment inputs, or
candidate workspace. The command is trusted benchmark code running with the
orchestrator's ordinary process permissions, so deployments should give the
orchestrator only the credentials and filesystem access the materializer
needs.

The benchmark command owns all resource semantics, including URLs, cache
layout and locking, checksums, retries, extraction, conversion, and generated
filenames. Brunner only supplies the isolated destination and enforces the
timeout and post-command isolation checks. A launch error, nonzero exit, or
timeout aborts staging and reports the command, exit code when available,
stdout, and stderr.

Because the command's working directory is the temporary challenge root,
module commands such as `python -m my_benchmark.materialize_challenge` require
the benchmark package to be installed or otherwise importable independently
of the orchestrator's original working directory. An absolute script path is
also valid.

For example:

```python
from __future__ import annotations

import os
from pathlib import Path


challenge_root = Path(os.environ["BRUNNER_CHALLENGE_ROOT"])
resources = challenge_root / "resources"
resources.mkdir(parents=True, exist_ok=True)
(resources / "generated-note.txt").write_text(
    "Candidate-visible generated resource.\n"
)
```

The repository includes a runnable harmless example:

```sh
brunner \
  --benchmark examples.text_benchmark.definition:build_materialized_definition \
  stage ./materialized-workspace
```

With no `materialize_command`, Brunner retains the existing direct challenge
copy behavior. `stage`, `trial-create`, and every container-isolated campaign
backend share this same staging path.

## Standard Qualitative Review

`QualitativeReviewDefinition` enables Brunner's packaged generic review. The
benchmark supplies only reviewer settings and lifecycle policy:

```python
qualitative_review=QualitativeReviewDefinition(
    reviewer=ProviderSettings(
        provider="codex",
        model="REVIEWER_MODEL",
        effort="high",
    ),
    required=False,
    run_if_evaluation_failed=True,
)
```

When configured, every evaluation path runs the review after the deterministic
evaluator and before campaign cleanup. This includes `trial-evaluate`,
`trial-assess`, and container or Kubernetes campaigns. Trial creation records
the review contract before execution.

Brunner writes:

```text
evaluation/qualitative-review-input.json
evaluation/qualitative-review.json
evaluation/qualitative-review.html
assessments/qualitative-review/
```

The packaged rubric covers approach classification, output provenance, task
and result fidelity, implementation quality, tests, reproducibility,
efficiency and time use, rule compliance, claims, transcript milestones, and
overall synthesis. It requires evidence for applicable judgments and uses the
canonical Brunner timing partition rather than asking the reviewer to invent
thinking or waiting time.

The JSON Schema is the structural source of truth. Brunner gives the reviewer
a resolved copy and validates the response against the same schema before
running the packaged renderer. Candidate provider/model identity is omitted
or redacted where practical. Reviewer identity, attempts, token usage, and
contract hashes are recorded by the assessment envelope rather than trusted
to reviewer self-report.

References to Brunner's common assessment schema are resolved by copying only
the referenced definitions and their transitive dependencies into the provider
schema. Benchmark-local `$defs` remain unchanged. Before a Codex reviewer is
launched, Brunner verifies that the resolved schema is a self-contained object
schema with closed, required root properties and resolvable local references.
Construction or preflight errors fail the assessment with zero reviewer
attempts.

The standard review is non-gating by default. Set `required=True` only when a
missing or invalid review should make the campaign trial unsuccessful. If
`qualitative_review` is omitted, existing benchmarks retain the previous
`assessment_status="not_configured"` behavior.

## Additional Assessments

Use `BenchmarkDefinition.assessments` for benchmark-specific qualitative or
domain review beyond the standard contract. These assessment prompts, rubrics,
output schemas, trusted evidence, and renderers remain benchmark-owned.
Brunner uses each schema as the structural source of truth: the reviewer
receives it and Brunner validates the returned JSON against the same file.
The rubric remains the semantic source of truth and should not duplicate a
field list already represented by the schema.

An assessment must define exactly one execution method:

- `reviewer=ProviderSettings(...)` invokes a fixed Codex or Claude model with
  structured output and read-only tools.
- `command=(...)` invokes trusted benchmark code that writes
  `BRUNNER_ASSESSMENT_OUTPUT`.

`portable_command_paths=True` records the active Python interpreter as
`{python}` and files beneath the assessment root as
`{assessment_root}/...` in the contract digest. Runtime commands remain
unchanged, and referenced files remain content-hashed. The packaged standard
review enables this so moving between equivalent Brunner installations does
not create false contract drift; benchmark-owned assessments retain their
literal command paths unless they opt in.

Command implementations can use the helper API instead of reading environment
variables directly:

```python
from brunner.assessment import (
    load_assessment_input,
    write_assessment_output,
)


assessment_input = load_assessment_input()
write_assessment_output(
    assessment_input,
    {
        "verdict": "pass",
        "evidence": [],
    },
)
```

`write_assessment_output()` validates against the configured benchmark schema
before writing.

Optional `prepare_command` writes benchmark-specific JSON to
`BRUNNER_ASSESSMENT_BENCHMARK_INPUT`; Brunner incorporates it into the
standard dossier. Optional `render_command` runs after output validation.
Declared `AssessmentReport` files must exist before the assessment succeeds.

Assessment commands receive:

```text
BRUNNER_TRIAL_ROOT
BRUNNER_ASSESSMENT_ID
BRUNNER_ASSESSMENT_WORKSPACE
BRUNNER_ASSESSMENT_INPUT
BRUNNER_ASSESSMENT_BENCHMARK_INPUT
BRUNNER_ASSESSMENT_OUTPUT
BRUNNER_ASSESSMENT_SCHEMA
BRUNNER_ASSESSMENT_RESULT
BRUNNER_EVALUATION_RESULTS
```

`trial_evidence_paths` selects trial-relative files or directories copied for
review. `trusted_evidence_paths` selects files under the assessment root.
Missing trial evidence is recorded as unavailable; missing trusted evidence
is a configuration error. Symlinks are rejected. Generated inputs, outputs,
and reports must be under `evaluation/` or `assessments/`; Brunner rejects
configurations that could overwrite the candidate workspace or deterministic
evaluation result.

Model-review evidence exists in two copies while the review runs: one durable
copy under the trial's assessment workspace and one temporary copy that
isolates the reviewer from the trial. Benchmarks with large datasets,
trajectories, videos, or generated resources should narrow
`trial_evidence_paths` to the source, summaries, metrics, and representative
artifacts the reviewer actually needs.

The packaged common schema can be referenced without copying it:

```json
{
  "properties": {
    "correctness": {
      "$ref": "https://brunner.dev/schemas/assessment-common.schema.json#/$defs/criterion"
    }
  }
}
```

Set `required=True` only when failure to obtain a valid assessment should make
the campaign trial unsuccessful. Deterministic `evaluation.status` is never
overwritten by an assessment result.

## Provider Attempt Semantics

Brunner treats every provider invocation as an isolated attempt. Event logs,
stderr, and provider final output are attempt-specific. A trial reaches
`complete` or `partial` only when the current attempt:

- emits a provider-specific successful terminal event;
- returns final JSON conforming to the generated response schema;
- leaves a contract-valid manifest and all required artifacts; and
- writes a run-status document exactly matching the provider response.

Provider adapters also inspect primary response model identity when the event
format exposes it. A mismatch between the requested model and the model that
produced a primary assistant response is a terminal `provider_error`; it is
not retried or evaluated. Attempts persist `requested_model`,
`observed_models`, and `model_mismatch` for audit. Claude uses
top-level `assistant.message.model` records for this check and deliberately
ignores subagent records and `modelUsage`, which may include helper models that
did not produce the primary response.

The canonical `transcript/final.json` is published only after those checks.
Files left by an earlier attempt cannot terminate a later one. Assessment
reviewers follow the same current-attempt and terminal-event rules. A
successful provider event without ready structured output does not start the
exit grace, so work still being completed by that provider is not killed
prematurely.

Provider launch failures are persisted as `provider_error`. Prompt delivery is
deadline-controlled even when a provider never reads stdin. Missing resumed
sessions are recognized from JSON events or stderr and cause an immediate
fresh invocation. Initial, resumed, and finalization prompts explicitly require
the exact run-status JSON as the provider's final response. A successful turn
that returns prose or a mismatched response receives an immediate corrective
continuation, but Brunner still requires current-attempt structured output and
never accepts the workspace run-status file by itself. Before an attempt
returns, Brunner terminates and reaps its remaining process group, including
undeclared children that could otherwise modify the workspace during
collection.

## Resource Accounting

Brunner maps provider counters into one token scheme:

| Field | Meaning |
| --- | --- |
| `logical_input_tokens` | All input context processed, including cache reads and writes |
| `uncached_input_tokens` | Input not served from cache |
| `cache_read_input_tokens` | Input served from a provider cache |
| `cache_write_input_tokens` | Input written to cache, or `null` when the provider does not expose it |
| `output_tokens` | All output tokens |
| `reasoning_output_tokens` | Reasoning subset when exposed, otherwise `null` |
| `total_tokens` | Logical input plus output; reasoning is not added again |

The provider-native aggregate remains under `provider_fields`. This prevents
the normalized values from hiding what the provider actually reported.

Timing is stored in `timing/accounting.json`. Its headline fields form an
exclusive partition of wall time:

- `agent_active_seconds`
- `foreground_tool_seconds`
- `external_wait_seconds`
- `subscription_wait_seconds`
- `runner_retry_wait_seconds`
- `runner_overhead_seconds`
- `unclassified_seconds`

Background work is different: `background_job_seconds` may overlap any of
those categories and is not added to the exclusive partition.

Use explicit annotations around simulations or other external work:

```python
from brunner import activity

with activity("background_job", "case-a"):
    launch_and_join_background_simulation()

with activity("external_wait", "case-b"):
    wait_for_existing_simulation()
```

The equivalent shell interface is:

```sh
brunner-activity run external_wait case-a -- python simulate.py
brunner-activity start background_job case-b
brunner-activity end background_job case-b
```

Do not mark all simulation runtime as `external_wait`. Use it only when the
agent is blocked. Mark a concurrently running process as `background_job` so
its overlap with agent work remains visible.

Open `foreground_tool`, `external_wait`, and `background_job` intervals also
protect declared work from the soft finalization boundary. Brunner waits for
the interval to close, up to the trial's hard deadline. A provider terminal
event likewise waits for valid current output and declared work to drain before
its exit grace starts.
Undeclared child processes are reaped as an orphaned process group before the
attempt returns.

An interval that is never closed cannot hold the trial open indefinitely.
Brunner releases it when:

- it was started by an earlier attempt, whose process group is already gone;
- the process holding it open has exited; or
- it has run longer than `RuntimeDefaults.max_activity_interval_seconds`
  (six hours by default).

Prefer the forms that let Brunner check the second rule, because they survive
a benchmark crash:

```python
with activity("background_job", "case-a"):   # holds the interval
    run_simulation()
```

```sh
brunner-activity run background_job case-a -- python simulate.py
```

A bare `brunner-activity start` exits immediately, so its PID proves nothing
about the work. That form is still supported, but a missing `end` is caught
only by the maximum-interval rule. Raise
`max_activity_interval_seconds` for benchmarks with legitimately longer single
intervals, or set it to `None` to rely on the hard deadline alone. Released
intervals appear as `activity_interval_stale` events in `timing/events.jsonl`.

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
from brunner.backends import ContainerBackend


def build_campaign(definition, contract):
    plan = CampaignPlan(
        campaign_id="comparison-01",
        root=Path("campaigns/comparison-01"),
        trials=(
            CampaignTrial(
                "codex-a-first-pass",
                "codex",
                "MODEL_A",
                effort="high",
            ),
            CampaignTrial(
                "claude-b-july-30",
                "claude",
                "MODEL_B",
                effort="high",
            ),
        ),
        backend_image="my-benchmark-agent:latest",
        cpu_limit="8",
        memory_limit="32Gi",
        max_parallel=2,
        included_artifact_groups=frozenset({"debug"}),
        collection_retry_seconds=60,
        collection_max_attempts=3,
    )
    return CampaignRunner(
        definition,
        contract,
        plan,
        ContainerBackend(
            max_parallel=2,
            inherited_environment=("OPENAI_API_KEY",),
            nonsecret_environment={
                "HTTPS_PROXY": "http://proxy.internal:3128",
            },
        ),
    )
```

The first `CampaignTrial` argument is the caller-owned trial ID. Brunner does
not derive identity from provider, model, effort, or a run counter. Multiple
items may use the same execution configuration:

```python
trials=(
    CampaignTrial("baseline", "codex", "MODEL_A", effort="high"),
    CampaignTrial("rerun-after-fix", "codex", "MODEL_A", effort="high"),
)
```

Campaign state is append-only by trial ID. Repeating an existing ID with the
same execution attributes is a no-op, whether it is pending, running, failed,
or complete. Adding another ID creates another trial without invalidating
existing state, and list order may change freely. Reusing an ID with different
provider, model, effort, or environment keys is rejected because the
persisted trial would otherwise be ambiguous. Removing an ID from the Python
list does not delete or cancel its historical campaign entry.

```sh
brunner --benchmark my_benchmark.definition \
  campaign-init my_benchmark.campaign
brunner --benchmark my_benchmark.definition \
  campaign-step my_benchmark.campaign
brunner --benchmark my_benchmark.definition \
  campaign-run my_benchmark.campaign --poll-seconds 10
```

Campaign backends must declare `agent_isolation = "container"`;
`CampaignRunner` rejects host-process backends. For OCI execution, use
`ContainerBackend` and set `backend_image` on the plan. For Kubernetes,
construct `KubernetesBackend(KubernetesProfile(...))`. Agent and
artifact-reader images must contain Brunner; the agent image also needs the
selected provider CLI. The benchmark package, evaluator, and references are
not required in the agent image.

Kubernetes workload requests and limits are independent. Set `cpu_request`,
`memory_request`, and `ephemeral_storage_request` to scheduler reservations;
set `cpu_limit`, `memory_limit`, and `ephemeral_storage_limit` to permitted
burst ceilings. Omit a limit when the namespace policy should provide it or
when no hard cap is desired. Brunner does not copy requests into limits.

```python
plan = CampaignPlan(
    ...,
    cpu_request="2",
    cpu_limit="8",
    memory_request="8Gi",
    memory_limit="32Gi",
    ephemeral_storage_request="1Gi",
    ephemeral_storage_limit="3Gi",
)
```

`WorkloadSpec.cpu`, `WorkloadSpec.memory`, and `WorkloadSpec.storage` remain
compatibility shorthands that set both Kubernetes request and limit when no
explicit side is supplied. Either side can be overridden incrementally, so
existing code can add only limit fields to become burstable. New benchmark
code should use the explicit request and limit fields. For OCI execution,
explicit `cpu_limit` and `memory_limit` take precedence; request and ephemeral
storage fields have no effect because an OCI runtime has no scheduler
reservation or portable ephemeral-storage limit.

Campaign trials do not accept environment-variable names or values. Provider
credentials and deployment networking belong to the backend configuration:

- `ContainerBackend.inherited_environment` names credentials that must exist in
  the orchestrator environment. Brunner passes `--env NAME` to the OCI runtime,
  never `--env NAME=value`, so secret values are absent from command arguments
  and campaign state.
- `KubernetesProfile.secret_environment` maps container variable names to
  Kubernetes Secret name/key references.
- `ContainerBackend.nonsecret_environment` and
  `KubernetesProfile.nonsecret_environment` are for explicit non-secret
  deployment settings such as proxy addresses or certificate paths.

Do not put secret values in non-secret environment mappings.

`campaign-step` returns a persisted `paused_backend_connectivity` state when
the backend cannot be reached. `campaign-run` keeps waiting and retries until
connectivity returns or the process is interrupted; it does not alter remote
workloads while disconnected.

Kubernetes helper, Job, and PVC cleanup is synchronous. Brunner removes stale
staging and artifact-reader pods by workload labels before reuse and does not
mark a trial complete until deletions finish. If Kubernetes becomes unreachable
during cleanup, the campaign remains in `cleanup_pending` and retries later.
Other cleanup failures, including deletion timeouts and finalizers, also remain
in `cleanup_pending` and retry after `cleanup_retry_seconds`. Cleanup failure
does not replace an already established pipeline or benchmark result.

Artifact-transfer interruptions retain verified partial files and retry after
`collection_retry_seconds`, up to `collection_max_attempts`. Checksum,
identity, path, and other integrity failures are not retried automatically.
Only completed backend collection calls and non-connectivity collection
failures consume that attempt limit. Orchestrator interruption and backend
connectivity pauses leave the in-progress collection attempt uncharged.
An empty remote log response does not overwrite a previously recovered
workload log. Terminal Kubernetes snapshots preserve structured Job and Pod
events before cleanup. They also include relevant warning events for pending
PVCs and artifact-reader mount failures, and the campaign dashboard shows
those warnings with live elapsed time. The orchestrator's Kubernetes identity
must be allowed to read Events. A terminal event-read failure stops
reconciliation before cleanup rather than silently discarding the diagnostics.

`RuntimeDefaults.timeout_seconds` is the agent's hard deadline.
`backend_shutdown_grace_seconds` is added only to the enclosing backend
workload deadline, leaving time for terminal state and accounting artifacts
to be written after the agent deadline. Keep it positive and large enough for
the selected backend to stop and persist the runner cleanly.

Campaigns bound the states a stuck backend can hide in:

| Setting | Purpose | Default |
| --- | --- | --- |
| `submission_retry_seconds` | Delay before retrying an ambiguous or partially failed submission | 60 seconds |
| `submission_max_attempts` | Bounds idempotent submission/adoption attempts | 3 |
| `trial_timeout_seconds` | Flags a trial the backend still reports pending or running | Backend workload deadline plus `trial_timeout_margin_seconds` |
| `trial_timeout_margin_seconds` | Slack added to the derived default | 5 minutes |
| `infrastructure_max_restarts` | Relaunches an interrupted backend workload against its existing persistent trial | 2 |
| `cleanup_retry_seconds` | Delay before retrying failed backend cleanup | 60 seconds |
| `max_pause_seconds` | Optional limit before backend disconnection requires manual attention | Unlimited |
| `evaluation_timeout_seconds` | One budget shared by reference validation, the evaluator, and all assessments | Benchmark evaluation timeout |

Reconciliation is sequential, so an evaluator that hangs blocks every other
trial in the campaign until its timeout expires. Set
`evaluation_timeout_seconds` to something much smaller than
`EvaluationDefinition.timeout_seconds` when a campaign has many trials.
Exceeding `trial_timeout_seconds` marks the live trial as needing attention but
keeps its lifecycle phase pending or running. Brunner continues inspecting it,
resolves the attention marker when it terminates, and keeps its slot reserved
throughout, so the campaign cannot quietly exceed `max_parallel`. Exceeding
`max_pause_seconds` moves the campaign to `attention_required` without
cancelling remote work.

## CLI

```text
contract-check       Validate contract and print digest
contract-render      Render the generated output-requirements prompt section
stage                Stage an isolated challenge
trial-create         Create a durable trial
trial-evaluate       Run trusted evaluation
trial-assess         Rerun configured assessments over existing evaluation
reference-build      Build a reference manifest
reference-validate   Verify a reference bundle
campaign-init        Create campaign state and trials
campaign-step        Reconcile one campaign iteration
campaign-run         Reconcile until complete, paused, or attention required
```

Remote backends invoke `python -m brunner.agent_cli` inside the agent
container. The module reads only staged trial metadata and does not import
benchmark code; it is intentionally not installed as a public console command.
It handles `SIGTERM` and `SIGINT` as graceful interruption requests, terminating
the active provider process group, recording the signal, persisting resumable
state, and exiting nonzero. Kubernetes Jobs set
`BRUNNER_TERMINATION_LOG=/dev/termination-log`; the CLI writes a compact
pipeline summary there so Job inspection can distinguish a terminal provider
result from interruption or missing output even when Kubernetes reports an
inconsistent zero exit code.

An agent exit code of zero means that a current terminal provider result exists
and trusted evaluation may run. It is pipeline completion, not benchmark
success. Campaign entries report pipeline status, benchmark status, overall
outcome, and failure class separately. If collection finds no terminal provider
result, Brunner preserves the diagnostics but does not evaluate the incomplete
workspace.

Campaign and evaluation records use the failure contract in
[`failure-model.md`](failure-model.md). Invalid candidate submissions use the
`candidate` domain, while evaluator, reference, required assessment, reporting,
and cleanup failures retain their trusted-infrastructure domains. A required
reviewer outage therefore makes benchmark success indeterminate instead of
recording a candidate failure.

Provider retry and subscription-reset waits are absolute deadlines in the
trial's `status.json`, so replacing a pod or restarting the agent does not
bypass or restart an existing wait.
