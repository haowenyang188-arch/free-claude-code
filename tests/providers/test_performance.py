"""Tests for provider performance helpers."""

import pytest

from providers.common.performance import BatchProcessor, async_memoize, memoize


def test_memoize_reused_decorator_keeps_callable_results_separate() -> None:
    decorator = memoize()

    def make_operation(label: str):
        def operation(value: int) -> tuple[str, int]:
            return label, value

        # Simulate two callable objects that share a name, which is common for
        # factory-created functions.
        operation.__name__ = "operation"
        return operation

    first = decorator(make_operation("first"))
    second = decorator(make_operation("second"))

    assert first(1) == ("first", 1)
    assert second(1) == ("second", 1)


def test_memoize_caches_none_results() -> None:
    calls = 0

    @memoize()
    def returns_none(value: int) -> None:
        nonlocal calls
        calls += 1

    assert returns_none(1) is None
    assert returns_none(1) is None
    assert calls == 1


@pytest.mark.asyncio
async def test_async_memoize_caches_none_results() -> None:
    calls = 0

    @async_memoize()
    async def returns_none(value: int) -> None:
        nonlocal calls
        calls += 1

    assert await returns_none(1) is None
    assert await returns_none(1) is None
    assert calls == 1


@pytest.mark.parametrize("batch_size", [0, -1])
def test_batch_processor_rejects_non_positive_batch_size(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        BatchProcessor(batch_size=batch_size)


def test_batch_processor_rejects_negative_wait() -> None:
    with pytest.raises(ValueError, match="max_wait"):
        BatchProcessor(max_wait=-0.1)


@pytest.mark.asyncio
async def test_batch_processor_processes_batches_at_requested_size() -> None:
    processor = BatchProcessor(batch_size=2, max_wait=0)
    for item in range(5):
        await processor.add(item)

    batches: list[list[int]] = []

    async def collect(batch: list[int]) -> list[int]:
        batches.append(batch)
        return batch

    assert await processor.process(collect) == [0, 1, 2, 3, 4]
    assert batches == [[0, 1], [2, 3], [4]]
