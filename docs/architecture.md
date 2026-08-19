# Architecture

## Lifecycle Boundary

Brunner separates benchmark execution into six trust and responsibility
stages.

1. **Stage**: optionally materialize resources into a temporary challenge
   copy, copy only challenge-visible files, render output requirements from
   the canonical contract, and record challenge/contract digests.
2. **Run**: execute a provider in the staged workspace with durable state,
   retries, continuation, finalization, timeout, and structured final output.
3. **Evaluate**: on Sterling, validate the submission contract and run trusted
   benchmark-specific scoring against optional verified references on the
   same trial PVC.
4. **Collect**: preserve logs, evaluator results, and selected artifacts
   through a resumable, checksum-verified transfer.
5. **Assess**: in separate trusted Sterling Jobs, build compact evidence
   dossiers and run the configured standard qualitative review plus any
   domain-specific, schema-bound command or model reviews.
6. **Campaign**: a cluster-resident controller schedules the matrix,
   reconciles backend state, collects outputs, starts assessment Jobs, cleans
   up trial resources, publishes a monitor, and finalizes a result bundle.

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
| Campaigns | Controller, ConfigMap lock, durable state/results PVCs, monitor, retrieval | Trial matrix and deployment profile |

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

When a challenge defines a materialization command, Brunner's trusted cluster
preparation Job first copies the source challenge into a fresh temporary
directory. It runs the command there, rechecks symlinks and forbidden names,
then uses that materialized copy for prompt rendering, schema generation,
staging, and the challenge digest. The source checkout in the controller image
is never modified. Without a command, direct-copy staging is unchanged.

Materialization is part of trial creation, before the Kubernetes backend
receives a workload. Materialized resources are therefore candidate-visible,
included in `challenge_sha256`, and copied to Sterling storage with the rest
of the trial. They are not added to an agent image.

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
after the Sterling evaluator result and selected evidence have been collected.
The reviewer receives the same output schema that Brunner later uses to
validate the response. This post-collection review does not rerun deterministic
scoring or require the raw evaluator-only dataset.

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

Model reviewers run without session persistence. The assessment Job is the
outer sandbox: it mounts only the selected collected trial subdirectory, has no
service-account token, and receives provider-only egress. Codex therefore
bypasses its unsupported nested user-namespace sandbox; Claude exposes and
explicitly authorizes only read, glob, and grep tools while denying interactive
permission requests. Reviewers execute in a temporary workspace with
`BRUNNER_*` environment variables removed. Candidate provider and model fields
are omitted from the dossier and matching structured fields are redacted from
copied JSON evidence. The result records that provider family may still be
inferable from transcript structure. The temporary reviewer workspace is a
second copy of the selected evidence; large benchmarks should select compact
review inputs rather than whole datasets or trajectory trees.
Reviewer-authored milestones are sequence-ordered but carry null timestamps;
the copied Brunner timing accounting remains the sole source for elapsed time
and interval timestamps.

Reviewer CLI state uses a separate provider home under the trusted assessment
work root rather than the operating system temporary directory. Brunner removes
that home after the review and excludes it from result publication even if an
assessment process terminates before cleanup.

Brunner also packages
`https://brunner.dev/schemas/assessment-common.schema.json` as an optional
schema resource. Benchmarks may reference its generic `evidence` and
`criterion` definitions while retaining their own rating vocabulary and
domain-specific output structure. Provider schemas inline only referenced
common definitions and their dependencies; the typeless common-schema document
is never embedded as a `$defs` entry. Provider-specific preflight failures are
terminal assessment configuration errors and create no reviewer attempt.
The Codex projection also closes every object, requires all declared
properties, and removes unsupported assertion keywords. Brunner still validates
the response against the unchanged benchmark schema, which remains the single
source of truth.

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
- Only the selected provider's credential references

Challenge materialization runs earlier as trusted cluster-side benchmark code.
Brunner gives it the temporary challenge root, does not pass trial,
reference, evaluation, assessment, or submission `BRUNNER_*` paths, and does
not copy trusted materials into the temporary challenge. The optional
`BRUNNER_RESOURCE_CACHE` value is passed through as a location only; download,
locking, checksum, extraction, conversion, and cache validity semantics remain
benchmark-owned.

