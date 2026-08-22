"""Agent planning and task decomposition utilities.

Provides intelligent task breakdown, dependency analysis, and execution scheduling
for complex multi-step operations.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    """Task execution status."""

    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskPriority(Enum):
    """Task priority levels."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class Task:
    """Represents a single task in a plan."""

    id: str
    description: str
    dependencies: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    estimated_time: int = 0  # minutes
    result: Any = None
    error: str | None = None
    subtasks: list[Task] = field(default_factory=list)

    def is_ready(self, completed_tasks: set[str]) -> bool:
        """Check if task is ready to execute.

        Args:
            completed_tasks: Set of completed task IDs

        Returns:
            True if all dependencies are satisfied
        """
        return all(dep in completed_tasks for dep in self.dependencies)

    def to_dict(self) -> dict[str, Any]:
        """Convert task to dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "id": self.id,
            "description": self.description,
            "dependencies": self.dependencies,
            "status": self.status.value,
            "priority": self.priority.value,
            "estimated_time": self.estimated_time,
            "subtasks": [st.to_dict() for st in self.subtasks],
        }


@dataclass
class ExecutionPlan:
    """Represents a complete execution plan."""

    tasks: list[Task]
    name: str = "Execution Plan"
    description: str = ""

    def get_task(self, task_id: str) -> Task | None:
        """Get task by ID.

        Args:
            task_id: Task identifier

        Returns:
            Task or None if not found
        """
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_ready_tasks(self) -> list[Task]:
        """Get all tasks ready for execution.

        Returns:
            List of ready tasks sorted by priority
        """
        completed = {t.id for t in self.tasks if t.status == TaskStatus.COMPLETED}

        ready = [
            t
            for t in self.tasks
            if t.status == TaskStatus.PENDING and t.is_ready(completed)
        ]

        # Sort by priority (highest first)
        return sorted(ready, key=lambda t: t.priority.value, reverse=True)

    def get_blocked_tasks(self) -> list[Task]:
        """Get all blocked tasks.

        Returns:
            List of blocked tasks
        """
        return [t for t in self.tasks if t.status == TaskStatus.BLOCKED]

    def get_progress(self) -> dict[str, Any]:
        """Get execution progress statistics.

        Returns:
            Progress statistics
        """
        total = len(self.tasks)
        completed = sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in self.tasks if t.status == TaskStatus.FAILED)
        in_progress = sum(1 for t in self.tasks if t.status == TaskStatus.IN_PROGRESS)

        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "in_progress": in_progress,
            "pending": total - completed - failed - in_progress,
            "progress_percentage": (completed / total * 100) if total > 0 else 0,
        }

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to dictionary.

        Returns:
            Dictionary representation
        """
        return {
            "name": self.name,
            "description": self.description,
            "tasks": [t.to_dict() for t in self.tasks],
            "progress": self.get_progress(),
        }


class TaskDecomposer:
    """Decompose high-level goals into executable tasks."""

    @staticmethod
    def decompose_coding_task(description: str) -> list[Task]:
        """Decompose a coding task into subtasks.

        Args:
            description: High-level task description

        Returns:
            List of decomposed tasks
        """
        tasks = []

        # Analysis phase
        tasks.append(
            Task(
                id="task_1_analyze",
                description=f"Analyze requirements: {description}",
                dependencies=[],
                priority=TaskPriority.HIGH,
                estimated_time=15,
            )
        )

        # Design phase
        tasks.append(
            Task(
                id="task_2_design",
                description="Design solution architecture",
                dependencies=["task_1_analyze"],
                priority=TaskPriority.HIGH,
                estimated_time=20,
            )
        )

        # Implementation phase
        tasks.append(
            Task(
                id="task_3_implement",
                description="Implement core functionality",
                dependencies=["task_2_design"],
                priority=TaskPriority.CRITICAL,
                estimated_time=60,
            )
        )

        # Testing phase
        tasks.append(
            Task(
                id="task_4_test",
                description="Write and run tests",
                dependencies=["task_3_implement"],
                priority=TaskPriority.HIGH,
                estimated_time=30,
            )
        )

        # Documentation phase
        tasks.append(
            Task(
                id="task_5_document",
                description="Update documentation",
                dependencies=["task_3_implement"],
                priority=TaskPriority.MEDIUM,
                estimated_time=15,
            )
        )

        # Verification phase
        tasks.append(
            Task(
                id="task_6_verify",
                description="Final verification and review",
                dependencies=["task_4_test", "task_5_document"],
                priority=TaskPriority.HIGH,
                estimated_time=10,
            )
        )

        return tasks

    @staticmethod
    def decompose_bug_fix(description: str) -> list[Task]:
        """Decompose a bug fix into subtasks.

        Args:
            description: Bug description

        Returns:
            List of decomposed tasks
        """
        return [
            Task(
                id="bug_1_reproduce",
                description=f"Reproduce bug: {description}",
                priority=TaskPriority.CRITICAL,
                estimated_time=10,
            ),
            Task(
                id="bug_2_diagnose",
                description="Diagnose root cause",
                dependencies=["bug_1_reproduce"],
                priority=TaskPriority.CRITICAL,
                estimated_time=20,
            ),
            Task(
                id="bug_3_fix",
                description="Implement fix",
                dependencies=["bug_2_diagnose"],
                priority=TaskPriority.CRITICAL,
                estimated_time=30,
            ),
            Task(
                id="bug_4_test",
                description="Add regression test",
                dependencies=["bug_3_fix"],
                priority=TaskPriority.HIGH,
                estimated_time=15,
            ),
            Task(
                id="bug_5_verify",
                description="Verify fix and test coverage",
                dependencies=["bug_4_test"],
                priority=TaskPriority.HIGH,
                estimated_time=10,
            ),
        ]


