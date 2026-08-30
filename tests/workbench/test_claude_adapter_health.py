from __future__ import annotations

import pytest

from workbench.backend.agents.claude_adapter import ClaudeCodeAdapter


@pytest.mark.asyncio
async def test_claude_hook_health_is_disabled_by_default() -> None:
    adapter = ClaudeCodeAdapter("agent-1")

    health = (await adapter.get_status())["approval_health"]

    assert health == {
        "hook_installed": False,
        "hook_trust": "disabled",
        "hook_active": False,
        "last_hook_decision": None,
        "runtime_result": "not_probed",
        "status": "DISABLED",
    }


@pytest.mark.asyncio
async def test_claude_hook_configuration_is_not_activity_evidence() -> None:
    adapter = ClaudeCodeAdapter("agent-1", hook_installed=True)

    health = (await adapter.get_status())["approval_health"]

    assert health["hook_installed"] is True
    assert health["hook_trust"] == "unverified"
    assert health["hook_active"] is False
    assert health["status"] == "HOOK_REGISTERED_BUT_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_claude_hook_activity_requires_current_generation_and_contract() -> None:
    adapter = ClaudeCodeAdapter("agent-1", hook_installed=True)
    adapter.generation = "generation-1"

    invalid_events = (
        {
            "backend": "claude",
            "event": "PreToolUse",
            "generation": "old-generation",
            "decision": "silent",
        },
        {
            "backend": "codex",
            "event": "PreToolUse",
            "generation": "generation-1",
            "decision": "silent",
        },
        {
            "backend": "claude",
            "event": "Notification",
            "generation": "generation-1",
            "decision": "silent",
        },
        {
            "backend": "claude",
            "event": "PreToolUse",
            "generation": "generation-1",
            "decision": "ask",
        },
    )

    for event in invalid_events:
        adapter.observe_hook_activity(event)

    health = (await adapter.get_status())["approval_health"]
    assert health["hook_active"] is False
    assert health["hook_trust"] == "unverified"
    assert health["status"] == "HOOK_REGISTERED_BUT_NOT_ACTIVE"
    assert not adapter.record_hook_runtime_result("failed")
    assert (await adapter.get_status())["approval_health"]["runtime_result"] == (
        "not_probed"
    )

    adapter.observe_hook_activity(
        {
            "backend": "claude",
            "event": "PermissionRequest",
            "generation": "generation-1",
            "decision": "deny",
        }
    )

    health = (await adapter.get_status())["approval_health"]
    assert health["hook_active"] is True
    assert health["hook_trust"] == "trusted"
    assert health["last_hook_decision"] == "deny"
    assert health["status"] == "ACTIVE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        ("timeout", "TIMEOUT"),
        ("environment limitation", "ENVIRONMENT_LIMITATION"),
        ("failed", "FAILED"),
    ],
)
async def test_claude_hook_runtime_result_is_explicitly_classified(
    result: str, expected_status: str
) -> None:
    adapter = ClaudeCodeAdapter("agent-1", hook_installed=True)

    adapter.record_hook_runtime_result(result)
    health = (await adapter.get_status())["approval_health"]

    assert health["runtime_result"] == result.replace(" ", "_")
    assert health["status"] == expected_status
    assert health["hook_active"] is False


@pytest.mark.asyncio
async def test_claude_hook_runtime_result_is_bound_to_generation() -> None:
    adapter = ClaudeCodeAdapter("agent-1", hook_installed=True)
    adapter.generation = "generation-1"

    assert adapter.record_hook_runtime_result("timeout", generation="generation-1")
    assert (await adapter.get_status())["approval_health"]["status"] == "TIMEOUT"

    # A new run resets the health contract.  A delayed result from the old run
    # must not be allowed to poison the new generation.
    adapter.generation = "generation-2"
    adapter._reset_approval_health()
    assert not adapter.record_hook_runtime_result("failed", generation="generation-1")
    health = (await adapter.get_status())["approval_health"]
    assert health["runtime_result"] == "not_probed"
    assert health["status"] == "HOOK_REGISTERED_BUT_NOT_ACTIVE"

    # Omitting the generation while a run is active is also fail-closed.
    assert not adapter.record_hook_runtime_result("timeout")
    assert (await adapter.get_status())["approval_health"]["runtime_result"] == (
        "not_probed"
    )


@pytest.mark.asyncio
async def test_claude_hook_failure_takes_precedence_over_active() -> None:
    adapter = ClaudeCodeAdapter("agent-1", hook_installed=True)
    adapter.generation = "generation-1"
    adapter.observe_hook_activity(
        {
            "backend": "claude",
            "event": "PreToolUse",
            "generation": "generation-1",
            "decision": "silent",
        }
    )
    assert (await adapter.get_status())["approval_health"]["status"] == "ACTIVE"

    adapter.record_hook_runtime_result(
        "environment limitation", generation="generation-1"
    )
    health = (await adapter.get_status())["approval_health"]
    assert health["status"] == "ENVIRONMENT_LIMITATION"
    assert health["hook_active"] is False


@pytest.mark.asyncio
async def test_claude_cleanup_clears_active_hook_health() -> None:
    adapter = ClaudeCodeAdapter("agent-1", hook_installed=True)
    adapter.generation = "generation-1"
    adapter.observe_hook_activity(
        {
            "backend": "claude",
            "event": "PermissionRequest",
            "generation": "generation-1",
            "decision": "deny",
        }
    )
    assert (await adapter.get_status())["approval_health"]["status"] == "ACTIVE"

    await adapter.cleanup()
    health = (await adapter.get_status())["approval_health"]
    assert adapter.generation is None
    assert health["hook_active"] is False
    assert health["hook_trust"] == "unverified"
    assert health["status"] == "HOOK_REGISTERED_BUT_NOT_ACTIVE"


def test_claude_hook_runtime_result_rejects_unknown_classification() -> None:
    adapter = ClaudeCodeAdapter("agent-1", hook_installed=True)

    with pytest.raises(ValueError, match="unsupported Claude hook runtime result"):
        adapter.record_hook_runtime_result("pass")
