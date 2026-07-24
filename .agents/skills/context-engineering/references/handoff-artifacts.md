# Handoff Artifacts

Use these templates only after replacing every placeholder with facts from the
user's current task and verified environment. Omit inapplicable fields instead
of borrowing values from examples or earlier tasks.

## Contents

- [Storage Contract](#storage-contract)
- [Parent-To-Child Task Brief](#parent-to-child-task-brief)
- [Child-To-Parent Result Report](#child-to-parent-result-report)
- [Persistent Progress Ledger](#persistent-progress-ledger)
- [Transfer Protocol](#transfer-protocol)

## Storage Contract

Choose one `<coordination-root>` for the current task:

```text
<coordination-root>/
  task-brief.md
  progress-ledger.md
  results/
    <work-unit>.md
```

Record the task ID in every file. Keep one writer per mutable file. A parent
normally owns `task-brief.md` and `progress-ledger.md`; each child owns only
its assigned result report unless the task contract says otherwise.

## Parent-To-Child Task Brief

```markdown
# Task Brief

**Task ID:** <stable current-task identifier>
**Work unit:** <unique delegated unit>
**Source request:** <current user request or faithful scoped excerpt>
**Objective:** <one verifiable outcome>
**Inputs:** <allowed files, facts, artifacts, and prior outputs>
**Outputs:** <required artifacts and result-report path>
**Scope:** <included work>
**Exclusions:** <explicitly forbidden or parent-owned work>
**Dependencies:** <required predecessor states or none>
**Resource ownership:** <files, services, devices, or accounts this unit owns>
**Shared interfaces:** <contracts consumed or produced, with source location>
**Constraints:** <applicable system, user, and project rules>
**Acceptance conditions:** <observable checks>
**Failure impact:** <downstream work blocked or invalidated>
**Verified context:** <fact + provenance + validity>
**Open questions:** <unresolved items that materially affect this unit>
**Reporting contract:** <required status values and evidence>
```

The receiver must verify the task ID, scope, dependencies, ownership, and
artifact paths before acting. Report a mismatch instead of inferring a merge.

## Child-To-Parent Result Report

```markdown
# Result Report

**Task ID:** <same identifier as the brief>
**Work unit:** <same unique unit>
**Status:** completed | partial | blocked | failed
**Outcome:** <what was actually achieved>
**Artifacts:** <created or changed paths>
**Evidence:** <commands, outputs, citations, or direct observations>
**Validation:** <checks run and exact pass/fail result>
**Decisions:** <decision + source or rationale>
**Deviations:** <difference from the brief and why>
**Unresolved issues:** <remaining gaps, blockers, or uncertainty>
**Failure impact:** <downstream consequences>
**Recommended next action:** <one concrete parent-owned action>
**Contamination check:** <confirm no unrelated task/example became a goal>
```

Never report `completed` when acceptance evidence is missing. Do not edit a
sibling result or the parent's progress ledger unless ownership was assigned.

## Persistent Progress Ledger

```markdown
# Progress Ledger

**Task ID:** <stable current-task identifier>
**Source request:** <current user request>
**Last updated:** <timestamp and writer>
**Current scope:** <active deliverables and exclusions>
**Latest overrides:** <new user requirements that supersede earlier plans>

## Completed
- <outcome> | Evidence: <source> | Artifact: <path>

## In Progress
- <work unit> | Owner: <single writer> | Next check: <condition>

## Blocked Or Failed
- <work unit> | Cause: <evidence> | Impact: <downstream effect>

## Decisions And Provenance
- <decision> | Source: <user, rule, file, or tool output> | Validity: <state>

## Verification State
- <check> | Result: pass | fail | not run | blocked | Evidence: <source>

## Next Actions
1. <next dependency-valid action>

## Superseded Context
- <old statement> | Superseded by: <new source> | Retained for: <reason>
```

Update the ledger only from verified reports or direct evidence. Preserve
failed and superseded states; do not rewrite history to make the task appear
cleaner.

## Transfer Protocol

1. The sender writes or refreshes the task brief from the current task.
2. The receiver validates identity, scope, ownership, dependencies, and paths.
3. The receiver performs only the assigned unit and writes its result report.
4. The parent validates evidence and acceptance conditions.
5. The parent serially updates the progress ledger and decides the next action.
6. On resume, read the brief, ledger, then only the result reports needed for
   the active dependency chain.