Candidate and reviewer processes execute inside Kubernetes workload isolation
boundaries and without inherited user configuration or external tool
connections. Codex uses `--dangerously-bypass-approvals-and-sandbox` because
Sterling is the enforced outer sandbox and does not provide nested user
namespaces. This also keeps initial and resumed candidate invocations
compatible because `codex exec resume` does not accept `--sandbox`. Claude
bypasses its interactive permission system and likewise relies on the outer
Kubernetes isolation boundary. Runner-owned metadata, backend, evaluation,
assessment, usage, and status paths are snapshotted around every attempt. Any
mutation is restored and terminates the trial as a provider error.

Claude candidate runs therefore require an outer isolation boundary. Campaign
construction requires Kubernetes-backed agent isolation and trusted
evaluation; Brunner does not support host-process or local-container campaigns.

Kubernetes candidate and helper pods do not mount service-account tokens. They
run as UID/GID 1000 with the runtime-default seccomp profile, all Linux
capabilities dropped, privilege escalation disabled, and a read-only container
root. The trial PVC and an ephemeral `/tmp` volume are their only writable
mounts during staging and execution. Artifact readers mount the trial PVC
read-only so collection cannot alter remote results. Agent and artifact-reader
images must support this non-root contract. Service-link environment injection
is disabled.

Brunner installs a namespace-scoped Squid Deployment, ConfigMap, Service, and
NetworkPolicy before staging. Squid permits HTTPS `CONNECT` only to
`.openai.com`, `.openai.azure.com`, `.anthropic.com`, and `.claude.ai`;
everything else is denied. Squid alone may query the selected cluster DNS Pods
and open outbound TCP 443 connections.

Workload-scoped NetworkPolicies allow pipeline Pods to reach only Squid on TCP
3128. Brunner reads the Service's numeric ClusterIP and injects that address
into the agent's proxy variables, so pipeline Pods receive no DNS egress at
all. Pipeline, stager, and artifact-reader Pods deny ingress; stager and
artifact-reader Pods also have no egress. Generic environment mappings cannot
override proxy variables. The Squid image and ACL configuration digest are
recorded in the Job and persisted backend handle; resume and restart reject
identity drift.

Because Kubernetes policies are additive, Brunner lists existing namespace
NetworkPolicies before creating the staging helper and again immediately
before Job creation. The default `strict` isolation mode refuses to launch
when another policy with nonempty ingress or egress rules selects any Brunner
workload Pod. `controlled-egress` mode is available for an administrator-owned
personal namespace whose baseline policy permits ingress: it tolerates
additive ingress while continuing to reject every additive egress rule that
selects a pipeline or helper Pod. Brunner still renders its own empty ingress
rules in that mode, but does not claim exclusive ingress because the namespace
policy remains additive.

The isolation mode is recorded in the Job and backend handle; adoption and
restart reject mode drift. A controlled namespace must limit pod and
NetworkPolicy administration to trusted principals. Shared or multi-tenant
namespaces require `strict` mode plus RBAC or admission controls that prevent
other principals from changing matching policies after validation. Sterling
must use a CNI that enforces Kubernetes NetworkPolicy; successful API creation
alone does not prove packet-level enforcement.

Each remote Job runs `python -m brunner.agent_cli` in an agent init container.
After it produces a terminal provider result, Kubernetes starts the trusted
evaluator as the Job's main container. Both use the trial PVC, but only the
evaluator mounts the separately provisioned reference PVC, read-only. Provider
Secrets and proxy settings are present only in the agent init container.
Campaign configuration maps providers to existing Secret name/key references,
and the workload factory selects only the current trial's provider mapping.
Brunner never reads Secret values and never creates or updates Secrets from
laptop environment variables. A missing Secret or key prevents the Pod from
starting and is classified as a terminal infrastructure/configuration failure.
Secret values never enter campaign state, workload identity, trial contents,
or Pod manifests.

Agent, evaluator, and artifact-reader images are immutable digest references by
default. Every image reports or validates Brunner's runtime protocol before it
is trusted: the agent checks staged metadata, the evaluator checks its
serialized specification, and stager/readers answer the remote protocol
probe. Mutable tags are available only through the explicit
`require_image_digests=False` testing escape hatch.

The evaluator image contains benchmark-specific scoring code and Brunner's
evaluator helper API. `python -m brunner.evaluation_cli` validates the staged
contract, candidate submission, reference bundle, evaluator result, and report
paths before recording a terminal evaluation summary. The evaluator bootstrap
and all benchmark/reference validation commands run from a fresh evaluator-only
`/tmp` directory with safe Python path settings, never from the
candidate-controlled workspace or the reference mount. The orchestrator does
not execute evaluator code.

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
The provider leader is not the process-group lifetime boundary. If it exits
while a child command remains live, Brunner waits for that non-zombie process
to finish rather than treating it as an orphan. Linux process-table inspection
distinguishes live members from zombies, and the agent process reaps zombies
that have been adopted by it. Once the group has contained no live process for
a short drain grace, unmatched provider activity and unguarded benchmark
activity are released as stale and the attempt closes. A benchmark activity
with a still-live guard remains authoritative. The hard trial deadline remains
the final safeguard and terminates descendants that do not finish.

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

