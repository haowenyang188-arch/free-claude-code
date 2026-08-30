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
import uuid
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


class ApprovalScope(StrEnum):
    """Role-level authorization scope for an approval request.

    ``NORMAL``  — the caller is subject to the standard risk policy
                  (LEVEL_A auto-approve, LEVEL_B one-shot, LEVEL_C deny).
    ``DENY_ONLY`` — the caller may never receive an automatic approval
                  (Codex Reviewer).  Evaluated BEFORE the risk policy; the
                  decision is an audited REJECTED, never an approval.
    """

    NORMAL = "normal"
    DENY_ONLY = "deny_only"


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
_APPROVAL_PROVIDERS = frozenset({"workbench", "codex_cli", "claude_cli"})


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    normalized = value.strip()
    if "\x00" in normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError(f"{field} contains control characters")
    return normalized


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _provider_text(value: str) -> str:
    normalized = _required_text(value, "provider").lower()
    if normalized not in _APPROVAL_PROVIDERS:
        raise ValueError("provider must be workbench, codex_cli, or claude_cli")
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
    provider: str,
    session_id: str,
    thread_id: str | None,
    turn_id: str | None,
    item_id: str | None,
    approval_id: str | None,
    call_id: str | None,
    one_shot_id: str | None,
    normalized_command: str,
    argv: tuple[str, ...],
    cwd: Path,
    requested_permission: str,
    workspace_target: Path,
    permission_scope: str | None,
    patch_identity: str | None,
    approval_scope: str = ApprovalScope.NORMAL,
) -> str:
    # ``one_shot_id`` is a server-side lookup nonce, not command content.  It
    # must remain separate so the same normalized intent can be bound to one
    # approval record without changing the hash shown to the executor/UI.
    payload = {
        "argv": list(argv),
        "cwd": str(cwd),
        "approval_id": approval_id,
        "call_id": call_id,
        "item_id": item_id,
        "normalized_command": normalized_command,
        "patch_identity": patch_identity,
        "approval_scope": approval_scope,
        "permission_scope": permission_scope,
        "provider": provider,
        "requested_permission": requested_permission,
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "workspace_target": str(workspace_target),
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _risk_for(argv: tuple[str, ...], cwd: Path | None = None) -> CommandRisk:
    executable = _executable_name(argv[0])
    arguments = tuple(value.lower() for value in argv[1:])
    if executable in {"codex-file-change", "claude-file-change"}:
        target = argv[-1] if len(argv) > 1 else ""
        if _critical_path_reference(target):
            return CommandRisk.LEVEL_C
        return CommandRisk.LEVEL_B
    if any(_critical_path_reference(argument) for argument in argv[1:]):
        return CommandRisk.LEVEL_C
    if _contains_sensitive_reference(arguments, executable=executable):
        return CommandRisk.LEVEL_C
    if executable in {"curl", "curl.exe", "wget", "wget.exe"}:
        if _is_loopback_health_check(arguments, executable=executable):
            return CommandRisk.LEVEL_A
        return (
            CommandRisk.LEVEL_C
            if _network_request_is_destructive(arguments)
            else CommandRisk.LEVEL_B
        )
    if executable in _SHELL_WRAPPER_EXECUTABLES:
        return _shell_wrapper_risk(executable, arguments)
    if _direct_system_command_is_dangerous(executable, arguments):
        return CommandRisk.LEVEL_C
    if executable in _DANGEROUS_EXECUTABLES and not _safe_system_diagnostic(
        executable, arguments
    ):
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
            if not rest:
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
            if arguments[0] in {"install", "ci", "update", "exec", "dlx"}:
                return CommandRisk.LEVEL_B
            if arguments[0] in {"test", "lint", "typecheck", "check"}:
                return CommandRisk.LEVEL_A
            if arguments[0] == "run":
                return (
                    CommandRisk.LEVEL_A
                    if len(arguments) > 1
                    and arguments[1]
                    in {
                        "test",
                        "lint",
                        "typecheck",
                        "check",
                        "build",
                        "format",
                        "dev",
                        "start",
                    }
                    else CommandRisk.LEVEL_B
                )
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


def _safe_system_diagnostic(executable: str, arguments: tuple[str, ...]) -> bool:
    """Allow non-mutating status/query probes to reach the native sandbox."""
    if executable in {"reg", "reg.exe"}:
        return bool(arguments) and arguments[0] in {"query", "compare"}
    if executable in {"sc", "sc.exe"}:
        return bool(arguments) and arguments[0] in {"query", "qc", "enumdepends"}
    if executable in {"systemctl", "systemctl.exe"}:
        return bool(arguments) and arguments[0] in {
            "status",
            "is-active",
            "is-enabled",
            "show",
            "list-units",
            "list-unit-files",
        }
    if executable in {"chmod", "chmod.exe", "chown", "chown.exe"}:
        return not any(
            value in {"-R", "--recursive", "-rf", "--reference=/", "/"}
            for value in arguments
        )
    if executable in {"service", "service.exe"}:
        return bool(arguments) and arguments[1:2] in [("status",)]
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


def _critical_path_reference(value: str) -> bool:
    normalized = value.replace("\\", "/").lower()
    if _sensitive_argument(normalized):
        return True
    path = Path(normalized)
    critical_roots = ("/", "/boot", "/etc", "/root", "/usr", "/var/lib")
    if str(path) == "/":
        return True
    if path.is_absolute() and any(
        str(path) == root or str(path).startswith(f"{root}/")
        for root in critical_roots
        if root != "/"
    ):
        return True
    return normalized.startswith(("c:/windows", "c:/program files"))


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


def _network_request_is_destructive(arguments: tuple[str, ...]) -> bool:
    """Return whether a network request has an explicit destructive method."""
    for index, value in enumerate(arguments):
        if (
            value in {"-X", "--request", "--method"}
            and index + 1 < len(arguments)
            and arguments[index + 1].upper() in {"DELETE", "PURGE"}
        ):
            return True
        if value.startswith(("-X=", "--request=", "--method=")):
            method = value.split("=", 1)[1].upper()
            if method in {"DELETE", "PURGE"}:
                return True
    return False


def _is_loopback_health_check(
    arguments: tuple[str, ...], *, executable: str | None = None
) -> bool:
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
            "--location",
            "--compressed",
            "--no-progress-meter",
            "--retry-all-errors",
            "--spider",
            "-nv",
            "--no-verbose",
            "--server-response",
        }:
            index += 1
            continue
        if (
            value.startswith("-")
            and not value.startswith("--")
            and len(value) > 1
            and all(flag in "sSfILqnv" for flag in value[1:])
        ):
            index += 1
            continue
        if value in {"-X", "--request", "--method"} and index + 1 < len(arguments):
            if arguments[index + 1].upper() in {"GET", "HEAD"}:
                index += 2
                continue
            return False
        if value.startswith(("-X=", "--request=", "--method=")):
            if value.split("=", 1)[1].upper() in {"GET", "HEAD"}:
                index += 1
                continue
            return False
        if value in {"--url", "-u"} and index + 1 < len(arguments):
            if arguments[index + 1].startswith(("http://", "https://")):
                index += 2
                continue
            return False
        if value.startswith("--url="):
            index += 1
            continue
        if value in {
            "--max-time",
            "--connect-timeout",
            "--retry",
            "--retry-delay",
            "--retry-max-time",
            "--timeout",
            "--tries",
        } and index + 1 < len(arguments):
            index += 2
            continue
        if any(
            value.startswith(prefix)
            for prefix in (
                "--max-time=",
                "--connect-timeout=",
                "--retry=",
                "--retry-delay=",
                "--retry-max-time=",
                "--timeout=",
                "--tries=",
            )
        ):
            index += 1
            continue
        if value in {"-O", "--output", "--output-document"} and index + 1 < len(
            arguments
        ):
            # A sink/stdout does not mutate workspace state; a file output is
            # an ordinary write and therefore requires an explicit approval.
            if arguments[index + 1] in {"-", "/dev/null"}:
                index += 2
                continue
            return False
        if value.startswith(("-O-", "--output=-", "--output-document=-")):
            index += 1
            continue
        if executable in {"wget", "wget.exe"} and value in {"--method=GET"}:
            index += 1
            continue
        return False
    return True


