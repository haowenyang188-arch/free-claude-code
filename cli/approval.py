"""Protocol-first approval policy and terminal prompt parsing.

The module deliberately contains no window, mouse, OCR, or network code.  It
can be used by CLI hooks and by a later PTY adapter without making either path
responsible for the other's side effects.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ApprovalDecision(StrEnum):
    """Decision emitted by the local policy."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ApprovalScope(StrEnum):
    """How long an approval grant is intended to live."""

    ONCE = "once"
    SESSION = "session"
    PERMANENT = "permanent"


class ApprovalPromptKind(StrEnum):
    """Prompt family inferred from the rendered terminal menu."""

    COMMAND = "command"
    FILE = "file"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A normalized, untrusted request arriving at the policy boundary."""

    backend: str
    tool_name: str
    command: str | None = None
    workspace: str | None = None
    prompt: str = ""
    scope: ApprovalScope = ApprovalScope.ONCE
    process_id: int | None = None
    generation: str | None = None

    def __post_init__(self) -> None:
        backend = _required_text(self.backend, "backend").lower()
        tool_name = _required_text(self.tool_name, "tool_name")
        command = _optional_text(self.command, "command")
        workspace = _optional_text(self.workspace, "workspace")
        prompt = _bounded_text(self.prompt, "prompt", 16_384)
        scope = self.scope
        if not isinstance(scope, ApprovalScope):
            try:
                scope = ApprovalScope(str(scope))
            except ValueError as exc:
                raise ValueError("scope must be once, session, or permanent") from exc
        if self.process_id is not None and (
            isinstance(self.process_id, bool) or not isinstance(self.process_id, int)
        ):
            raise ValueError("process_id must be an integer")
        generation = _optional_text(self.generation, "generation")

        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "generation", generation)

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        backend: str | None = None,
    ) -> ApprovalRequest:
        """Build a request from Claude/Codex hook-shaped JSON."""
        if not isinstance(payload, Mapping):
            raise ValueError("approval payload must be an object")

        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            tool_input = payload.get("toolInput")
        if not isinstance(tool_input, Mapping):
            tool_input = {}

        resolved_backend = backend or payload.get("backend")
        if not isinstance(resolved_backend, str) or not resolved_backend.strip():
            resolved_backend = (
                "codex" if payload.get("turn_id") or payload.get("turnId") else "claude"
            )

        tool_name = payload.get("tool_name") or payload.get("toolName")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("approval payload is missing tool_name")

        command = tool_input.get("command") or tool_input.get("cmd")
        if not isinstance(command, str):
            command = None

        workspace = payload.get("cwd") or payload.get("workspace")
        if not isinstance(workspace, str):
            workspace = None

        prompt = payload.get("prompt") or payload.get("message") or ""
        if not isinstance(prompt, str):
            prompt = ""

        raw_scope = (
            payload.get("approval_scope")
            or payload.get("approvalScope")
            or tool_input.get("approval_scope")
            or tool_input.get("approvalScope")
            or ApprovalScope.ONCE.value
        )

        process_id = payload.get("process_id") or payload.get("pid")
        if process_id is not None and not isinstance(process_id, int):
            process_id = None

        generation = payload.get("generation")
        if not isinstance(generation, str):
            generation = None

        return cls(
            backend=resolved_backend,
            tool_name=tool_name,
            command=command,
            workspace=workspace,
            prompt=prompt,
            scope=raw_scope,
            process_id=process_id,
            generation=generation,
        )


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    """The policy result, safe to expose to a hook caller."""

    decision: ApprovalDecision
    reason: str
    scope: ApprovalScope | None = None
    matched_rule: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """An exact token-prefix rule for a safe command."""

    command_prefix: str
    max_scope: ApprovalScope = ApprovalScope.ONCE
    backends: frozenset[str] = field(
        default_factory=lambda: frozenset({"claude", "codex"})
    )
    workspace: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalOption:
    """One option rendered in a terminal approval menu."""

    label: str
    decision: ApprovalDecision
    scope: ApprovalScope | None = None
    key: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedApprovalPrompt:
    """A complete approval menu extracted from PTY text."""

    kind: ApprovalPromptKind
    prompt_text: str
    options: tuple[ApprovalOption, ...]
    command_prefix: str | None
    signature: str

    def option_for_scope(self, scope: ApprovalScope) -> ApprovalOption | None:
        """Return the first affirmative option with the requested scope."""
        return next(
            (
                option
                for option in self.options
                if option.decision is ApprovalDecision.ALLOW and option.scope is scope
            ),
            None,
        )


DEFAULT_SAFE_COMMAND_PREFIXES = (
    "pwd",
    "ls",
    "dir",
    "cat",
    "head",
    "tail",
    "rg",
    "grep",
    "find",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch --show-current",
    "git rev-parse",
)

DEFAULT_SAFE_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "TodoRead"})

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", re.ASCII)
_CONTROL_RE = re.compile(r"[\x00\r\n]")
_SHELL_SYNTAX_RE = re.compile(r"(?:[;&|<>`]|\$\(|\$\{|\\\n)")
_DANGEROUS_RE = re.compile(
    r"(?ix)"
    r"(?:^|[;&|]\s*|\$\(\s*|`\s*)"
    r"(?:"
    r"sudo\b|rm\b|rmdir\b|del\b|erase\b|format\b|"
    r"git\s+(?:reset\s+--hard|clean\b|push\s+--force(?:\s|$)|branch\s+-D\b)|"
    r"(?:dd|mkfs)\b|"
    r"(?:shutdown|reboot)\b|"
    r"(?:powershell|pwsh)\s+.*(?:-enc(?:odedcommand)?\b|-command\b)|"
    r"(?:python|python3|node|ruby)\s+-c\b|"
    r"npm\s+publish\b|"
    r"docker\s+system\s+prune\b"
    r")"
)
_SCOPE_RANK = {
    ApprovalScope.ONCE: 0,
    ApprovalScope.SESSION: 1,
    ApprovalScope.PERMANENT: 2,
}


class ApprovalPolicy:
    """Deterministic, opt-in policy for low-risk approval requests."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        allowed_command_prefixes: list[str] | tuple[str, ...] | None = None,
        allowed_workspaces: list[str | os.PathLike[str]]
        | tuple[str | os.PathLike[str], ...]
        | None = None,
        safe_tools: frozenset[str] | set[str] | tuple[str, ...] | None = None,
        denied_command_prefixes: list[str] | tuple[str, ...] | None = None,
        max_auto_scope: ApprovalScope = ApprovalScope.ONCE,
        allow_permanent: bool = False,
        backends: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    ) -> None:
        if not isinstance(max_auto_scope, ApprovalScope):
            max_auto_scope = ApprovalScope(str(max_auto_scope))
        self.enabled = bool(enabled)
        self.allowed_command_prefixes = tuple(
            _normalize_prefix(value)
            for value in (allowed_command_prefixes or ())
            if _normalize_prefix(value)
        )
        self.denied_command_prefixes = tuple(
            _normalize_prefix(value)
            for value in (denied_command_prefixes or ())
            if _normalize_prefix(value)
        )
        self.allowed_workspaces = tuple(
            _resolve_path(value) for value in (allowed_workspaces or ())
        )
        self.safe_tools = frozenset(safe_tools or DEFAULT_SAFE_TOOLS)
        self.max_auto_scope = max_auto_scope
        self.allow_permanent = bool(allow_permanent)
        self.backends = frozenset(
            str(value).strip().lower() for value in (backends or ("claude", "codex"))
        )

    @classmethod
    def low_risk(
        cls,
        *,
        enabled: bool = False,
        allowed_workspaces: list[str | os.PathLike[str]]
        | tuple[str | os.PathLike[str], ...]
        | None = None,
        max_auto_scope: ApprovalScope = ApprovalScope.ONCE,
    ) -> ApprovalPolicy:
        """Create the conservative built-in read-only policy."""
        return cls(
            enabled=enabled,
            allowed_command_prefixes=DEFAULT_SAFE_COMMAND_PREFIXES,
            allowed_workspaces=allowed_workspaces,
            max_auto_scope=max_auto_scope,
        )

    @classmethod
    def from_environment(cls) -> ApprovalPolicy:
        """Load the explicit hook configuration without exposing secrets."""
        enabled = _parse_bool(os.environ.get("FCC_APPROVAL_ENABLED", "false"))
        scope = ApprovalScope(
            os.environ.get("FCC_APPROVAL_SCOPE", ApprovalScope.ONCE.value).strip()
        )
        commands = _split_env_list(os.environ.get("FCC_APPROVAL_COMMANDS", ""))
        workspaces = _split_env_list(os.environ.get("FCC_APPROVAL_WORKSPACES", ""))
        return cls(
            enabled=enabled,
            allowed_command_prefixes=commands or DEFAULT_SAFE_COMMAND_PREFIXES,
            allowed_workspaces=workspaces,
            max_auto_scope=scope,
            allow_permanent=_parse_bool(
                os.environ.get("FCC_APPROVAL_ALLOW_PERMANENT", "false")
            ),
        )

    def evaluate(self, request: ApprovalRequest) -> ApprovalResult:
        """Evaluate a request; anything uncertain remains an interactive ask."""
        if not self.enabled:
            return ApprovalResult(
                ApprovalDecision.ASK, "automatic approval is disabled"
            )
        if request.backend not in self.backends:
            return ApprovalResult(ApprovalDecision.ASK, "backend is not enabled")
        if not self._workspace_is_allowed(request.workspace):
            return ApprovalResult(
                ApprovalDecision.ASK,
                "workspace is outside the approval allowlist",
            )
        if request.scope is ApprovalScope.PERMANENT and not self.allow_permanent:
            return ApprovalResult(
                ApprovalDecision.ASK,
                "permanent approval requires explicit opt-in",
            )
        if _SCOPE_RANK[request.scope] > _SCOPE_RANK[self.max_auto_scope]:
            return ApprovalResult(
                ApprovalDecision.ASK,
                "requested approval scope exceeds the configured limit",
            )

        tool_name = request.tool_name.lower()
        if tool_name in {tool.lower() for tool in self.safe_tools}:
            return ApprovalResult(
                ApprovalDecision.ALLOW,
                "matched a read-only tool",
                scope=request.scope,
                matched_rule=request.tool_name,
            )

        if tool_name not in {"bash", "shell", "command_execution", "powershell"}:
            return ApprovalResult(
                ApprovalDecision.ASK,
                "tool is not covered by the low-risk policy",
            )
        if not request.command:
            return ApprovalResult(ApprovalDecision.ASK, "command text is unavailable")

        if reason := _dangerous_command_reason(request.command):
            return ApprovalResult(ApprovalDecision.DENY, reason)

        tokens = _simple_command_tokens(request.command)
        if tokens is None:
            return ApprovalResult(
                ApprovalDecision.ASK,
                "compound or unparseable shell syntax requires review",
            )

        for prefix in self.denied_command_prefixes:
            if _tokens_match_prefix(tokens, _prefix_tokens(prefix)):
                return ApprovalResult(
                    ApprovalDecision.DENY,
                    "command matches an explicit deny rule",
                    matched_rule=prefix,
                )

        for prefix in self.allowed_command_prefixes:
            if _tokens_match_prefix(tokens, _prefix_tokens(prefix)):
                return ApprovalResult(
                    ApprovalDecision.ALLOW,
                    "matched a low-risk command rule",
                    scope=request.scope,
                    matched_rule=prefix,
                )

        return ApprovalResult(ApprovalDecision.ASK, "command is not in the allowlist")

    def _workspace_is_allowed(self, workspace: str | None) -> bool:
        if not self.allowed_workspaces:
            return True
        if not workspace:
            return False
        resolved_workspace = _resolve_path(workspace)
        if resolved_workspace is None:
            return False
        for root in self.allowed_workspaces:
            if root is None:
                continue
            try:
                resolved_workspace.relative_to(root)
            except ValueError:
                continue
            return True
        return False


