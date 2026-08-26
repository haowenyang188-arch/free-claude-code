"""Simplified wrapper for CodexSession to avoid import issues."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from typing import Any

from loguru import logger


class CodexSessionWrapper:
    """Simplified Codex session manager for workbench integration."""

    def __init__(
        self,
        workspace_path: str,
        *,
        codex_bin: str = "codex",
        sandbox_mode: str = "read-only",
        model: str | None = None,
    ) -> None:
        self.workspace = os.path.normpath(os.path.abspath(workspace_path))
        self.codex_bin = codex_bin
        self.sandbox_mode = sandbox_mode
        self.model = model
        self.process: asyncio.subprocess.Process | None = None
        self.current_session_id: str | None = None
        self._is_busy = False
        self._cli_lock = asyncio.Lock()

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    def build_command(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> list[str]:
        """Build an argv list without invoking a shell."""
        if not prompt.strip():
            raise ValueError("Codex prompt must not be empty")

        command = [self.codex_bin, "exec"]
        if session_id and not session_id.startswith("pending_"):
            command.extend(["resume", session_id])

        command.append("--json")
        if self.model:
            command.extend(["--model", self.model])
        command.append("--skip-git-repo-check")

        # New threads must receive the explicit sandbox choice
        if not session_id or session_id.startswith("pending_"):
            command.extend(["--cd", self.workspace, "--sandbox", self.sandbox_mode])

        command.append(prompt)
        return command

    async def start_task(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Run one Codex turn and yield normalized events."""
        async with self._cli_lock:
            command = self.build_command(prompt, session_id=session_id)
            self._is_busy = True

            env = os.environ.copy()
            env.setdefault("TERM", "dumb")
            env.setdefault("PYTHONIOENCODING", "utf-8")

            try:
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workspace,
                    env=env,
                )

                if not self.process.stdout:
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
                logger.info(f"Codex CLI exited with code {return_code}")

                if stderr_text and return_code != 0:
                    logger.warning(f"Codex CLI stderr: {stderr_text[:200]}")
                    yield {"type": "error", "error": {"message": stderr_text}}

                yield {
                    "type": "exit",
                    "code": return_code,
                    "stderr": stderr_text or None,
                }
            except Exception as exc:
                logger.error(f"Codex CLI session failed: {type(exc).__name__}")
                yield {"type": "error", "error": {"message": str(exc)}}
                yield {"type": "exit", "code": 1, "stderr": str(exc)}
            finally:
                self._is_busy = False
                self.process = None

    async def _read_events(
        self, stdout: asyncio.StreamReader
    ) -> AsyncGenerator[dict[str, Any]]:
        """Convert Codex JSONL records to normalized events."""
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

        if event_type in {"response.output_text.delta", "output_text.delta"}:
            text = record.get("delta") or record.get("text")
            if isinstance(text, str) and text:
                return self._assistant_text(text)

        if event_type == "error":
            error = record.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            return {"type": "error", "error": {"message": str(message or "Codex error")}}

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
        """Terminate the active process."""
        process = self.process
        if not process or process.returncode is not None:
            return False
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
            return True
        except Exception as exc:
            logger.error(f"Error stopping Codex CLI: {type(exc).__name__}")
            return False

    async def pause(self) -> bool:
        if self.process and self.process.returncode is None:
            self.process.send_signal(19)  # SIGSTOP
            return True
        return False

    async def resume_process(self) -> bool:
        if self.process and self.process.returncode is None:
            self.process.send_signal(18)  # SIGCONT
            return True
        return False

    def get_stats(self) -> dict[str, Any]:
        return {
            "backend": "codex",
            "session_id": self.current_session_id,
            "is_busy": self.is_busy,
        }