def _executable_name(value: str) -> str:
    """Normalize POSIX and Windows executable paths for risk classification."""
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


@dataclass(frozen=True, slots=True)
class CommandIntent:
    """A normalized command identity, unbound until Workbench records it."""

    session_id: str
    call_id: str | None
    normalized_command: str
    argv: tuple[str, ...]
    cwd: Path
    requested_permission: str
    command_hash: str
    risk: CommandRisk
    provider: str = "workbench"
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    approval_id: str | None = None
    one_shot_id: str | None = None
    workspace_target: Path | None = None
    permission_scope: str | None = None
    patch_identity: str | None = None
    approval_scope: str = ApprovalScope.NORMAL

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        cwd: str | Path,
        requested_permission: str,
        call_id: str | None = None,
        command: str | None = None,
        argv: list[str] | tuple[str, ...] | None = None,
        provider: str = "workbench",
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        approval_id: str | None = None,
        workspace_target: str | Path | None = None,
        permission_scope: str | None = None,
        patch_identity: str | None = None,
        approval_scope: str = ApprovalScope.NORMAL,
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
        normalized_provider = _provider_text(provider)
        normalized_thread_id = _optional_text(thread_id, "thread_id")
        normalized_turn_id = _optional_text(turn_id, "turn_id")
        normalized_item_id = _optional_text(item_id, "item_id")
        normalized_approval_id = _optional_text(approval_id, "approval_id")
        normalized_call_id = _optional_text(call_id, "call_id")
        if not any((normalized_item_id, normalized_approval_id, normalized_call_id)):
            raise ValueError(
                "one of item_id, approval_id, or call_id must identify the request"
            )
        normalized_scope = _optional_text(permission_scope, "permission_scope")
        normalized_patch = _optional_text(patch_identity, "patch_identity")
        if approval_scope not in (ApprovalScope.NORMAL, ApprovalScope.DENY_ONLY):
            raise ValueError("approval_scope must be 'normal' or 'deny_only'")
        normalized_approval_scope = str(approval_scope)
        target_value = (
            resolved_cwd if workspace_target is None else Path(workspace_target)
        )
        try:
            resolved_target = target_value.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                "workspace_target must reference an existing path"
            ) from exc
        normalized = shlex.join(resolved_argv)
        risk = _risk_for(resolved_argv, resolved_cwd)
        if _native_workspace_write_is_level_a(
            normalized_provider,
            resolved_argv,
            normalized_scope,
            resolved_cwd,
            resolved_target,
            risk,
        ):
            risk = CommandRisk.LEVEL_A
        elif risk is CommandRisk.LEVEL_A and (
            not _path_within(resolved_cwd, resolved_target)
            or _permission_scope_requires_approval(
                normalized_scope,
                resolved_argv,
                resolved_target,
            )
        ):
            # A provider may attach a capability request to an otherwise safe
            # command. The command itself remains readable, but the added
            # capability still needs one-shot approval.
            risk = CommandRisk.LEVEL_B
        normalized_session_id = _required_text(session_id, "session_id")
        intent = cls(
            session_id=normalized_session_id,
            call_id=normalized_call_id,
            normalized_command=normalized,
            argv=resolved_argv,
            cwd=resolved_cwd,
            requested_permission=permission,
            command_hash=_command_hash(
                provider=normalized_provider,
                session_id=normalized_session_id,
                thread_id=normalized_thread_id,
                turn_id=normalized_turn_id,
                item_id=normalized_item_id,
                approval_id=normalized_approval_id,
                call_id=normalized_call_id,
                one_shot_id=None,
                normalized_command=normalized,
                argv=resolved_argv,
                cwd=resolved_cwd,
                requested_permission=permission,
                workspace_target=resolved_target,
                permission_scope=normalized_scope,
                patch_identity=normalized_patch,
                approval_scope=normalized_approval_scope,
            ),
            risk=risk,
            provider=normalized_provider,
            thread_id=normalized_thread_id,
            turn_id=normalized_turn_id,
            item_id=normalized_item_id,
            approval_id=normalized_approval_id,
            one_shot_id=None,
            workspace_target=resolved_target,
            permission_scope=normalized_scope,
            patch_identity=normalized_patch,
            approval_scope=normalized_approval_scope,
        )
        intent.verify_integrity()
        return intent

    def verify_integrity(self, *, require_bound: bool = False) -> None:
        """Recompute the canonical command identity immediately before use."""
        canonical_command = shlex.join(self.argv)
        expected_risk = _risk_for(self.argv, self.cwd)
        if _native_workspace_write_is_level_a(
            self.provider,
            self.argv,
            self.permission_scope,
            self.cwd,
            self.workspace_target or self.cwd,
            expected_risk,
        ):
            expected_risk = CommandRisk.LEVEL_A
        elif expected_risk is CommandRisk.LEVEL_A and (
            not _path_within(self.cwd, self.workspace_target or self.cwd)
            or _permission_scope_requires_approval(
                self.permission_scope,
                self.argv,
                self.workspace_target or self.cwd,
            )
        ):
            expected_risk = CommandRisk.LEVEL_B
        expected_hash = _command_hash_for_intent(
            self,
            normalized_command=canonical_command,
            one_shot_id=self.one_shot_id,
        )
        if (
            self.normalized_command != canonical_command
            or not hmac.compare_digest(self.command_hash, expected_hash)
            or _provider_text(self.provider) != self.provider
            or _required_text(self.session_id, "session_id") != self.session_id
            or _optional_text(self.thread_id, "thread_id") != self.thread_id
            or _optional_text(self.turn_id, "turn_id") != self.turn_id
            or _optional_text(self.item_id, "item_id") != self.item_id
            or _optional_text(self.approval_id, "approval_id") != self.approval_id
            or _optional_text(self.call_id, "call_id") != self.call_id
            or _optional_text(self.one_shot_id, "one_shot_id") != self.one_shot_id
            or not any((self.item_id, self.approval_id, self.call_id))
            or (require_bound and self.one_shot_id is None)
            or self.workspace_target is None
            or self.risk is not expected_risk
        ):
            raise ApprovalIntegrityError("approval_integrity_mismatch")

    @property
    def reference(self) -> ApprovalReference:
        """Return the exact bound identity used by decision endpoints."""
        self.verify_integrity(require_bound=True)
        assert self.one_shot_id is not None
        return ApprovalReference.create(
            provider=self.provider,
            session_id=self.session_id,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            item_id=self.item_id,
            approval_id=self.approval_id,
            call_id=self.call_id,
            one_shot_id=self.one_shot_id,
            command_hash=self.command_hash,
        )