class ApprovalPromptParser:
    """Parse complete Codex/Claude terminal approval menus conservatively."""

    def parse(self, text: str) -> ParsedApprovalPrompt | None:
        cleaned = _strip_ansi(text)
        lines = []
        for raw_line in cleaned.splitlines():
            line = re.sub(r"^\s*[>›▸]\s*", "", raw_line).strip()
            line = re.sub(r"\s+", " ", line)
            if line:
                lines.append(line)
        if not lines:
            return None

        options: list[ApprovalOption] = []
        for line in lines:
            option = self._parse_option(line)
            if option is not None:
                options.append(option)

        has_allow = any(option.decision is ApprovalDecision.ALLOW for option in options)
        has_deny = any(option.decision is ApprovalDecision.DENY for option in options)
        if not has_allow or not has_deny:
            return None

        prompt_text = "\n".join(lines)
        lowered = prompt_text.lower()
        if re.search(r"\b(?:file|files|edit|edits|changes?)\b", lowered):
            kind = ApprovalPromptKind.FILE
        elif re.search(r"\b(?:command|commands|shell|bash|powershell)\b", lowered):
            kind = ApprovalPromptKind.COMMAND
        else:
            kind = ApprovalPromptKind.UNKNOWN

        command_prefix = _extract_command_prefix(prompt_text)
        signature = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        return ParsedApprovalPrompt(
            kind=kind,
            prompt_text=prompt_text,
            options=tuple(options),
            command_prefix=command_prefix,
            signature=signature,
        )

    @staticmethod
    def _parse_option(line: str) -> ApprovalOption | None:
        match = re.match(
            r"^(?:\d+[.)]\s*)?(?P<label>.*?)(?:\s+\((?P<key>[^()]+)\))?$",
            line,
        )
        if match is None:
            return None
        label = match.group("label").strip()
        key = match.group("key")
        key = key.strip().lower() if key else None
        lowered = label.lower()
        is_allow = bool(re.search(r"\b(?:yes|allow|approve|proceed)\b", lowered))
        is_deny = bool(
            re.search(r"\b(?:no|cancel|deny|dismiss|abort|esc(?:ape)?)\b", lowered)
        )
        if not is_allow and not is_deny:
            return None

        if is_deny and not is_allow:
            return ApprovalOption(
                label=label,
                decision=ApprovalDecision.DENY,
                key=key,
            )

        if re.search(
            r"(?:always\s+allow|don't\s+ask\s+again|do\s+not\s+ask\s+again|"
            r"commands?\s+that\s+start\s+with)",
            lowered,
        ):
            scope = ApprovalScope.PERMANENT
        elif re.search(r"(?:for\s+this\s+session|in\s+this\s+session)", lowered):
            scope = ApprovalScope.SESSION
        else:
            scope = ApprovalScope.ONCE

        return ApprovalOption(
            label=label,
            decision=ApprovalDecision.ALLOW,
            scope=scope,
            key=key,
        )