class DependencyAnalyzer:
    """Analyze task dependencies and detect issues."""

    @staticmethod
    def detect_circular_dependencies(tasks: list[Task]) -> list[list[str]]:
        """Detect circular dependencies in task graph.

        Args:
            tasks: List of tasks

        Returns:
            List of circular dependency chains
        """
        task_map = {t.id: t for t in tasks}
        cycles = []

        def dfs(task_id: str, visited: set[str], path: list[str]) -> None:
            if task_id in path:
                # Found a cycle
                cycle_start = path.index(task_id)
                cycles.append([*path[cycle_start:], task_id])
                return

            if task_id in visited:
                return

            visited.add(task_id)
            path.append(task_id)

            task = task_map.get(task_id)
            if task:
                for dep in task.dependencies:
                    dfs(dep, visited, path.copy())

        for task in tasks:
            dfs(task.id, set(), [])

        return cycles

    @staticmethod
    def get_critical_path(tasks: list[Task]) -> list[Task]:
        """Find the critical path (longest path) in task graph.

        Args:
            tasks: List of tasks

        Returns:
            Tasks on the critical path
        """
        task_map = {t.id: t for t in tasks}

        # Calculate longest path using dynamic programming
        memo: dict[str, tuple[int, list[str]]] = {}

        def longest_path(task_id: str) -> tuple[int, list[str]]:
            if task_id in memo:
                return memo[task_id]

            task = task_map[task_id]

            if not task.dependencies:
                result = (task.estimated_time, [task_id])
            else:
                max_time = 0
                max_path: list[str] = []

                for dep in task.dependencies:
                    dep_time, dep_path = longest_path(dep)
                    if dep_time > max_time:
                        max_time = dep_time
                        max_path = dep_path

                result = (max_time + task.estimated_time, [*max_path, task_id])

            memo[task_id] = result
            return result

        # Find the task with the longest path
        max_time = 0
        critical_path_ids: list[str] = []

        for task in tasks:
            time, path = longest_path(task.id)
            if time > max_time:
                max_time = time
                critical_path_ids = path

        return [task_map[tid] for tid in critical_path_ids if tid in task_map]


class ExecutionScheduler:
    """Schedule task execution based on dependencies and resources."""

    def __init__(self, max_concurrent: int = 3):
        """Initialize scheduler.

        Args:
            max_concurrent: Maximum concurrent tasks
        """
        self.max_concurrent = max_concurrent

    def schedule(self, plan: ExecutionPlan) -> list[list[Task]]:
        """Schedule tasks into execution batches.

        Args:
            plan: Execution plan

        Returns:
            List of task batches (each batch can run concurrently)
        """
        batches: list[list[Task]] = []
        remaining = {t.id for t in plan.tasks}
        completed: set[str] = set()

        while remaining:
            ready = []

            for task_id in list(remaining):
                task = plan.get_task(task_id)
                if task and task.is_ready(completed):
                    ready.append(task)

            if not ready:
                # No tasks ready - possible circular dependency
                break

            # Sort by priority and take up to max_concurrent
            ready.sort(key=lambda t: t.priority.value, reverse=True)
            batch = ready[: self.max_concurrent]
            batches.append(batch)

            # Mark batch as completed
            for task in batch:
                remaining.remove(task.id)
                completed.add(task.id)

        return batches


def create_plan_from_description(
    description: str, task_type: str = "coding"
) -> ExecutionPlan:
    """Create execution plan from high-level description.

    Args:
        description: Task description
        task_type: Type of task ("coding", "bug_fix", "refactor")

    Returns:
        Execution plan
    """
    decomposer = TaskDecomposer()

    if task_type == "bug_fix":
        tasks = decomposer.decompose_bug_fix(description)
        name = f"Bug Fix: {description[:50]}"
    else:
        tasks = decomposer.decompose_coding_task(description)
        name = f"Coding Task: {description[:50]}"

    return ExecutionPlan(tasks=tasks, name=name, description=description)
