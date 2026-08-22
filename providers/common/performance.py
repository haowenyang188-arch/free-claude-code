"""Performance optimization utilities for provider operations."""

import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class SimpleCache:
    """Simple in-memory cache with TTL support."""

    def __init__(self, ttl: float = 300):
        """Initialize cache.

        Args:
            ttl: Time-to-live in seconds (default: 5 minutes)
        """
        self.ttl = ttl
        self._cache: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any | None:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if expired/missing
        """
        if key not in self._cache:
            return None

        value, timestamp = self._cache[key]
        if time.time() - timestamp > self.ttl:
            del self._cache[key]
            return None

        return value

    def set(self, key: str, value: Any) -> None:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache
        """
        self._cache[key] = (value, time.time())

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()

    def size(self) -> int:
        """Get number of cached items."""
        # Clean expired items first
        current_time = time.time()
        expired = [
            k for k, (_, ts) in self._cache.items() if current_time - ts > self.ttl
        ]
        for key in expired:
            del self._cache[key]
        return len(self._cache)


class BatchProcessor:
    """Process items in batches for better performance."""

    def __init__(self, batch_size: int = 10, max_wait: float = 0.1):
        """Initialize batch processor.

        Args:
            batch_size: Maximum batch size
            max_wait: Maximum wait time in seconds
        """
        self.batch_size = batch_size
        self.max_wait = max_wait
        self._queue: list[Any] = []
        self._lock = asyncio.Lock()

    async def add(self, item: Any) -> None:
        """Add item to batch queue.

        Args:
            item: Item to add
        """
        async with self._lock:
            self._queue.append(item)

    async def process(self, processor: Callable[[list[Any]], Any]) -> list[Any]:
        """Process batched items.

        Args:
            processor: Function to process batch

        Returns:
            List of results
        """
        results = []

        while True:
            async with self._lock:
                if not self._queue:
                    break

                # Take batch
                batch = self._queue[: self.batch_size]
                self._queue = self._queue[self.batch_size :]

            # Process batch
            batch_results = await processor(batch)
            results.extend(batch_results)

            # Wait if more items might come
            if self._queue:
                await asyncio.sleep(self.max_wait)

        return results


def memoize(ttl: float = 300):
    """Decorator for caching function results.

    Args:
        ttl: Time-to-live in seconds

    Returns:
        Decorator function
    """
    cache = SimpleCache(ttl=ttl)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            # Create cache key from arguments
            key = f"{func.__name__}:{args}:{sorted(kwargs.items())}"

            # Check cache
            result = cache.get(key)
            if result is not None:
                return result

            # Call function and cache result
            result = func(*args, **kwargs)
            cache.set(key, result)
            return result

        wrapper.cache = cache  # type: ignore
        return wrapper

    return decorator


async def async_memoize_wrapper(
    func: Callable[..., Any], cache: SimpleCache, *args: Any, **kwargs: Any
) -> Any:
    """Async wrapper for memoization."""
    key = f"{func.__name__}:{args}:{sorted(kwargs.items())}"

    result = cache.get(key)
    if result is not None:
        return result

    result = await func(*args, **kwargs)
    cache.set(key, result)
    return result


def async_memoize(ttl: float = 300):
    """Decorator for caching async function results.

    Args:
        ttl: Time-to-live in seconds

    Returns:
        Decorator function
    """
    cache = SimpleCache(ttl=ttl)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await async_memoize_wrapper(func, cache, *args, **kwargs)

        wrapper.cache = cache  # type: ignore
        return wrapper

    return decorator


class PerformanceMonitor:
    """Monitor performance metrics."""

    def __init__(self):
        """Initialize monitor."""
        self.metrics: dict[str, list[float]] = {}

    def record(self, name: str, duration: float) -> None:
        """Record a timing metric.

        Args:
            name: Metric name
            duration: Duration in seconds
        """
        if name not in self.metrics:
            self.metrics[name] = []
        self.metrics[name].append(duration)

    def get_stats(self, name: str) -> dict[str, float]:
        """Get statistics for a metric.

        Args:
            name: Metric name

        Returns:
            Dict with count, total, avg, min, max
        """
        if name not in self.metrics or not self.metrics[name]:
            return {
                "count": 0,
                "total": 0,
                "avg": 0,
                "min": 0,
                "max": 0,
            }

        values = self.metrics[name]
        return {
            "count": len(values),
            "total": sum(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    def get_all_stats(self) -> dict[str, dict[str, float]]:
        """Get statistics for all metrics.

        Returns:
            Dict mapping metric names to their stats
        """
        return {name: self.get_stats(name) for name in self.metrics}


def timed(monitor: PerformanceMonitor, name: str | None = None):
    """Decorator to time function execution.

    Args:
        monitor: Performance monitor instance
        name: Metric name (defaults to function name)

    Returns:
        Decorator function
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        metric_name = name or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            start = time.time()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.time() - start
                monitor.record(metric_name, duration)

        return wrapper

    return decorator


def async_timed(monitor: PerformanceMonitor, name: str | None = None):
    """Decorator to time async function execution.

    Args:
        monitor: Performance monitor instance
        name: Metric name (defaults to function name)

    Returns:
        Decorator function
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        metric_name = name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.time()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.time() - start
                monitor.record(metric_name, duration)

        return wrapper

    return decorator


# Global performance monitor instance
global_monitor = PerformanceMonitor()