Brunner keeps monitoring the provider's process group after the leader has
been reaped because abandoning a live descendant would let it write into the
workspace while artifacts are being collected. Zombie-only groups do not
count as live work. Reaping the leader frees its PID, so a recycled PID could
in principle make the group check report a stranger's group. That residual
race is accepted: losing the descendant-liveness guarantee is the worse
failure.

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

Campaign backends must declare container agent isolation and Kubernetes trusted
evaluation. `KubernetesBackend` creates a PVC, stages the trial through a
helper pod, creates the durable agent-then-evaluator Job, and recovers selected
files through reader pods. Helper pods explicitly
use `/tmp` as their working directory so an image working directory beneath
`/brunner/trial` cannot create unwritable paths when the trial PVC is mounted.
Submission is idempotent across ambiguous backend responses. Before copying,
the stager clears an incomplete PVC, then verifies the complete remote
workspace inventory and challenge digest. Only verified PVCs receive staged,
challenge, workload, and runtime-protocol annotations. A retry adopts an
existing Job or PVC only when those identities match exactly. Backend objects
do not keep process-local handle
registries; persisted trial/backend state and remote labels are the recovery
sources of truth after an orchestrator restart.

The backend workload deadline includes the agent hard deadline,
`backend_shutdown_grace_seconds`, and the evaluator timeout. The outer Job
therefore survives long enough for both terminal agent persistence and trusted
evaluation. Kubernetes resource names include a digest of a random trial
resource ID persisted in trial metadata. Moving a campaign directory therefore
does not change remote identity. Legacy trials derive the ID from immutable
metadata rather than the orchestrator's absolute path.

Campaign state pins one materialized challenge digest, the trusted evaluation
and reference identity, and each trial's canonical workload digest. The
workload digest covers commands, immutable image references, timeouts,
resources, labels, and trusted evaluation. Existing trial IDs remain
append-only, but changing what an already-created trial means requires a new
ID. If the image comes from `KubernetesProfile.agent_image`, Brunner first
normalizes it into the workload so the effective image is hashed. Workloads
cannot supply Brunner's ownership, role, or restart labels.

`WorkloadSpec` carries independent CPU, memory, and ephemeral-storage request
and limit fields. Kubernetes renders them independently, allowing a low
scheduler reservation and a higher burst ceiling instead of forcing
Guaranteed QoS by setting requests equal to limits. Legacy `cpu`, `memory`,
and `storage` values are Kubernetes request-and-limit shorthands when neither
explicit side overrides them, which preserves existing callers. Evaluator
requests and limits are carried separately in the trusted evaluation spec.
GPU counts remain equal requests and limits because Kubernetes extended
resources are not overcommitted.

Before launching, Kubernetes preflight checks API access, required RBAC,
reference-PVC identity, runtime images, managed-proxy rollout, overlapping
egress policies, and ResourceQuota headroom. Capacity accounts for Jobs, Pods,
PVCs, storage, CPU, memory, ephemeral storage, extended resources, and
NetworkPolicies. Init-container resources use Kubernetes' effective
`max(init, sum(regular))` scheduling rule, and quota capacity is combined with
the configured parallel limit.

Kubernetes distinguishes connectivity failures from rejected requests and
workload failures. The agent and evaluator each write compact summaries to
their Kubernetes termination logs. Inspection treats those summaries,
container signals, and reasons such as `OOMKilled` as authoritative even when
Kubernetes records an inconsistent exit code. Job-level backoff is disabled:
each Brunner workload generation runs exactly one Pod, and only Brunner may
start a replacement after classifying the previous result. Inspection still
evaluates all Pods in creation order when adopting legacy or externally
modified Jobs. Missing Jobs with intact PVCs are retryable infrastructure
failures; missing Jobs and PVCs are terminal storage loss. Brunner reports
pending PVCs, preserves logs from every Job Pod, and
captures terminal Job and Pod events before cleanup when available. Event
RBAC, expiry, or transient failures become warnings and never block artifact
recovery. It also includes Kubernetes
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

