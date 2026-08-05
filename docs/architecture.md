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
5. resolves and preflights the provider output schema;
6. runs either a trusted command or a fixed Codex/Claude reviewer;
7. validates the output against the benchmark's exact JSON Schema;
8. optionally runs a benchmark renderer and registers its reports; and
9. records attempts, usage, hashes, blinding limitations, and failure details.

Model reviewers run without session persistence. Codex uses its read-only
sandbox; Claude exposes and explicitly authorizes only read, glob, and grep
tools while denying interactive permission requests. They execute in a
temporary workspace outside the trial with `BRUNNER_*` environment variables
removed. Candidate provider and model fields are omitted from the dossier and
matching structured fields are redacted from copied JSON evidence. The result
records that provider family may still be inferable from transcript structure.
The temporary reviewer workspace is a second copy of the selected evidence;
large benchmarks should select compact review inputs rather than whole
datasets or trajectory trees.

Brunner also packages
`https://brunner.dev/schemas/assessment-common.schema.json` as an optional
schema resource. Benchmarks may reference its generic `evidence` and
`criterion` definitions while retaining their own rating vocabulary and
domain-specific output structure. Provider schemas inline only referenced
common definitions and their dependencies; the typeless common-schema document
is never embedded as a `$defs` entry. Provider-specific preflight failures are
terminal assessment configuration errors and create no reviewer attempt.

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

Candidate processes execute inside the selected backend's isolation boundary
and without inherited user configuration or external tool connections. Codex
uses its workspace-write sandbox on the initial invocation. Resumed Codex
sessions inherit that sandbox because `codex exec resume` does not accept the
`--sandbox` option. Claude bypasses its interactive permission system and
relies on the outer container or Kubernetes workload isolation, matching the
proven granular benchmark execution model and avoiding unsupported nested
user-namespace sandboxes. Runner-owned metadata, backend, evaluation,
assessment, usage, and status paths are snapshotted around every attempt. Any
mutation is restored and terminates the trial as a provider error.

Claude candidate runs therefore require an outer isolation boundary. Container
and Kubernetes backends provide that boundary. Campaign construction rejects
backends that do not declare container isolation; Brunner does not support
running candidate agents as host processes.

Kubernetes candidate and helper pods do not mount service-account tokens. They
run as UID/GID 1000 with the runtime-default seccomp profile, all Linux
capabilities dropped, privilege escalation disabled, and a read-only container
root. The trial PVC and an ephemeral `/tmp` volume are their only writable
mounts. Agent and artifact-reader images must support this non-root contract.

Remote jobs run `python -m brunner.agent_cli` inside the agent container. That
internal module does not import the benchmark package. Evaluator source and
trusted references do not need to be present in the agent image.

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
a nonterminal run resumes. Initial, continuation, and finalization prompts all
require the provider to return only the exact run-status JSON object. A
successful provider turn that omits or mismatches that object receives an
immediate output-repair continuation rather than transient-service backoff.
Reviewer attempts use the same attempt isolation.

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

A trial stops after `max_attempts` provider process launches. An attempt is
checkpointed before launch and again immediately after `Popen` succeeds, so an
orchestrator or pod interruption before the provider starts does not consume
the cap. Without that bound a provider that fails immediately would retry for
the whole trial window and bury the original failure.

Rejected subscription boundaries are distinct from ordinary retry backoff.
When a provider exposes a reset epoch, Brunner waits directly for that
boundary and records the interval as `subscription_wait`. The absolute
`retry_not_before_epoch` and the following exponential-backoff value are stored
in `status.json` before waiting. A restarted agent therefore waits only for the
remaining interval instead of retrying immediately or restarting the full
delay.

The remote agent CLI converts `SIGTERM` and `SIGINT` into the runner's stop
event. The active provider process group is terminated, the attempt and
`interrupted` state are persisted, and a later backend restart resumes from
that state. The CLI records the actual signal and exits with the conventional
`128 + signal` status instead of converting interruption into process success.
A signal received during retry waiting preserves the absolute retry boundary.

