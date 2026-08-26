"""Build a minimal environment for local Codex and Claude CLI processes.

The execution process must not inherit arbitrary provider, proxy, MCP, or
credential state from the gateway.  This module keeps a small runtime baseline
and requires each credential or routing override to be projected explicitly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from .runtime_registry import RuntimeBackend

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_BASE_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "PATH",
        "TERM",
        "TMP",
        "TMPDIR",
        "TEMP",
        "TZ",
        "XDG_RUNTIME_DIR",
    }
)
_DEFAULT_VALUES = {"TERM": "dumb", "PYTHONIOENCODING": "utf-8"}
_CREDENTIAL_KEYS = {
    RuntimeBackend.CLAUDE: frozenset(
        {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}
    ),
    RuntimeBackend.CODEX: frozenset({"CODEX_API_KEY", "OPENAI_API_KEY"}),
}
_ROUTING_KEYS = {
    RuntimeBackend.CLAUDE: frozenset({"ANTHROPIC_API_URL", "ANTHROPIC_BASE_URL"}),
    RuntimeBackend.CODEX: frozenset({"CODEX_BASE_URL", "OPENAI_API_BASE", "OPENAI_BASE_URL"}),
}
_SENSITIVE_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "AUTH")


def build_cli_environment(
    backend: str | RuntimeBackend,
    *,
    parent_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    allow_credentials: bool = False,
) -> dict[str, str]:
    """Return a deterministic, least-privilege child environment.

    ``parent_env`` is validated before filtering, so malformed inherited state
    fails before a subprocess is created.  Credentials and routing variables
    never flow through implicitly.  They may only be passed in ``extra_env``
    with the appropriate explicit authorization.
    """
    runtime = _coerce_backend(backend)
    if not isinstance(allow_credentials, bool):
        raise ValueError("allow_credentials must be a boolean")

    inherited = _validate_environment(
        os.environ if parent_env is None else parent_env, "parent_env"
    )
    overrides = _validate_environment(extra_env or {}, "extra_env")

    result = {
        key: value
        for key, value in inherited.items()
        if _is_safe_base_key(key) and key not in _DEFAULT_VALUES
    }
    result.setdefault("PATH", os.defpath)
    result.update(_DEFAULT_VALUES)

    if allow_credentials:
        for key in _CREDENTIAL_KEYS[runtime]:
            value = inherited.get(key)
            if value is not None:
                result[key] = value

    allowed_overrides = set(_SAFE_BASE_KEYS) | {
        key for key in overrides if key.startswith("LC_")
    }
    allowed_overrides.update(_ROUTING_KEYS[runtime])
    if allow_credentials:
        allowed_overrides.update(_CREDENTIAL_KEYS[runtime])

    for key, value in overrides.items():
        if key not in allowed_overrides:
            if key in _CREDENTIAL_KEYS[runtime] or _looks_sensitive(key):
                raise ValueError(
                    f"extra environment key {key!r} requires allow_credentials=True"
                )
            raise ValueError(f"extra environment key {key!r} is not allowlisted")
        result[key] = value

    return {key: result[key] for key in sorted(result)}


def describe_cli_environment(
    environment: Mapping[str, str],
    *,
    backend: str | RuntimeBackend,
) -> dict[str, Any]:
    """Describe a projected environment without exposing any values."""
    runtime = _coerce_backend(backend)
    validated = _validate_environment(environment, "environment")
    keys = sorted(validated)
    return {
        "backend": runtime.value,
        "key_count": len(keys),
        "keys": keys,
        "credential_keys": [key for key in keys if _looks_sensitive(key)],
        "routing_keys": [key for key in keys if key in _ROUTING_KEYS[runtime]],
    }


def _coerce_backend(value: str | RuntimeBackend) -> RuntimeBackend:
    try:
        return RuntimeBackend(value)
    except ValueError as exc:
        raise ValueError(f"unsupported runtime backend: {value!r}") from exc


def _validate_environment(
    environment: Mapping[str, str],
    field: str,
) -> dict[str, str]:
    if not isinstance(environment, Mapping):
        raise ValueError(f"{field} must be a mapping")

    result: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or _ENV_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"environment key is invalid in {field}")
        if not isinstance(value, str):
            raise ValueError(f"environment value for {key!r} must be a string")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"environment value for {key!r} must not contain NUL or newline")
        result[key] = value
    return result


def _is_safe_base_key(key: str) -> bool:
    return key in _SAFE_BASE_KEYS or key.startswith("LC_")


def _looks_sensitive(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in _SENSITIVE_PARTS)


__all__ = ["build_cli_environment", "describe_cli_environment"]
