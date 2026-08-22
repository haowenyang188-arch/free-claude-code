"""Tests for agent planning utilities."""

import pytest

from providers.common.agent_planning import (
    DependencyAnalyzer,
    ExecutionPlan,
    ExecutionScheduler,
    Task,
    TaskDecomposer,
    TaskPriority,
    TaskStatus,
    create_plan_from_description,
)


class TestTask:
    """Test Task class."""

    def test_task_creation(self):
        task = Task(id="task_1", description="Test task")
        assert task.id == "task_1"
        assert task.description == "Test task"
        assert task.status == TaskStatus.PENDING
        assert task.priority == TaskPriority.MEDIUM

    def test_task_is_ready_no_dependencies(self):
        task = Task(id="task_1", description="Test")
        assert task.is_ready(set())

    def test_task_is_ready_with_dependencies(self):
        task = Task(id="task_2", description="Test", dependencies=["task_1"])
        assert not task.is_ready(set())
        assert task.is_ready({"task_1"})

    def test_task_to_dict(self):
        task = Task(
            id="task_1",
            description="Test",
            priority=TaskPriority.HIGH,
            estimated_time=30,
        )
        data = task.to_dict()
        assert data["id"] == "task_1"
        assert data["priority"] == TaskPriority.HIGH.value
        assert data["estimated_time"] == 30


class TestExecutionPlan:
    """Test ExecutionPlan class."""

    def test_get_task(self):
        tasks = [
            Task(id="task_1", description="First"),
            Task(id="task_2", description="Second"),
        ]
        plan = ExecutionPlan(tasks=tasks)

        task = plan.get_task("task_1")
        assert task is not None
        assert task.description == "First"

        assert plan.get_task("nonexistent") is None

    def test_get_ready_tasks(self):
        tasks = [
            Task(id="task_1", description="First"),
            Task(id="task_2", description="Second", dependencies=["task_1"]),
            Task(id="task_3", description="Third", priority=TaskPriority.HIGH),
        ]
        plan = ExecutionPlan(tasks=tasks)

        ready = plan.get_ready_tasks()
        assert len(ready) == 2  # task_1 and task_3
        assert ready[0].id == "task_3"  # Higher priority first

    def test_get_progress(self):
        tasks = [
            Task(id="task_1", description="Done", status=TaskStatus.COMPLETED),
            Task(id="task_2", description="Doing", status=TaskStatus.IN_PROGRESS),
            Task(id="task_3", description="Todo"),
        ]
        plan = ExecutionPlan(tasks=tasks)

        progress = plan.get_progress()
        assert progress["total"] == 3
        assert progress["completed"] == 1
        assert progress["in_progress"] == 1
        assert progress["pending"] == 1
        assert progress["progress_percentage"] == pytest.approx(33.33, rel=0.1)


class TestTaskDecomposer:
    """Test TaskDecomposer class."""

    def test_decompose_coding_task(self):
        tasks = TaskDecomposer.decompose_coding_task("Implement login feature")
        assert len(tasks) == 6
        assert any("analyze" in t.description.lower() for t in tasks)
        assert any("design" in t.description.lower() for t in tasks)
        assert any("implement" in t.description.lower() for t in tasks)
        assert any("test" in t.description.lower() for t in tasks)

    def test_decompose_bug_fix(self):
        tasks = TaskDecomposer.decompose_bug_fix("Fix null pointer exception")
        assert len(tasks) == 5
        assert any("reproduce" in t.description.lower() for t in tasks)
        assert any("diagnose" in t.description.lower() for t in tasks)
        assert any("fix" in t.description.lower() for t in tasks)


class TestDependencyAnalyzer:
    """Test DependencyAnalyzer class."""

    def test_detect_no_circular_dependencies(self):
        tasks = [
            Task(id="task_1", description="First"),
            Task(id="task_2", description="Second", dependencies=["task_1"]),
        ]
        cycles = DependencyAnalyzer.detect_circular_dependencies(tasks)
        assert len(cycles) == 0

    def test_detect_circular_dependencies(self):
        tasks = [
            Task(id="task_1", description="First", dependencies=["task_2"]),
            Task(id="task_2", description="Second", dependencies=["task_1"]),
        ]
        cycles = DependencyAnalyzer.detect_circular_dependencies(tasks)
        assert len(cycles) > 0

    def test_get_critical_path(self):
        tasks = [
            Task(id="task_1", description="First", estimated_time=10),
            Task(
                id="task_2",
                description="Second",
                dependencies=["task_1"],
                estimated_time=20,
            ),
            Task(
                id="task_3",
                description="Third",
                dependencies=["task_2"],
                estimated_time=30,
            ),
        ]
        critical = DependencyAnalyzer.get_critical_path(tasks)
        assert len(critical) == 3
        assert critical[-1].id == "task_3"


class TestExecutionScheduler:
    """Test ExecutionScheduler class."""

    def test_schedule_tasks(self):
        tasks = [
            Task(id="task_1", description="First"),
            Task(id="task_2", description="Second", dependencies=["task_1"]),
            Task(id="task_3", description="Third"),
        ]
        plan = ExecutionPlan(tasks=tasks)

        scheduler = ExecutionScheduler(max_concurrent=2)
        batches = scheduler.schedule(plan)

        assert len(batches) >= 2
        # First batch should have task_1 and task_3 (no dependencies)
        assert len(batches[0]) <= 2


class TestCreatePlanFromDescription:
    """Test plan creation from description."""

    def test_create_coding_plan(self):
        plan = create_plan_from_description("Implement user login", "coding")
        assert len(plan.tasks) > 0
        assert "Coding Task" in plan.name

    def test_create_bug_fix_plan(self):
        plan = create_plan_from_description("Fix login bug", "bug_fix")
        assert len(plan.tasks) > 0
        assert "Bug Fix" in plan.name
