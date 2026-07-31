# Architecture

## Lifecycle Boundary

Brunner separates benchmark execution into six trust and responsibility
stages.

1. **Stage**: copy only challenge-visible files, render output requirements
   from the canonical contract, and record challenge/contract digests.
2. **Run**: execute a provider in the staged workspace with durable state,
   retries, continuation, finalization, timeout, and structured final output.
3. **Collect**: preserve logs and copy artifacts through a resumable,
   checksum-verified transfer.
4. **Evaluate**: validate the submission contract, then run trusted
   benchmark-specific scoring against optional verified references.
5. **Assess**: optionally build an evidence dossier and run trusted,
   schema-bound command or model reviews without changing deterministic
   evaluation status.
6. **Campaign**: schedule a matrix, reconcile backend state, recover outputs,
   evaluate, clean up, and publish a dashboard.

## Ownership

| Concern | Brunner owns | Benchmark owns |
|---|---|---|
| Identity | Metadata/digest recording | Benchmark ID and version |
| Agent input | Isolated staging | Challenge files and prompt prose |
| Output definition | Rendering and validation | `output-contract.json` |
| Provider runtime | Commands, timestamped events, retries, resource accounting | Model/effort selection |
| Evaluation | Trusted invocation and result envelope | Metrics and scoring code |
| Assessment | Dossier, execution, validation, provenance, status | Rubric, prompt, output schema, evidence, renderer |
| References | Manifest and integrity validation | Reference content |
| Artifacts | Inventory, resume, checksum, groups | Retention policy |
| Infrastructure | Backend interfaces and implementations | Runtime profile/images |
| Campaigns | Durable state and dashboard | Trial matrix |

## Canonical Output Contract

`output-contract.json` is the single machine-readable definition for:

- Submission manifest path and JSON Schema
- Run-status path and work-unit IDs
- Required/optional artifacts
- Artifact paths or manifest JSON pointers
- Media types and byte bounds
- Optional artifact JSON Schemas
- Human-readable output constraints

During staging Brunner writes:

```text
workspace/
  PROMPT.md
  schema/
    output-contract.json
    submission.schema.json
    final-response.schema.json
    artifacts/<artifact-id>.schema.json
```

The prompt output section is rendered from the same contract. Generic
submission validation follows manifest pointers, rejects path escape and
symlink traversal, validates JSON artifacts, hashes accepted files, and
requires a `complete` status to list every work unit.

Evaluator code calls `load_evaluation_input()`. That API reloads the staged
contract, checks its SHA-256 against trial metadata, validates the submission,
validates the reference manifest, and exposes artifacts by contract ID.
Evaluator code therefore handles domain scoring, not output discovery or
structural validation.

## Assessment Contracts

An assessment is a trusted post-evaluation operation. Each benchmark owns an
assessment directory containing its reviewer prompt, rubric, and output
schema. Brunner records a contract digest over those materials, trusted
evidence, reviewer identity or command, input/output paths, and renderer
configuration when the trial is created.

For every assessment Brunner:

1. verifies the recorded assessment contract has not changed;
2. writes a standard `review-input.json` dossier with deterministic results,
   artifact hashes, timing facts, usage, and evidence locations;
3. copies allowlisted trial and trusted evidence into an assessment workspace;
4. optionally runs a benchmark input-builder command;
5. runs either a trusted command or a fixed Codex/Claude reviewer;
6. validates the output against the benchmark's exact JSON Schema;
7. optionally runs a benchmark renderer and registers its reports; and
8. records attempts, usage, hashes, blinding limitations, and failure details.

Model reviewers run without session persistence. Codex uses its read-only
sandbox; Claude is limited to read, glob, and grep tools. They execute in a
temporary workspace outside the trial with `BRUNNER_*` environment variables
removed. Candidate provider and model fields are omitted from the dossier and
matching structured fields are redacted from copied JSON evidence. The result
records that provider family may still be inferable from transcript structure.

Brunner packages
`https://brunner.dev/schemas/assessment-common.schema.json` as an optional
schema resource. Benchmarks may reference its generic `evidence` and
`criterion` definitions while retaining their own rating vocabulary and
domain-specific output structure.

Assessment status is separate from deterministic evaluation status. Optional
assessment failures remain visible but do not fail a campaign trial. A failed
required assessment sets `required_assessments_complete` to false and makes
the campaign trial unsuccessful.

## Trust Boundary

