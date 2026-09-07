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
   up trial resources, and publishes checksummed result snapshots for local
   synchronization.

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
| Campaigns | Controller, ConfigMap lock, temporary state/results PVCs, archive synchronization/restoration, local monitor, verified retirement | Trial matrix and deployment profile |

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
after the selected evidence has been collected. Normally the dossier includes
Sterling's deterministic evaluator result. If the agent pipeline never produces
a terminal provider result, Brunner records deterministic evaluation as
`not_run` and still runs assessments whose
`run_if_evaluation_failed` policy is enabled. The reviewer receives the same
output schema that Brunner later uses to validate the response. This
post-collection review does not rerun deterministic scoring or require the raw
evaluator-only dataset.

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
outer sandbox: it mounts the selected collected trial subdirectory read-only,
a separate output subdirectory read/write, has no service-account token, and
receives provider-only egress. Codex therefore
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
non-gating by default and runs on failed or unavailable deterministic
evaluations so it can diagnose the failure and describe partial agent work;
both behaviors are configurable.

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

Brunner installs a campaign-scoped Squid Deployment, ConfigMap, Service, and
NetworkPolicy before staging. Its labels and ingress selectors include the
campaign identity, so one campaign cannot use or mutate another campaign's
proxy. Squid permits HTTPS `CONNECT` only to
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
NetworkPolicies immediately before Job creation. The default `strict`
isolation mode refuses to launch
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
Subscription boundaries with a provider reset timestamp wait until that
boundary. Authentication, authorization, unavailable model, invalid request,
and permanent credit exhaustion such as Claude's `out_of_credits` /
`credits_required` rejection terminate immediately.
Deterministic provider-launch validation failures are terminal as well. Claude
receives a provider-specific copy of the generated final-response schema with
the top-level Draft 2020-12 `$schema` declaration omitted because current
Claude Code releases reject that meta-schema URI locally. The canonical staged
schema remains unchanged and is still used for prompt generation and Brunner's
own validation.

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
evaluation. `KubernetesBackend` creates a trial PVC and an agent/evaluator Job
whose first init container copies the prepared trial directly from the control
PVC. The stager resumes or discards only its own `.brunner-part` files,
verifies every size and SHA-256 from the stage report, and atomically publishes
each completed file. It refuses to overwrite changed or unexpected destination
content, so ambiguous recovery cannot destroy candidate work. It never streams
trial bytes through `kubectl` or the controller process. A fully staged PVC is
reused on agent restart.

Only verified PVCs receive staged, challenge, workload, and runtime-protocol
annotations. Submission is idempotent across ambiguous responses, and a retry
adopts an existing Job or PVC only when those identities match exactly.
Backend objects do not keep process-local handle registries; persisted
trial/backend state and remote labels are the recovery sources of truth after
an orchestrator restart.

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
pending PVCs, preserves logs from every Job Pod, and captures Job and Pod
events before terminal cleanup or orchestrator-forced termination when
available. Event RBAC, expiry, or transient failures become warnings and never
block artifact recovery.

Artifact collection runs as a separate durable Kubernetes Job that mounts the
trial PVC read-only and the control PVC once, read/write. The prepared baseline
and collection destination are separate directories on that control mount; the
same PVC is not mounted a second time. It resumes partial files,
verifies every SHA-256, and hard-links unchanged staged files from the prepared
trial instead of copying them again. The controller only submits and observes
this Job; Kubernetes API connectivity loss cannot interrupt the data transfer.
Final cleanup waits for collection Jobs and workload Jobs. A failed
provider-error workload's PVC remains retained after artifact collection when
retained-session continuation is enabled; successful and non-resumable
workloads release their eligible PVCs normally.

Failed Kubernetes Jobs whose agent process was interrupted by a signal,
eviction, node loss, OOM termination, or another retryable infrastructure event
can be relaunched against the same staged PVC. This classification does not
depend on a nonzero container exit code. Restart Jobs use deterministic
generation names, so an ambiguous restart response is adoptable. Deadline
expiry, terminal provider/configuration failures, and container configuration
failures are not retried. The campaign bounds automatic restart generations
with `infrastructure_max_restarts`.

Provider-error continuation is separate from automatic infrastructure retry.
The laptop writes an immutable request to a campaign continuation ConfigMap;
the fenced controller validates the terminal campaign entry and launches a
continuation generation against the original PVC. The request is excluded from
the workload digest because it changes retry authority, not the pinned
challenge, image, command, resources, secrets, or evaluator. The agent reopens
the terminal runner state, preserves prior attempts, resets its deadline, and
raises the attempt ceiling by exactly one. A strict-resume marker prevents
missing provider sessions from falling back to a new session.

## Cluster Campaign Control

The normative failure taxonomy, operation matrix, resource-ownership rules, and
fault-injection requirements are defined in
[`failure-model.md`](failure-model.md). Every external operation must translate
failure into durable state before returning control to reconciliation.

