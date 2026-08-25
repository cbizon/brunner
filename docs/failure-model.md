# Failure Model

Brunner treats failure handling as part of the benchmark result protocol. Every
operation boundary must either complete or translate its failure into durable
state before returning control to its caller. A raw exception, an unknown
lifecycle phase, or an indefinitely invisible wait is a Brunner defect.

This document is the normative failure-state contract. Architecture and backend
changes must update this matrix and add fault-injection coverage for each new
external operation.

## Failure Record

The canonical failure record has these fields:

| Field | Meaning |
| --- | --- |
| `operation` | The operation that failed, not the caller that noticed later |
| `domain` | The owner of the failure |
| `reason` | Stable machine-readable reason code |
| `message` | Human-readable diagnostics |
| `disposition` | `retry`, `wait`, `candidate_failed`, `attention`, or `terminal` |
| `retryable` | Whether Brunner may repeat this operation automatically |
| `cleanup_required` | Whether side effects may still exist |
| `error_type` | Exception type when the record originated from an exception |
| `resource` | Exhausted or unavailable resource when known |
| `details` | Operation-specific evidence |
| `occurred_at` | UTC timestamp |

Failure domains are:

| Domain | Ownership |
| --- | --- |
| `candidate` | Candidate output or candidate-controlled workload behavior |
| `provider` | Requested model/provider execution and identity |
| `backend` | Kubernetes execution and observation |
| `integrity` | Trusted identity, checksum, isolation, or path invariant |
| `evaluation` | Deterministic evaluator or reference-validation infrastructure |
| `assessment` | Trusted qualitative reviewer or renderer infrastructure |
| `configuration` | Benchmark or deployment configuration |
| `orchestrator` | Controller bookkeeping, persistence, or cluster-local resources |
| `reporting` | Non-authoritative HTML/dashboard presentation |
| `cleanup` | Removal of backend resources after the result is known |

`failure_class` remains a compatibility summary. `benchmark` means the
candidate failed a valid benchmark operation. `infrastructure` means the
benchmark result is absent or indeterminate because a trusted component failed.
The complete failure record is authoritative.

## Required Boundary Behavior

Every boundary must be tested at five interruption points where applicable:

1. Before any side effect.
2. After a partial side effect but before a handle is persisted.
3. After operation success but before campaign state is persisted.
4. During cleanup.
5. After controller restart or ConfigMap lock handoff.

Unexpected `Exception` values at an external boundary become a durable
`orchestrator` failure. `KeyboardInterrupt`, `SystemExit`, and fatal process
termination are not converted into ordinary failures, but construction and
state publication must be atomic so they cannot expose partial state.

## Operation Matrix