def _command_hash_for_intent(
    intent: CommandIntent,
    *,
    normalized_command: str | None = None,
    one_shot_id: str | None,
) -> str:
    return _command_hash(
        provider=intent.provider,
        session_id=intent.session_id,
        thread_id=intent.thread_id,
        turn_id=intent.turn_id,
        item_id=intent.item_id,
        approval_id=intent.approval_id,
        call_id=intent.call_id,
        one_shot_id=one_shot_id,
        normalized_command=normalized_command or intent.normalized_command,
        argv=intent.argv,
        cwd=intent.cwd,
        requested_permission=intent.requested_permission,
        workspace_target=intent.workspace_target or intent.cwd,
        permission_scope=intent.permission_scope,
        patch_identity=intent.patch_identity,
        approval_scope=intent.approval_scope,
    )


def _bind_one_shot(intent: CommandIntent, one_shot_id: str) -> CommandIntent:
    """Bind a Workbench-owned one-shot identifier to an unbound intent."""
    intent.verify_integrity()
    if intent.one_shot_id is not None:
        raise ApprovalIntegrityError("approval_integrity_mismatch")
    normalized = _required_text(one_shot_id, "one_shot_id")
    bound = replace(
        intent,
        one_shot_id=normalized,
    )
    bound.verify_integrity(require_bound=True)
    return bound