The laptop is only a submit, status, archive-sync, local-monitor, and explicit
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
continuation request ConfigMap
```

The preparation Job has no service-account token. It has explicit CPU and
memory requests and limits, loads the benchmark and campaign modules from the
immutable controller image, materializes and stages the challenge, creates
durable trials on the control PVC, and writes a campaign-digest marker. The
controller directly observes the marker, a durable failure record, and the
preparation Job's terminal condition. A failed or timed-out preparation
therefore becomes visible campaign attention instead of an indefinite init
wait.

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
not an implicit takeover. Every Kubernetes mutation, state write, result copy,
and dashboard write is fenced before and after the side effect; loss of the
lock terminates reconciliation rather than allowing a stale controller to keep
writing. If the controller Pod is evicted, restarted, or moved to another node,
it reloads `campaign.json` and its atomic backup from the control PVC and adopts
existing Jobs and PVCs from persisted handles. Laptop sleep or network loss has
no effect on this loop.

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

Collection is a resumable, checksum-verified PVC-to-PVC Kubernetes Job whose
destination is the campaign control PVC rather than a laptop directory. Trial
PVCs are deleted only after collection, assessment, and reporting have
produced durable results.

Qualitative and domain assessments run in separate trusted Jobs. They mount the
selected collected trial subdirectory read-only, a separate writable
assessment-output subdirectory, and an empty `/tmp`; they cannot modify
authoritative evaluator evidence or traverse into another trial or
campaign-control state. The controller validates assessment output and merges
it into the collected trial only after the Job succeeds. Only model-review Jobs
receive reviewer Secret references and the campaign's managed-proxy
environment; the controller receives neither provider nor reviewer
credentials. Assessment Pods have no service-account token and their
NetworkPolicy permits egress only to that campaign's numeric Squid ClusterIP.

When durable campaign state changes, the controller copies authoritative state
and newly published trial results to the results PVC, regenerates the dashboard,
and creates `result-manifest.json` with every archive file's path, size, and
SHA-256. A state digest excludes the poll-only `updated_at` field, so an idle
controller does not repeatedly hash a large result tree. It annotates the
results PVC and status ConfigMap with the manifest identity. At terminal
campaign state the manifest is marked terminal and the controller stops
mutating the result tree. The status ConfigMap remains a small observable
summary; it is not the authoritative campaign record.

`campaign-sync` starts a short-lived, tokenless, read-only helper Pod with
deny-all ingress and egress. It reads files in bounded chunks, resumes local
`.part` files by byte offset, detects a snapshot that changed during transfer,
verifies every SHA-256, and atomically publishes the matching manifest last.
The local directory is therefore either the previous valid archive or the new
valid archive, never a claimed partially downloaded snapshot. Active snapshots
can be synchronized repeatedly; only a terminal snapshot is eligible for
retirement or restoration.

`campaign-monitor` is a local static server over a verified archive. It needs
neither a benchmark import nor cluster connectivity, so retired results remain
browsable indefinitely.

`campaign-retire` synchronizes once more, requires a terminal resumable archive,
checks the exact manifest identity against the results PVC and live controller
status, and only then deletes the Deployment, Jobs, Pods, NetworkPolicies, RBAC,
status/lock ConfigMaps, and every campaign-labeled PVC. This releases requested
cluster storage quota; the local verified archive becomes the durable campaign
record. Shared reference and resource-cache claims are benchmark-owned and are
not deleted or copied into the archive.

`campaign-submit --resume-from ARCHIVE` reverses that boundary. It validates
the archive's checksums, terminal state, benchmark, contract, evaluator, backend,
trial identities, and standardized paths before creating remote resources. A
short-lived tokenless helper Pod with deny-all networking restores
manifest-listed results, authoritative campaign state, and each trial's compact
metadata into fresh PVCs. Preparation and the controller start only after
restore completes. Reconciliation then preserves historical completed IDs and
stages only newly appended IDs. Existing differing remote content is never
overwritten silently.

Archive restoration uses the versioned `brunner-archive-stream-v1` protocol
over one bidirectional `kubectl exec -i` connection per successful session.
The sender provides the existing archive manifest once; the helper reconciles
files on the PVC and requests only missing data. Chunks are bounded at 4 MiB,
checksum-checked, flushed, and fsynced before acknowledgement. Each completed
file is verified against its manifest SHA-256 and published without replacing
an existing destination. File information, writes, and commits do not open
separate Kubernetes connections.

The control PVC's `.brunner-restore.json` binds restore progress to the campaign
and archive-manifest hash. The checkpoint records verified files/bytes and the
current partial offset, but it is not trusted as proof that files still match.
Reconnecting reconciles existing files locally in the helper, without replaying
per-file Kubernetes calls. Existing valid files and legacy `.brunner-part`
uploads are reusable; a partial prefix must match the local source before it
is extended. An interrupted chunk, lost acknowledgement, or interrupted commit
is resolved from the actual files. Conflicting committed content, unsafe paths,
malformed checkpoints, and storage failures are terminal errors.

The single campaign archive-writer Pod uses a process lock on its local
`emptyDir`, not on the network PVC, to serialize receivers. A dead receiver
releases that lock. Reconnects adopt the same compatible Pod. Replacing a legacy
or terminated helper requires confirmed normal Pod deletion; Brunner never
force-deletes it or runs a second writer against the same claims. A completed
restore can only be verified read-only, so a competing reconnect cannot reset
its checkpoint while another submit starts preparation.

Transport failures reconnect with bounded exponential backoff. The streaming
inactivity timeout is separate from the short-command timeout; continued
transfer or verification progress does not consume a fixed whole-upload
deadline. Exhausting the no-progress retry budget or cancelling the submit
leaves the helper, deny-all NetworkPolicy, and PVC data available for the same
`--resume-from` command. Diagnostics include acknowledged upload bytes,
verified files/bytes, and reconnects.

The helper copies compact campaign state and historical trial metadata directly
from results to control. It publishes the results manifest and restore marker
only after verification, then marks the checkpoint complete. Preparation also
checks the checkpoint, independently of the submit client, before touching
historical state. Helper cleanup occurs only after successful restoration;
cleanup errors explicitly report that restoration completed and submission
needs another attempt. No cleanup exception can replace an upload failure.
The stream protocol is independent of the agent runtime protocol: using it
requires updated local Brunner and controller/archive-writer images, not
rebuilding unchanged agent or evaluator images.
