"""
CLI Session Manager for Multi-Instance Claude CLI Support

Manages a pool of CLISession instances, each handling one conversation.
This enables true parallel processing where multiple conversations run
simultaneously in separate CLI processes.
"""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from loguru import logger

from .checkpoint import CheckpointManifest, CheckpointStore
from .codex_session import CodexSession
from .runtime_registry import RuntimeBackend, RuntimeRegistry
from .session import CLISession

SessionBackend = CLISession | CodexSession


class CLISessionManager:
    """
    Manages multiple CLISession instances for parallel conversation processing.

    Each new conversation gets its own CLISession with its own subprocess.
    Replies to existing conversations reuse the same CLISession instance.
    """

    def __init__(
        self,
        workspace_path: str,
        api_url: str,
        allowed_dirs: list[str] | None = None,
        plans_directory: str | None = None,
        agent_backend: str = "claude",
        agent_permission_mode: str = "plan",
        claude_auth_mode: str = "proxy",
        claude_bin: str = "claude",
        codex_bin: str = "codex",
        codex_model: str | None = None,
        codex_sandbox: str = "read-only",
        codex_approval_required: bool = True,
        runtime_registry: RuntimeRegistry | None = None,
        preflight_runtime: bool = True,
        isolation_mode: str = "safe",
    ):
        """
        Initialize the session manager.

        Args:
            workspace_path: Working directory for CLI processes
            api_url: API URL for the proxy
            allowed_dirs: Directories the CLI is allowed to access
            plans_directory: Directory for Claude Code CLI plan files (passed via --settings)
        """
        self.workspace = workspace_path
        self.api_url = api_url
        self.allowed_dirs = allowed_dirs or []
        self.plans_directory = plans_directory
        if agent_backend not in ("claude", "codex"):
            raise ValueError("agent_backend must be 'claude' or 'codex'")
        self.agent_backend = agent_backend
        self.agent_permission_mode = agent_permission_mode
        if claude_auth_mode not in ("proxy", "local"):
            raise ValueError("claude_auth_mode must be 'proxy' or 'local'")
        self.claude_auth_mode = claude_auth_mode
        self.claude_bin = claude_bin
        self.codex_bin = codex_bin
        self.codex_model = codex_model
        self.codex_sandbox = codex_sandbox
        self.codex_approval_required = codex_approval_required
        if (
            self.agent_backend == "codex"
            and self.codex_approval_required
            and self.codex_sandbox == "danger-full-access"
        ):
            raise ValueError(
                "codex_approval_required cannot be used with danger-full-access"
            )

        self.runtime_registry = runtime_registry or RuntimeRegistry(
            executables={"claude": claude_bin, "codex": codex_bin}
        )
        self.preflight_runtime = preflight_runtime
        if isolation_mode not in {"safe", "inherit"}:
            raise ValueError("isolation_mode must be 'safe' or 'inherit'")
        self.isolation_mode = isolation_mode

        self._sessions: dict[str, SessionBackend] = {}
        self._pending_sessions: dict[str, SessionBackend] = {}
        self._temp_to_real: dict[str, str] = {}
        self._real_to_temp: dict[str, str] = {}
        self._lock = asyncio.Lock()

        logger.info("CLISessionManager initialized")

    async def get_or_create_session(
        self, session_id: str | None = None
    ) -> tuple[SessionBackend, str, bool]:
        """
        Get an existing session or create a new one.

        Returns:
            Tuple of (CLISession instance, session_id, is_new_session)
        """
        async with self._lock:
            if session_id:
                lookup_id = self._temp_to_real.get(session_id, session_id)

                if lookup_id in self._sessions:
                    return self._sessions[lookup_id], lookup_id, False
                if lookup_id in self._pending_sessions:
                    return self._pending_sessions[lookup_id], lookup_id, False

            temp_id = session_id if session_id else f"pending_{uuid.uuid4().hex[:8]}"

            if self.agent_backend == "codex":
                new_session = CodexSession(
                    workspace_path=self.workspace,
                    codex_bin=self.codex_bin,
                    sandbox_mode=self.codex_sandbox,
                    model=self.codex_model,
                    approval_mode=self.codex_approval_required,
                    isolation_mode=self.isolation_mode,
                    runtime_registry=self.runtime_registry,
                    preflight_runtime=self.preflight_runtime,
                )
            else:
                new_session = CLISession(
                    workspace_path=self.workspace,
                    api_url=self.api_url,
                    allowed_dirs=self.allowed_dirs,
                    plans_directory=self.plans_directory,
                    permission_mode=self.agent_permission_mode,
                    use_proxy=self.claude_auth_mode == "proxy",
                    claude_bin=self.claude_bin,
                    isolation_mode=self.isolation_mode,
                    runtime_registry=self.runtime_registry,
                    preflight_runtime=self.preflight_runtime,
                )
            self._pending_sessions[temp_id] = new_session
            logger.info(f"Created new session: {temp_id}")

            return new_session, temp_id, True

    async def register_real_session_id(
        self, temp_id: str, real_session_id: str
    ) -> bool:
        """Register the real session ID from CLI output."""
        async with self._lock:
            if temp_id not in self._pending_sessions:
                logger.warning(f"Temp session {temp_id} not found")
                return False

            session = self._pending_sessions.pop(temp_id)
            self._sessions[real_session_id] = session
            self._temp_to_real[temp_id] = real_session_id
            self._real_to_temp[real_session_id] = temp_id

            logger.info(f"Registered session: {temp_id} -> {real_session_id}")
            return True

    async def remove_session(self, session_id: str) -> bool:
        """Remove a session from the manager."""
        async with self._lock:
            if session_id in self._pending_sessions:
                session = self._pending_sessions.pop(session_id)
                await session.stop()
                return True

            if session_id in self._sessions:
                session = self._sessions.pop(session_id)
                await session.stop()
                temp_id = self._real_to_temp.pop(session_id, None)
                if temp_id is not None:
                    self._temp_to_real.pop(temp_id, None)
                return True

            return False

    async def stop_all(self):
        """Stop all sessions."""
        async with self._lock:
            all_sessions = list(self._sessions.values()) + list(
                self._pending_sessions.values()
            )
            for session in all_sessions:
                try:
                    await session.stop()
                    reject = getattr(session, "reject", None)
                    if callable(reject):
                        reject()
                except Exception as e:
                    logger.error(f"Error stopping session: {e}")

            self._sessions.clear()
            self._pending_sessions.clear()
            self._temp_to_real.clear()
            self._real_to_temp.clear()
            logger.info("All sessions stopped")

    async def runtime_status(self, *, force: bool = False) -> dict:
        """Return a user-safe readiness report for the selected CLI backend."""
        probe = await self.runtime_registry.probe(self.agent_backend, force=force)
        safe_profile = await self.runtime_registry.probe_safe_profile(
            self.agent_backend, force=force
        )
        return {
            "backend": self.agent_backend,
            "runtime": probe.to_mapping(),
            "isolation_mode": self.isolation_mode,
            "safe_profile": safe_profile.to_mapping(),
        }

    async def build_checkpoint(
        self,
        session_id: str,
        *,
        run_id: str,
        step_id: str,
        input_digest: str,
    ) -> CheckpointManifest:
        """Build a fail-closed checkpoint for one completed safe CLI turn.

        This does not resume anything.  Consumers must explicitly validate the
        resulting manifest before using its session ID in a later turn.
        """
        _require_sha256(input_digest, "input_digest")
        run_id = _required_text(run_id, "run_id")
        step_id = _required_text(step_id, "step_id")
        if not self._checkpoint_policy_is_safe():
            raise RuntimeError("checkpointing requires a safe read-only policy")

        async with self._lock:
            resolved_id = self._temp_to_real.get(session_id, session_id)
            session = self._sessions.get(resolved_id) or self._pending_sessions.get(
                resolved_id
            )
            if session is None:
                raise RuntimeError("checkpoint session is not managed")
            if session.is_busy is not False:
                raise RuntimeError("cannot checkpoint an active session")
            if getattr(session, "has_pending_approval", False) is True:
                raise RuntimeError("cannot checkpoint with pending approval")
            runtime_session_id = getattr(session, "current_session_id", None)
            generation = getattr(session, "generation", None)
            last_run_id = getattr(session, "last_run_id", None)
            if not isinstance(last_run_id, str) or last_run_id.strip() != run_id:
                raise RuntimeError("checkpoint run does not match session")

        if not isinstance(runtime_session_id, str) or not runtime_session_id.strip():
            raise RuntimeError("checkpoint session has no runtime session ID")
        if not isinstance(generation, str) or not generation.strip():
            raise RuntimeError("checkpoint session has no runtime generation")

        backend = RuntimeBackend(self.agent_backend)
        probe = await self.runtime_registry.probe(backend)
        if not probe.available or not probe.version:
            raise RuntimeError("checkpoint runtime is not ready with a version")
        profile = await self.runtime_registry.probe_safe_profile(backend)
        if not profile.available:
            raise RuntimeError("checkpoint safe profile is unavailable")

        # The runtime probes yield control. Recheck the session identity before
        # publishing a manifest so a new turn cannot inherit an old lease.
        async with self._lock:
            current = self._sessions.get(resolved_id) or self._pending_sessions.get(
                resolved_id
            )
            if (
                current is not session
                or current.is_busy is not False
                or getattr(current, "current_session_id", None) != runtime_session_id
                or getattr(current, "generation", None) != generation
                or getattr(current, "last_run_id", None) != last_run_id
            ):
                raise RuntimeError("session changed during preflight")

        return CheckpointManifest(
            checkpoint_id=str(uuid.uuid4()),
            run_id=run_id,
            backend=backend,
            runtime_version=probe.version,
            session_id=runtime_session_id,
            generation=generation,
            workspace_digest=_workspace_digest(self.workspace),
            policy_digest=_policy_digest(
                backend=self.agent_backend,
                isolation_mode=self.isolation_mode,
                agent_permission_mode=self.agent_permission_mode,
                claude_auth_mode=self.claude_auth_mode,
                codex_sandbox=self.codex_sandbox,
                codex_approval_required=self.codex_approval_required,
                preflight_runtime=self.preflight_runtime,
                allowed_dirs=self.allowed_dirs,
                claude_bin=self.claude_bin,
                codex_bin=self.codex_bin,
                codex_model=self.codex_model,
                api_url=self.api_url,
                plans_directory=self.plans_directory,
                runtime_executable=probe.executable,
                safe_profile_flags=profile.required_flags,
            ),
            step_id=step_id,
            input_digest=input_digest,
        )

    async def write_checkpoint(
        self,
        store: CheckpointStore,
        session_id: str,
        *,
        run_id: str,
        step_id: str,
        input_digest: str,
    ) -> CheckpointManifest:
        """Persist an explicitly requested safe checkpoint atomically."""
        if not isinstance(store, CheckpointStore):
            raise TypeError("store must be a CheckpointStore")
        manifest = await self.build_checkpoint(
            session_id,
            run_id=run_id,
            step_id=step_id,
            input_digest=input_digest,
        )
        await asyncio.to_thread(store.save, manifest)
        return manifest

    def _checkpoint_policy_is_safe(self) -> bool:
        if self.isolation_mode != "safe":
            return False
        if self.agent_backend == RuntimeBackend.CODEX:
            return self.codex_sandbox == "read-only"
        return self.agent_permission_mode == "plan"

    def get_stats(self) -> dict:
        """Get session statistics."""
        return {
            "backend": self.agent_backend,
            "active_sessions": len(self._sessions),
            "pending_sessions": len(self._pending_sessions),
            "busy_count": sum(1 for s in self._sessions.values() if s.is_busy),
        }


def _workspace_digest(workspace: str) -> str:
    normalized = str(Path(workspace).expanduser().resolve(strict=False))
    return _sha256(normalized)


def _policy_digest(**policy: object) -> str:
    encoded = json.dumps(
        policy, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return _sha256(encoded)


def _require_sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a SHA-256 hex digest") from exc


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field} must not contain control characters")
    return normalized


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
