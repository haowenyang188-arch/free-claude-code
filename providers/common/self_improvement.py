"""Self-improvement utilities for automatic code optimization.

Provides code quality analysis, performance monitoring, and automatic
optimization suggestions.
"""

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CodeIssue:
    """Represents a code quality issue."""

    file_path: str
    line: int
    severity: str  # "error", "warning", "info"
    category: str  # "complexity", "style", "performance", "security"
    message: str
    suggestion: str | None = None


@dataclass
class PerformanceBottleneck:
    """Represents a performance bottleneck."""

    function_name: str
    file_path: str
    avg_time: float
    call_count: int
    total_time: float
    suggestion: str


class CodeQualityAnalyzer:
    """Analyze code quality and suggest improvements."""

    @staticmethod
    def analyze_complexity(file_path: str) -> list[CodeIssue]:
        """Analyze cyclomatic complexity of Python code.

        Args:
            file_path: Path to Python file

        Returns:
            List of complexity issues
        """
        issues = []

        try:
            with open(file_path) as f:
                tree = ast.parse(f.read(), filename=file_path)

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    complexity = CodeQualityAnalyzer._calculate_complexity(node)

                    if complexity > 10:
                        severity = "error" if complexity > 15 else "warning"
                        issues.append(
                            CodeIssue(
                                file_path=file_path,
                                line=node.lineno,
                                severity=severity,
                                category="complexity",
                                message=f"Function '{node.name}' has cyclomatic complexity of {complexity}",
                                suggestion="Consider breaking down into smaller functions",
                            )
                        )
        except Exception as e:
            issues.append(
                CodeIssue(
                    file_path=file_path,
                    line=0,
                    severity="error",
                    category="syntax",
                    message=f"Failed to parse file: {e}",
                )
            )

        return issues

    @staticmethod
    def _calculate_complexity(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> int:
        """Calculate cyclomatic complexity of a function.

        Args:
            node: AST function node

        Returns:
            Complexity score
        """

        class _ComplexityVisitor(ast.NodeVisitor):
            def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef):
                self.root = root
                self.complexity = 1

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                if node is self.root:
                    self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                if node is self.root:
                    self.generic_visit(node)

            def visit_Lambda(self, node: ast.Lambda) -> None:
                return

            def visit_If(self, node: ast.If) -> None:
                self.complexity += 1
                self.generic_visit(node)

            def visit_While(self, node: ast.While) -> None:
                self.complexity += 1
                self.generic_visit(node)

            def visit_For(self, node: ast.For) -> None:
                self.complexity += 1
                self.generic_visit(node)

            def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
                self.complexity += 1
                self.generic_visit(node)

            def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
                self.complexity += 1
                self.generic_visit(node)

            def visit_With(self, node: ast.With) -> None:
                self.complexity += 1
                self.generic_visit(node)

            def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
                self.complexity += 1
                self.generic_visit(node)

            def visit_BoolOp(self, node: ast.BoolOp) -> None:
                self.complexity += len(node.values) - 1
                self.generic_visit(node)

        visitor = _ComplexityVisitor(node)
        visitor.visit(node)
        return visitor.complexity

    @staticmethod
    def analyze_file_size(file_path: str, max_lines: int = 500) -> list[CodeIssue]:
        """Check if file is too large.

        Args:
            file_path: Path to file
            max_lines: Maximum recommended lines

        Returns:
            List of size issues
        """
        issues = []

        try:
            with open(file_path) as f:
                lines = len(f.readlines())

            if lines > max_lines:
                issues.append(
                    CodeIssue(
                        file_path=file_path,
                        line=0,
                        severity="warning",
                        category="maintainability",
                        message=f"File has {lines} lines (recommended: <{max_lines})",
                        suggestion="Consider splitting into multiple modules",
                    )
                )
        except Exception:
            pass

        return issues

    @staticmethod
    def analyze_duplicates(project_dir: str) -> list[CodeIssue]:
        """Find duplicate code blocks.

        Args:
            project_dir: Project directory

        Returns:
            List of duplication issues
        """
        # Simple implementation - check for repeated function names
        issues = []
        function_locations: dict[str, list[tuple[str, int]]] = {}

        for py_file in Path(project_dir).rglob("*.py"):
            if "test" in str(py_file) or ".venv" in str(py_file):
                continue

            try:
                with open(py_file) as f:
                    tree = ast.parse(f.read(), filename=str(py_file))

                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name not in function_locations:
                            function_locations[node.name] = []
                        function_locations[node.name].append(
                            (str(py_file), node.lineno)
                        )
            except Exception:
                continue

        # Report functions with similar names
        for name, locations in function_locations.items():
            if len(locations) > 1:
                issues.append(
                    CodeIssue(
                        file_path=locations[0][0],
                        line=locations[0][1],
                        severity="info",
                        category="duplication",
                        message=f"Function '{name}' appears in {len(locations)} files",
                        suggestion="Consider extracting to shared utility module",
                    )
                )

        return issues


