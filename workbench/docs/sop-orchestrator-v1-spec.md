# SOP Orchestrator V1 Specification

## Objective

Turn a project goal and a selected SOP into a controlled multi-agent run. The
orchestrator owns the workflow graph, creates minimal context packages, starts
eligible step runs, validates structured artifacts, and pauses at explicit
human review gates.

## Non-Goals

- No Hive, Queen, Swarm, self-organizing agents, shared task pool, or agent self-claim.
- No business-level peer-to-peer subagent messaging.
- No automatic SOP selection, task decomposition, agent selection, or re-planning in V1.
- No production deployment or distributed worker fleet in this slice.

## V1 Contracts

`SOP Step -> Role -> Required Capabilities -> Agent Profile -> Runtime` is a
data contract. A runtime adapter executes one `StepRun` and never decides the
next workflow transition.

Business handoff is artifact-only. Orchestrator control messages, runtime
protocol events, and status events remain internal control-plane traffic.

Each step receives a `ContextPackage` containing only the goal summary, step
instructions, declared constraints, upstream artifact IDs, and output schema.

## Persistence

- SQLite stores versioned domain entity snapshots and indexed workflow metadata.
- JSONL remains the append-only event stream for replay and audit.
- File Artifact Store writes immutable payloads and manifests with SHA-256 hashes.
- Artifact replacement creates a new ID and may reference `supersedes_id`.

## Workflow Semantics

- A step is `READY` only when every declared dependency is accepted or its
  handoff has been accepted.
- Ready steps may be executed automatically by the orchestrator according to
  their declared sequential/parallel mode.
- A step with `requires_review=true` enters `WAITING_REVIEW` after artifact
  validation; downstream steps remain locked until the gate is approved.
- Rejected artifacts do not create handoffs or unlock downstream steps.
- Retry and rework create a new task attempt and preserve the prior attempt.

## Success Criteria

- A two-step research-to-review SOP can be instantiated and persisted.
- Initial dependency-free steps become ready without manual task creation.
- A successful artifact is persisted, hashed, validated, and handed off.
- Handoff acceptance unlocks only the declared downstream step.
- A rejected artifact blocks downstream work and records a structured reason.
- SQLite state survives a new store instance; JSONL events replay by cursor.
- No production files outside the scoped workbench slice are modified.