| Operation | Principal failures and resources | Outer interpreter | Required disposition |
| --- | --- | --- | --- |
| Definition loading | Missing module, invalid schema, bad image/command | CLI or campaign initialization | Terminal `configuration`; no backend side effects |
| Materialization | Launch failure, timeout, output volume, disk/inodes, escaped descendants | Staging | Terminal `configuration` or `orchestrator`; delete temporary copy |
| Challenge staging | Symlink, forbidden name, render/schema/hash failure, source mutation, disk/inodes | Staging or trial creation | Terminal `integrity`/`configuration`; never publish partial workspace |
| Trial construction | Filesystem failure or interruption | Trial creation | Atomic publish; incomplete temporary trial must not occupy the requested ID |
| State persistence | Control-PVC disk/inodes, permission, serialization, Pod/node loss | Controller | Preserve previous valid state; stop new side effects if authoritative state cannot be written |
| Campaign lock | Concurrent controller, API loss, renewal timeout, stale holder, malformed lock state | ConfigMap with `resourceVersion` compare-and-swap plus side-effect fences | Only the current holder may reconcile; fence before and after external mutations; stop after the lock cannot be renewed within its duration; reject malformed state |
| Capacity/preflight | API loss, RBAC, quota, no nodes, mutable/incompatible images, bad reference claim | Scheduler/backend | Connectivity pause, terminal configuration, or visible quota `wait`; never an invisible running state |
| Credential reference | Absent Secret/key or malformed `secretKeyRef` | Kubernetes pod startup | Terminal container-configuration infrastructure failure; Brunner never reads or provisions Secret values |
| Submission | Partial NetworkPolicy/PVC/Job, interrupted PVC-to-PVC stage, corrupt remote copy, rejection, timeout, ambiguous response | Campaign submission reconciliation and stager init container | Resume partial staging; adopt only matching digests after ambiguity; possible side effects require cleanup |
| Scheduling/startup | Unschedulable, image pull, mount, secret, GPU/storage unavailable | Backend inspection | Typed backend failure; retry only transient infrastructure |
| Agent startup | Missing executable/config, corrupt trial state, permission/disk failure | Agent CLI and backend | Durable nonzero infrastructure result with diagnostics |
| Provider execution | Auth, subscription/rate limit, model substitution, deterministic CLI/schema validation, tool/sandbox denial, network, malformed terminal event | Runner/provider adapter | Retry transient service/network failures; terminate deterministic configuration failures; preserve requested/observed identity |
| Runtime resources | CPU, memory, ephemeral storage, PVC, PID/FD, token/context, deadline | Backend plus runner | Attribute to candidate only when a candidate-specific limit proves ownership |
| Output/timing capture | Log disk full, malformed event, output flood | Runner | Terminal provider parsing must not depend on optional diagnostic writes |
| Inspection | Connectivity, malformed JSON, deleted Job/PVC, multi-Pod retry history, missing Events | Backend and campaign | Pause on connectivity; restart missing Job with intact PVC; event loss is diagnostic-only |
| Retry/resume | Retry deadline, exhausted budget, stale session, unsupported resume | Runner or campaign | Absolute persisted retry time; bounded retries; terminal reason at exhaustion |
| Submission validation | Missing/invalid manifest, schema/path/size violation | Evaluator | `candidate_failed`; this is a valid benchmark result |
| Reference validation | Drift, missing trusted files, validator failure | Evaluator | `integrity` or `evaluation`; benchmark result indeterminate |
| Deterministic evaluation | Launch, timeout, crash, invalid result, runtime resource failure | Sterling evaluator and campaign | `evaluation`; never `benchmark` unless a valid evaluator reports candidate failure |
| Artifact collection | Transfer Job failure, oversized changed/new inventory, staged-file reuse failure, malformed inventory, checksum/path violation, control-PVC exhaustion | PVC-to-PVC collection Job and controller | Adopt or poll the durable Job after controller/API interruption, reuse unchanged staged files on the control PVC, omit declared/evaluated raw artifacts, use bounded diagnostics for incomplete oversized trials, retry terminal transport failure only |
| Result publication | Results-PVC exhaustion, oversized result, interruption, checksum/path violation | Controller | Omit unchanged challenge and assessment working copies; publish checksummed snapshots when durable state changes; do not clean trial storage until publication completes |
| Qualitative assessment | Provider quota/auth, timeout, invalid review, renderer failure, output merge failure | Trusted assessment Job and controller | Mount evidence read-only, write separately, validate before merge; `assessment`; required-review failure makes result indeterminate |
| Archive synchronization | Reader startup, laptop disconnect, partial file, changing remote snapshot, manifest/file checksum mismatch, local disk | Sync client | Download into a resumable sibling staging tree, preserve the prior local archive, and swap only after complete verification |
| Archive restoration | Writer startup, laptop disconnect, partial upload, changed destination, invalid historical state | Submit client | Require a terminal compatible archive; resume checksum-verified uploads; restore state before preparation/controller startup; never overwrite differing remote content |
| Campaign retirement | Final sync failure, nonterminal/nonresumable archive, stale controller status, deletion timeout | Retirement client | Verify remote bytes and exact terminal manifest before deletion; delete all campaign-owned workloads and PVCs only after verification |
| Reporting | Serialization, template error, disk full | Evaluation or campaign save | Record `reporting`; never block cleanup or replace the authoritative result |
| Cleanup | API loss, controller shutdown race, finalizer, deletion timeout, helper leak | Campaign cleanup reconciliation | Stop controller Pods before deleting Jobs, sweep replacement Pods, persist `cleanup_pending`, and retry; result remains authoritative |
| Aggregation | Unknown phase or contradictory fields | Campaign | Durable `orchestrator` attention; never silently report `running` |
| Restart recovery | Pod eviction, SIGTERM, node loss, crash between operations | Controller initialization | Recover only idempotent phases; preserve deadlines, handles, and cleanup obligations |

