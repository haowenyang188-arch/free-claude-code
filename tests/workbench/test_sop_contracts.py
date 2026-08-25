from __future__ import annotations

from pathlib import Path

import pytest

from workbench.backend.domain.models import (
    AcceptanceCriteria,
    AgentInstance,
    AgentProfile,
    Artifact,
    ArtifactType,
    Goal,
    Handoff,
    HandoffStatus,
    Project,
    Role,
    RoleBinding,
    Runtime,
    RuntimeKind,
    Session,
    SopDefinition,
    SopOrigin,
    SopRun,
    StepDefinition,
    StepRun,
    StepStatus,
    Task,
    TaskStatus,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore


def test_sop_definition_keeps_origin_and_version_for_future_instantiation() -> None:
    sop = SopDefinition(
        id="sop-research-build",
        name="Research then build",
        version=2,
        origin=SopOrigin.TEMPLATE,
        template_id="software-delivery",
        stages=[
            {
                "id": "research",
                "name": "Research",
                "steps": [
                    StepDefinition(
                        id="research-report",
                        name="Write research report",
                        role_id="researcher",
                        output_type=ArtifactType.RESEARCH_REPORT,
                    )
                ],
            }
        ],
    )

    assert sop.origin is SopOrigin.TEMPLATE
    assert sop.template_id == "software-delivery"
    assert sop.version == 2
    assert sop.stages[0].steps[0].role_id == "researcher"


def test_role_binding_keeps_runtime_separate_from_role() -> None:
    role = Role(id="reviewer", name="Reviewer", capabilities=["code-review"])
    profile = AgentProfile(
        id="codex-profile",
        name="Codex",
        capabilities=["code-review", "shell"],
    )
    instance = AgentInstance(id="codex-1", profile_id=profile.id)
    runtime = Runtime(id="codex-runtime", name="Codex", kind=RuntimeKind.CODEX)
    session = Session(
        id="session-1", runtime_id=runtime.id, agent_instance_id=instance.id
    )
    binding = RoleBinding(
        id="binding-1",
        role_id=role.id,
        agent_instance_id=instance.id,
        runtime_id=runtime.id,
        session_id=session.id,
    )

    assert binding.role_id != binding.runtime_id
    assert session.runtime_id == runtime.id
    assert instance.profile_id == profile.id


def test_task_acceptance_and_handoff_are_first_class_records() -> None:
    task = Task(
        id="task-research",
        step_run_id="step-research",
        role_id="researcher",
        status=TaskStatus.VALIDATING,
        acceptance_criteria=[
            AcceptanceCriteria(id="has-sources", description="Contains source links")
        ],
    )
    artifact = Artifact(
        id="artifact-report",
        task_id=task.id,
        type=ArtifactType.RESEARCH_REPORT,
        uri="artifacts/report.md",
    )
    validation = ValidationResult(
        id="validation-1",
        task_id=task.id,
        artifact_id=artifact.id,
        status=ValidationStatus.ACCEPTED,
        validator_id="source-validator",
    )
    handoff = Handoff(
        id="handoff-1",
        from_task_id=task.id,
        to_step_id="step-architecture",
        artifact_ids=[artifact.id],
        status=HandoffStatus.READY,
    )

    assert validation.status is ValidationStatus.ACCEPTED
    assert handoff.artifact_ids == [artifact.id]
    assert task.acceptance_criteria[0].id == "has-sources"


@pytest.mark.parametrize("path_name", ["state.json", "events.jsonl"])
def test_json_workflow_store_uses_configured_paths(
    tmp_path: Path, path_name: str
) -> None:
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    store.save_entity("projects", Project(id="project-1", name="Demo"))
    store.append_event(
        stream_id="sop-run-1",
        event_type="sop_started",
        payload={"project_id": "project-1", "api_token": "hidden"},
    )

    assert (tmp_path / path_name).exists()


def test_json_workflow_store_round_trips_entities_and_redacts_events(
    tmp_path: Path,
) -> None:
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    store.save_entity(
        "goals", Goal(id="goal-1", project_id="project-1", description="Ship it")
    )
    store.save_entity(
        "sop_runs",
        SopRun(id="run-1", goal_id="goal-1", sop_definition_id="sop-1", sop_version=1),
    )
    store.save_entity(
        "step_runs",
        StepRun(
            id="step-run-1",
            sop_run_id="run-1",
            step_id="step-1",
            status=StepStatus.PENDING,
        ),
    )
    first = store.append_event(
        stream_id="run-1",
        event_type="sop_started",
        payload={"message": "started", "auth_token": "must-not-persist"},
    )
    second = store.append_event(
        stream_id="run-1",
        event_type="step_started",
        payload={"message": "step"},
    )

    restored = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    assert restored.get_entity("goals", "goal-1")["description"] == "Ship it"
    assert [event.sequence for event in restored.replay_events("run-1")] == [1, 2]
    assert restored.replay_events("run-1", after=first.sequence)[0].id == second.id
    assert restored.replay_events("run-1")[0].payload["auth_token"] == "[redacted]"
