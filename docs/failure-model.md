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
| `backend` | OCI/Kubernetes execution and observation |
| `integrity` | Trusted identity, checksum, isolation, or path invariant |
| `evaluation` | Deterministic evaluator or reference-validation infrastructure |
| `assessment` | Trusted qualitative reviewer or renderer infrastructure |
| `configuration` | Benchmark or deployment configuration |
| `orchestrator` | Brunner bookkeeping, persistence, or local resources |
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
5. After orchestrator restart.

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
| State persistence | Disk/inodes, permission, serialization, machine loss | Orchestrator | Preserve previous valid state; stop new side effects if authoritative state cannot be written |
| Campaign locking | Concurrent orchestrator or stale diagnostic owner text | OS file lock | Terminal local orchestration error; kernel lock is authoritative |
| Capacity observation | API loss, quota, no nodes, malformed response | Scheduler | Connectivity pause or visible `wait`; never an invisible running state |
| Submission | Partial PVC/helper/Job/container, rejection, timeout, ambiguous response | Campaign submission reconciliation | Adopt idempotently after ambiguity; possible side effects require cleanup |
| Scheduling/startup | Unschedulable, image pull, mount, secret, GPU/storage unavailable | Backend inspection | Typed backend failure; retry only transient infrastructure |
| Agent startup | Missing executable/config, corrupt trial state, permission/disk failure | Agent CLI and backend | Durable nonzero infrastructure result with diagnostics |
| Provider execution | Auth, subscription/rate limit, model substitution, tool/sandbox denial, network, malformed terminal event | Runner/provider adapter | Provider-specific retry or terminal result; preserve requested/observed identity |
| Runtime resources | CPU, memory, ephemeral storage, PVC, PID/FD, token/context, deadline | Backend plus runner | Attribute to candidate only when a candidate-specific limit proves ownership |
| Output/timing capture | Log disk full, malformed event, output flood | Runner | Terminal provider parsing must not depend on optional diagnostic writes |
| Inspection | Connectivity, malformed JSON, deleted Job/container, missing Events | Backend and campaign | Pause on connectivity; durable attention on unknown/malformed state |
| Retry/resume | Retry deadline, exhausted budget, stale session, unsupported resume | Runner or campaign | Absolute persisted retry time; bounded retries; terminal reason at exhaustion |
| Artifact collection | Transfer loss, helper failure, malformed inventory, checksum/path violation, local disk | Backend and campaign | Retry transport only; integrity never consumes a transport retry |
| Submission validation | Missing/invalid manifest, schema/path/size violation | Evaluator | `candidate_failed`; this is a valid benchmark result |
| Reference validation | Drift, missing trusted files, validator failure | Evaluator | `integrity` or `evaluation`; benchmark result indeterminate |
| Deterministic evaluation | Launch, timeout, crash, invalid result, runtime resource failure | Campaign | `evaluation`; never `benchmark` unless a valid evaluator reports candidate failure |
| Qualitative assessment | Provider quota/auth, timeout, invalid review, renderer failure | Campaign | `assessment`; required-review failure makes result indeterminate |
| Reporting | Serialization, template error, disk full | Evaluation or campaign save | Record `reporting`; never block cleanup or replace the authoritative result |
| Cleanup | API loss, finalizer, deletion timeout, helper leak | Campaign cleanup reconciliation | Persist `cleanup_pending` and retry; result remains authoritative |
| Aggregation | Unknown phase or contradictory fields | Campaign | Durable `orchestrator` attention; never silently report `running` |
| Restart recovery | Sleep, SIGTERM, crash between operations | Campaign initialization | Recover only idempotent phases; preserve deadlines, handles, and cleanup obligations |

## Resource Ownership

Resource exhaustion is classified by ownership, not only by the operating
system reason:

| Resource event | Classification |
| --- | --- |
| Node eviction, node loss, control-plane outage | Retryable backend infrastructure |
| Brunner runner/provider process exceeds a shared pod limit | Infrastructure |
| Candidate process exceeds an explicitly isolated benchmark limit | Candidate failure |
| Shared pod `OOMKilled` without process ownership evidence | Infrastructure; retry is bounded |
| PVC or orchestrator disk full during bookkeeping | Orchestrator/integrity, not candidate |
| Candidate artifact exceeds an output-contract size limit | Candidate failure |
| Provider token/context/subscription exhaustion | Provider policy |
| Kubernetes quota or storage-class exhaustion | Backend wait or configuration |

When candidate and harness processes share a cgroup, Brunner cannot reliably
attribute pod-level memory exhaustion to the candidate. Benchmarks that score
resource consumption need a separately measurable candidate resource boundary.

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
- A campaign may wait indefinitely for connectivity by policy, but the wait
  reason and start time must be durable and visible.
- A successful test suite is insufficient unless each external boundary has
  failure injection for side-effect ambiguity and restart recovery.

## Current Coverage

The fault-injection suite covers atomic JSON replacement, interrupted trial
construction, source symlink rejection, partial and rejected submission,
primary-state corruption and backup recovery, malformed and unknown
campaign/backend states, zero backend capacity, cleanup retry, collection
integrity, malformed remote inventories, evaluator versus candidate
attribution, required assessment failure, and non-gating report/dashboard
failure.

Backend-side submission journals, detached child reaping,
candidate-versus-runner cgroup attribution, and backup recovery after a
simultaneous primary/backup storage failure remain deployment-level follow-up
work. Pre-evaluator hashing/schema work and very large log/JSON inputs also
still need hard byte and execution-time bounds.
