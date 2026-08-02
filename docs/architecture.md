# Architecture

## Lifecycle Boundary

Brunner separates benchmark execution into six trust and responsibility
stages.

1. **Stage**: optionally materialize resources into a temporary challenge
   copy, copy only challenge-visible files, render output requirements from
   the canonical contract, and record challenge/contract digests.
2. **Run**: execute a provider in the staged workspace with durable state,
   retries, continuation, finalization, timeout, and structured final output.
3. **Collect**: preserve logs and copy artifacts through a resumable,
   checksum-verified transfer.
4. **Evaluate**: validate the submission contract, then run trusted
   benchmark-specific scoring against optional verified references.
5. **Assess**: build evidence dossiers and run the configured standard
   qualitative review plus any domain-specific, schema-bound command or model
   reviews without changing deterministic evaluation status.
6. **Campaign**: schedule a matrix, reconcile backend state, recover outputs,
   evaluate, clean up, and publish a dashboard.

## Ownership

| Concern | Brunner owns | Benchmark owns |
|---|---|---|
| Identity | Metadata/digest recording | Benchmark ID and version |
| Agent input | Temporary materialization, isolated staging | Challenge files, prompt prose, and resource preparation command |
| Output definition | Rendering and validation | `output-contract.json` |
| Provider runtime | Commands, timestamped events, retries, resource accounting | Model/effort selection |
| Evaluation | Trusted invocation and result envelope | Metrics and scoring code |
| Standard qualitative review | Generic rubric, prompt, schema, dossier, execution, validation, renderer, provenance, status | Reviewer identity and whether completion gates success |
| Domain assessments | Dossier, execution, validation, provenance, status | Rubric, prompt, output schema, evidence, renderer |
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

When a challenge defines a materialization command, Brunner first copies the
source challenge into a fresh orchestrator-side temporary directory. It runs
the command there, rechecks symlinks and forbidden names, then uses that
materialized copy for prompt rendering, schema generation, staging, and the
challenge digest. The source checkout is never modified. Without a command,
the original direct-copy staging path is unchanged.

Materialization is part of trial creation, before any local, container, or
Kubernetes backend receives a workload. Materialized resources are therefore
candidate-visible, included in `challenge_sha256`, and copied to remote
storage with the rest of the trial. They are not added to an agent image.

Evaluator code calls `load_evaluation_input()`. That API reloads the staged
contract, checks its SHA-256 against trial metadata, validates the submission,
validates the reference manifest, and exposes artifacts by contract ID.
Evaluator code therefore handles domain scoring, not output discovery or
structural validation.

## Assessment Contracts

An assessment is a trusted post-evaluation operation. Brunner packages a
standard qualitative review that classifies the approach, checks output
provenance, reviews generic task/result/implementation quality, tests,
reproducibility, rule compliance, claims, transcript milestones, and canonical
time accounting. Its prompt, rubric, output schema, and HTML renderer are one
versioned Brunner contract.

A benchmark enables that contract with `QualitativeReviewDefinition`, which
supplies the fixed reviewer identity and policy. Brunner records the standard
contract digest when it creates the trial and runs the review automatically
after deterministic evaluation on direct, local, container, and campaign
paths. The reviewer receives the same output schema that Brunner later uses to
validate the response.

Benchmarks may also own additional assessment directories for domain-specific
criteria. Those directories contain their reviewer prompt, rubric, output
schema, trusted evidence, and optional renderer.

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

Brunner also packages
`https://brunner.dev/schemas/assessment-common.schema.json` as an optional
schema resource. Benchmarks may reference its generic `evidence` and
`criterion` definitions while retaining their own rating vocabulary and
domain-specific output structure.

Assessment status is separate from deterministic evaluation status. Optional
assessment failures remain visible but do not fail a campaign trial. A failed
required assessment sets `required_assessments_complete` to false and makes
the campaign trial unsuccessful. The standard qualitative review is
non-gating by default and runs on failed deterministic evaluations so it can
diagnose the failure; both behaviors are configurable.

## Trust Boundary

The agent runtime receives only:

- The staged challenge
- Generated schemas and contract
- Minimal `metadata/agent-run.json`
- Provider credentials supplied by the runtime

Challenge materialization runs earlier as trusted orchestrator-side benchmark
code. Brunner gives it the temporary challenge root, does not pass trial,
reference, evaluation, assessment, or submission `BRUNNER_*` paths, and does
not copy trusted materials into the temporary challenge. The optional
`BRUNNER_RESOURCE_CACHE` value is passed through as a location only; download,
locking, checksum, extraction, conversion, and cache validity semantics remain
benchmark-owned.

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

Provider adapters define commands, terminal-event recognition, primary model
identity observations, usage parsing, failure classification, and resume
behavior. The runner persists:

- Immutable benchmark/provider/contract identity
- Session identity and whether a session has started
- Every attempt with event/stderr paths and terminal observations
- Requested and provider-observed primary model identities
- Retry delay, finalization transition, deadline, and final response
- Raw provider events plus local receipt timestamps
- Canonical token accounting with provider-native source counters
- Exclusive wall-time accounting and overlapping background-job intervals

Every provider invocation writes to attempt-specific event, stderr, and final
output paths. Brunner accepts structured output only from the current attempt
and only after that attempt emits a successful provider terminal event. A
`complete` or `partial` response must exactly match the contract-valid run
status, manifest, and artifacts in the workspace. Only then does Brunner write
the canonical `transcript/final.json`; stale canonical files are removed when
a nonterminal run resumes. Reviewer attempts use the same attempt isolation.

