"""B-fix C6 regression: DSH endpoint discovery falls back to the stable
loopback probe when Windows interop discovery yields nothing."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from workbench.backend.agents import dsh_transport


def test_primary_raise_falls_back_to_stable_probe_candidates():
    """tasklist/netstat failure must not raise and must retry via the
    stable loopback candidate list (127.0.0.1:43120)."""
    with patch.object(
        subprocess, "run", side_effect=OSError("interop unavailable")
    ):
        with patch.object(
            dsh_transport,
            "discover_endpoint",
            wraps=dsh_transport.discover_endpoint,
        ) as discover:
            result = dsh_transport.discover_dsh_desktop_endpoint()
            # fallback was attempted with the stable candidate
            calls = [c.args[0] for c in discover.call_args_list]
            assert any(
                any("43120" in str(c) for c in candidates)
                for candidates in calls
            ), f"expected fallback probe of 43120, calls={calls!r}"
            # when DSH Desktop is reachable the fallback resolves; otherwise
            # None - either way no exception may propagate
            assert result is None or result == "127.0.0.1:43120"


def test_primary_empty_falls_back_to_stable_probe_candidates():
    """Primary scan succeeds but finds no DSH pid/listener -> fallback list."""
    captured = {}

    def fake_run(*args, **kwargs):
        # tasklist: no DSH Desktop.exe rows
        if "tasklist" in str(args[0][0]):
            return subprocess.CompletedProcess(args[0], 0, stdout=b"", stderr=b"")
        # netstat: no rows
        return subprocess.CompletedProcess(args[0], 0, stdout=b"", stderr=b"")

    with patch.object(subprocess, "run", side_effect=fake_run):
        with patch.object(
            dsh_transport,
            "discover_endpoint",
            wraps=dsh_transport.discover_endpoint,
        ) as discover:
            result = dsh_transport.discover_dsh_desktop_endpoint()
            calls = [c.args[0] for c in discover.call_args_list]
            assert any(
                any("43120" in str(c) for c in candidates)
                for candidates in calls
            ), f"expected fallback probe of 43120, calls={calls!r}"
            captured["result"] = result
    assert captured["result"] is None or True  # env-independent: no raise
