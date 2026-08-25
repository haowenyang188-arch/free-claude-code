# ADR-001: Use A Controlled SOP Orchestrator

## Status

Accepted

## Context

The existing workbench launches one Task directly against one runtime adapter.
The product now requires a supervisor-controlled SOP pipeline with explicit
roles, capabilities, runtimes, isolated context, artifact handoff, review
gates, retry, and rework. The system must remain understandable and manually
governable; autonomous peer coordination is explicitly out of scope.

## Decision

Add a lightweight deterministic SOP engine between the Workbench service and
the existing Claude/Codex adapters.

- The Orchestrator is the only owner of DAG state and transitions.
- Subagents exchange business outputs only through immutable, hashed Artifacts
  and explicit Handoffs.
- Runtime adapters execute one StepRun at a time and report events/results.
- SQLite stores domain records; JSONL remains the append-only event log; files
  store Artifact payloads and manifests.
- Human review gates are represented as explicit state transitions. Automation
  may advance eligible steps, but it may not bypass a gate.

## Alternatives Considered

### LangGraph as the V1 application core

LangGraph is a strong reference for durable graph execution and checkpoints,
but adopting it as the product core would couple the workbench domain to one
agent framework before the Role/Capability/Runtime contracts stabilize.

### Temporal as the V1 runtime

Temporal provides excellent durable execution and retry semantics, but adds a
distributed service and operational surface that is not required for the first
local, human-controlled release.

### Peer-to-peer or self-organizing agents

Rejected. It conflicts with the product boundary, weakens auditability, and
makes context and authorization harder to reason about.

## Consequences

The first implementation is intentionally local and explicit. The runtime seam
can later host LangGraph, Temporal, or another durable executor without
changing the SOP, Role, Artifact, or Handoff contracts.

