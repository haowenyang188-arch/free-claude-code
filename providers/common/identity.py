"""Validated, provider-neutral execution identity metadata.

Provider payloads use different names for the same lifecycle identifiers.  The
normalizer keeps those identifiers optional and never invents a value when the
runtime did not provide one.  Payloads and secrets are intentionally outside
this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IDENTITY_FIELDS = (
    "provider",
    "runtime_id",
    "session_id",
    "generation",
    "thread_id",
    "turn_id",
    "item_id",
    "approval_id",
    "one_shot_id",
    "agent_id",
    "tool_id",
    "call_id",
    "message_id",
    "run_id",
)

_ALIASES: dict[str, tuple[str, ...]] = {
    "provider": ("provider", "provider_id"),
    "runtime_id": ("runtime_id", "runtimeId", "runtime_session_id", "runtimeSessionId"),
    "session_id": ("session_id", "sessionId"),
    "generation": ("generation",),
    "thread_id": ("thread_id", "threadId"),
    "turn_id": ("turn_id", "turnId"),
    "item_id": ("item_id", "itemId"),
    "approval_id": ("approval_id", "approvalId"),
    "one_shot_id": ("one_shot_id", "oneShotId", "oneshot_id", "oneShotID"),
    "agent_id": ("agent_id", "agentId"),
    "tool_id": ("tool_id", "toolId", "tool_use_id", "toolUseId"),
    "call_id": ("call_id", "callId"),
    "message_id": ("message_id", "messageId"),
    "run_id": ("run_id", "runId"),
}


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 512 or any(ord(char) < 0x20 for char in value):
        return None
    return value


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Optional identifiers used to correlate one provider execution."""

    provider: str | None = None
    runtime_id: str | None = None
    session_id: str | None = None
    generation: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    approval_id: str | None = None
    one_shot_id: str | None = None
    agent_id: str | None = None
    tool_id: str | None = None
    call_id: str | None = None
    message_id: str | None = None
    run_id: str | None = None

    def to_mapping(self, *, include_unknown: bool = True) -> dict[str, str | None]:
        """Return stable JSON metadata without copying arbitrary payload data."""
        values = {field: getattr(self, field) for field in IDENTITY_FIELDS}
        return (
            values
            if include_unknown
            else {key: value for key, value in values.items() if value is not None}
        )

    @classmethod
    def from_mapping(cls, value: Any) -> RuntimeIdentity:
        """Normalize known aliases from an untrusted provider mapping."""
        if not hasattr(value, "get"):
            return cls()
        return cls(
            **{
                field: next(
                    (
                        normalized
                        for alias in aliases
                        if (normalized := _identifier(value.get(alias))) is not None
                    ),
                    None,
                )
                for field, aliases in _ALIASES.items()
            }
        )

    def merge(self, *values: RuntimeIdentity | None) -> RuntimeIdentity:
        """Fill missing fields from later identities without overwriting truth."""
        result = self.to_mapping()
        for value in values:
            if value is None:
                continue
            for field in IDENTITY_FIELDS:
                if result[field] is None:
                    result[field] = getattr(value, field)
        return RuntimeIdentity(**result)


def normalize_identity(
    value: Any, *fallbacks: RuntimeIdentity | None
) -> RuntimeIdentity:
    """Normalize a mapping and merge explicit fallback identities."""
    return RuntimeIdentity.from_mapping(value).merge(*fallbacks)


__all__ = ["IDENTITY_FIELDS", "RuntimeIdentity", "normalize_identity"]
