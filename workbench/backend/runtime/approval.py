"""One-shot approval records for explicit Workbench command execution.

This module does not spawn a process. It binds a concrete command identity to
one approval decision and lets the executor consume that decision exactly once.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import shlex
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path


class CommandSyntaxError(ValueError):
    """Raised when a command string needs a shell parser or dynamic expansion."""


class ApprovalIntegrityError(RuntimeError):
    """Raised when a grant is unavailable or no longer matches its command."""


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    APPROVAL_TIMEOUT = "approval_timeout"
    APPROVAL_UNAVAILABLE = "approval_unavailable"
    APPROVAL_INTEGRITY_MISMATCH = "approval_integrity_mismatch"
    CONSUMED = "consumed"


class CommandRisk(StrEnum):
    LEVEL_A = "level_a"
    LEVEL_B = "level_b"
    LEVEL_C = "level_c"


_SHELL_SYNTAX = re.compile(r"(?:&&|\|\||[;|&`()<>]|\$\(|\$\{|\n|\r)")
_DANGEROUS_EXECUTABLES = frozenset(
    {
        "rm",
        "rmdir",
        "del",
        "erase",
        "format",
        "diskpart",
        "dd",
        "mkfs",
        "fdisk",
        "reg",
        "sc",
        "net",
        "shutdown",
        "reboot",
        "taskkill",
        "powershell",
        "pwsh",
        "cmd",
        "bash",
        "sh",
        "zsh",
        "fish",
        "curl",
        "wget",
        "sudo",
        "doas",
        "su",
        "runas",
        "chmod",
        "chown",
        "setfacl",
        "mount",
        "umount",
        "systemctl",
        "service",
        "iptables",
        "ip6tables",
        "ufw",
        "diskutil",
        "parted",
        "lsblk",
        "rm.exe",
        "rmdir.exe",
        "del.exe",
        "erase.exe",
        "format.exe",
        "diskpart.exe",
        "reg.exe",
        "sc.exe",
        "net.exe",
        "taskkill.exe",
        "powershell.exe",
        "pwsh.exe",
        "cmd.exe",
        "systemctl.exe",
        "service.exe",
        "curl.exe",
        "wget.exe",
    }
)
_SAFE_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show"})
_SAFE_EXECUTABLES = frozenset(
    {
        "pwd",
        "ls",
        "dir",
        "cat",
        "head",
        "tail",
        "grep",
        "rg",
        "find",
        "pytest",
        "ruff",
        "ty",
        "test",
        "true",
        "which",
    }
)
_PROJECT_TOOL_EXECUTABLES = frozenset(
    {
        "python",
        "python3",
        "python3.14",
        "python.exe",
        "python3.exe",
        "node",
        "node.exe",
        "npm",
        "npm.cmd",
        "pnpm",
        "pnpm.cmd",
        "yarn",
        "yarn.cmd",
        "uv",
        "uv.exe",
    }
)
_SHELL_WRAPPER_EXECUTABLES = frozenset(
    {
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "cmd",
        "cmd.exe",
        "bash",
        "bash.exe",
        "sh",
        "sh.exe",
        "zsh",
        "fish",
    }
)
_SENSITIVE_MARKERS = (
    ".env",
    ".aws",
    ".ssh",
    ".gnupg",
    "auth.json",
    "credentials",
    "credential",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "password",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    normalized = value.strip()
    if "\x00" in normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError(f"{field} contains control characters")
    return normalized


def _parse_command(command: str) -> tuple[str, ...]:
    text = _required_text(command, "command")
    if _SHELL_SYNTAX.search(text):
        raise CommandSyntaxError("shell composition is not executable by the Workbench")
    try:
        argv = tuple(shlex.split(text, posix=True))
    except ValueError as exc:
        raise CommandSyntaxError("command cannot be parsed safely") from exc
    return _validate_argv(argv)


def _validate_argv(argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not argv:
        raise CommandSyntaxError("argv must contain an executable")
    normalized = tuple(_required_text(value, "argv item") for value in argv)
    if any(_SHELL_SYNTAX.search(value) for value in normalized):
        raise CommandSyntaxError("argv contains shell composition")
    return normalized


def _command_hash(
    *,
    normalized_command: str,
    argv: tuple[str, ...],
    cwd: Path,
    requested_permission: str,
) -> str:
    payload = {
        "argv": list(argv),
        "cwd": str(cwd),
        "normalized_command": normalized_command,
        "requested_permission": requested_permission,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _risk_for(argv: tuple[str, ...], cwd: Path | None = None) -> CommandRisk:
    executable = _executable_name(argv[0])
    arguments = tuple(value.lower() for value in argv[1:])
    if _contains_sensitive_reference(arguments, executable=executable):
        return CommandRisk.LEVEL_C
    if executable in {"curl", "curl.exe", "wget", "wget.exe"}:
        return (
            CommandRisk.LEVEL_A
            if _is_loopback_health_check(arguments)
            else CommandRisk.LEVEL_C
        )
    if executable in _SHELL_WRAPPER_EXECUTABLES:
        return _shell_wrapper_risk(executable, arguments)
    if _direct_system_command_is_dangerous(executable, arguments):
        return CommandRisk.LEVEL_C
    if executable in _DANGEROUS_EXECUTABLES:
        return CommandRisk.LEVEL_C
    if executable in {"git", "git.exe"}:
        command_index = _git_subcommand_index(arguments)
        if command_index is None:
            return CommandRisk.LEVEL_B
        if cwd is not None and _git_scope_is_external(arguments, command_index, cwd):
            return CommandRisk.LEVEL_B
        command = arguments[command_index]
        rest = arguments[command_index + 1 :]
        if command in {"reset", "config"}:
            return CommandRisk.LEVEL_C
        if command == "clean":
            return (
                CommandRisk.LEVEL_A
                if rest and all(value in {"-n", "--dry-run"} for value in rest)
                else CommandRisk.LEVEL_C
            )
        if command == "checkout":
            if "--" in rest or any(value in {".", ".."} for value in rest):
                return CommandRisk.LEVEL_C
            return CommandRisk.LEVEL_B
        if command == "restore":
            return (
                CommandRisk.LEVEL_B
                if "--staged" in rest and "--worktree" not in rest
                else CommandRisk.LEVEL_C
            )
        if command == "push":
            return (
                CommandRisk.LEVEL_C if _has_force_option(rest) else CommandRisk.LEVEL_B
            )
        if command == "stash" and rest and rest[0] in {"drop", "clear"}:
            return CommandRisk.LEVEL_C
        if command == "branch":
            if any(
                value
                in {"-d", "-D", "--delete", "--force", "-f", "-m", "-M", "-c", "-C"}
                for value in rest
            ):
                return CommandRisk.LEVEL_C
            if rest == ("--show-current",):
                return CommandRisk.LEVEL_A
            return CommandRisk.LEVEL_B
        if command in _SAFE_GIT_SUBCOMMANDS:
            if cwd is not None and _has_path_outside_cwd(rest, cwd):
                return CommandRisk.LEVEL_B
            if command == "diff" and "--no-index" in rest:
                return CommandRisk.LEVEL_B
            if command == "diff" and any(
                value.startswith("--output") for value in rest
            ):
                return CommandRisk.LEVEL_B
            return CommandRisk.LEVEL_A
        return CommandRisk.LEVEL_B
    if executable in _SAFE_EXECUTABLES:
        if executable == "find" and any(
            value in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
            for value in arguments
        ):
            return CommandRisk.LEVEL_C
        if cwd is not None and _has_path_outside_cwd(argv[1:], cwd):
            return CommandRisk.LEVEL_B
        return CommandRisk.LEVEL_A
    if executable in _PROJECT_TOOL_EXECUTABLES:
        if executable.startswith("python"):
            if arguments and arguments[0] in {"-c", "-"}:
                return CommandRisk.LEVEL_C
            if arguments and arguments[0] == "-m" and len(arguments) > 1:
                return (
                    CommandRisk.LEVEL_A
                    if arguments[1] in {"pytest", "ruff", "ty"}
                    else CommandRisk.LEVEL_B
                )
            if cwd is not None and _has_path_outside_cwd(argv[1:], cwd):
                return CommandRisk.LEVEL_C
            return CommandRisk.LEVEL_B
        if executable.startswith("node"):
            if arguments and arguments[0] in {"-e", "--eval", "-p", "--print"}:
                return CommandRisk.LEVEL_C
            if cwd is not None and _has_path_outside_cwd(argv[1:], cwd):
                return CommandRisk.LEVEL_C
            return CommandRisk.LEVEL_B
        if (
            executable in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}
            and arguments
        ):
            if arguments[0] in {"publish", "unpublish", "deprecate", "unstar"}:
                return CommandRisk.LEVEL_C
            if arguments[0] in {"install", "ci", "update", "exec", "dlx", "run"}:
                return CommandRisk.LEVEL_B
            if arguments[0] in {"test", "lint", "typecheck", "check"}:
                return CommandRisk.LEVEL_A
            if (
                arguments[0] == "run"
                and len(arguments) > 1
                and arguments[1]
                in {
                    "test",
                    "lint",
                    "typecheck",
                    "check",
                }
            ):
                return CommandRisk.LEVEL_A
        if executable in {"uv", "uv.exe"} and arguments:
            if arguments[0] == "run" and len(arguments) > 1:
                if arguments[1] in {"pytest", "ruff", "ty"}:
                    return CommandRisk.LEVEL_A
                return CommandRisk.LEVEL_B
            if arguments[0] in {"sync", "pip", "tool"}:
                return CommandRisk.LEVEL_B
    if cwd is not None and _has_path_outside_cwd(argv[1:], cwd):
        return CommandRisk.LEVEL_B
    return CommandRisk.LEVEL_B


def _contains_sensitive_reference(
    arguments: tuple[str, ...], *, executable: str
) -> bool:
    candidates = arguments
    if executable in {"grep", "rg"}:
        files_mode = "--files" in arguments
        if not files_mode:
            first_operand = next(
                (
                    index
                    for index, value in enumerate(arguments)
                    if not value.startswith("-")
                ),
                None,
            )
            candidates = () if first_operand is None else arguments[first_operand + 1 :]
    return any(
        _sensitive_argument(argument, allow_bare=executable not in {"git", "git.exe"})
        for argument in candidates
    )


def _shell_wrapper_risk(executable: str, arguments: tuple[str, ...]) -> CommandRisk:
    """Classify a shell wrapper by the complete inline command it carries."""
    lowered = tuple(value.lower() for value in arguments)
    if executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        if any(value in {"-enc", "-encodedcommand", "-encoded"} for value in lowered):
            return CommandRisk.LEVEL_C
        if "-executionpolicy" in lowered:
            index = lowered.index("-executionpolicy")
            if index + 1 < len(lowered) and lowered[index + 1] in {
                "bypass",
                "unrestricted",
                "remotesigned",
            }:
                return CommandRisk.LEVEL_C
        inner = _wrapper_inner_command(lowered, {"-command", "-c"})
        if inner is not None and _inline_command_is_dangerous(inner):
            return CommandRisk.LEVEL_C
        return CommandRisk.LEVEL_B
    if executable in {"cmd", "cmd.exe"}:
        inner = _wrapper_inner_command(lowered, {"/c", "/k"})
        return (
            CommandRisk.LEVEL_C
            if inner is not None and _inline_command_is_dangerous(inner)
            else CommandRisk.LEVEL_B
        )
    inner = _wrapper_inner_command(lowered, {"-c"})
    return (
        CommandRisk.LEVEL_C
        if inner is not None and _inline_command_is_dangerous(inner)
        else CommandRisk.LEVEL_B
    )


def _wrapper_inner_command(
    arguments: tuple[str, ...], switches: set[str]
) -> str | None:
    for index, value in enumerate(arguments):
        if value in switches and index + 1 < len(arguments):
            return " ".join(arguments[index + 1 :])
    return None


def _inline_command_is_dangerous(command: str) -> bool:
    lowered = command.lower()
    if re.search(
        r"\b(?:rm|rmdir|del|erase|format|diskpart|reg|sc|netsh|taskkill|"
        r"systemctl|service|mount|umount|chmod|chown|iptables|ufw)\b",
        lowered,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:remove|set|new|stop|start|restart)[-_](?:item|itemproperty|service|"
            r"acl|localuser|localgroup)\b|"
            r"\b(?:invoke-expression|iex|downloadstring|invoke-webrequest|iwr)\b",
            lowered,
        )
    )


def _direct_system_command_is_dangerous(
    executable: str, arguments: tuple[str, ...]
) -> bool:
    """Catch dangerous commands supplied directly as argv without a wrapper."""
    if executable in {
        "remove-item",
        "remove_item",
        "remove-itemproperty",
        "remove_itemproperty",
        "set-itemproperty",
        "set_itemproperty",
        "set-executionpolicy",
        "set_executionpolicy",
        "invoke-expression",
        "invoke_expression",
        "invoke-webrequest",
        "invoke_webrequest",
        "iex",
        "iwr",
        "vssadmin",
        "wmic",
        "wevtutil",
        "auditpol",
        "bcdedit",
    }:
        return True
    if executable in {"reg", "reg.exe", "sc", "sc.exe", "netsh", "diskpart"}:
        return bool(arguments) and arguments[0] in {
            "delete",
            "add",
            "create",
            "config",
            "stop",
            "start",
            "change",
            "set",
        }
    if executable in {"wsl", "wsl.exe"}:
        return "--unregister" in arguments
    return False


def _sensitive_argument(argument: str, *, allow_bare: bool = True) -> bool:
    normalized = argument.replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    path_parts = normalized.replace(":", "/").split("/")
    sensitive_names = {
        ".env",
        ".credentials.json",
        "auth.json",
        "credentials",
        "credential",
        "secret",
        "token",
        "api_key",
        "apikey",
        "private_key",
        "password",
        "authorized_keys",
        "known_hosts",
        "id_rsa",
        "id_ed25519",
    }
    if any(part in sensitive_names for part in path_parts) and (
        allow_bare or len(path_parts) > 1
    ):
        return True
    if any(
        part.startswith((".env.", "credential.", "credentials.", "secret.", "token."))
        for part in path_parts
    ):
        return True
    if basename.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    if (
        any(part in {".ssh", ".aws", ".gnupg"} for part in normalized.split("/"))
        or "/.config/gcloud/" in normalized
    ):
        return True
    return bool(
        re.match(
            r"^(?:--?(?:token|api[-_]?key|secret|password|private[-_]?key)|"
            r"(?:api[-_]?key|secret|token|password)=)",
            basename,
        )
    )


def _git_subcommand_index(arguments: tuple[str, ...]) -> int | None:
    index = 0
    while index < len(arguments) and arguments[index] in {
        "-C",
        "--git-dir",
        "--work-tree",
    }:
        index += 2
    return (
        index
        if index < len(arguments) and not arguments[index].startswith("-")
        else None
    )


def _git_scope_is_external(
    arguments: tuple[str, ...], command_index: int, cwd: Path
) -> bool:
    """Reject git options and comparison operands that escape the worktree."""
    option_index = 0
    while option_index < command_index:
        option = arguments[option_index]
        if option in {"-C", "--git-dir", "--work-tree"}:
            if option_index + 1 >= command_index:
                return True
            value = arguments[option_index + 1]
            if _path_is_external(value, cwd):
                return True
            option_index += 2
            continue
        if option.startswith(("--git-dir=", "--work-tree=")) and _path_is_external(
            option.split("=", 1)[1], cwd
        ):
            return True
        option_index += 1
    command = arguments[command_index]
    if command == "diff":
        rest = arguments[command_index + 1 :]
        if "--no-index" in rest:
            operands = rest[rest.index("--no-index") + 1 :]
            return any(_path_is_external(value, cwd) for value in operands)
    return False


def _path_is_external(value: str, cwd: Path) -> bool:
    if not value or value.startswith("-"):
        return False
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        candidate.resolve(strict=False).relative_to(cwd)
    except OSError, ValueError:
        return True
    return False


def _has_force_option(arguments: tuple[str, ...]) -> bool:
    return any(
        value in {"-f", "--force", "--force-with-lease"}
        or (value.startswith("-") and not value.startswith("--") and "f" in value[1:])
        for value in arguments
    )


def _has_path_outside_cwd(arguments: tuple[str, ...], cwd: Path) -> bool:
    for value in arguments:
        if not value or value.startswith("-") or _looks_like_non_path(value):
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            candidate.resolve(strict=False).relative_to(cwd)
        except ValueError:
            return True
        except OSError:
            return True
    return False


def _looks_like_non_path(value: str) -> bool:
    return value in {"--", "-", "*"} or (
        "=" not in value and "/" not in value and "\\" not in value and "." not in value
    )


def _is_loopback_health_check(arguments: tuple[str, ...]) -> bool:
    urls = [value for value in arguments if value.startswith(("http://", "https://"))]
    if not urls:
        return False
    if any(
        not re.match(
            r"https?://(?:127\.0\.0\.1|localhost|\[?::1\]?)(?::\d+)?(?:/|$)", url
        )
        for url in urls
    ):
        return False
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value.startswith(("http://", "https://")):
            index += 1
            continue
        if value in {
            "-s",
            "-S",
            "-f",
            "-I",
            "--silent",
            "--show-error",
            "--fail",
            "--head",
        }:
            index += 1
            continue
        if (
            value.startswith("-")
            and not value.startswith("--")
            and len(value) > 1
            and all(flag in "sSfIL" for flag in value[1:])
        ):
            index += 1
            continue
        if value in {"-X", "--request"} and index + 1 < len(arguments):
            if arguments[index + 1].upper() == "GET":
                index += 2
                continue
            return False
        return False
    return True


def _executable_name(value: str) -> str:
    """Normalize POSIX and Windows executable paths for risk classification."""
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


@dataclass(frozen=True, slots=True)
class CommandIntent:
    """A fully normalized command identity that can be approved once."""

    session_id: str
    call_id: str
    normalized_command: str
    argv: tuple[str, ...]
    cwd: Path
    requested_permission: str
    command_hash: str
    risk: CommandRisk

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        call_id: str,
        cwd: str | Path,
        requested_permission: str,
        command: str | None = None,
        argv: list[str] | tuple[str, ...] | None = None,
    ) -> CommandIntent:
        if command is None and argv is None:
            raise CommandSyntaxError("command or argv is required")
        parsed = _parse_command(command) if command is not None else None
        supplied = _validate_argv(argv) if argv is not None else None
        if parsed is not None and supplied is not None and parsed != supplied:
            raise CommandSyntaxError("command and argv do not describe the same call")
        resolved_argv = parsed or supplied
        assert resolved_argv is not None
        try:
            resolved_cwd = Path(cwd).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("cwd must reference an existing directory") from exc
        if not resolved_cwd.is_dir():
            raise ValueError("cwd must reference a directory")
        permission = _required_text(requested_permission, "requested_permission")
        normalized = shlex.join(resolved_argv)
        return cls(
            session_id=_required_text(session_id, "session_id"),
            call_id=_required_text(call_id, "call_id"),
            normalized_command=normalized,
            argv=resolved_argv,
            cwd=resolved_cwd,
            requested_permission=permission,
            command_hash=_command_hash(
                normalized_command=normalized,
                argv=resolved_argv,
                cwd=resolved_cwd,
                requested_permission=permission,
            ),
            risk=_risk_for(resolved_argv, resolved_cwd),
        )

    def verify_integrity(self) -> None:
        """Recompute the canonical command identity immediately before use."""
        canonical_command = shlex.join(self.argv)
        expected_hash = _command_hash(
            normalized_command=canonical_command,
            argv=self.argv,
            cwd=self.cwd,
            requested_permission=self.requested_permission,
        )
        if self.normalized_command != canonical_command or not hmac.compare_digest(
            self.command_hash, expected_hash
        ):
            raise ApprovalIntegrityError("approval_integrity_mismatch")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    session_id: str
    call_id: str
    normalized_command: str
    argv: tuple[str, ...]
    cwd: Path
    requested_permission: str
    command_hash: str
    risk: CommandRisk
    status: ApprovalState
    created_at: datetime
    expires_at: datetime
    approved_at: datetime | None = None
    consumed_at: datetime | None = None
    reason: str | None = None


class ApprovalManager:
    """In-memory, non-persistent one-shot grants.

    Grants deliberately do not survive a service restart and never contain a
    process identity. PID/job ownership begins only after the executor spawns.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ApprovalRecord] = {}
        self._lock = asyncio.Lock()

    async def request(
        self, intent: CommandIntent, *, approval_timeout_seconds: float = 300.0
    ) -> ApprovalRecord:
        intent.verify_integrity()
        if approval_timeout_seconds < 0:
            raise ValueError("approval_timeout_seconds must be non-negative")
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=approval_timeout_seconds)
        if intent.risk is CommandRisk.LEVEL_A:
            status = ApprovalState.APPROVED
            approved_at: datetime | None = now
            reason = "level_a_policy"
        elif intent.risk is CommandRisk.LEVEL_C:
            status = ApprovalState.REJECTED
            approved_at = None
            reason = "level_c_denied"
        elif approval_timeout_seconds == 0:
            status = ApprovalState.APPROVAL_TIMEOUT
            approved_at = None
            reason = "approval_timeout"
        else:
            status = ApprovalState.PENDING
            approved_at = None
            reason = None
        record = ApprovalRecord(
            session_id=intent.session_id,
            call_id=intent.call_id,
            normalized_command=intent.normalized_command,
            argv=intent.argv,
            cwd=intent.cwd,
            requested_permission=intent.requested_permission,
            command_hash=intent.command_hash,
            risk=intent.risk,
            status=status,
            created_at=now,
            expires_at=expires_at,
            approved_at=approved_at,
            reason=reason,
        )
        async with self._lock:
            existing = self._records.get((intent.session_id, intent.call_id))
            if existing is not None:
                if hmac.compare_digest(existing.command_hash, intent.command_hash):
                    return existing
                raise ApprovalIntegrityError("approval_integrity_mismatch")
            self._records[(intent.session_id, intent.call_id)] = record
        return record

    async def approve(
        self, *, session_id: str, call_id: str, command_hash: str
    ) -> ApprovalRecord:
        async with self._lock:
            record = self._require_matching(session_id, call_id, command_hash)
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[(session_id, call_id)] = record
                raise ApprovalIntegrityError("approval_timeout")
            if record.status is not ApprovalState.PENDING:
                raise ApprovalIntegrityError("approval_unavailable")
            approved = replace(
                record,
                status=ApprovalState.APPROVED,
                approved_at=datetime.now(UTC),
                reason="allow_once",
            )
            self._records[(session_id, call_id)] = approved
            return approved

    async def reject(
        self, *, session_id: str, call_id: str, command_hash: str
    ) -> ApprovalRecord:
        async with self._lock:
            record = self._require_matching(session_id, call_id, command_hash)
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[(session_id, call_id)] = record
                raise ApprovalIntegrityError("approval_timeout")
            if record.status is not ApprovalState.PENDING:
                raise ApprovalIntegrityError("approval_unavailable")
            rejected = replace(record, status=ApprovalState.REJECTED, reason="rejected")
            self._records[(session_id, call_id)] = rejected
            return rejected

    async def cancel(
        self, *, session_id: str, call_id: str, command_hash: str
    ) -> ApprovalRecord:
        async with self._lock:
            record = self._require_matching(session_id, call_id, command_hash)
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[(session_id, call_id)] = record
                raise ApprovalIntegrityError("approval_timeout")
            if record.status is not ApprovalState.PENDING:
                raise ApprovalIntegrityError("approval_unavailable")
            cancelled = replace(
                record, status=ApprovalState.CANCELLED, reason="cancelled"
            )
            self._records[(session_id, call_id)] = cancelled
            return cancelled

    async def consume(self, intent: CommandIntent) -> ApprovalRecord:
        intent.verify_integrity()
        async with self._lock:
            record = self._require_matching(
                intent.session_id, intent.call_id, intent.command_hash
            )
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[(intent.session_id, intent.call_id)] = record
                raise ApprovalIntegrityError("approval_timeout")
            if record.status is not ApprovalState.APPROVED:
                if record.status is ApprovalState.REJECTED:
                    raise ApprovalIntegrityError("rejected")
                if record.status is ApprovalState.CANCELLED:
                    raise ApprovalIntegrityError("cancelled")
                raise ApprovalIntegrityError("approval_unavailable")
            consumed = replace(
                record,
                status=ApprovalState.CONSUMED,
                consumed_at=datetime.now(UTC),
            )
            self._records[(intent.session_id, intent.call_id)] = consumed
            return consumed

    async def get(self, *, session_id: str, call_id: str) -> ApprovalRecord:
        async with self._lock:
            key = (session_id, call_id)
            record = self._records.get(key)
            if record is None:
                raise ApprovalIntegrityError("approval_unavailable")
            updated = self._expire_if_needed(record)
            self._records[key] = updated
            return updated

    def _require_matching(
        self, session_id: str, call_id: str, command_hash: str
    ) -> ApprovalRecord:
        record = self._records.get((session_id, call_id))
        if record is None:
            raise ApprovalIntegrityError("approval_unavailable")
        if not hmac.compare_digest(record.command_hash, command_hash):
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        return record

    @staticmethod
    def _expire_if_needed(record: ApprovalRecord) -> ApprovalRecord:
        if (
            record.status is ApprovalState.PENDING
            and datetime.now(UTC) >= record.expires_at
        ):
            return replace(
                record,
                status=ApprovalState.APPROVAL_TIMEOUT,
                reason="approval_timeout",
            )
        return record


__all__ = [
    "ApprovalIntegrityError",
    "ApprovalManager",
    "ApprovalRecord",
    "ApprovalState",
    "CommandIntent",
    "CommandRisk",
    "CommandSyntaxError",
]
