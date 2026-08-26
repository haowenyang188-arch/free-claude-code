"""Protocol-first approval policy and terminal prompt parsing.

The module deliberately contains no window, mouse, OCR, or network code.  It
can be used by CLI hooks and by a later PTY adapter without making either path
responsible for the other's side effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from collections.abc import Iterable, Mapping
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
    tool_input: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

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
        tool_input = self.tool_input
        if not isinstance(tool_input, Mapping):
            raise ValueError("tool_input must be an object")
        tool_input = dict(tool_input)

        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "tool_input", tool_input)

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
            # Scope is coordinator-owned. Hook input is untrusted and cannot
            # upgrade a one-shot request to a session or permanent grant.
            scope=ApprovalScope.ONCE,
            process_id=process_id,
            generation=generation,
            tool_input=tool_input,
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
)

DEFAULT_SAFE_TOOLS = frozenset({"Glob", "LS", "TodoRead"})

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
_SHELL_WRAPPER_NAMES = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "fish",
        "pwsh",
        "powershell",
        "env",
        "command",
        "xargs",
        "find",
    }
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
        allowed_command_prefixes: Iterable[str] | None = None,
        allowed_workspaces: Iterable[str | os.PathLike[str]] | None = None,
        safe_tools: Iterable[str] | None = None,
        denied_command_prefixes: Iterable[str] | None = None,
        max_auto_scope: ApprovalScope = ApprovalScope.ONCE,
        allow_permanent: bool = False,
        backends: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(allow_permanent, bool):
            raise ValueError("allow_permanent must be a boolean")
        if not isinstance(max_auto_scope, ApprovalScope):
            max_auto_scope = ApprovalScope(str(max_auto_scope))
        self.enabled = enabled
        command_prefixes = _string_iterable(
            allowed_command_prefixes, "allowed_command_prefixes"
        )
        denied_prefixes = _string_iterable(
            denied_command_prefixes, "denied_command_prefixes"
        )
        workspace_values = _path_iterable(allowed_workspaces, "allowed_workspaces")
        safe_tool_values = (
            tuple(DEFAULT_SAFE_TOOLS)
            if safe_tools is None
            else _string_iterable(safe_tools, "safe_tools")
        )
        backend_values = (
            ("claude", "codex")
            if backends is None
            else _string_iterable(backends, "backends")
        )
        self.allowed_command_prefixes = tuple(
            _normalize_prefix(value)
            for value in command_prefixes
            if _normalize_prefix(value)
        )
        self.denied_command_prefixes = tuple(
            _normalize_prefix(value)
            for value in denied_prefixes
            if _normalize_prefix(value)
        )
        self.allowed_workspaces = tuple(
            _resolve_path(value) for value in workspace_values
        )
        self.safe_tools = frozenset(safe_tool_values)
        self.max_auto_scope = max_auto_scope
        self.allow_permanent = allow_permanent
        self.backends = frozenset(value.strip().lower() for value in backend_values)

    @classmethod
    def low_risk(
        cls,
        *,
        enabled: bool = False,
        allowed_workspaces: Iterable[str | os.PathLike[str]] | None = None,
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
        enabled = _parse_bool(
            _first_env(
                "FCC_APPROVAL_ENABLED", "CLI_AUTO_APPROVAL_ENABLED", default="false"
            )
        )
        scope = ApprovalScope(
            _first_env(
                "FCC_APPROVAL_SCOPE",
                "CLI_AUTO_APPROVAL_SCOPE",
                default=ApprovalScope.ONCE.value,
            ).strip()
        )
        commands = _load_env_list(
            "FCC_APPROVAL_COMMANDS_JSON",
            "FCC_APPROVAL_COMMANDS",
            "CLI_AUTO_APPROVAL_COMMANDS",
        )
        workspaces = _load_env_list(
            "FCC_APPROVAL_WORKSPACES_JSON",
            "FCC_APPROVAL_WORKSPACES",
            "CLI_AUTO_APPROVAL_WORKSPACES",
        )
        return cls(
            enabled=enabled,
            allowed_command_prefixes=commands or DEFAULT_SAFE_COMMAND_PREFIXES,
            allowed_workspaces=workspaces,
            max_auto_scope=scope,
            allow_permanent=_parse_bool(
                _first_env(
                    "FCC_APPROVAL_ALLOW_PERMANENT",
                    "CLI_AUTO_APPROVAL_ALLOW_PERMANENT",
                    default="false",
                )
            ),
        )

    def to_hook_environment(self) -> dict[str, str]:
        """Serialize only non-secret policy data for a child hook process."""
        workspaces = [str(path) for path in self.allowed_workspaces if path is not None]
        return {
            "FCC_APPROVAL_ENABLED": str(self.enabled).lower(),
            "FCC_APPROVAL_SCOPE": self.max_auto_scope.value,
            "FCC_APPROVAL_COMMANDS_JSON": json.dumps(
                self.allowed_command_prefixes, ensure_ascii=True, separators=(",", ":")
            ),
            "FCC_APPROVAL_WORKSPACES_JSON": json.dumps(
                workspaces, ensure_ascii=True, separators=(",", ":")
            ),
            "FCC_APPROVAL_ALLOW_PERMANENT": str(self.allow_permanent).lower(),
        }

    def claude_hook_settings(self) -> dict[str, Any]:
        """Return a one-shot Claude settings fragment for this policy."""
        if not self.enabled:
            return {}
        return {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "^(Bash|Glob|LS|TodoRead)$",
                        "hooks": [
                            {
                                "type": "command",
                                "command": _approval_hook_command(),
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }

    def codex_hook_config_overrides(self) -> tuple[str, ...]:
        """Return inline Codex TOML overrides for supported hook events."""
        if not self.enabled:
            return ()
        handler = (
            '{type="command",command='
            f"{json.dumps(_approval_hook_command())},timeout=5}}"
        )
        group = f'{{matcher="Bash",hooks=[{handler}]}}'
        return (
            f"hooks.PreToolUse=[{group}]",
            f"hooks.PermissionRequest=[{group}]",
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

        if request.command:
            if reason := _dangerous_command_reason(request.command):
                return ApprovalResult(ApprovalDecision.DENY, reason)
            if reason := _sensitive_path_reason(request.command):
                return ApprovalResult(ApprovalDecision.DENY, reason)
            if _requires_manual_wrapper_review(request.command):
                return ApprovalResult(
                    ApprovalDecision.ASK,
                    "command wrapper or dynamic execution requires manual approval",
                )
            if _simple_command_tokens(request.command) is None:
                return ApprovalResult(
                    ApprovalDecision.ASK,
                    "compound or unparseable shell syntax requires review",
                )

        tool_name = request.tool_name.lower()
        if tool_name in {tool.lower() for tool in self.safe_tools}:
            if not _safe_tool_input_is_valid(request):
                return ApprovalResult(
                    ApprovalDecision.ASK,
                    "tool input does not match the read-only tool schema",
                )
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

        tokens = _simple_command_tokens(request.command)
        if tokens is None:
            return ApprovalResult(
                ApprovalDecision.ASK,
                "compound or unparseable shell syntax requires review",
            )
        if not _command_paths_are_contained(tokens, request.workspace):
            return ApprovalResult(
                ApprovalDecision.ASK,
                "command path operands are outside the approval workspace",
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
        if not self.enabled or not self.allowed_workspaces:
            return False
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
        saw_numbered_option = any(
            re.match(r"^\d+[.)]\s+", line) is not None for line in lines
        )
        has_approval_anchor = any(
            re.search(
                r"\b(?:do you want to proceed|permission|approval required|approve|allow this)",
                line,
                flags=re.IGNORECASE,
            )
            for line in lines
        )
        if (
            not has_allow
            or not has_deny
            or not (saw_numbered_option or has_approval_anchor)
        ):
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
        if not (
            re.match(r"^\d+[.)]\s+", line) or re.search(r"\s+\([^()]+\)\s*$", line)
        ):
            return None
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
        if re.search(r"\b(?:do\s+not|don't|never)\s+allow\b", lowered):
            return None
        is_allow = bool(re.search(r"\b(?:yes|allow|approve|proceed)\b", lowered))
        is_deny = bool(
            re.search(r"\b(?:no|cancel|deny|dismiss|abort|esc(?:ape)?)\b", lowered)
        )
        if (is_allow and is_deny) or (not is_allow and not is_deny):
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

        if event_name == "PreToolUse":
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


def _string_iterable(value: Iterable[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, os.PathLike)):
        raise ValueError(f"{field_name} must be an iterable of strings")
    try:
        values = tuple(value)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be an iterable of strings") from exc
    if not all(isinstance(item, str) for item in values):
        raise ValueError(f"{field_name} must contain only strings")
    return values


def _path_iterable(
    value: Iterable[str | os.PathLike[str]] | None, field_name: str
) -> tuple[str | os.PathLike[str], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, os.PathLike)):
        raise ValueError(f"{field_name} must be an iterable of paths")
    try:
        values = tuple(value)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be an iterable of paths") from exc
    if not all(isinstance(item, (str, os.PathLike)) for item in values):
        raise ValueError(f"{field_name} must contain only paths")
    return values


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
    tokens = _shell_tokens(command)
    if not tokens:
        return "command could not be parsed safely"
    if _contains_dangerous_command(tokens):
        return "destructive or unrestricted command requires manual approval"
    if re.search(
        r"(?ix)\bgit(?:\s+-\S+)*\s+push\b(?:(?![;&|]).)*(?:^|\s)(?:-f|--force(?:-with-lease)?)(?:\s|$)",
        command,
    ):
        return "force-push requires manual approval"
    if _DANGEROUS_RE.search(command):
        return "destructive or unrestricted command requires manual approval"
    return None


def _shell_tokens(command: str) -> tuple[str, ...]:
    if len(command) > 8_192 or _CONTROL_RE.search(command):
        return ()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return tuple(lexer)
    except ValueError:
        return ()


def _contains_dangerous_command(tokens: tuple[str, ...]) -> bool:
    separators = {";", "&&", "||", "|", "&", "<", ">", ">>", "<<<"}
    command_starts = {0}
    for index, token in enumerate(tokens[:-1]):
        if token in separators:
            command_starts.add(index + 1)

    for index in sorted(command_starts):
        if index >= len(tokens):
            continue
        executable_index = _skip_command_wrappers(tokens, index)
        if executable_index is None:
            continue
        executable = _executable_name(tokens[executable_index])
        if executable in _DANGEROUS_EXECUTABLES:
            return True
        if executable in {"git", "git.exe"} and _git_command_is_dangerous(
            tokens[executable_index:]
        ):
            return True

        if executable in {"find", "xargs"} and any(
            _executable_name(value) in _DANGEROUS_EXECUTABLES
            for value in tokens[executable_index + 1 :]
        ):
            return True

        if executable in _SHELL_WRAPPER_NAMES:
            for option_index, value in enumerate(
                tokens[executable_index + 1 :], executable_index + 1
            ):
                if value in {"-c", "-Command", "-command"} and option_index + 1 < len(
                    tokens
                ):
                    inner = _shell_tokens(tokens[option_index + 1])
                    if inner and _contains_dangerous_command(inner):
                        return True

    return False


_DANGEROUS_EXECUTABLES = frozenset(
    {
        "sudo",
        "rm",
        "rmdir",
        "del",
        "erase",
        "format",
        "dd",
        "mkfs",
        "shutdown",
        "reboot",
    }
)


def _skip_command_wrappers(tokens: tuple[str, ...], index: int) -> int | None:
    while index < len(tokens):
        token = tokens[index]
        if not token or token in {";", "&&", "||", "|", "&"}:
            return None
        executable = _executable_name(token)
        if executable in {
            "env",
            "command",
            "sudo",
            "nohup",
            "time",
            "nice",
            "stdbuf",
            "timeout",
        }:
            index += 1
            while index < len(tokens) and (
                tokens[index].startswith("-") or "=" in tokens[index]
            ):
                index += 1
            continue
        return index
    return None


def _executable_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _git_command_is_dangerous(tokens: tuple[str, ...]) -> bool:
    lowered = [token.lower() for token in tokens]
    if "reset" in lowered and "--hard" in lowered:
        return True
    if "clean" in lowered:
        return True
    if "branch" in lowered and any(token.lower() == "-d" for token in tokens):
        return True
    try:
        push_index = lowered.index("push")
    except ValueError:
        return False
    for token in lowered[push_index + 1 :]:
        if token in {"-f", "--force", "--force-with-lease"} or (
            token.startswith("-") and not token.startswith("--") and "f" in token[1:]
        ):
            return True
    return False


def _safe_tool_input_is_valid(request: ApprovalRequest) -> bool:
    tool_name = request.tool_name.lower()
    if (
        request.command is not None
        or "command" in request.tool_input
        or "cmd" in request.tool_input
    ):
        return False
    if tool_name == "todoread":
        return True
    if tool_name == "ls":
        path_value = request.tool_input.get("path")
        return path_value is None or (
            isinstance(path_value, str)
            and _sensitive_path_reason(path_value) is None
            and _path_is_contained(path_value, request.workspace)
        )
    if tool_name == "read":
        path_value = request.tool_input.get("file_path") or request.tool_input.get(
            "path"
        )
        return (
            isinstance(path_value, str)
            and bool(path_value.strip())
            and _sensitive_path_reason(path_value) is None
            and _path_is_contained(path_value, request.workspace)
        )
    if tool_name == "glob":
        pattern = request.tool_input.get("pattern")
        path_value = request.tool_input.get("path")
        return (
            isinstance(pattern, str)
            and _relative_pattern_is_safe(pattern)
            and (
                path_value is None
                or (
                    isinstance(path_value, str)
                    and _sensitive_path_reason(path_value) is None
                    and _path_is_contained(path_value, request.workspace)
                )
            )
        )
    return False


def _sensitive_path_reason(command: str) -> str | None:
    normalized = command.replace("\\", "/").lower()
    if re.search(
        r"(?:^|[\s/:])(?:\.env(?:\.[^\s/]*)?|\.credentials\.json|"
        r"id_(?:rsa|ed25519)|authorized_keys|known_hosts)(?:$|[\s/:])",
        normalized,
    ):
        return "reading a sensitive credential path requires manual approval"
    if re.search(r"(?:^|/)(?:\.ssh|\.aws|\.gnupg|\.config/gcloud)(?:/|$)", normalized):
        return "reading a sensitive credential path requires manual approval"
    if re.search(r"(?:^|[\s/])[^\s/]+\.(?:pem|key|p12|pfx)(?:$|[\s])", normalized):
        return "reading a sensitive credential path requires manual approval"
    return None


def _requires_manual_wrapper_review(command: str) -> bool:
    tokens = _shell_tokens(command)
    if not tokens:
        return True
    executable_index = _skip_command_wrappers(tokens, 0)
    if executable_index is None:
        return True
    executable = _executable_name(tokens[executable_index])
    if executable in {"xargs", "sh", "bash", "zsh", "fish", "pwsh", "powershell"}:
        return True
    if executable in {"grep", "rg"}:
        return True
    if executable == "find" and any(
        token in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        for token in tokens[executable_index + 1 :]
    ):
        return True
    if executable in {"git", "git.exe"}:
        lowered = [token.lower() for token in tokens[executable_index + 1 :]]
        if "config" in lowered or any(token.startswith("alias.") for token in lowered):
            return True
    return False


def _command_paths_are_contained(
    tokens: tuple[str, ...], workspace: str | None
) -> bool:
    if not tokens:
        return False
    root = _resolve_path(workspace) if workspace else None
    if root is None:
        return False
    for token in tokens[1:]:
        if token == "--" or token.startswith("-") or "=" in token:
            continue
        if _operand_looks_like_path(token, root) and not _path_is_contained(
            token, str(root)
        ):
            return False
    return True


def _operand_looks_like_path(value: str, root: Path) -> bool:
    normalized = value.replace("\\", "/")
    if (
        _contains_dynamic_path_syntax(normalized)
        or normalized.startswith(("/", "~/", "./", "../"))
        or normalized in {".", ".."}
        or "/" in normalized
    ):
        return True
    candidate = root / value
    return candidate.exists() or candidate.is_symlink()


def _path_is_contained(value: str, workspace: str | None) -> bool:
    if not workspace or not isinstance(value, str) or "\x00" in value:
        return False
    if _contains_dynamic_path_syntax(value):
        return False
    root = _resolve_path(workspace)
    if root is None:
        return False
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except OSError, RuntimeError, ValueError:
        return False
    return True


def _relative_pattern_is_safe(pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    if _contains_dynamic_path_syntax(normalized) or normalized.startswith("/"):
        return False
    return all(part not in {"..", ""} for part in normalized.split("/"))


def _contains_dynamic_path_syntax(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return bool(
        "$" in normalized
        or re.search(r"%[^%]+%", normalized)
        or any(marker in normalized for marker in ("*", "?", "[", "]", "{", "}"))
        or normalized.startswith("~")
        or re.match(r"^[a-zA-Z]:", normalized)
        or normalized.startswith("//")
    )


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


def _approval_hook_command() -> str:
    return f"{shlex.quote(sys.executable)} -m cli.approval_hook"


def _first_env(*keys: str, default: str) -> str:
    for key in keys:
        value = os.environ.get(key)
        if value is not None:
            return value
    return default


def _load_env_list(*keys: str) -> list[str]:
    for key in keys:
        value = os.environ.get(key)
        if value is None or not value.strip():
            continue
        if key.endswith("_JSON"):
            try:
                parsed = json.loads(value)
            except TypeError, ValueError:
                continue
            if isinstance(parsed, list) and all(
                isinstance(item, str) for item in parsed
            ):
                return [item for item in parsed if item.strip()]
            continue
        return _split_env_list(value)
    return []


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
