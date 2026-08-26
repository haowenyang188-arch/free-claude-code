"""Non-interactive Codex CLI session runner.

The runner intentionally uses ``create_subprocess_exec`` and a read-only
sandbox by default.  It exposes the small event vocabulary consumed by the
existing messaging layer while keeping Codex-specific JSONL parsing here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from loguru import logger

from .process_registry import register_process, unregister_process
from .runtime_environment import build_cli_environment
from .runtime_registry import RuntimeBackend, RuntimeRegistry
from .staging import StagedWorkspace


class CodexSession:
    """Manage one persistent Codex thread from a local WSL process."""

    shell = False

    def __init__(
        self,
        workspace_path: str,
        *,
        codex_bin: str = "codex",
        sandbox_mode: str = "read-only",
        model: str | None = None,
        approval_mode: bool = False,
        isolation_mode: str = "safe",
        runtime_registry: RuntimeRegistry | None = None,
        preflight_runtime: bool = False,
    ) -> None:
        self.workspace = os.path.normpath(os.path.abspath(workspace_path))
        self.codex_bin = codex_bin
        self.sandbox_mode = sandbox_mode
        self.model = model
        self.approval_mode = approval_mode
        if isolation_mode not in {"safe", "inherit"}:
            raise ValueError("isolation_mode must be 'safe' or 'inherit'")
        self.isolation_mode = isolation_mode
        self.runtime_registry = runtime_registry or RuntimeRegistry(
            executables={RuntimeBackend.CODEX: codex_bin}
        )
        self.preflight_runtime = preflight_runtime
        if approval_mode and sandbox_mode == "danger-full-access":
            raise ValueError(
                "Codex staged approval cannot be combined with danger-full-access"
            )
        self._staged_workspace: StagedWorkspace | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.current_session_id: str | None = None
        self.generation: str | None = None
        self.last_run_id: str | None = None
        self._is_busy = False
        self._cancel_requested = False
        self._cli_lock = asyncio.Lock()

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    def build_command(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        fork_session: bool = False,
    ) -> list[str]:
        """Build an argv list without invoking a shell or interpolating input."""
        if not prompt.strip():
            raise ValueError("Codex prompt must not be empty")

        command = [self.codex_bin, "exec"]
        if session_id and not session_id.startswith("pending_"):
            command.extend(["fork" if fork_session else "resume", session_id])

        command.append("--json")
        if self.isolation_mode == "safe":
            command.extend(["--ignore-user-config", "--ignore-rules", "--strict-config"])
        if self.model:
            command.extend(["--model", self.model])
        command.append("--skip-git-repo-check")

        # Resume/fork inherit the original thread policy.  New threads must
        # receive the explicit safe sandbox choice.
        if not session_id or session_id.startswith("pending_"):
            command.extend(["--cd", self.workspace, "--sandbox", self.sandbox_mode])

        command.append(prompt)
        return command

    async def start_task(
        self,
        prompt: str,
        session_id: str | None = None,
        fork_session: bool = False,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Run one Codex turn and yield normalized events."""
        awaiting_approval = False
        async with self._cli_lock:
            if self.preflight_runtime:
                probe = await self.runtime_registry.probe(RuntimeBackend.CODEX)
                if not probe.available:
                    reason = probe.reason or "runtime_unavailable"
                    yield {
                        "type": "error",
                        "error": {"message": f"Codex CLI preflight failed: {reason}"},
                    }
                    yield {"type": "exit", "code": 127, "stderr": reason}
                    return

            command = self.build_command(
                prompt, session_id=session_id, fork_session=fork_session
            )
            if self.approval_mode and self.sandbox_mode != "read-only":
                self._staged_workspace = StagedWorkspace.create(self.workspace)
                command = self.build_command(
                    prompt,
                    session_id=session_id,
                    fork_session=fork_session,
                )
                if "--cd" in command:
                    command[command.index("--cd") + 1] = str(
                        self._staged_workspace.path
                    )
            execution_workspace = (
                self._staged_workspace.path
                if self._staged_workspace is not None
                else self.workspace
            )
            self._is_busy = True
            self._cancel_requested = False
            env = build_cli_environment(RuntimeBackend.CODEX)
            generation = uuid.uuid4().hex
            self.generation = generation

            try:
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=execution_workspace,
                    env=env,
                )
                if self.process.pid:
                    register_process(self.process.pid, generation=generation)

                if not self.process.stdout:
                    self.reject()
                    yield {"type": "exit", "code": 1, "stderr": "No stdout"}
                    return

                async for event in self._read_events(self.process.stdout):
                    if event.get("type") == "session_info":
                        self.current_session_id = event.get("session_id")
                    yield event

                stderr_text = ""
                if self.process.stderr:
                    stderr_bytes = await self.process.stderr.read()
                    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

                return_code = await self.process.wait()
                logger.info(
                    f"Codex CLI exited with code {return_code}, stderr_present={bool(stderr_text)}"
                )

                if stderr_text:
                    # Filter out benign Codex CLI informational messages
                    is_benign = any(
                        msg in stderr_text
                        for msg in [
                            "Reading additional input from stdin",
                            "Waiting for input",
                        ]
                    )
                    if is_benign:
                        logger.debug(
                            "Codex CLI informational message: {} bytes",
                            len(stderr_bytes),
                        )
                    elif return_code != 0:
                        # Do not put command arguments or credentials in logs.
                        logger.warning(
                            "Codex CLI returned stderr ({} bytes)", len(stderr_bytes)
                        )
                        yield {"type": "error", "error": {"message": stderr_text}}
                    else:
                        logger.debug(
                            "Codex CLI stderr captured on successful exit ({} bytes)",
                            len(stderr_bytes),
                        )
                elif return_code != 0:
                    logger.warning(
                        f"CODEX_SESSION: Process exited with code {return_code} but no stderr captured"
                    )

                if self._staged_workspace is not None:
                    changes = self._staged_workspace.changes()
                    if changes and return_code == 0:
                        awaiting_approval = True
                        yield {
                            "type": "approval_required",
                            "diff": self._staged_workspace.diff(),
                            "changed_paths": [change.path for change in changes],
                        }
                        yield {
                            "type": "approval_waiting",
                            "awaiting_approval": True,
                        }
                        return
                    if changes:
                        self.reject()
                    else:
                        self._staged_workspace.cleanup()
                        self._staged_workspace = None
                yield {
                    "type": "exit",
                    "code": return_code,
                    "stderr": stderr_text or None,
                }
            except asyncio.CancelledError:
                await asyncio.shield(self.stop())
                self.reject()
                raise
            except OSError as exc:
                self.reject()
                logger.error("Unable to start Codex CLI: {}", type(exc).__name__)
                yield {"type": "error", "error": {"message": str(exc)}}
                yield {"type": "exit", "code": 1, "stderr": str(exc)}
            except Exception as exc:
                self.reject()
                logger.error("Codex CLI session failed: {}", type(exc).__name__)
                yield {"type": "error", "error": {"message": str(exc)}}
                yield {"type": "exit", "code": 1, "stderr": str(exc)}
            finally:
                if self.process and self.process.pid:
                    unregister_process(self.process.pid, generation=generation)
                # Clean up staged workspace only if not awaiting approval
                # If awaiting approval, workspace must survive until approve()/reject()
                if self._staged_workspace is not None and not awaiting_approval:
                    with contextlib.suppress(Exception):
                        self._staged_workspace.cleanup()
                    self._staged_workspace = None
                self._is_busy = False
                self.process = None

    async def _read_events(
        self, stdout: asyncio.StreamReader
    ) -> AsyncGenerator[dict[str, Any]]:
        """Convert Codex JSONL records to Claude-message-compatible events."""
        while True:
            raw_line = await stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                yield {"type": "raw", "content": line}
                continue

            if not isinstance(record, dict):
                continue
            session_id = self._extract_session_id(record)
            if session_id and self.current_session_id is None:
                self.current_session_id = session_id
                yield {"type": "session_info", "session_id": session_id}

            event = self._normalize_record(record)
            if event is not None:
                yield event

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        event_type = record.get("type")
        item = record.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type in {"agent_message", "message"}:
                text = self._text_from(item)
                if text:
                    return self._assistant_text(text)
            if item_type in {"command_execution", "tool_call"}:
                return self._tool_event(item, completed=event_type == "item.completed")
            if item_type in {"file_change", "file_changes"}:
                return {"type": "file_change", "item": item}

        if event_type in {"response.output_text.delta", "output_text.delta"}:
            text = record.get("delta") or record.get("text")
            if isinstance(text, str) and text:
                return self._assistant_text(text)

        if event_type == "error":
            error = record.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            # Add context to generic error messages
            error_msg = str(message or "Codex error")
            if error_msg == "Codex error" and "type" in record:
                error_msg = f"Codex error (event: {record['type']})"
            return {
                "type": "error",
                "error": {"message": error_msg},
            }

        # Some Codex versions emit a top-level message/text record.
        text = self._text_from(record)
        if event_type in {"message", "assistant", "agent_message"} and text:
            return self._assistant_text(text)
        return None

    @staticmethod
    def _assistant_text(text: str) -> dict[str, Any]:
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }

    @staticmethod
    def _tool_event(item: dict[str, Any], *, completed: bool) -> dict[str, Any]:
        tool_id = str(item.get("id") or item.get("call_id") or "codex-tool")
        name = str(item.get("name") or item.get("command") or "command")
        if not completed:
            return {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": name,
                            "input": item.get("input") or item.get("command"),
                        }
                    ]
                },
            }
        result = (
            item.get("aggregated_output")
            or item.get("output")
            or item.get("result")
            or ""
        )
        return {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                        "is_error": bool(item.get("status") == "failed"),
                    }
                ]
            },
        }

    @staticmethod
    def _text_from(value: dict[str, Any]) -> str:
        for key in ("text", "message", "content", "summary"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
            if isinstance(candidate, dict):
                nested = candidate.get("text")
                if isinstance(nested, str):
                    return nested
        return ""

    @staticmethod
    def _extract_session_id(record: dict[str, Any]) -> str | None:
        for key in ("thread_id", "session_id", "threadId", "sessionId"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for key in ("thread", "session"):
            nested = record.get(key)
            if isinstance(nested, dict):
                value = nested.get("id") or nested.get("thread_id")
                if isinstance(value, str) and value.strip():
                    return value
        return None

    async def stop(self) -> bool:
        """Terminate the active process, escalating to SIGKILL after five seconds."""
        process = self.process
        if not process or process.returncode is not None:
            return False
        try:
            self._cancel_requested = True
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
            # Clean up any pending staged workspace
            self.reject()
            return True
        except Exception as exc:
            logger.error("Error stopping Codex CLI: {}", type(exc).__name__)
            return False

    async def approve(self) -> dict[str, Any]:
        """Apply the pending staged diff after an explicit operator decision."""
        staged = self._staged_workspace
        if staged is None:
            raise RuntimeError("no staged Codex changes are awaiting approval")
        changes = staged.apply()
        staged.cleanup()
        self._staged_workspace = None
        return {"changed_paths": [change.path for change in changes]}

    def reject(self) -> None:
        staged = self._staged_workspace
        if staged is not None:
            staged.cleanup()
            self._staged_workspace = None

    async def pause(self) -> bool:
        if self.process and self.process.returncode is None:
            self.process.send_signal(signal.SIGSTOP)
            return True
        return False

    async def resume(self) -> bool:
        if self.process and self.process.returncode is None:
            self.process.send_signal(signal.SIGCONT)
            return True
        return False

    async def send_message(self, message: str) -> bool:
        # The JSONL exec process is one-shot; follow-ups are launched by the
        # manager as a resumed/forked session instead of writing to dead stdin.
        return False

    def get_stats(self) -> dict[str, Any]:
        return {
            "backend": "codex",
            "session_id": self.current_session_id,
            "generation": self.generation,
            "is_busy": self.is_busy,
        }