Agent process exit zero means only that Brunner has a current terminal provider
result that can be evaluated. It does not mean the candidate passed the
benchmark. Timeout, provider failure without a terminal result, interruption,
and other incomplete pipeline states exit nonzero.

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

Campaign backends must declare `agent_isolation = "container"`.
`ContainerBackend` bind-mounts the trial into an OCI runtime.
`KubernetesBackend` creates a PVC, stages the trial through a helper pod,
creates a Job, and recovers files through reader pods. Helper pods explicitly
use `/tmp` as their working directory so an image working directory beneath
`/brunner/trial` cannot create unwritable paths when the trial PVC is mounted.
Submission is idempotent across ambiguous backend responses: a Kubernetes
retry adopts an existing labeled Job before considering staging, and an OCI
retry adopts the deterministic named container. Kubernetes records completed
staging on the PVC so a retry after staging but before Job creation does not
copy the trial again. Backend objects do not keep process-local handle
registries; persisted trial/backend state and remote labels are the recovery
sources of truth after an orchestrator restart.

The backend workload deadline is the agent hard deadline plus
`backend_shutdown_grace_seconds`; the outer backend therefore does not kill
the runner at the exact instant the runner must persist timeout and accounting
artifacts. Container and Kubernetes resource names include a digest of the
caller-owned workload identity and trial path, preventing normalization or
truncation collisions.

`WorkloadSpec` carries independent CPU, memory, and ephemeral-storage request
and limit fields. Kubernetes renders them independently, allowing a low
scheduler reservation and a higher burst ceiling instead of forcing
Guaranteed QoS by setting requests equal to limits. Legacy `cpu`, `memory`,
and `storage` values are Kubernetes request-and-limit shorthands when neither
explicit side overrides them, which preserves existing callers. OCI runtimes
have no scheduler-request concept, so the container backend applies only
explicit CPU and memory limits or the legacy values as limits. Ephemeral
storage settings are Kubernetes-only. GPU counts remain equal requests and
limits because Kubernetes extended resources are not overcommitted.

Kubernetes distinguishes connectivity failures from rejected requests and
workload failures. The agent writes a compact pipeline summary to Kubernetes'
termination log. Inspection treats that summary, the container signal, and
termination reasons such as `OOMKilled` as authoritative even if the Job says
`Complete` or the recorded container exit code is zero. Terminal warning
events such as `Evicted` likewise override an otherwise successful Job. It
reports pending PVCs, inspects terminated init/main containers, preserves
previously recovered workload logs, and captures terminal Job and Pod events
in the persisted backend snapshot before cleanup. It also includes Kubernetes
warning events for pending storage and failed artifact readers. It retries
artifact readers,
excludes failed reader nodes when rescheduling, resumes partial files by byte
offset, and verifies every SHA-256. Before helper creation and final cleanup,
Brunner finds stale stager and reader pods by workload/role labels and waits
for their deletion. Final cleanup likewise waits for Job and eligible PVC
deletion; connectivity loss leaves campaign cleanup pending rather than
silently leaking resources. A failed workload's PVC is retained until artifact
collection succeeds.

Failed Kubernetes Jobs whose agent process was interrupted by a signal,
eviction, node loss, OOM termination, or another retryable infrastructure event
can be relaunched against the same staged PVC. This classification does not
depend on a nonzero container exit code. Restart Jobs use deterministic
generation names, so an ambiguous restart response is adoptable. Deadline
expiry, terminal provider/configuration failures, and container configuration
failures are not retried. The campaign bounds automatic restart generations
with `infrastructure_max_restarts`.

## Campaign State

The normative failure taxonomy, operation matrix, resource-ownership rules, and
fault-injection requirements are defined in
[`failure-model.md`](failure-model.md). Every external operation must translate
failure into a durable record before returning control to campaign
reconciliation.

Campaign trial IDs are supplied explicitly by the benchmark. They are not
derived from provider, model, effort, or a run count. `campaign.json` records
the contract identity, append-only trial list, handles, snapshots, collection
attempts, evaluation results, outcomes, and recent events. Each completed entry
separates:

- `pipeline`: runner status, terminal-result availability, interruption signal,
  and infrastructure classification