The agent runtime receives only:

- The staged challenge
- Generated schemas and contract
- Minimal `metadata/agent-run.json`
- Provider credentials supplied by the runtime

Candidate Codex and Claude processes execute with workspace-only write
permissions and without inherited user configuration or external tool
connections. Runner-owned metadata, backend, evaluation, assessment, usage,
and status paths are snapshotted around every attempt. Any mutation is
restored and terminates the trial as a provider error.

Remote jobs run `brunner-agent`, which does not import the benchmark package.
Evaluator source and trusted references do not need to be present in the
agent image.

Evaluation can run as a trusted host subprocess or in
`EvaluationDefinition.image`. Container evaluation uses:

- No network
- A read-only container root
- The collected trial mounted read/write
- The reference bundle mounted read-only

The evaluator image contains benchmark-specific scoring code and Brunner's
evaluator helper API.

## Durable Agent Runtime

Provider adapters define commands, terminal-event recognition, usage parsing,
failure classification, and resume behavior. The runner persists:

- Immutable benchmark/provider/contract identity
- Session identity and whether a session has started
- Every attempt with event/stderr paths and terminal observations
- Retry delay, finalization transition, deadline, and final response
- Raw provider events plus local receipt timestamps
- Canonical token accounting with provider-native source counters
- Exclusive wall-time accounting and overlapping background-job intervals

Ordinary transient API failures retry with bounded exponential delay.
Authentication, authorization, unavailable model, invalid request, and
disabled-credit conditions terminate immediately.

The work deadline is a soft transition into finalization. An open provider
tool or benchmark-declared `external_wait`/`background_job` interval may drain
until the hard trial deadline. A provider terminal event starts its exit grace
only after declared work drains. Undeclared orphan process groups are
terminated and reaped before the attempt returns, so artifact collection
cannot race a leftover child process.

Rejected subscription boundaries are distinct from ordinary retry backoff.
When a provider exposes a reset epoch, Brunner waits directly for that
boundary and records the interval as `subscription_wait`.

Foreground tool intervals come from provider lifecycle events. The remainder
of an active provider attempt is `agent_active`, which includes model/API
processing and provider latency. Benchmarks must emit explicit
`external_wait` or `background_job` events when they need finer simulation
accounting; Brunner does not classify shell commands by text.

## Backend Interface

All execution backends implement:

```text
submit -> inspect -> logs -> collect -> cleanup
                     ^
                   capacity
```

`LocalBackend` launches a detached state-writing worker. `ContainerBackend`
bind-mounts the trial into an OCI runtime. `KubernetesBackend` creates a PVC,
stages the trial through a helper pod, creates a Job, and recovers files
through reader pods.

The backend workload deadline is the agent hard deadline plus
`backend_shutdown_grace_seconds`; the outer backend therefore does not kill
the runner at the exact instant the runner must persist timeout and accounting
artifacts. Container and Kubernetes resource names include a digest of the
caller-owned workload identity and trial path, preventing normalization or
truncation collisions.

Kubernetes distinguishes connectivity failures from rejected requests and
workload failures. It reports pending PVCs, inspects terminated init/main
containers, preserves workload logs, retries artifact readers, excludes
failed reader nodes when rescheduling, resumes partial files by byte offset,
and verifies every SHA-256. A failed workload's PVC is retained until artifact
collection succeeds.

## Campaign State

Campaign trial IDs are supplied explicitly by the benchmark. They are not
derived from provider, model, effort, or a run count. `campaign.json` records
the contract identity, append-only trial list, handles, snapshots, collection
attempts, evaluation results, outcomes, and recent events.

Campaign reconciliation:

- Adds new caller-supplied trial IDs without invalidating existing state
- Treats an existing matching ID as already known, regardless of list order
- Rejects only an ID reused with conflicting execution attributes
- Submits only up to plan and backend capacity
- Resumes from persisted handles
- Pauses the campaign on backend connectivity loss
- Collects artifacts for both successful and failed workloads
- Recovers interrupted collection and evaluation phases after a crash
- Keeps durable collection/evaluation failures visible without retry loops
- Continues healthy running or pending trials when another needs attention
- Does not clean up when recovery fails
- Runs trusted evaluation after verified collection
- Runs configured assessments after deterministic evaluation
- Regenerates `index.html` after each transition

No campaign environment values are serialized. A plan may name environment
variables to pass at submission time; their values are read from the current
process environment.
