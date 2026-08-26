from __future__ import annotations

import json

import pytest


def _manifest(**overrides):
    from cli.checkpoint import CheckpointManifest

    values = {
        "checkpoint_id": "cp-1",
        "run_id": "run-1",
        "backend": "codex",
        "runtime_version": "0.149.1",
        "session_id": "thread-1",
        "generation": "gen-1",
        "workspace_digest": "workspace-sha",
        "policy_digest": "policy-sha",
        "step_id": "step-1",
        "input_digest": "input-sha",
        "side_effects_allowed": False,
    }
    values.update(overrides)
    return CheckpointManifest(**values)


def test_manifest_round_trips_and_redacts_no_payload_values() -> None:
    manifest = _manifest()

    encoded = manifest.to_mapping()
    restored = type(manifest).from_mapping(encoded)

    assert restored == manifest
    assert encoded["schema_version"] == 1
    assert "api_key" not in json.dumps(encoded).lower()


def test_manifest_rejects_side_effecting_checkpoint() -> None:
    from cli.checkpoint import CheckpointError

    with pytest.raises(CheckpointError, match="side_effects_allowed"):
        _manifest(side_effects_allowed=True)


def test_manifest_compatibility_requires_exact_execution_identity() -> None:
    manifest = _manifest()

    assert manifest.is_compatible(
        backend="codex",
        runtime_version="0.149.1",
        session_id="thread-1",
        generation="gen-1",
        workspace_digest="workspace-sha",
        policy_digest="policy-sha",
    ) is True
    assert manifest.is_compatible(
        backend="codex",
        runtime_version="0.149.2",
        session_id="thread-1",
        generation="gen-1",
        workspace_digest="workspace-sha",
        policy_digest="policy-sha",
    ) is False


def test_store_writes_atomically_and_loads_manifest(tmp_path) -> None:
    from cli.checkpoint import CheckpointStore

    path = tmp_path / "checkpoint.json"
    store = CheckpointStore(path)
    manifest = _manifest()

    store.save(manifest)

    assert CheckpointStore(path).load() == manifest
    assert not list(tmp_path.glob("*.tmp"))


def test_store_rejects_invalid_or_tampered_manifest(tmp_path) -> None:
    from cli.checkpoint import CheckpointError, CheckpointStore

    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    with pytest.raises(CheckpointError, match="invalid checkpoint"):
        CheckpointStore(path).load()