## Resource Ownership

Resource exhaustion is classified by ownership, not only by the operating
system reason:

| Resource event | Classification |
| --- | --- |
| Node eviction, node loss, control-plane outage | Retryable backend infrastructure |
| Brunner runner/provider process exceeds a shared pod limit | Infrastructure |
| Candidate process exceeds an explicitly isolated benchmark limit | Candidate failure |
| Shared pod `OOMKilled` without process ownership evidence | Infrastructure; retry is bounded |
| Control/results PVC full during bookkeeping | Orchestrator/integrity, not candidate |
| Candidate artifact exceeds an output-contract size limit | Candidate failure |
| Provider token/context/subscription exhaustion | Provider policy |
| Kubernetes quota or storage-class exhaustion | Backend wait or configuration |

The agent init container and evaluator main container have separate resource
envelopes and termination records. Pod-wide failures still require container
and event evidence before attributing exhaustion to either phase.

## Invariants

- Candidate failures require a valid terminal provider result and a trusted
  validation/evaluation decision.
- Trusted evaluator, reference, reviewer, reporting, and cleanup failures never
  become candidate failures.
- Integrity failures are never automatically retried as transport failures.
- Reporting is presentation only and cannot control cleanup or outcome.
- Cleanup remains durable until resources are deleted or an operator explicitly
  accepts retention.
- Unknown phases and malformed backend responses require attention; they never
  fall through to an unmarked running state.
- NetworkPolicy, challenge, workload, runtime, image, and reference identities
  must match before a remote resource is adopted.
- Candidate evaluation failure exits the evaluator container successfully; the
  benchmark result carries failure while the Kubernetes pipeline completes.
- Kubernetes Job backoff is zero. Brunner is the only component allowed to
  repeat a classified workload generation.
- Exceeding the campaign trial deadline terminates the live Job and becomes a
  retryable infrastructure result; it is never only an advisory marker.
- A failed old Pod cannot override an active replacement or a successful Job.
- Missing Events never prevent collection or cleanup.
- A campaign may wait indefinitely for connectivity by policy, but the wait
  reason and start time must be durable and visible.
- The laptop is not a reconciliation principal; laptop sleep or disconnect
  cannot pause a running campaign.
- Remote campaign deletion is explicit and gated by a final verified terminal
  archive; retirement removes all campaign-owned PVCs and control resources.
- A successful test suite is insufficient unless each external boundary has
  failure injection for side-effect ambiguity and restart recovery.

## Current Coverage

The fault-injection suite covers atomic JSON replacement, interrupted trial
construction, source symlink rejection, partial and rejected submission,
primary-state corruption and backup recovery, malformed and unknown
campaign/backend states, ConfigMap lock exclusion and fencing, terminal
preparation failure, zero backend capacity, cleanup retry, durable collection
submission, collection integrity, malformed remote inventories, evaluator versus candidate
attribution, required assessment failure, non-gating report/dashboard failure,
campaign-scoped NetworkPolicy rendering/order, resumable remote stage
verification, read-only assessment evidence, runtime identity,
immutable images, multi-Pod retries, missing Jobs, reference identity,
ResourceQuota capacity, diagnostic collection, bounded result publication,
resumable verified archive synchronization, local archive monitoring,
verified retirement, and terminal archive restoration.

Backend-side submission journals, detached child reaping,
candidate-versus-runner cgroup attribution, and backup recovery after a
simultaneous primary/backup storage failure remain deployment-level follow-up
work. Pre-evaluator hashing/schema work and very large log/JSON inputs also
still need hard byte and execution-time bounds.