@dataclass(frozen=True, slots=True)
class ApprovalReference:
    """Complete immutable identity required for every post-request action."""

    provider: str
    session_id: str
    one_shot_id: str
    command_hash: str
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    approval_id: str | None = None
    call_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        session_id: str,
        one_shot_id: str,
        command_hash: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        approval_id: str | None = None,
        call_id: str | None = None,
    ) -> ApprovalReference:
        reference = cls(
            provider=_provider_text(provider),
            session_id=_required_text(session_id, "session_id"),
            thread_id=_optional_text(thread_id, "thread_id"),
            turn_id=_optional_text(turn_id, "turn_id"),
            item_id=_optional_text(item_id, "item_id"),
            approval_id=_optional_text(approval_id, "approval_id"),
            call_id=_optional_text(call_id, "call_id"),
            one_shot_id=_required_text(one_shot_id, "one_shot_id"),
            command_hash=_required_text(command_hash, "command_hash").lower(),
        )
        reference.verify_integrity()
        return reference

    def verify_integrity(self) -> None:
        if (
            _provider_text(self.provider) != self.provider
            or _required_text(self.session_id, "session_id") != self.session_id
            or _optional_text(self.thread_id, "thread_id") != self.thread_id
            or _optional_text(self.turn_id, "turn_id") != self.turn_id
            or _optional_text(self.item_id, "item_id") != self.item_id
            or _optional_text(self.approval_id, "approval_id") != self.approval_id
            or _optional_text(self.call_id, "call_id") != self.call_id
            or _required_text(self.one_shot_id, "one_shot_id") != self.one_shot_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.command_hash)
            or not any((self.item_id, self.approval_id, self.call_id))
        ):
            raise ApprovalIntegrityError("approval_integrity_mismatch")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    session_id: str
    call_id: str | None
    one_shot_id: str
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
    provider: str = "workbench"
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    approval_id: str | None = None
    workspace_target: Path | None = None
    permission_scope: str | None = None
    patch_identity: str | None = None
    approval_scope: str = ApprovalScope.NORMAL

    @property
    def intent(self) -> CommandIntent:
        intent = CommandIntent(
            session_id=self.session_id,
            call_id=self.call_id,
            normalized_command=self.normalized_command,
            argv=self.argv,
            cwd=self.cwd,
            requested_permission=self.requested_permission,
            command_hash=self.command_hash,
            risk=self.risk,
            provider=self.provider,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            item_id=self.item_id,
            approval_id=self.approval_id,
            one_shot_id=self.one_shot_id,
            workspace_target=self.workspace_target,
            permission_scope=self.permission_scope,
            patch_identity=self.patch_identity,
            approval_scope=self.approval_scope,
        )
        intent.verify_integrity(require_bound=True)
        return intent

    @property
    def reference(self) -> ApprovalReference:
        return self.intent.reference


