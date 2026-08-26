"""Tests for code quality analysis."""

import ast
from textwrap import dedent

from providers.common.self_improvement import CodeQualityAnalyzer


def test_complexity_excludes_nested_function_decisions() -> None:
    tree = ast.parse(
        dedent(
            """
        def outer(value):
            if value:
                return 1

            def inner(item):
                if item:
                    for _ in range(2):
                        pass
                return item

            return inner(value)
        """
        )
    )

    outer = tree.body[0]
    assert isinstance(outer, ast.FunctionDef)
    assert CodeQualityAnalyzer._calculate_complexity(outer) == 2


def test_complexity_supports_async_functions() -> None:
    tree = ast.parse(
        dedent(
            """
        async def worker(items):
            async for item in items:
                if item:
                    continue
        """
        )
    )

    worker = tree.body[0]
    assert isinstance(worker, ast.AsyncFunctionDef)
    assert CodeQualityAnalyzer._calculate_complexity(worker) == 3
