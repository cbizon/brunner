# Architecture

## Lifecycle Boundary

Brunner separates benchmark execution into five trust and responsibility
stages.

1. **Stage**: copy only challenge-visible files, render output requirements
   from the canonical contract, and record challenge/contract digests.
2. **Run**: execute a provider in the staged workspace with durable state,
   retries, continuation, finalization, timeout, and structured final output.
3. **Collect**: preserve logs and copy artifacts through a resumable,
   checksum-verified transfer.
4. **Evaluate**: validate the submission contract, then run trusted
   benchmark-specific scoring against optional verified references.
5. **Campaign**: schedule a matrix, reconcile backend state, recover outputs,
   evaluate, clean up, and publish a dashboard.

## Ownership

| Concern | Brunner owns | Benchmark owns |
|---|---|---|
| Identity | Metadata/digest recording | Benchmark ID and version |
| Agent input | Isolated staging | Challenge files and prompt prose |
| Output definition | Rendering and validation | `output-contract.json` |
| Provider runtime | Commands, events, retries, usage | Model/effort selection |
| Evaluation | Trusted invocation and result envelope | Metrics and scoring code |
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

## Trust Boundary

The agent runtime receives only:

- The staged challenge
- Generated schemas and contract
- Minimal `metadata/agent-run.json`
- Provider credentials supplied by the runtime

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
- Timing and normalized usage summaries

Ordinary transient API failures retry with bounded exponential delay.
Authentication, authorization, unavailable model, invalid request, and
disabled-credit conditions terminate immediately. A provider terminal event
is authoritative even if child processes linger; lingering processes are
terminated after a configured grace period.

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

Kubernetes distinguishes connectivity failures from rejected requests and
workload failures. It reports pending PVCs, inspects terminated init/main
containers, preserves workload logs, retries artifact readers, excludes
failed reader nodes when rescheduling, resumes partial files by byte offset,
and verifies every SHA-256. A failed workload's PVC is retained until artifact
collection succeeds.

## Campaign State

Campaign IDs and trial IDs are deterministic. `campaign.json` records the
plan digest, contract digest, provider matrix, handles, snapshots, collection
attempts, evaluation results, outcomes, and recent events.

Campaign reconciliation:

- Submits only up to plan and backend capacity
- Resumes from persisted handles
- Pauses the campaign on backend connectivity loss
- Collects artifacts for both successful and failed workloads
- Does not clean up when recovery fails
- Runs trusted evaluation after verified collection
- Regenerates `index.html` after each transition

No campaign environment values are serialized. A plan may name environment
variables to pass at submission time; their values are read from the current
process environment.