## Cluster Campaign Control

The normative failure taxonomy, operation matrix, resource-ownership rules, and
fault-injection requirements are defined in
[`failure-model.md`](failure-model.md). Every external operation must translate
failure into durable state before returning control to reconciliation.

The laptop is only a submit, observe, port-forward, retrieve, and explicit
retirement client. It never calls the campaign reconciliation engine.
`campaign-submit` creates one campaign control plane in Sterling:

```text
ServiceAccount + namespace Role/RoleBinding
control PVC (ReadWriteMany)
results PVC (ReadWriteMany)
trusted preparation Job
one-replica Recreate controller Deployment
controller lock ConfigMap
status ConfigMap
ClusterIP monitor Service
```

The preparation Job has no service-account token. It loads the benchmark and
campaign modules from the immutable controller image, materializes and stages
the challenge, creates durable trials on the control PVC, and writes a
campaign-digest marker. The controller does not start reconciliation until
that marker exists.

The submitted control-plane manifests carry the exact agent, artifact-reader,
proxy, controller, and evaluator image identities as trusted environment
metadata. Preparation, controller, and assessment processes apply those image
fields immediately after importing the benchmark modules and before validating
the campaign or evaluation contract. This avoids a self-referential controller
build: the controller image may contain an older embedded digest, while the
submitted immutable digest remains the authoritative runtime identity and part
of the validated campaign digest. No other benchmark or campaign fields can be
overridden through this mechanism.

The controller is the only campaign component with Kubernetes API credentials.
A dedicated ConfigMap lock, renewed by pod UID through Kubernetes
`resourceVersion` compare-and-swap, prevents two live controller processes from
reconciling the same campaign. A replacement may take the lock only after the
recorded renewal duration expires. Malformed lock state is an integrity failure,
not an implicit takeover. If the controller Pod is evicted, restarted, or moved
to another node, it reloads `campaign.json` and its atomic backup from the
control PVC and adopts existing Jobs and PVCs from persisted handles. Laptop
sleep or network loss has no effect on this loop.

The controller writes:

```text
control PVC:
  campaign.json
  campaign.json.bak
  trials/<test-id>/

results PVC:
  index.html
  trials/<test-id>/
  campaign.json
  result-manifest.json
```

Campaign trial IDs are supplied explicitly by the benchmark. They are not
derived from provider, model, effort, or a run count. Reordering the configured
trial list does not change campaign identity. Adding a new ID reopens a
finalized campaign, invalidates the old result manifest, and preserves already
completed trial results. Reusing an ID with changed execution attributes is
rejected.

Each state transition atomically replaces both `campaign.json` and
`campaign.json.bak`. If the primary JSON is unreadable, initialization loads
the backup and records an explicit state-recovery failure and event. The
reconciliation engine persists phases before side effects, adopts existing
remote resources, pauses on Kubernetes connectivity loss, resumes interrupted
collection and evaluation, bounds infrastructure restarts, respects
ResourceQuota capacity, captures terminal Pod/Job Events before cleanup, and
keeps integrity/configuration failures as explicit attention states.

Collection is resumable and checksum verified, but its destination is the
results PVC rather than a laptop directory. Trial PVCs are deleted only after
collection, assessment, and reporting have produced durable results.

Qualitative and domain assessments run in separate trusted Jobs that mount only
the selected collected trial subdirectory from the control PVC plus an empty
`/tmp`; they cannot traverse into another trial or campaign-control state. Only
model-review Jobs receive reviewer Secret references and managed-proxy
environment; the controller receives neither provider nor reviewer
credentials. Assessment Pods have no service-account token and their
NetworkPolicy permits egress only to Brunner's numeric Squid ClusterIP.

At terminal campaign state, the controller copies authoritative state to the
results PVC and creates `result-manifest.json` with every result file's path,
size, and SHA-256. It annotates the results PVC with the manifest identity and
then stops mutating the result tree. The status ConfigMap remains a small
observable summary; it is not the authoritative campaign record.

`campaign-retrieve` starts a short-lived, tokenless, read-only helper Pod with
deny-all ingress and egress. It reads files in bounded chunks, resumes local
`.part` files by byte offset, verifies every SHA-256, and atomically publishes
completed files. Retrieval remains possible after controller retirement
because the results PVC annotations carry the manifest identity.

`campaign-delete` removes controller resources and the control PVC. It
preserves the results PVC unless `--delete-results` is explicit. This makes
result deletion a separate irreversible decision after verified retrieval.