When a provider reports that a primary assistant response came from a model
other than the requested model, Brunner terminates the process and records a
terminal `provider_error`. This makes provider-side safety substitutions,
downgrades, or routing changes failures of the requested model rather than
benchmark results for an undisclosed replacement. Claude identity is taken
from top-level `assistant.message.model` records; subagent messages and
aggregate `modelUsage` entries are not primary identity evidence because they
may include legitimate internal helper models. Claude's `<synthetic>` assistant
records, including subscription-limit notices, are also not model identity
evidence. If a provider exposes no primary model identity in its event stream,
Brunner does not invent one.

Provider launch errors become durable `provider_error` results rather than
escaping before status is written. Prompt input is delivered on a separate
thread, so a provider that never reads stdin remains subject to the normal
soft stop, hard deadline, and process-group termination logic.

Ordinary transient API failures retry with bounded exponential delay.
Authentication, authorization, unavailable model, invalid request, and
disabled-credit conditions terminate immediately.

Session-unavailable detection examines both parsed JSON events and stderr. If
a resumed session is missing, Brunner clears the persisted session-started
state and immediately retries with a fresh invocation instead of repeatedly
resuming an invalid session.

The work deadline is a soft transition into finalization. An open provider
tool or benchmark-declared `external_wait`/`background_job` interval may drain
until the hard trial deadline. A successful provider terminal event starts
its exit grace only after the current structured response and required
submission are valid and declared work has drained. A success event without
ready output does not terminate a provider that is still finishing work, but
it does not disable the soft deadline or consume the reserved finalization
window once declared work is idle.
Undeclared orphan process groups are terminated and reaped before the attempt
returns, so artifact collection cannot race a leftover child process or a
child that writes files after the provider leader exits.

Liveness is never inferred from bookkeeping alone. A declared interval defers
the soft deadline only while it is credibly open: Brunner ignores starts from
earlier attempts, releases intervals whose holding process has exited, and
caps any interval at `max_activity_interval_seconds`. Released intervals are
recorded as `activity_interval_stale` timing events, and time accounting ends
them where they were released rather than charging them to the end of the
trial. A released interval is also dropped from the pairing queue, so a later
`end` for a reused activity ID closes the interval it belongs to. The open-interval set is
maintained incrementally from bytes appended to the activity log, so polling
cost does not grow with the length of the run. Revalidating the submission
after a successful terminal event is throttled to
`submission_poll_seconds`, because that check rehashes every artifact and
would otherwise run on every poll and delay deadline enforcement.

Stream pumps that stay blocked on a pipe inherited by a grandchild are
unblocked by closing the pipe, and any output that arrives after the logs
close is counted rather than lost to an exception inside a daemon thread.

Brunner keeps monitoring the provider's process group until it is gone, even
after the leader has been reaped, because abandoning it would let descendants
write into the workspace while artifacts are being collected. Reaping the
leader frees its PID, so a recycled PID could in principle make the group
check report a stranger's group. That residual race is accepted: losing the
orphan-reaping guarantee is the worse failure.

A trial stops after `max_attempts` provider invocations. Without that bound a
provider that fails immediately would retry for the whole trial window and
bury the original failure.

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

`LocalBackend` launches a detached state-writing worker. The worker can only
record its own PID after its interpreter has started, so the launcher records
`launcher.json` with its process ID and eventual exit code, and captures the
worker's output to `backend/local/worker.log`. A worker that dies before it
takes ownership of the state file is therefore reported as failed instead of
leaving the trial pending forever. `ContainerBackend`
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
containers, preserves previously recovered workload logs, and includes
Kubernetes warning events for pending storage and failed artifact readers. It
retries artifact readers, excludes failed reader nodes when rescheduling,
resumes partial files by byte offset, and verifies every SHA-256. A failed
workload's PVC is retained until artifact collection succeeds.

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
- Pauses individual steps on backend connectivity loss; `run()` waits and
  resumes without changing remote workloads
- Collects artifacts for both successful and failed workloads
- Recovers interrupted collection and evaluation phases after a crash
- Retries interrupted artifact transfers with a bounded, configurable policy
- Keeps integrity and evaluation failures durable instead of retrying them
- Continues healthy running or pending trials when another needs attention
- Flags a trial the backend still reports as pending or running past
  `trial_timeout_seconds`, which defaults to the backend workload deadline
  plus `trial_timeout_margin_seconds`
- Stops waiting on an unreachable backend after `max_pause_seconds` instead of
  polling a disconnected backend indefinitely
- Bounds evaluation with `evaluation_timeout_seconds`, shared as one budget
  across reference validation, the evaluator, and every assessment;
  reconciliation is sequential, so an unbounded evaluator would block every
  other trial
- Keeps an overdue trial's backend slot reserved while the backend still
  reports its workload as pending or running, so flagging it cannot let the
  campaign exceed `max_parallel`
- Does not clean up when recovery fails
- Runs trusted evaluation after verified collection
- Runs the configured standard qualitative review and domain assessments after
  deterministic evaluation
- Regenerates `index.html` after each transition with live elapsed time,
  backend warnings, usage, timing, and report links

No campaign environment values are serialized. A plan may name environment
variables to pass at submission time; their values are read from the current
process environment.