class AutoOptimizer:
    """Automatic code optimization engine."""

    @staticmethod
    def optimize_imports(file_path: str) -> bool:
        """Optimize imports in Python file.

        Args:
            file_path: Path to Python file

        Returns:
            True if optimizations were applied
        """
        try:
            # Use isort to sort imports
            result = subprocess.run(
                ["python", "-m", "isort", file_path],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def apply_formatter(file_path: str, formatter: str = "ruff") -> bool:
        """Apply code formatter.

        Args:
            file_path: Path to file
            formatter: Formatter to use ("ruff", "black")

        Returns:
            True if formatting was applied
        """
        try:
            if formatter == "ruff":
                result = subprocess.run(
                    ["ruff", "format", file_path],
                    capture_output=True,
                    text=True,
                )
            else:
                result = subprocess.run(
                    ["black", file_path],
                    capture_output=True,
                    text=True,
                )
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def suggest_refactoring(issue: CodeIssue) -> str:
        """Generate refactoring suggestion for an issue.

        Args:
            issue: Code issue

        Returns:
            Refactoring suggestion
        """
        if issue.category == "complexity":
            return (
                f"Break down function at {issue.file_path}:{issue.line} into smaller functions. "
                "Consider using the Extract Method refactoring pattern."
            )
        elif issue.category == "duplication":
            return (
                "Create a shared utility module and move duplicate code there. "
                "Update all call sites to use the shared implementation."
            )
        elif issue.category == "maintainability":
            return (
                "Split large file into multiple modules based on responsibility. "
                "Group related functions into separate files."
            )
        else:
            return issue.suggestion or "Review and refactor as needed"


class SelfImprovementLoop:
    """Continuous self-improvement loop."""

    def __init__(self, project_dir: str):
        """Initialize improvement loop.

        Args:
            project_dir: Project directory to analyze
        """
        self.project_dir = project_dir
        self.analyzer = CodeQualityAnalyzer()
        self.optimizer = AutoOptimizer()
        self.improvement_history: list[dict[str, Any]] = []

    def run_analysis(self) -> list[CodeIssue]:
        """Run complete code analysis.

        Returns:
            List of all detected issues
        """
        issues: list[CodeIssue] = []

        # Analyze all Python files
        for py_file in Path(self.project_dir).rglob("*.py"):
            if "test" in str(py_file) or ".venv" in str(py_file):
                continue

            # Check complexity
            issues.extend(self.analyzer.analyze_complexity(str(py_file)))

            # Check file size
            issues.extend(self.analyzer.analyze_file_size(str(py_file)))

        # Check for duplicates
        issues.extend(self.analyzer.analyze_duplicates(self.project_dir))

        return issues

    def prioritize_issues(self, issues: list[CodeIssue]) -> list[CodeIssue]:
        """Prioritize issues by severity and impact.

        Args:
            issues: List of issues

        Returns:
            Sorted list of issues (highest priority first)
        """
        severity_order = {"error": 3, "warning": 2, "info": 1}

        return sorted(
            issues,
            key=lambda i: (severity_order.get(i.severity, 0), i.line),
            reverse=True,
        )

    def apply_automatic_fixes(self, issues: list[CodeIssue]) -> int:
        """Apply automatic fixes for issues.

        Args:
            issues: List of issues to fix

        Returns:
            Number of issues fixed
        """
        fixed = 0

        for issue in issues:
            if issue.category == "style" and self.optimizer.apply_formatter(
                issue.file_path
            ):
                fixed += 1
            # Add more automatic fixes here

        return fixed

    def generate_improvement_report(self, issues: list[CodeIssue]) -> dict[str, Any]:
        """Generate improvement report.

        Args:
            issues: List of detected issues

        Returns:
            Report dictionary
        """
        by_severity = {
            "error": [i for i in issues if i.severity == "error"],
            "warning": [i for i in issues if i.severity == "warning"],
            "info": [i for i in issues if i.severity == "info"],
        }

        by_category = {}
        for issue in issues:
            if issue.category not in by_category:
                by_category[issue.category] = []
            by_category[issue.category].append(issue)

        return {
            "total_issues": len(issues),
            "by_severity": {k: len(v) for k, v in by_severity.items()},
            "by_category": {k: len(v) for k, v in by_category.items()},
            "top_issues": issues[:10],
            "recommendations": [
                self.optimizer.suggest_refactoring(i) for i in issues[:5]
            ],
        }

    def iterate(self) -> dict[str, Any]:
        """Run one iteration of improvement loop.

        Returns:
            Iteration results
        """
        # Analyze code
        issues = self.run_analysis()
        prioritized = self.prioritize_issues(issues)

        # Apply automatic fixes
        fixed = self.apply_automatic_fixes(prioritized)

        # Generate report
        report = self.generate_improvement_report(prioritized)
        report["auto_fixed"] = fixed

        # Record in history
        self.improvement_history.append(report)

        return report


def analyze_performance_bottlenecks(
    monitor: Any, threshold_ms: float = 100
) -> list[PerformanceBottleneck]:
    """Analyze performance data to find bottlenecks.

    Args:
        monitor: PerformanceMonitor instance
        threshold_ms: Threshold in milliseconds

    Returns:
        List of detected bottlenecks
    """
    bottlenecks = []

    for metric_name, times in monitor.metrics.items():
        if not times:
            continue

        avg_time = sum(times) / len(times)
        total_time = sum(times)

        if avg_time * 1000 > threshold_ms:  # Convert to ms
            bottlenecks.append(
                PerformanceBottleneck(
                    function_name=metric_name,
                    file_path="unknown",
                    avg_time=avg_time,
                    call_count=len(times),
                    total_time=total_time,
                    suggestion=f"Function takes {avg_time * 1000:.1f}ms on average. "
                    "Consider optimization or caching.",
                )
            )

    return sorted(bottlenecks, key=lambda b: b.total_time, reverse=True)
