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
            image=(
                "registry.example/my-benchmark-evaluator@sha256:"
                "0123456789abcdef0123456789abcdef"
                "0123456789abcdef0123456789abcdef"
            ),
            command=("python", "-m", "my_benchmark.evaluator"),
            timeout_seconds=12 * 60 * 60,
            cpu_request="3",
            cpu_limit="8",
            memory_request="16Gi",
            memory_limit="64Gi",
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
            collect_evaluated_artifacts=False,
            max_collection_bytes=10 * 1024 * 1024 * 1024,
            max_diagnostic_collection_bytes=512 * 1024 * 1024,
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
copy behavior. `stage`, `trial-create`, and every Kubernetes campaign share
this same staging path.

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

When configured, the campaign runs the review after Sterling's deterministic
evaluator result has been selectively collected and before cleanup.
`trial-assess` can rerun assessments over an existing collected evaluation.
Trial creation records the review contract before execution.

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

The evaluator command runs only inside `EvaluationDefinition.image` on
Sterling. It must write the path in `BRUNNER_EVALUATION_RESULTS`. Brunner
validates the result envelope on the PVC, then generates the general run report
after selective collection. Evaluators may list additional report files. The
image must contain Brunner, the benchmark evaluator package, and every runtime
dependency used by the evaluator; the command is interpreted inside that image,
not on the orchestrator.

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
from brunner.backends import KubernetesBackend, KubernetesProfile


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
        backend_image=(
            "registry.example/my-benchmark-agent@sha256:"
            "0123456789abcdef0123456789abcdef"
            "0123456789abcdef0123456789abcdef"
        ),
        cpu_request="2",
        cpu_limit="8",
        memory_request="8Gi",
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
        KubernetesBackend(
            KubernetesProfile(
                namespace="bizon",
                agent_image=plan.backend_image,
                artifact_reader_image=plan.backend_image,
                reference_claim_name="my-benchmark-reference",
                storage_size="250Gi",
                storage_class_name="sterling-storage-class",
                image_pull_secrets=("registry-credentials",),
                secret_environment={
                    "OPENAI_API_KEY": (
                        "provider-credentials",
                        "OPENAI_API_KEY",
                    ),
                },
                proxy_image=(
                    "ubuntu/squid@sha256:"
                    "0123456789abcdef0123456789abcdef"
                    "0123456789abcdef0123456789abcdef"
                ),
                max_parallel=2,
            )
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
existing state, and list order may change freely. Reusing an ID with a changed
provider, model, effort, command, image, timeout, resource envelope, label, or
trusted evaluation contract is rejected because the persisted remote workload
would otherwise be ambiguous. The campaign also requires every trial to share
one deterministic materialized challenge digest. Removing an ID from the
Python list does not delete or cancel its historical campaign entry.

```sh
brunner --benchmark my_benchmark.definition \
  campaign-init my_benchmark.campaign
brunner --benchmark my_benchmark.definition \
  campaign-step my_benchmark.campaign
brunner --benchmark my_benchmark.definition \
  campaign-run my_benchmark.campaign --poll-seconds 10
```

`campaign-run` serves the campaign directory at
`http://127.0.0.1:8765/` while reconciling and keeps that monitor available
after the campaign becomes terminal. Use `--exit-after-terminal` for batch
automation, or `--host` and `--port` to change the listener.

Campaigns require `KubernetesBackend`. Brunner renders one durable Job with the
agent as an init container and the trusted evaluator as the main container.
Agent and artifact-reader images must contain Brunner; the agent image also
needs the selected provider CLI. The evaluator image must contain Brunner and
the benchmark evaluator package.

Production images must use `image@sha256:<digest>` references and must contain
a compatible Brunner runtime protocol. Set
`KubernetesProfile.require_image_digests=False` only in controlled tests.

Before any staging helper is created, Brunner installs its namespace-scoped
Squid proxy and applies two workload NetworkPolicies. The pipeline may reach
only Squid on TCP 3128; helpers have no egress. Brunner injects the proxy's
numeric Service ClusterIP only into the agent, so the pipeline does not receive
DNS access. Squid alone may query cluster DNS and connect to external TCP 443,
and its deny-by-default ACL permits only OpenAI, Azure OpenAI, Anthropic, and
Claude domains. Brunner rejects another standard Kubernetes NetworkPolicy with
nonempty egress rules that also selects the pipeline Pod, because egress
permissions are additive. Do not place proxy variables in
`nonsecret_environment`. Sterling's CNI must enforce Kubernetes NetworkPolicy;
Brunner cannot infer enforcement from successful object creation.

The stager clears an incomplete trial PVC before copying, verifies every
remote challenge file against the local stage inventory, rejects remote
symlinks, and annotates the PVC only after verification. Existing Jobs and
PVCs are adopted only when challenge, workload, runtime-protocol, and ownership
annotations match the current trial.

Brunner hashes the effective agent image even when it is supplied by
`KubernetesProfile.agent_image`. Custom workload labels must not use
`app.kubernetes.io/name`, `dev.brunner/workload`, `dev.brunner/role`, or
`dev.brunner/restart-generation`; those labels are reserved for ownership and
reconciliation.

When a benchmark defines `ReferenceDefinition`, configure
`KubernetesProfile.reference_claim_name` with an existing Sterling PVC
containing the validated reference bundle. Only the evaluator mounts that PVC,
read-only. Brunner fails submission if the claim is absent rather than copying
trusted references into the trial or agent image.

The reference claim must support `ReadWriteMany` and carry
`dev.brunner/reference-manifest-sha256`, whose value is the SHA-256 of the
manifest file bytes approved by the orchestrator. The evaluator independently
checks that mounted manifest and requires exact `benchmark_id`,
`benchmark_version`, and `contract_sha256` metadata. Provisioning and annotating
the large reference PVC remain deployment operations; Brunner never transfers
the reference through the candidate PVC.

Kubernetes artifact collection reads each file through resumable
`kubectl exec` calls. `KubernetesProfile.artifact_chunk_bytes` controls the
maximum bytes requested by each call, and `command_timeout_seconds` bounds
that call. `artifact_chunk_attempts` retries a failed path, offset, and byte
count on the same mounted reader before Brunner replaces the reader pod;
`artifact_chunk_retry_seconds` controls the delay between those retries.
Smaller chunks are more resilient on slow or unstable links but increase
command overhead. These values apply to the orchestrator-side transfer; the
remote reader protocol accepts any positive chunk size.

Files recorded in the Sterling evaluator's validated `submission.artifacts`
list are omitted from collection unless
`ArtifactPolicy.collect_evaluated_artifacts=True`. This keeps trajectories,
videos, and other evaluator-only data on the trial PVC. Even with explicit
opt-in, `max_collection_bytes` rejects an oversized inventory before transfer.
Brunner also compares the remote workspace to the per-file inventory recorded
during staging. Unchanged challenge files are hard-linked from the local staged
trial into the collected view instead of being downloaded from Sterling.
Changed challenge files count against `max_collection_bytes`; if local
hard-link reuse is unavailable, collection fails rather than silently copying
a large immutable input.

If evaluation never completed and the remaining inventory still exceeds
`max_collection_bytes`, Brunner switches to
`ArtifactPolicy.failure_diagnostic_globs`, bounded by
`max_diagnostic_collection_bytes`. The collection result records
`collection_mode="diagnostics"` plus omitted file and byte counts. Submission
artifacts declared by the staged output contract are excluded even when no
evaluation result exists.

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
code should use the explicit request and limit fields. Evaluator requests and
limits are configured independently on `EvaluationDefinition`.

Campaign trials do not accept environment-variable names or values. Provider
credentials and deployment networking belong to the backend configuration:

- `KubernetesProfile.secret_environment` maps container variable names to
  Kubernetes Secret name/key references for the agent init container.
- `KubernetesProfile.nonsecret_environment` supplies explicit non-secret agent
  deployment settings such as certificate paths.
- `proxy_image` supplies the digest-pinned Squid image for Brunner's managed
  provider-only egress proxy.
- `proxy_cpu_request`, `proxy_cpu_limit`, `proxy_memory_request`, and
  `proxy_memory_limit` configure the shared proxy Deployment.

The evaluator container receives neither mapping.

Do not put secret values in non-secret environment mappings.

`campaign-step` returns a persisted `paused_backend_connectivity` state when
the backend cannot be reached. `campaign-run` keeps waiting and retries until
connectivity returns or the process is interrupted; it does not alter remote
workloads while disconnected.

Before launch, Brunner checks cluster access, required Job/PVC/Pod/Event/
NetworkPolicy/ConfigMap/Service/Deployment RBAC, immutable images, reference
identity, managed-proxy rollout, and ResourceQuota capacity. Quota capacity
includes object counts, PVC storage, CPU, memory,
ephemeral storage, and extended resources, using Kubernetes' effective
init-container scheduling request. A quota limit appears as a visible
`backend_capacity` scheduler wait rather than oversubmission.

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
should be allowed to read Events. Preflight reports missing Event RBAC before
launch, but terminal Event expiry, RBAC drift, or transient read failure is
recorded as a warning and never blocks collection or cleanup.

`RuntimeDefaults.timeout_seconds` is the agent's hard deadline.
`backend_shutdown_grace_seconds` leaves time for terminal state and accounting
artifacts after the agent deadline. The enclosing Job deadline also includes
the effective evaluator timeout.

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
| `evaluation_timeout_seconds` | Optional campaign cap on the Sterling evaluator container | Benchmark evaluation timeout |

Kubernetes enforces evaluator timeout even while the orchestrator is asleep or
disconnected. Set `evaluation_timeout_seconds` to cap a benchmark's configured
evaluation timeout for a particular campaign.
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
trial-assess         Rerun configured assessments over existing evaluation
reference-build      Build a reference manifest
reference-validate   Verify a reference bundle
campaign-init        Create campaign state and trials
campaign-step        Reconcile one campaign iteration
campaign-run         Reconcile and serve the monitor until interrupted
```

Kubernetes invokes `python -m brunner.agent_cli` inside the agent init
container. The module reads only staged trial metadata and does not import
benchmark code; it is intentionally not installed as a public console command.
It handles `SIGTERM` and `SIGINT` as graceful interruption requests, terminating
the active provider process group, recording the signal, persisting resumable
state, and exiting nonzero. Kubernetes Jobs set
`BRUNNER_TERMINATION_LOG=/dev/termination-log`; the CLI writes a compact
pipeline summary there so Job inspection can distinguish a terminal provider
result from interruption or missing output even when Kubernetes reports an
inconsistent zero exit code.

After a terminal provider result, Kubernetes starts
`python -m brunner.evaluation_cli` in the evaluator container. It receives no
provider Secret or agent proxy environment, mounts the trial PVC read/write and
the configured reference PVC read-only, and records its own termination
summary for candidate-versus-infrastructure classification.

A valid candidate failure exits `evaluation_cli` with zero so Kubernetes marks
the pipeline complete. Exit code 2 is reserved for evaluator, reference,
runtime-protocol, or other trusted infrastructure failure.

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