@dataclass(frozen=True, slots=True)
class _CapabilityGrant:
    one_shot_id: str
    provider: str
    session_id: str
    thread_id: str | None
    turn_id: str
    workspace_target: Path
    permission_scope: str
    granted_at: datetime


class ApprovalManager:
    """In-memory, non-persistent one-shot grants.

    Grants deliberately do not survive a service restart and never contain a
    process identity. PID/job ownership begins only after the executor spawns.
    """

    def __init__(self) -> None:
        self._records: dict[
            tuple[
                str,
                str,
                str | None,
                str | None,
                str | None,
                str | None,
                str | None,
            ],
            ApprovalRecord,
        ] = {}
        self._one_shot_keys: dict[
            str,
            tuple[
                str,
                str,
                str | None,
                str | None,
                str | None,
                str | None,
                str | None,
            ],
        ] = {}
        self._waiters: dict[str, asyncio.Future[ApprovalRecord]] = {}
        self._capability_grants: dict[
            tuple[str, str, str | None, str], list[_CapabilityGrant]
        ] = {}
        self._lock = asyncio.Lock()

    async def request(
        self, intent: CommandIntent, *, approval_timeout_seconds: float = 300.0
    ) -> ApprovalRecord:
        intent.verify_integrity()
        if intent.one_shot_id is not None:
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        if approval_timeout_seconds < 0:
            raise ValueError("approval_timeout_seconds must be non-negative")
        async with self._lock:
            key = self._record_key(intent)
            existing = self._records.get(key)
            if existing is not None:
                existing_unbound_hash = _command_hash_for_intent(
                    existing.intent, one_shot_id=None
                )
                if hmac.compare_digest(existing_unbound_hash, intent.command_hash):
                    refreshed = self._expire_if_needed(existing)
                    self._records[key] = refreshed
                    return refreshed
                raise ApprovalIntegrityError("approval_integrity_mismatch")
            one_shot_id = uuid.uuid4().hex
            while one_shot_id in self._one_shot_keys:
                one_shot_id = uuid.uuid4().hex
            bound_intent = _bind_one_shot(intent, one_shot_id)
            now = datetime.now(UTC)
            expires_at = now + timedelta(seconds=approval_timeout_seconds)
            if bound_intent.approval_scope == ApprovalScope.DENY_ONLY:
                # ROLE AUTHORITY precedes RISK POLICY: a DENY_ONLY principal
                # (Codex Reviewer) may never receive an automatic approval,
                # even for a LEVEL_A command.  The record is still created and
                # audited (approval_id/thread_id/turn_id/item_id/call_id/
                # request/reason/decision all preserved); the decision is a
                # plain REJECTED.  No force-accept/force-reject bypass exists.
                status = ApprovalState.REJECTED
                approved_at = None
                reason = "reviewer_role_policy_deny_only"
            elif bound_intent.risk is CommandRisk.LEVEL_A:
                status = ApprovalState.APPROVED
                approved_at: datetime | None = now
                reason = "level_a_policy"
            elif bound_intent.risk is CommandRisk.LEVEL_C:
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
            assert bound_intent.one_shot_id is not None
            record = ApprovalRecord(
                session_id=bound_intent.session_id,
                call_id=bound_intent.call_id,
                one_shot_id=bound_intent.one_shot_id,
                normalized_command=bound_intent.normalized_command,
                argv=bound_intent.argv,
                cwd=bound_intent.cwd,
                requested_permission=bound_intent.requested_permission,
                command_hash=bound_intent.command_hash,
                risk=bound_intent.risk,
                status=status,
                created_at=now,
                expires_at=expires_at,
                approved_at=approved_at,
                reason=reason,
                provider=bound_intent.provider,
                thread_id=bound_intent.thread_id,
                turn_id=bound_intent.turn_id,
                item_id=bound_intent.item_id,
                approval_id=bound_intent.approval_id,
                workspace_target=bound_intent.workspace_target,
                permission_scope=bound_intent.permission_scope,
                patch_identity=bound_intent.patch_identity,
                approval_scope=bound_intent.approval_scope,
            )
            if intent.risk is CommandRisk.LEVEL_B and self._reusable_grant_locked(
                bound_intent
            ):
                record = replace(
                    record,
                    status=ApprovalState.APPROVED,
                    approved_at=now,
                    reason="same_turn_capability",
                )
            self._records[key] = record
            self._one_shot_keys[record.one_shot_id] = key
        return record

    async def approve(
        self,
        reference: ApprovalReference | None = None,
        *,
        session_id: str | None = None,
        call_id: str | None = None,
        command_hash: str | None = None,
        provider: str = "workbench",
    ) -> ApprovalRecord:
        async with self._lock:
            reference = self._coerce_reference_locked(
                reference,
                session_id=session_id,
                call_id=call_id,
                command_hash=command_hash,
                provider=provider,
            )
            key, record = self._require_matching(reference)
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[key] = record
                raise ApprovalIntegrityError("approval_timeout")
            if record.status is not ApprovalState.PENDING:
                raise ApprovalIntegrityError("approval_unavailable")
            approved = replace(
                record,
                status=ApprovalState.APPROVED,
                approved_at=datetime.now(UTC),
                reason="allow_once",
            )
            self._records[key] = approved
            self._register_grant_locked(approved)
            self._resolve_waiter_locked(approved.one_shot_id, approved)
            return approved

    async def reject(
        self,
        reference: ApprovalReference | None = None,
        *,
        session_id: str | None = None,
        call_id: str | None = None,
        command_hash: str | None = None,
        provider: str = "workbench",
    ) -> ApprovalRecord:
        async with self._lock:
            reference = self._coerce_reference_locked(
                reference,
                session_id=session_id,
                call_id=call_id,
                command_hash=command_hash,
                provider=provider,
            )
            key, record = self._require_matching(reference)
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[key] = record
                raise ApprovalIntegrityError("approval_timeout")
            if record.status is not ApprovalState.PENDING:
                raise ApprovalIntegrityError("approval_unavailable")
            rejected = replace(record, status=ApprovalState.REJECTED, reason="rejected")
            self._records[key] = rejected
            self._resolve_waiter_locked(rejected.one_shot_id, rejected)
            return rejected

    async def cancel(
        self,
        reference: ApprovalReference | None = None,
        *,
        session_id: str | None = None,
        call_id: str | None = None,
        command_hash: str | None = None,
        provider: str = "workbench",
    ) -> ApprovalRecord:
        async with self._lock:
            reference = self._coerce_reference_locked(
                reference,
                session_id=session_id,
                call_id=call_id,
                command_hash=command_hash,
                provider=provider,
            )
            key, record = self._require_matching(reference)
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[key] = record
                raise ApprovalIntegrityError("approval_timeout")
            if record.status not in {ApprovalState.PENDING, ApprovalState.APPROVED}:
                raise ApprovalIntegrityError("approval_unavailable")
            cancelled = replace(
                record, status=ApprovalState.CANCELLED, reason="cancelled"
            )
            self._records[key] = cancelled
            if record.status is ApprovalState.APPROVED:
                self._revoke_grant_locked(record)
            self._resolve_waiter_locked(cancelled.one_shot_id, cancelled)
            return cancelled

    async def wait_for_terminal(
        self, intent: CommandIntent, *, timeout_seconds: float | None = None
    ) -> ApprovalRecord:
        """Wait for an external decision on this exact one-shot grant."""
        intent.verify_integrity(require_bound=True)
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        async with self._lock:
            key, record = self._require_matching_intent(intent)
            record = self._expire_if_needed(record)
            self._records[key] = record
            if record.status is not ApprovalState.PENDING:
                return record
            one_shot_id = record.one_shot_id
            if one_shot_id in self._waiters:
                raise ApprovalIntegrityError("approval_unavailable")
            loop = asyncio.get_running_loop()
            future: asyncio.Future[ApprovalRecord] = loop.create_future()
            self._waiters[one_shot_id] = future
            remaining = max(
                0.0, (record.expires_at - datetime.now(UTC)).total_seconds()
            )
            if timeout_seconds is not None:
                remaining = min(remaining, timeout_seconds)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except TimeoutError:
            async with self._lock:
                current = self._records.get(key)
                if current is None:
                    raise ApprovalIntegrityError("approval_unavailable") from None
                if current.status is ApprovalState.PENDING:
                    current = replace(
                        current,
                        status=ApprovalState.APPROVAL_TIMEOUT,
                        reason="approval_timeout",
                    )
                    self._records[key] = current
                self._resolve_waiter_locked(one_shot_id, current)
                return current
        finally:
            async with self._lock:
                if self._waiters.get(one_shot_id) is future:
                    self._waiters.pop(one_shot_id, None)

    async def consume(self, intent: CommandIntent) -> ApprovalRecord:
        intent.verify_integrity(require_bound=True)
        async with self._lock:
            key, record = self._require_matching_intent(intent)
            record = self._expire_if_needed(record)
            if record.status is ApprovalState.APPROVAL_TIMEOUT:
                self._records[key] = record
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
            self._records[key] = consumed
            return consumed

    async def get(
        self,
        reference: ApprovalReference | None = None,
        *,
        session_id: str | None = None,
        call_id: str | None = None,
        command_hash: str | None = None,
        provider: str | None = None,
    ) -> ApprovalRecord:
        async with self._lock:
            reference = self._coerce_reference_locked(
                reference,
                session_id=session_id,
                call_id=call_id,
                command_hash=command_hash,
                provider=provider or "workbench",
            )
            key, record = self._require_matching(reference)
            updated = self._expire_if_needed(record)
            self._records[key] = updated
            return updated

    def _require_matching(
        self,
        reference: ApprovalReference,
    ) -> tuple[
        tuple[
            str,
            str,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
        ],
        ApprovalRecord,
    ]:
        reference.verify_integrity()
        key = self._one_shot_keys.get(reference.one_shot_id)
        if key is None:
            raise ApprovalIntegrityError("approval_unavailable")
        record = self._records.get(key)
        if record is None:
            raise ApprovalIntegrityError("approval_unavailable")
        if record.reference != reference:
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        return key, record

    def _coerce_reference_locked(
        self,
        reference: ApprovalReference | None,
        *,
        session_id: str | None,
        call_id: str | None,
        command_hash: str | None,
        provider: str,
    ) -> ApprovalReference:
        """Resolve legacy identity arguments without weakening the binding.

        Older adapters and the HTTP/UI contract identify a request by
        provider/session/call/hash.  Resolve that tuple to the stored complete
        reference, requiring exactly one candidate and an exact hash match.
        New callers should pass ``ApprovalReference`` directly.
        """
        if reference is not None:
            if any(value is not None for value in (session_id, call_id, command_hash)):
                raise ApprovalIntegrityError("approval_integrity_mismatch")
            return reference
        if session_id is None or call_id is None:
            raise ApprovalIntegrityError("approval_unavailable")
        normalized_provider = _provider_text(provider)
        normalized_session = _required_text(session_id, "session_id")
        normalized_call = _required_text(call_id, "call_id")
        matches = [
            record
            for key, record in self._records.items()
            if key[0] == normalized_provider
            and key[1] == normalized_session
            and key[-1] == normalized_call
        ]
        if len(matches) != 1:
            raise ApprovalIntegrityError("approval_unavailable")
        record = matches[0]
        if command_hash is None:
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        if command_hash is not None and not hmac.compare_digest(
            record.command_hash, command_hash.lower()
        ):
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        return record.reference

    def _require_matching_intent(
        self, intent: CommandIntent
    ) -> tuple[
        tuple[
            str,
            str,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
        ],
        ApprovalRecord,
    ]:
        if intent.one_shot_id is None:
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        key, record = self._require_matching(intent.reference)
        if record.intent != intent:
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        return key, record

    @staticmethod
    def _record_key(
        intent: CommandIntent,
    ) -> tuple[
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ]:
        return (
            _provider_text(intent.provider),
            intent.session_id,
            intent.thread_id,
            intent.turn_id,
            intent.item_id,
            intent.approval_id,
            intent.call_id,
        )

    def _register_grant_locked(self, record: ApprovalRecord) -> None:
        if (
            record.status is not ApprovalState.APPROVED
            or record.turn_id is None
            or record.permission_scope is None
            or record.workspace_target is None
        ):
            return
        key = (
            record.provider,
            record.session_id,
            record.thread_id,
            record.turn_id,
        )
        grant = _CapabilityGrant(
            one_shot_id=record.one_shot_id,
            provider=record.provider,
            session_id=record.session_id,
            thread_id=record.thread_id,
            turn_id=record.turn_id,
            workspace_target=record.workspace_target,
            permission_scope=record.permission_scope,
            granted_at=datetime.now(UTC),
        )
        grants = self._capability_grants.setdefault(key, [])
        if not any(
            existing.permission_scope == grant.permission_scope
            and existing.workspace_target == grant.workspace_target
            for existing in grants
        ):
            grants.append(grant)

    def _revoke_grant_locked(self, record: ApprovalRecord) -> None:
        if record.turn_id is None:
            return
        key = (
            record.provider,
            record.session_id,
            record.thread_id,
            record.turn_id,
        )
        grants = self._capability_grants.get(key)
        if not grants:
            return
        remaining = [
            grant for grant in grants if grant.one_shot_id != record.one_shot_id
        ]
        if remaining:
            self._capability_grants[key] = remaining
        else:
            self._capability_grants.pop(key, None)

    def _reusable_grant_locked(self, intent: CommandIntent) -> bool:
        if (
            intent.turn_id is None
            or intent.permission_scope is None
            or intent.workspace_target is None
        ):
            return False
        grants = self._capability_grants.get(
            (intent.provider, intent.session_id, intent.thread_id, intent.turn_id)
        )
        return any(
            _scope_allows(
                grant.permission_scope,
                intent.permission_scope,
                grant.workspace_target,
                intent.workspace_target,
            )
            for grant in grants or ()
        )

    async def clear_turn(
        self,
        *,
        provider: str,
        session_id: str,
        thread_id: str | None = None,
        turn_id: str,
    ) -> None:
        """Discard capability grants when a provider turn is complete."""
        key = (
            _provider_text(provider),
            _required_text(session_id, "session_id"),
            _optional_text(thread_id, "thread_id"),
            _required_text(turn_id, "turn_id"),
        )
        async with self._lock:
            self._capability_grants.pop(key, None)

    def _resolve_waiter_locked(
        self,
        one_shot_id: str,
        record: ApprovalRecord,
    ) -> None:
        future = self._waiters.get(one_shot_id)
        if future is not None and not future.done():
            future.set_result(record)

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


