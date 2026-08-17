"""Validated, dependency-free configuration for the optional DSH runtime.

``HarnessConfig`` is intentionally independent from the application's
Pydantic settings.  Importing it has no process, filesystem, or Node runtime
side effects.  The bridge may construct it directly or use ``from_env`` when
the opt-in ``DSH_*`` environment variables are available.
"""

from __future__ import annotations

import json
import math
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_CONCURRENCY = 1
MAX_MAX_CONCURRENCY = 64
DEFAULT_MAX_FRAME_BYTES = 1 * 1024 * 1024
MAX_MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 64 * 1024
MAX_MAX_STDERR_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TOKENS = 49_152
MAX_MAX_TOKENS = 1_048_576
REQUIRED_CORDIS_PLUGIN = "@deepseek-ai/dsh-sdk-jsonrpc-server"
DEFAULT_CORDIS_PLUGINS = (
    REQUIRED_CORDIS_PLUGIN,
    "@deepseek-ai/dsh-agent-spine-demo",
    "@deepseek-ai/dsh-llm-deepseek",
    "@deepseek-ai/dsh-session-persistence-jsonl",
    "@deepseek-ai/dsh-session-checkpoint-policy",
    "@deepseek-ai/dsh-sandbox-local",
    "@deepseek-ai/dsh-sandbox-policy",
    "@deepseek-ai/dsh-subprocess-local",
    "@deepseek-ai/dsh-bash-sandbox",
    "@deepseek-ai/dsh-fs-sandbox",
)
_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
# The sandbox graph must use the configuration project's pinned Node closure.
# The rc.6 single-file runtime cannot resolve these sandbox provider packages
# from an external Cordis config and therefore is not a safe default here.
DEFAULT_RUNTIME_COMMAND = ("node", str(_RUNTIME_DIR / "runner.mjs"))
DEFAULT_CORDIS_CONFIG = _RUNTIME_DIR / "cordis.yml"

_MAX_TIMEOUT_SECONDS = 24 * 60 * 60.0
_MAX_ARG_BYTES = 64 * 1024
_MAX_PLUGIN_NAME_BYTES = 512
_CONTROL_CHARS = frozenset("\x00\r\n")
_PLUGIN_CONTAINER_KEYS = frozenset({"plugin", "plugins", "extension", "extensions"})
_PLUGIN_ID_KEYS = frozenset(
    {"id", "name", "package", "module", "plugin", "plugin_name", "pluginname"}
)


class HarnessConfigError(ValueError):
    """Raised when a Harness setting is unsafe or internally inconsistent."""


def _invalid(field: str, detail: str) -> HarnessConfigError:
    return HarnessConfigError(f"{field}: {detail}")


def _validate_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _invalid(field, "must be a string")
    if not allow_empty and not value:
        raise _invalid(field, "must not be empty")
    if any(char in value for char in _CONTROL_CHARS):
        raise _invalid(field, "must not contain NUL or newline characters")
    return value


def _normalize_argv(
    value: object,
    field: str,
    *,
    required: bool = False,
) -> tuple[str, ...] | None:
    if value is None:
        if required:
            raise _invalid(field, "is required when DeepSeek Harness is enabled")
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _invalid(field, "must be a sequence of argv strings, not a shell string")

    items: list[str] = []
    total_bytes = 0
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        text = _validate_text(item, item_field)
        size = len(text.encode("utf-8"))
        if size > _MAX_ARG_BYTES:
            raise _invalid(item_field, f"exceeds {_MAX_ARG_BYTES} UTF-8 bytes")
        total_bytes += size
        if total_bytes > _MAX_ARG_BYTES:
            raise _invalid(field, f"combined arguments exceed {_MAX_ARG_BYTES} bytes")
        items.append(text)

    if required and not items:
        raise _invalid(field, "must contain an executable")
    return tuple(items)


