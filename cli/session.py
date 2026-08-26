"""Claude Code CLI session management."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from loguru import logger

from .process_registry import register_process, unregister_process
from .runtime_environment import build_cli_environment
from .runtime_registry import RuntimeBackend, RuntimeRegistry


class CLISession:
    """Manages a single persistent Claude Code CLI subprocess."""

    def __init__(
        self,
        workspace_path: str,
        api_url: str,
        allowed_dirs: list[str] | None = None,
        plans_directory: str | None = None,
        permission_mode: str = "plan",
        use_proxy: bool = True,
        claude_bin: str = "claude",
        isolation_mode: str = "safe",
        runtime_registry: RuntimeRegistry | None = None,
        preflight_runtime: bool = False,
    ):
        self.workspace = os.path.normpath(os.path.abspath(workspace_path))
        self.api_url = api_url
        self.allowed_dirs = [os.path.normpath(d) for d in (allowed_dirs or [])]
        self.plans_directory = plans_directory
        allowed_permission_modes = {
            "plan",
            "acceptEdits",
            "auto",
            "bypassPermissions",
        }
        if permission_mode not in allowed_permission_modes:
            raise ValueError(
                "permission_mode must be one of: "
                + ", ".join(sorted(allowed_permission_modes))
            )
        self.permission_mode = permission_mode
        self.use_proxy = use_proxy
        if isolation_mode not in {"safe", "inherit"}:
            raise ValueError("isolation_mode must be 'safe' or 'inherit'")
        self.claude_bin = claude_bin
        self.isolation_mode = isolation_mode
        self.runtime_registry = runtime_registry or RuntimeRegistry(
            executables={RuntimeBackend.CLAUDE: claude_bin}
        )
        self.preflight_runtime = preflight_runtime
        self.process: asyncio.subprocess.Process | None = None
        self.current_session_id: str | None = None
        self.generation: str | None = None
        self._is_busy = False
        self._cli_lock = asyncio.Lock()

    @property
    def is_busy(self) -> bool:
        """Check if a task is currently running."""
        return self._is_busy

    async def start_task(
        self, prompt: str, session_id: str | None = None, fork_session: bool = False
    ) -> AsyncGenerator[dict]:
        """
        Start a new task or continue an existing session.

        Args:
            prompt: The user's message/prompt
            session_id: Optional session ID to resume

        Yields:
            Event dictionaries from the CLI
        """
        async with self._cli_lock:
            if self.preflight_runtime:
                probe = await self.runtime_registry.probe(RuntimeBackend.CLAUDE)
                if not probe.available:
                    reason = probe.reason or "runtime_unavailable"
                    yield {
                        "type": "error",
                        "error": {"message": f"Claude CLI preflight failed: {reason}"},
                    }
                    yield {"type": "exit", "code": 127, "stderr": reason}
                    return

            self._is_busy = True
            extra_env: dict[str, str] = {}
            if self.use_proxy:
                if self.api_url.endswith("/v1"):
                    base_url = self.api_url[:-3]
                else:
                    base_url = self.api_url
                extra_env.update(
                    {
                        "ANTHROPIC_API_URL": self.api_url,
                        "ANTHROPIC_BASE_URL": base_url,
                    }
                )
            env = build_cli_environment(
                RuntimeBackend.CLAUDE,
                extra_env=extra_env,
            )
            if self.use_proxy:
                # The proxy route needs an API-key-shaped value, but the
                # gateway must not inherit unrelated account credentials.
                env["ANTHROPIC_API_KEY"] = "sk-placeholder-key-for-proxy"

            # Build command
            if session_id and not session_id.startswith("pending_"):
                cmd = [
                    self.claude_bin,
                    "--resume",
                    session_id,
                ]
                if fork_session:
                    cmd.append("--fork-session")
                cmd += [
                    "-p",
                    prompt,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ]
                logger.info(f"Resuming Claude session {session_id}")
            else:
                cmd = [
                    self.claude_bin,
                    "-p",
                    prompt,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ]
                logger.info("Starting new Claude session")

            if self.isolation_mode == "safe":
                cmd.extend(["--safe-mode", "--strict-mcp-config"])

            if self.permission_mode == "bypassPermissions":
                cmd.append("--dangerously-skip-permissions")
            else:
                cmd.extend(["--permission-mode", self.permission_mode])

            if self.allowed_dirs:
                for d in self.allowed_dirs:
                    cmd.extend(["--add-dir", d])

            if self.plans_directory is not None:
                settings_json = json.dumps({"plansDirectory": self.plans_directory})
                cmd.extend(["--settings", settings_json])

            generation = uuid.uuid4().hex
            self.generation = generation
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workspace,
                    env=env,
                )
                if self.process and self.process.pid:
                    register_process(self.process.pid, generation=generation)

                if not self.process or not self.process.stdout:
                    yield {"type": "exit", "code": 1}
                    return

                session_id_extracted = False
                buffer = bytearray()

                try:
                    while True:
                        chunk = await self.process.stdout.read(65536)
                        if not chunk:
                            if buffer:
                                line_str = buffer.decode(
                                    "utf-8", errors="replace"
                                ).strip()
                                if line_str:
                                    async for event in self._handle_line_gen(
                                        line_str, session_id_extracted
                                    ):
                                        if event.get("type") == "session_info":
                                            session_id_extracted = True
                                        yield event
                            break

                        buffer.extend(chunk)

                        while True:
                            newline_pos = buffer.find(b"\n")
                            if newline_pos == -1:
                                break

                            line = buffer[:newline_pos]
                            buffer = buffer[newline_pos + 1 :]

                            line_str = line.decode("utf-8", errors="replace").strip()
                            if line_str:
                                async for event in self._handle_line_gen(
                                    line_str, session_id_extracted
                                ):
                                    if event.get("type") == "session_info":
                                        session_id_extracted = True
                                    yield event
                except asyncio.CancelledError:
                    # Cancelling the handler task should not leave a Claude CLI
                    # subprocess running in the background.
                    try:
                        await asyncio.shield(self.stop())
                    finally:
                        raise

                stderr_text = None
                if self.process.stderr:
                    stderr_output = await self.process.stderr.read()
                    if stderr_output:
                        stderr_text = stderr_output.decode(
                            "utf-8", errors="replace"
                        ).strip()

                return_code = await self.process.wait()
                logger.info(
                    f"Claude CLI exited with code {return_code}, stderr_present={bool(stderr_text)}"
                )

                # Only emit stderr as error if exit code is non-zero
                # This avoids false errors from warnings/info messages
                if stderr_text and return_code != 0:
                    logger.error(
                        "Claude CLI exited with code {} and stderr ({} bytes)",
                        return_code,
                        len(stderr_output),
                    )
                    logger.info("CLI_SESSION: Yielding error event from stderr")
                    yield {"type": "error", "error": {"message": stderr_text}}
                elif stderr_text:
                    # Keep successful stderr out of logs; it can contain provider details.
                    logger.debug(
                        "Claude CLI stderr captured on successful exit ({} bytes)",
                        len(stderr_output),
                    )
                elif return_code != 0:
                    logger.warning(
                        f"CLI_SESSION: Process exited with code {return_code} but no stderr captured"
                    )

                yield {
                    "type": "exit",
                    "code": return_code,
                    "stderr": stderr_text,
                }
            finally:
                self._is_busy = False
                if self.process and self.process.pid:
                    unregister_process(self.process.pid, generation=generation)

    async def _handle_line_gen(
        self, line_str: str, session_id_extracted: bool
    ) -> AsyncGenerator[dict]:
        """Process a single line and yield events."""
        try:
            event = json.loads(line_str)
            if not session_id_extracted:
                extracted_id = self._extract_session_id(event)
                if extracted_id:
                    self.current_session_id = extracted_id
                    logger.info(f"Extracted session ID: {extracted_id}")
                    yield {"type": "session_info", "session_id": extracted_id}

            yield event
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON output: {line_str}")
            yield {"type": "raw", "content": line_str}

    def _extract_session_id(self, event: Any) -> str | None:
        """Extract session ID from CLI event."""
        if not isinstance(event, dict):
            return None

        if "session_id" in event:
            return event["session_id"]
        if "sessionId" in event:
            return event["sessionId"]

        for key in ["init", "system", "result", "metadata"]:
            if key in event and isinstance(event[key], dict):
                nested = event[key]
                if "session_id" in nested:
                    return nested["session_id"]
                if "sessionId" in nested:
                    return nested["sessionId"]

        if "conversation" in event and isinstance(event["conversation"], dict):
            conv = event["conversation"]
            if "id" in conv:
                return conv["id"]

        return None

    async def stop(self):
        """Stop the CLI process."""
        if self.process and self.process.returncode is None:
            try:
                logger.info(f"Stopping Claude CLI process {self.process.pid}")
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5.0)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
                if self.process and self.process.pid:
                    generation = self.generation
                    if generation is not None:
                        unregister_process(self.process.pid, generation=generation)
                return True
            except Exception as e:
                logger.error(f"Error stopping process: {e}")
                return False
        return False

    def get_stats(self) -> dict[str, Any]:
        """Return runtime identity without exposing process arguments or env."""
        return {
            "backend": "claude",
            "session_id": self.current_session_id,
            "generation": self.generation,
            "is_busy": self.is_busy,
        }