def _scope_allows(
    granted: str,
    requested: str,
    granted_workspace: Path,
    requested_workspace: Path,
) -> bool:
    if not _path_within(requested_workspace, granted_workspace):
        return False
    if granted == requested:
        return True
    granted_parts = granted.split(":", 2)
    requested_parts = requested.split(":", 2)
    if granted_parts[0] == "filesystem" and requested_parts[0] == "filesystem":
        if len(granted_parts) != 3 or len(requested_parts) != 3:
            return False
        granted_access, requested_access = granted_parts[1], requested_parts[1]
        if granted_access == "read" and requested_access != "read":
            return False
        if granted_access not in {"read", "write"} or requested_access not in {
            "read",
            "write",
        }:
            return False
        return _path_within(Path(requested_parts[2]), Path(granted_parts[2]))
    if granted_parts[0] == "network" and requested_parts[0] == "network":
        if len(granted_parts) not in {2, 3} or len(requested_parts) not in {2, 3}:
            return False
        granted_host = granted_parts[-1].lower().rstrip(".")
        requested_host = requested_parts[-1].lower().rstrip(".")
        if granted_host == "*":
            return True
        return requested_host == granted_host or requested_host.endswith(
            f".{granted_host}"
        )
    return False


def _native_workspace_write_is_level_a(
    provider: str,
    argv: tuple[str, ...],
    permission_scope: str | None,
    cwd: Path,
    workspace_target: Path,
    risk: CommandRisk,
) -> bool:
    if provider not in {"codex_cli", "claude_cli"} or risk is not CommandRisk.LEVEL_B:
        return False
    if not permission_scope or not permission_scope.startswith("filesystem:write:"):
        return False
    if not argv or argv[0] not in {"codex-file-change", "claude-file-change"}:
        return False
    path = Path(permission_scope.split(":", 2)[2])
    return _path_within(cwd, workspace_target) and _path_within(path, workspace_target)


def _permission_scope_requires_approval(
    scope: str | None,
    argv: tuple[str, ...],
    workspace_target: Path,
) -> bool:
    if not scope:
        return False
    if scope.startswith("codex:"):
        return True
    if scope.startswith("network:"):
        host = scope.removeprefix("network:").strip().lower().rstrip(".")
        if host in {"localhost", "127.0.0.1", "::1", "[::1]"}:
            return not _is_loopback_health_check(
                tuple(value.lower() for value in argv[1:]),
                executable=_executable_name(argv[0]) if argv else None,
            )
        return True
    if scope.startswith("filesystem:"):
        parts = scope.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"read", "write"}:
            return True
        return parts[1] != "read" or not _path_within(Path(parts[2]), workspace_target)
    return True


def _path_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except OSError, ValueError:
        return False
    return True


__all__ = [
    "ApprovalIntegrityError",
    "ApprovalManager",
    "ApprovalRecord",
    "ApprovalState",
    "CommandIntent",
    "CommandRisk",
    "CommandSyntaxError",
]