- `benchmark`: whether trusted evaluation ran and whether it succeeded
- `outcome`: overall campaign result
- `failure_class`: `infrastructure` or `benchmark` when the outcome failed
- `failure`: canonical operation, domain, reason, disposition, retryability,
  cleanup obligation, and diagnostics
- `failures`: bounded append-only history of prior failure records

Every initialize, step, and continuous run holds an exclusive operating-system
lock on `campaign.lock`. A second orchestrator fails immediately with the
recorded PID and hostname instead of racing state writes or submissions. The
kernel releases the lock automatically when its process exits or crashes; the
lock file's owner record is diagnostic and is not itself treated as proof that
an orchestrator is alive.

Each campaign-state transition atomically replaces both `campaign.json` and
`campaign.json.bak`. If the primary JSON is unreadable, initialization loads
the last valid backup and records an explicit state-recovery failure and event.

Campaign reconciliation:

- Adds new caller-supplied trial IDs without invalidating existing state
- Treats an existing matching ID as already known, regardless of list order
- Rejects only an ID reused with conflicting execution attributes
- Submits only up to plan and backend capacity
- Persists a `submitting` phase before backend side effects and immediately
  persists the returned or adopted handle
- Resumes from persisted handles
- Pauses individual steps on backend connectivity loss; `run()` waits and
  resumes without changing remote workloads
- Collects artifacts for both successful and failed workloads
- Recovers interrupted collection and evaluation phases after a crash
- Counts a collection attempt only after the backend transfer returns or
  reports a non-connectivity failure; an orchestrator crash or connectivity
  pause while `collect()` is in progress does not consume the retry limit
- Relaunches retryable infrastructure failures against existing persistent
  agent state with a bounded restart count
- Retains an append-only retry history with each generation's backend snapshot,
  including Pod and Job events captured before that generation is deleted
- Retries interrupted artifact transfers with a bounded, configurable policy
- Uses the same persisted cleanup transition for initial and resumed cleanup,
  including identical completion and retry events; non-connectivity cleanup
  failures remain `cleanup_pending` instead of becoming a dead manual state
- Keeps integrity and evaluation failures durable instead of retrying them
- Continues healthy running or pending trials when another needs attention
- Flags a trial the backend still reports as pending or running past
  `trial_timeout_seconds`, which defaults to the backend workload deadline
  plus `trial_timeout_margin_seconds`, while continuing to inspect it until it
  reaches a terminal state
- Waits indefinitely for an unreachable backend by default, preserving remote
  lifecycle state across orchestrator sleep or network loss; deployments may
  set `max_pause_seconds` to require manual attention after a bounded interval
- Bounds evaluation with `evaluation_timeout_seconds`, shared as one budget
  across reference validation, the evaluator, and every assessment;
  reconciliation is sequential, so an unbounded evaluator would block every
  other trial
- Keeps an overdue trial's backend slot reserved while the backend still
  reports its workload as pending or running, so flagging it cannot let the
  campaign exceed `max_parallel`
- Does not clean up when recovery fails
- Runs trusted evaluation after verified collection only when the runner
  produced a current terminal provider result
- Collects diagnostics but records `benchmark.status = "not_run"` for an
  interrupted or incomplete agent pipeline
- Runs the configured standard qualitative review and domain assessments after
  deterministic evaluation
- Regenerates `index.html` after each transition with live elapsed time,
  backend warnings, usage, timing, and report links
- Treats dashboard and run-report generation as non-authoritative
  presentation; reporting failure is recorded but cannot block cleanup
- Converts unknown persisted phases and unexpected backend exceptions into
  explicit attention states rather than silently leaving the campaign running
- Records backend capacity exhaustion as a visible scheduler wait
- Recovers an unreadable primary campaign state from the last atomic backup and
  records that recovery in campaign state

Campaign trials contain no environment passthrough. OCI credential variables
are configured on `ContainerBackend` and inherited by name without including
their values in command arguments. Kubernetes credentials are represented only
as Secret name/key references. Explicit non-secret proxy or certificate
settings may be configured on a backend profile; no environment values are
stored in campaign state.