def _normalize_path(
    value: str | Path | None,
    field: str,
    *,
    require_directory: bool = False,
    allow_directory: bool = False,
) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise _invalid(field, "must be a path string")
    raw = str(value)
    _validate_text(raw, field)
    try:
        path = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _invalid(field, f"cannot be resolved: {exc}") from exc

    if require_directory:
        if not path.exists() or not path.is_dir():
            raise _invalid(field, "must point to an existing directory")
    elif path.exists() and not path.is_file() and not allow_directory:
        raise _invalid(field, "must point to a file when it exists")
    return path


def _normalize_allowlist(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _invalid("plugin_allowlist", "must be a sequence of plugin names")
    names: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        field = f"plugin_allowlist[{index}]"
        if not isinstance(item, str):
            raise _invalid(field, "must be a string")
        # Check control characters before trimming so a malformed value cannot
        # be silently normalized into an apparently valid plugin id.
        _validate_text(item, field)
        name = item.strip()
        if not name:
            raise _invalid(field, "must not be empty")
        if len(name.encode("utf-8")) > _MAX_PLUGIN_NAME_BYTES:
            raise _invalid(field, f"exceeds {_MAX_PLUGIN_NAME_BYTES} UTF-8 bytes")
        if name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)


def _validate_timeout(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _invalid(field, "must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise _invalid(field, "must be finite and greater than zero")
    if number > _MAX_TIMEOUT_SECONDS:
        raise _invalid(field, f"must not exceed {_MAX_TIMEOUT_SECONDS:g} seconds")
    return number


def _validate_positive_int(value: object, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(field, "must be an integer")
    if value < 1 or value > maximum:
        raise _invalid(field, f"must be between 1 and {maximum}")
    return value


def _parse_bool(raw: str, field: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise _invalid(field, "must be one of true/false, yes/no, or 1/0")


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(key, "must be a string")
    return value


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = _env_value(env, key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise _invalid(key, "must be a number") from exc


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _env_value(env, key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip(), 10)
    except ValueError as exc:
        raise _invalid(key, "must be an integer") from exc


def _env_argv(env: Mapping[str, str], key: str) -> tuple[str, ...] | None:
    raw = _env_value(env, key)
    if raw is None or not raw.strip():
        return None
    try:
        values = tuple(shlex.split(raw, comments=False, posix=True))
    except ValueError as exc:
        raise _invalid(key, f"has invalid shell quoting: {exc}") from exc
    return values


def _env_allowlist(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = _env_value(env, key)
    if raw is None or not raw.strip():
        return ()
    values = tuple(part.strip() for part in raw.split(","))
    if any(not part for part in values):
        raise _invalid(key, "contains an empty plugin name")
    return values


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """Opt-in settings and resource limits for a DSH runtime process.

    ``runtime_command`` and ``runtime_args`` are argv sequences.  They are
    never passed through a shell by the bridge.  An empty plugin allowlist is a
    deny-all policy for bridge-side checks; callers must explicitly list every
    plugin they permit.
    """

    enabled: bool = False
    runtime_command: Sequence[str] | None = None
    runtime_args: Sequence[str] = ()
    cordis_config: str | Path | None = None
    plugin_allowlist: Sequence[str] = ()
    workspace_root: str | Path | None = None
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES
    max_tokens: int = DEFAULT_MAX_TOKENS
    runtime_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise _invalid("enabled", "must be a boolean")

        command = _normalize_argv(
            self.runtime_command,
            "runtime_command",
            # ``None`` means the optional bundled carrier could not be
            # resolved yet; an explicitly supplied empty sequence is still an
            # invalid command.
            required=self.runtime_command is not None,
        )
        args = _normalize_argv(self.runtime_args, "runtime_args")
        assert args is not None
        allowlist = _normalize_allowlist(self.plugin_allowlist)
        workspace = _normalize_path(
            self.workspace_root,
            "workspace_root",
            require_directory=self.workspace_root is not None,
        )
        cordis_candidate = self.cordis_config
        if workspace is not None and isinstance(cordis_candidate, (str, Path)):
            candidate_path = Path(cordis_candidate)
            if not candidate_path.is_absolute():
                cordis_candidate = workspace / candidate_path
        cordis = _normalize_path(cordis_candidate, "cordis_config")
        timeout = _validate_timeout(
            self.request_timeout_seconds, "request_timeout_seconds"
        )
        shutdown_timeout = _validate_timeout(
            self.shutdown_timeout_seconds, "shutdown_timeout_seconds"
        )
        concurrency = _validate_positive_int(
            self.max_concurrency,
            "max_concurrency",
            maximum=MAX_MAX_CONCURRENCY,
        )
        max_frame = _validate_positive_int(
            self.max_frame_bytes,
            "max_frame_bytes",
            maximum=MAX_MAX_FRAME_BYTES,
        )
        max_stderr = _validate_positive_int(
            self.max_stderr_bytes,
            "max_stderr_bytes",
            maximum=MAX_MAX_STDERR_BYTES,
        )
        max_tokens = _validate_positive_int(
            self.max_tokens,
            "max_tokens",
            maximum=MAX_MAX_TOKENS,
        )

        version = self.runtime_version
        if version is not None:
            version = _validate_text(version, "runtime_version").strip()
            if not version:
                raise _invalid("runtime_version", "must not be empty")

        object.__setattr__(self, "runtime_command", command)
        object.__setattr__(self, "runtime_args", args)
        object.__setattr__(self, "plugin_allowlist", allowlist)
        object.__setattr__(self, "cordis_config", cordis)
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "request_timeout_seconds", timeout)
        object.__setattr__(self, "shutdown_timeout_seconds", shutdown_timeout)
        object.__setattr__(self, "max_concurrency", concurrency)
        object.__setattr__(self, "max_frame_bytes", max_frame)
        object.__setattr__(self, "max_stderr_bytes", max_stderr)
        object.__setattr__(self, "max_tokens", max_tokens)
        object.__setattr__(self, "runtime_version", version)

    @property
    def command_argv(self) -> tuple[str, ...] | None:
        """Return the complete immutable argv, or ``None`` when disabled."""

        if self.runtime_command is None:
            return None
        return (*self.runtime_command, *self.runtime_args)

    @property
    def request_timeout(self) -> float:
        """Compatibility alias for callers that omit the ``_seconds`` suffix."""

        return float(self.request_timeout_seconds)

    @property
    def shutdown_timeout(self) -> float:
        """Compatibility alias for callers that omit the ``_seconds`` suffix."""

        return float(self.shutdown_timeout_seconds)

    def is_plugin_allowed(self, plugin_name: str) -> bool:
        """Return whether a plugin is explicitly present in the allowlist."""

        return isinstance(plugin_name, str) and plugin_name in self.plugin_allowlist

    def validate_cordis_config(self) -> Path | None:
        """Validate an explicit Cordis JSON/YAML config before launching DSH.

        The official JSON-RPC server plugin is part of the host/runtime
        contract.  A custom config that removes it, or enables a plugin not
        explicitly allowlisted, is rejected before a subprocess is started.
        """
        if self.cordis_config is None:
            return None

        config_path = Path(self.cordis_config)
        is_bundled_config = config_path == DEFAULT_CORDIS_CONFIG.resolve()
        if self.workspace_root is not None and not is_bundled_config:
            config_path = self.resolve_workspace_path(config_path, must_exist=True)
        elif not config_path.is_file():
            raise _invalid("cordis_config", "must point to an existing file")

        try:
            raw = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _invalid("cordis_config", f"cannot be read: {exc}") from exc
        if len(raw.encode("utf-8")) > self.max_frame_bytes:
            raise _invalid("cordis_config", "exceeds max_frame_bytes")
        try:
            document = _load_cordis_document(raw, config_path)
        except (ValueError, RecursionError) as exc:
            raise _invalid("cordis_config", "must contain valid JSON or YAML") from exc

        configured_plugins = _extract_plugin_names(document)
        if REQUIRED_CORDIS_PLUGIN not in configured_plugins:
            raise _invalid(
                "cordis_config",
                f"must retain {REQUIRED_CORDIS_PLUGIN}",
            )
        allowed = set(self.plugin_allowlist)
        if REQUIRED_CORDIS_PLUGIN not in allowed:
            raise _invalid(
                "plugin_allowlist",
                f"must include {REQUIRED_CORDIS_PLUGIN}",
            )
        denied = sorted(configured_plugins - allowed)
        if denied:
            raise _invalid(
                "plugin_allowlist",
                "does not allow configured plugin(s): " + ", ".join(denied),
            )
        return config_path

    def resolve_workspace_path(
        self,
        candidate: str | Path,
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve a path and enforce the configured workspace boundary.

        Existing symlinks are resolved before the containment check, so a
        symlink cannot escape ``workspace_root`` unnoticed.
        """

        candidate_value = candidate
        if self.workspace_root is not None and not Path(candidate).is_absolute():
            candidate_value = self.workspace_root / Path(candidate)
        path = _normalize_path(
            candidate_value,
            "workspace_path",
            allow_directory=True,
        )
        assert path is not None
        if must_exist and not path.exists():
            raise _invalid("workspace_path", "must point to an existing path")
        if self.workspace_root is not None:
            try:
                path.relative_to(self.workspace_root)
            except ValueError as exc:
                raise _invalid(
                    "workspace_path", "must stay inside workspace_root"
                ) from exc
        return path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HarnessConfig:
        """Build a validated config from ``DSH_*`` variables.

        ``env`` is injectable for tests; omitting it reads a snapshot of the
        process environment.  Empty optional values leave conservative
        defaults in place.
        """

        values = _load_environment() if env is None else env
        enabled_raw = _env_value(values, "DSH_ENABLED")
        enabled = (
            False if enabled_raw is None else _parse_bool(enabled_raw, "DSH_ENABLED")
        )
        request_timeout = _env_float(
            values,
            "DSH_REQUEST_TIMEOUT",
            _env_float(
                values, "DSH_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
        )
        shutdown_timeout = _env_float(
            values,
            "DSH_SHUTDOWN_TIMEOUT",
            _env_float(
                values,
                "DSH_SHUTDOWN_TIMEOUT_SECONDS",
                DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
            ),
        )
        runtime_version = _env_value(values, "DSH_RUNTIME_VERSION") or None
        cordis_config = _env_value(values, "DSH_CORDIS_CONFIG") or None
        if enabled and cordis_config is None:
            cordis_config = str(DEFAULT_CORDIS_CONFIG)
        workspace_root = _env_value(values, "DSH_WORKSPACE_ROOT") or None

        runtime_command = _env_argv(values, "DSH_RUNTIME_COMMAND")
        if enabled and runtime_command is None:
            runtime_command = DEFAULT_RUNTIME_COMMAND
        plugin_allowlist = _env_allowlist(values, "DSH_PLUGIN_ALLOWLIST")
        if enabled and not plugin_allowlist:
            plugin_allowlist = DEFAULT_CORDIS_PLUGINS

        return cls(
            enabled=enabled,
            runtime_command=runtime_command,
            runtime_args=_env_argv(values, "DSH_RUNTIME_ARGS") or (),
            cordis_config=cordis_config,
            plugin_allowlist=plugin_allowlist,
            workspace_root=workspace_root,
            request_timeout_seconds=request_timeout,
            shutdown_timeout_seconds=shutdown_timeout,
            max_concurrency=_env_int(
                values, "DSH_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY
            ),
            max_frame_bytes=_env_int(
                values, "DSH_MAX_FRAME_BYTES", DEFAULT_MAX_FRAME_BYTES
            ),
            max_stderr_bytes=_env_int(
                values, "DSH_MAX_STDERR_BYTES", DEFAULT_MAX_STDERR_BYTES
            ),
            max_tokens=_env_int(values, "DSH_MAX_TOKENS", DEFAULT_MAX_TOKENS),
            runtime_version=runtime_version,
        )


__all__ = [
    "DEFAULT_CORDIS_CONFIG",
    "DEFAULT_CORDIS_PLUGINS",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_MAX_STDERR_BYTES",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_RUNTIME_COMMAND",
    "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
    "MAX_MAX_CONCURRENCY",
    "MAX_MAX_FRAME_BYTES",
    "MAX_MAX_STDERR_BYTES",
    "MAX_MAX_TOKENS",
    "REQUIRED_CORDIS_PLUGIN",
    "HarnessConfig",
    "HarnessConfigError",
]


def _load_environment() -> dict[str, str]:
    """Merge dotenv files with process variables for standalone config use."""
    merged: dict[str, str] = {}
    files = [Path.home() / ".config" / "free-claude-code" / ".env", Path(".env")]
    if explicit := os.environ.get("FCC_ENV_FILE"):
        files.append(Path(explicit))
    for path in files:
        if not path.is_file():
            continue
        try:
            merged.update(
                {
                    key: value
                    for key, value in dotenv_values(path).items()
                    if key and value is not None
                }
            )
        except OSError:
            continue
    merged.update(os.environ)
    return merged


def _extract_plugin_names(document: object) -> set[str]:
    """Extract plugin ids from common Cordis JSON config layouts."""
    names: set[str] = set()

    def collect(value: object, *, plugin_context: bool = False) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                if not isinstance(raw_key, str):
                    continue
                key = raw_key.strip()
                normalized = key.lower()
                if normalized in _PLUGIN_CONTAINER_KEYS:
                    collect(item, plugin_context=True)
                    continue
                if plugin_context and normalized in _PLUGIN_ID_KEYS:
                    if isinstance(item, str) and item.strip():
                        candidate = item.strip()
                        # Cordis ``id`` values are local labels (for example
                        # ``agent-core``); only package-like values identify a
                        # plugin.  ``name`` is the package field in the
                        # official config.
                        if normalized != "id" or _looks_like_plugin_id(candidate):
                            names.add(candidate)
                    continue
                if plugin_context and _looks_like_plugin_id(key):
                    names.add(key)
                    collect(item)
                    continue
                if isinstance(item, (Mapping, list)):
                    collect(item, plugin_context=plugin_context)
        elif isinstance(value, list):
            for item in value:
                if plugin_context and isinstance(item, str) and item.strip():
                    names.add(item.strip())
                elif isinstance(item, (Mapping, list)):
                    # The official YAML uses a top-level list of plugin
                    # objects (each with a ``name`` field), while JSON configs
                    # commonly use a ``plugins`` container.  Treat list items
                    # as plugin records only when already in plugin context.
                    collect(item, plugin_context=plugin_context)

    collect(document, plugin_context=isinstance(document, list))
    # Keep the required package detectable even when a config uses a compact
    # object form that does not expose a conventional "plugins" container.
    if _contains_string(document, REQUIRED_CORDIS_PLUGIN):
        names.add(REQUIRED_CORDIS_PLUGIN)
    return names


def _contains_string(value: object, target: str) -> bool:
    if isinstance(value, str):
        return value == target
    if isinstance(value, Mapping):
        return any(
            _contains_string(key, target) or _contains_string(item, target)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_string(item, target) for item in value)
    return False


def _looks_like_plugin_id(value: str) -> bool:
    return value.startswith("@") or "/" in value


def _load_cordis_document(raw: str, path: Path) -> object:
    """Parse JSON or Cordis YAML while treating Cordis JS tags as scalars."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(raw)

    try:
        import yaml
        from yaml.constructor import ConstructorError
    except ImportError as exc:
        raise ValueError("PyYAML is required for Cordis YAML configs") from exc

    class _CordisLoader(yaml.SafeLoader):
        pass

    def construct_unknown(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        raise ConstructorError(
            None, None, "unsupported Cordis YAML node", node.start_mark
        )

    _CordisLoader.add_constructor(None, construct_unknown)
    try:
        return yaml.load(raw, Loader=_CordisLoader)
    except yaml.YAMLError as exc:
        raise ValueError("invalid Cordis YAML") from exc
