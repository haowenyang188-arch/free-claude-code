import os
from unittest.mock import patch

import pytest


def test_process_registry_register_pid_zero_noop():
    """register_pid(0) is a no-op (early return)."""
    from cli import process_registry as pr

    before = len(pr._pids)
    pr.register_pid(0)
    assert len(pr._pids) == before


def test_process_registry_unregister_pid_zero_noop():
    """unregister_pid(0) is a no-op."""
    from cli import process_registry as pr

    pr.register_pid(99999)
    pr.unregister_pid(0)
    assert 99999 in pr._pids
    pr.unregister_pid(99999)


def test_process_registry_ensure_atexit_idempotent():
    """Second call to ensure_atexit_registered is idempotent."""
    from cli import process_registry as pr

    pr.ensure_atexit_registered()
    pr.ensure_atexit_registered()
    # Should not raise; atexit handler registered once


def test_process_registry_kill_all_exception_logged_no_raise(monkeypatch):
    """Exception in os.kill/taskkill is logged but does not raise."""
    from cli import process_registry as pr

    monkeypatch.setattr(pr, "_pids", {99999})
    monkeypatch.setattr(os, "name", "posix", raising=False)

    def _kill_raises(pid, sig):
        raise ProcessLookupError("no such process")

    with patch("os.kill", _kill_raises):
        pr.kill_all_best_effort()
    # Should not raise


def test_process_registry_register_unregister_does_not_crash():
    from cli import process_registry as pr

    pr.register_pid(12345)
    pr.unregister_pid(12345)


def test_process_registry_kill_all_best_effort_empty_is_noop():
    from cli import process_registry as pr

    # Ensure no exception on empty set
    pr.kill_all_best_effort()


def test_process_registry_kill_all_best_effort_windows_noop_when_taskkill_missing(
    monkeypatch,
):
    from cli import process_registry as pr

    # Simulate windows path in a stable way.
    monkeypatch.setattr(pr, "_pids", {12345})
    monkeypatch.setattr(os, "name", "nt", raising=False)

    # If taskkill isn't callable, we still should not crash.
    import subprocess

    def _boom(*args, **kwargs):
        raise FileNotFoundError("taskkill missing")

    monkeypatch.setattr(subprocess, "run", _boom)
    pr.kill_all_best_effort()


def test_generation_lease_unregister_requires_exact_generation(monkeypatch):
    from cli import process_registry as pr

    monkeypatch.setattr(pr, "_leases", {})
    monkeypatch.setattr(pr, "_process_fingerprint", lambda _pid: "start-1")

    lease = pr.register_process(4321, generation="gen-1")

    assert lease is not None
    assert pr.unregister_process(lease.pid, generation="other") is False
    assert 4321 in pr._leases
    assert pr.unregister_process(lease.pid, generation="gen-1") is True
    assert 4321 not in pr._leases


def test_register_process_rejects_conflicting_owner(monkeypatch):
    from cli import process_registry as pr

    monkeypatch.setattr(pr, "_leases", {})
    monkeypatch.setattr(pr, "_process_fingerprint", lambda _pid: "start-1")
    pr.register_process(4321, generation="gen-1")

    with pytest.raises(pr.ProcessOwnershipError, match="already registered"):
        pr.register_process(4321, generation="gen-2")


def test_kill_all_skips_pid_when_process_fingerprint_changed(monkeypatch):
    from cli import process_registry as pr

    monkeypatch.setattr(pr, "_pids", set())
    monkeypatch.setattr(pr, "_leases", {})
    monkeypatch.setattr(pr, "_process_fingerprint", lambda _pid: "start-1")
    pr.register_process(4321, generation="gen-1")
    monkeypatch.setattr(pr, "_process_fingerprint", lambda _pid: "start-2")
    killed: list[int] = []
    monkeypatch.setattr(os, "name", "posix", raising=False)
    monkeypatch.setattr(os, "kill", lambda pid, _sig: killed.append(pid))

    pr.kill_all_best_effort()

    assert killed == []