class ApprovalHook:
    """Adapt policy results to Claude and Codex hook output schemas."""

    def __init__(self, policy: ApprovalPolicy) -> None:
        self.policy = policy

    @staticmethod
    def policy_for(
        *,
        enabled: bool = False,
        allowed_workspaces: list[str | os.PathLike[str]]
        | tuple[str | os.PathLike[str], ...]
        | None = None,
    ) -> ApprovalPolicy:
        return ApprovalPolicy.low_risk(
            enabled=enabled,
            allowed_workspaces=allowed_workspaces,
        )

    def handle_payload(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            request = ApprovalRequest.from_mapping(payload)
        except TypeError, ValueError:
            return None

        result = self.policy.evaluate(request)
        if result.decision is ApprovalDecision.ASK:
            return None

        event_name = payload.get("hook_event_name") or payload.get("hookEventName")
        if event_name == "PermissionRequest":
            decision: dict[str, Any] = {"behavior": result.decision.value}
            if result.decision is ApprovalDecision.DENY:
                decision["message"] = result.reason
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                }
            }

        if event_name in {None, "PreToolUse"}:
            output: dict[str, Any] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": result.decision.value,
                "permissionDecisionReason": result.reason,
            }
            return {"hookSpecificOutput": output}

        return None


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if _CONTROL_RE.search(normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _bounded_text(value: object, field_name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if len(value) > limit:
        raise ValueError(f"{field_name} exceeds the size limit")
    if "\x00" in value:
        raise ValueError(f"{field_name} contains a NUL byte")
    return value.strip()


def _normalize_prefix(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or _CONTROL_RE.search(value):
        return ""
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError:
        return ""
    return " ".join(tokens)


def _prefix_tokens(prefix: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(prefix, posix=True))
    except ValueError:
        return ()


def _simple_command_tokens(command: str) -> tuple[str, ...] | None:
    if len(command) > 8_192 or not command.strip():
        return None
    if _CONTROL_RE.search(command) or _SHELL_SYNTAX_RE.search(command):
        return None
    try:
        tokens = tuple(shlex.split(command, posix=True))
    except ValueError:
        return None
    if not tokens or any(
        not token or token in {"&", "|", ";", "<", ">"} for token in tokens
    ):
        return None
    return tokens


def _tokens_match_prefix(
    command_tokens: tuple[str, ...], prefix_tokens: tuple[str, ...]
) -> bool:
    return bool(prefix_tokens) and command_tokens[: len(prefix_tokens)] == prefix_tokens


def _dangerous_command_reason(command: str) -> str | None:
    if _DANGEROUS_RE.search(command):
        return "destructive or unrestricted command requires manual approval"
    return None


def _resolve_path(value: str | os.PathLike[str]) -> Path | None:
    try:
        return Path(value).expanduser().resolve(strict=False)
    except OSError, RuntimeError, TypeError, ValueError:
        return None


def _strip_ansi(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value)


def _extract_command_prefix(prompt: str) -> str | None:
    match = re.search(
        r"commands?\s+that\s+start\s+with\s+[`'\"]([^`'\"]+)[`'\"]",
        prompt,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return None


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_env_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


__all__ = [
    "ApprovalDecision",
    "ApprovalHook",
    "ApprovalOption",
    "ApprovalPolicy",
    "ApprovalPromptKind",
    "ApprovalPromptParser",
    "ApprovalRequest",
    "ApprovalResult",
    "ApprovalRule",
    "ApprovalScope",
    "ParsedApprovalPrompt",
]
