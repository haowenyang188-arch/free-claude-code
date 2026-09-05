from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

SCRAPER_ROOT = Path(
    os.environ.get("RUNNER_SCRAPER_ROOT", "/home/gnen/scraper")
).expanduser()
CHROME_SCRIPT_DIR = os.environ.get(
    "RUNNER_CHROME_SCRIPT_DIR", "/mnt/c/Users/gnen0/Desktop/抓取工具"
)
LEDGER_PATH = Path(
    os.environ.get("RUNNER_LEDGER_PATH", str(Path.cwd() / ".runner" / "ledger.json"))
).expanduser()
MAX_LIMIT = 100
MAX_LOG_LINES = 4000
SMOKE_KEYWORD = "AI写真"
ALLOWED_START_FIELDS = frozenset(
    {"channel", "boot", "limit", "restart", "smoke", "kill_chrome"}
)
ALLOWED_STOP_FIELDS = frozenset({"kill_chrome"})
OUTPUT_RE = re.compile(r"(?:输出|output)\s*[:：]\s*(\S+\.json)", re.IGNORECASE)


@dataclass(frozen=True)
class ChannelSpec:
    channel: str
    cdp_port: int
    chrome_script: str


CHANNELS: dict[str, ChannelSpec] = {
    "xhs": ChannelSpec("xhs", 9222, "start_xhs_9222.ps1"),
    "douyin": ChannelSpec("douyin", 9224, "start_douyin_9224.ps1"),
}


@dataclass(frozen=True)
class StartRequest:
    channel: str
    boot: bool = True
    limit: int = 20
    restart: bool = False
    smoke: bool = False
    kill_chrome: bool = False


class RunnerError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class UnsupportedFieldError(ValueError):
    def __init__(self, fields: list[str]):
        self.fields = sorted(fields)
        super().__init__(f"unsupported field(s): {', '.join(self.fields)}")


def validate_start_payload(payload: object) -> StartRequest:
    if not isinstance(payload, Mapping):
        raise ValueError("request body must be a JSON object")
    if any(not isinstance(key, str) for key in payload):
        raise ValueError("request body keys must be strings")
    payload = cast(Mapping[str, Any], payload)
    extra = set(payload) - ALLOWED_START_FIELDS
    if extra:
        raise UnsupportedFieldError(list(extra))

    channel = payload.get("channel")
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise ValueError("channel must be xhs or douyin")

    limit = payload.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")

    values: dict[str, Any] = {
        "boot": payload.get("boot", True),
        "restart": payload.get("restart", False),
        "smoke": payload.get("smoke", False),
        "kill_chrome": payload.get("kill_chrome", False),
    }
    if any(not isinstance(value, bool) for value in values.values()):
        raise ValueError("boot, restart, smoke and kill_chrome must be booleans")
    return StartRequest(
        channel=channel,
        boot=values["boot"],
        limit=min(limit, MAX_LIMIT),
        restart=values["restart"],
        smoke=values["smoke"],
        kill_chrome=values["kill_chrome"],
    )


def build_scraper_argv(
    channel: str,
    *,
    limit: int,
    restart: bool,
    smoke: bool,
    scraper_root: Path = SCRAPER_ROOT,
) -> list[str]:
    if channel not in CHANNELS:
        raise ValueError("unsupported channel")
    argv = [
        "python3",
        str(scraper_root / "main.py"),
        "--mode",
        "search",
        "--platform",
        channel,
        "--port",
        str(CHANNELS[channel].cdp_port),
        "--keyword-group",
        "kaka_camera",
        "--limit",
        str(min(max(limit, 1), MAX_LIMIT)),
        "--slow-mode",
        "false" if smoke else "true",
    ]
    if smoke:
        argv.extend(["--keyword", SMOKE_KEYWORD])
        argv.append("--smoke-test")
    if restart:
        argv.append("--restart")
    return argv


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(url: str, timeout: float = 1.5) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError:
        return None


def cdp_healthy(port: int) -> bool:
    version = _read_json(f"http://127.0.0.1:{port}/json/version")
    if not isinstance(version, Mapping):
        return False
    browser = version.get("Browser")
    if not isinstance(browser, str) or not browser.startswith("Chrome/"):
        return False
    if not version.get("webSocketDebuggerUrl"):
        return False
    targets = _read_json(f"http://127.0.0.1:{port}/json")
    return isinstance(targets, list) and any(
        isinstance(target, Mapping)
        and target.get("type") == "page"
        and target.get("webSocketDebuggerUrl")
        for target in targets
    )


async def _run_process(argv: list[str], *, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RunnerError(
            "CHROME_BOOT_TIMEOUT", "Chrome 调试端口启动超时", 504
        ) from exc
    code = process.returncode if process.returncode is not None else 0
    return code, stdout.decode("utf-8", errors="replace")


@dataclass
class RunRecord:
    run_id: str
    channel: str
    argv: list[str]
    limit: int
    restart: bool
    smoke: bool
    kill_chrome: bool
    status: str = "starting"
    pid: int | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)
    output_files: list[str] = field(default_factory=list)
    registered: bool = False
    imported: bool = False
    chrome_booted: bool = False
    chrome_cleanup_error: str | None = None
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    stop_requested: bool = field(default=False, repr=False)
    done_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "channel": self.channel,
            "status": self.status,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "argv": self.argv,
            "output_files": self.output_files,
            "registered": self.registered,
            "imported": self.imported,
            "chrome_booted": self.chrome_booted,
            "chrome_cleanup_error": self.chrome_cleanup_error,
        }


class RunnerManager:
    def __init__(
        self,
        *,
        scraper_root: Path = SCRAPER_ROOT,
        ledger_path: Path = LEDGER_PATH,
    ) -> None:
        self.scraper_root = scraper_root
        self.ledger_path = ledger_path
        self.runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()
        self._watchers: set[asyncio.Task[Any]] = set()

    async def start(self, request: StartRequest) -> dict[str, Any]:
        # ponytail: one global lock serializes startup; per-channel locks only matter if parallel starts become a requirement.
        async with self._lock:
            if any(
                item.channel == request.channel
                and item.status in {"starting", "running"}
                for item in self.runs.values()
            ):
                raise RunnerError(
                    "RUN_ALREADY_ACTIVE", "该 channel 已有运行中的采集", 409
                )
            main_file = self.scraper_root / "main.py"
            if not main_file.is_file():
                raise RunnerError(
                    "SCRAPER_NOT_FOUND", f"采集入口不存在: {main_file}", 503
                )
            spec = CHANNELS[request.channel]
            chrome_booted = False
            if not await asyncio.to_thread(cdp_healthy, spec.cdp_port):
                if not request.boot:
                    raise RunnerError(
                        "CDP_UNAVAILABLE", f"Chrome CDP {spec.cdp_port} 未就绪", 503
                    )
                await self._boot_chrome(spec)
                chrome_booted = True
            elif request.boot:
                # Existing healthy Chrome is reused; do not restart a user's session.
                pass

            argv = build_scraper_argv(
                request.channel,
                limit=request.limit,
                restart=request.restart,
                smoke=request.smoke,
                scraper_root=self.scraper_root,
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(self.scraper_root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RunnerError("PYTHON_NOT_FOUND", "找不到 python3", 503) from exc
            except OSError as exc:
                raise RunnerError(
                    "SCRAPER_START_FAILED", "采集进程启动失败", 503
                ) from exc

            record = RunRecord(
                run_id=uuid.uuid4().hex[:8],
                channel=request.channel,
                argv=argv,
                limit=request.limit,
                restart=request.restart,
                smoke=request.smoke,
                kill_chrome=request.kill_chrome,
                status="running",
                pid=process.pid,
                process=process,
                chrome_booted=chrome_booted,
            )
            self.runs[record.run_id] = record
            watcher = asyncio.create_task(self._watch(record))
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)
            return record.public()

    async def _boot_chrome(self, spec: ChannelSpec) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        script = str(Path(CHROME_SCRIPT_DIR) / spec.chrome_script)
        if not powershell or not Path(script).is_file():
            raise RunnerError(
                "CHROME_BOOT_UNAVAILABLE",
                "Chrome 启动脚本不可用；请先启动受控 Chrome 调试 Profile",
                503,
            )
        code, _output = await _run_process(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
            ],
            timeout=60,
        )
        if code != 0:
            raise RunnerError("CHROME_BOOT_FAILED", "Chrome 调试端口启动失败", 503)
        for _ in range(45):
            if await asyncio.to_thread(cdp_healthy, spec.cdp_port):
                return
            await asyncio.sleep(1)
        raise RunnerError("CDP_BOOT_TIMEOUT", "Chrome 调试端口未在期限内就绪", 504)

    async def _watch(self, record: RunRecord) -> None:
        process = record.process
        if process is None or process.stdout is None:
            record.status = "failed"
            record.exit_code = record.exit_code if record.exit_code is not None else 1
            record.finished_at = record.finished_at or _now()
            record.process = None
            record.done_event.set()
            return
        try:
            async for raw_line in process.stdout:
                if isinstance(raw_line, bytes):
                    line = raw_line.decode("utf-8", errors="replace")
                else:
                    line = str(raw_line)
                line = line.rstrip("\r\n")
                record.lines.append(line)
                if len(record.lines) > MAX_LOG_LINES:
                    del record.lines[: len(record.lines) - MAX_LOG_LINES]
                match = OUTPUT_RE.search(line)
                if match and match.group(1) not in record.output_files:
                    record.output_files.append(match.group(1))
            code = await process.wait()
            record.exit_code = code
            record.finished_at = _now()
            record.status = (
                "stopped"
                if record.stop_requested
                else ("completed" if code == 0 else "failed")
            )
            record.process = None
            if record.kill_chrome and record.chrome_booted:
                try:
                    await self._cleanup_chrome(record)
                except Exception as exc:
                    record.chrome_cleanup_error = str(exc) or "Chrome 进程清理失败"
        except asyncio.CancelledError:
            raise
        except Exception:
            record.status = "failed"
            record.exit_code = record.exit_code if record.exit_code is not None else 1
            record.finished_at = record.finished_at or _now()
            record.process = None
        finally:
            record.done_event.set()

    def get(self, run_id: str) -> RunRecord:
        record = self.runs.get(run_id)
        if record is None:
            raise RunnerError("RUN_NOT_FOUND", "run 不存在", 404)
        return record

    def logs(self, run_id: str, after: int = 0) -> dict[str, Any]:
        record = self.get(run_id)
        start = max(after, 0)
        return {
            "run_id": run_id,
            "lines": record.lines[start:],
            "next": len(record.lines),
            "done": record.status in {"completed", "failed", "stopped"},
        }

    async def stop(
        self, run_id: str, *, kill_chrome: bool | None = None
    ) -> dict[str, Any]:
        record = self.get(run_id)
        if kill_chrome is not None:
            record.kill_chrome = kill_chrome
        process = record.process
        if process is None or record.status in {"completed", "failed", "stopped"}:
            if record.kill_chrome and record.chrome_booted:
                try:
                    await self._cleanup_chrome(record)
                except Exception as exc:
                    record.chrome_cleanup_error = str(exc) or "Chrome 进程清理失败"
            return record.public()

        record.stop_requested = True
        self._signal_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
            await process.wait()
        try:
            await asyncio.wait_for(record.done_event.wait(), timeout=5)
        except TimeoutError:
            record.status = "stopped"
            record.finished_at = record.finished_at or _now()
            record.process = None
        return record.public()

    @staticmethod
    def _signal_process_group(process: Any, sig: signal.Signals) -> None:
        """Terminate only the process group created for this run."""
        pid = getattr(process, "pid", None)
        try:
            if pid is None:
                raise OSError("process has no pid")
            os.killpg(os.getpgid(pid), sig)
            return
        except AttributeError, OSError, ProcessLookupError:
            method_name = "terminate" if sig == signal.SIGTERM else "kill"
            method = getattr(process, method_name, None)
            if callable(method):
                with suppress(OSError):
                    method()

    async def _cleanup_chrome(self, record: RunRecord) -> None:
        if not record.chrome_booted:
            return
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
        if not powershell or not taskkill:
            record.chrome_cleanup_error = "Windows 进程清理工具不可用"
            return
        port = CHANNELS[record.channel].cdp_port
        command = (
            f"$port={port}; Get-NetTCPConnection -LocalAddress 127.0.0.1 "
            "-LocalPort $port -State Listen -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty OwningProcess -Unique "
            "| ForEach-Object { $p=Get-CimInstance Win32_Process "
            '-Filter "ProcessId = $_" -ErrorAction SilentlyContinue; '
            "if ($p.Name -ieq 'chrome.exe' -and "
            "$p.CommandLine -match ('--remote-debugging-port=' + $port)) { $_ } }"
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [powershell, "-NoLogo", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            pids = [
                line.strip() for line in stdout.splitlines() if line.strip().isdigit()
            ]
            for pid in pids:
                await asyncio.to_thread(
                    subprocess.run,
                    [taskkill, "/PID", pid, "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    shell=False,
                )
            if result.returncode not in {0, 1}:  # 1 means no matching listener.
                record.chrome_cleanup_error = "Chrome 进程清理失败"
        except OSError, subprocess.SubprocessError:
            record.chrome_cleanup_error = "Chrome 进程清理失败"

    def _output_file(self, record: RunRecord) -> Path | None:
        candidates = [Path(item) for item in record.output_files]
        data_root = (self.scraper_root / "data").resolve()
        for candidate in candidates:
            resolved = (
                candidate if candidate.is_absolute() else self.scraper_root / candidate
            )
            try:
                if resolved.resolve().is_relative_to(data_root) and resolved.is_file():
                    return resolved.resolve()
            except OSError, ValueError:
                continue
        prefix = f"search_{record.channel}_"
        files = sorted(
            data_root.glob(f"{prefix}*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        try:
            started_at = datetime.fromisoformat(record.started_at).timestamp()
        except ValueError:
            started_at = 0
        for item in files:
            try:
                if item.stat().st_mtime >= started_at - 1:
                    return item.resolve()
            except OSError:
                continue
        return None

    def _read_ledger(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except FileNotFoundError, json.JSONDecodeError, OSError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _write_ledger(self, entries: list[dict[str, Any]]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix="ledger.", suffix=".tmp", dir=self.ledger_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.ledger_path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp_name)

    async def register(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        if record.status not in {"completed", "failed", "stopped"}:
            raise RunnerError("RUN_NOT_FINISHED", "运行尚未结束，不能登记", 409)
        entries = [item for item in self._read_ledger() if item.get("run_id") != run_id]
        record.registered = True
        entries.append({**record.public(), "registered_at": _now()})
        self._write_ledger(entries)
        return record.public()

    async def import_output(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        if record.status not in {"completed", "failed", "stopped"}:
            raise RunnerError("RUN_NOT_FINISHED", "运行尚未结束，不能导入", 409)
        output = self._output_file(record)
        if output is None:
            raise RunnerError("OUTPUT_NOT_FOUND", "没有找到该运行的 JSON 产出", 404)
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerError("OUTPUT_INVALID", "产出文件不是有效 JSON", 422) from exc
        if not isinstance(payload, Mapping):
            raise RunnerError("OUTPUT_INVALID", "产出文件结构无效", 422)
        records = payload.get("data")
        if isinstance(records, list):
            count = len(records)
        else:
            try:
                count = int(payload.get("total", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise RunnerError("OUTPUT_INVALID", "产出记录数无效", 422) from exc
        try:
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
        except OSError as exc:
            raise RunnerError("OUTPUT_INVALID", "读取产出文件失败", 422) from exc
        record.output_files = [str(output)]
        record.imported = True
        entries = [item for item in self._read_ledger() if item.get("run_id") != run_id]
        entries.append(
            {
                **record.public(),
                "imported_at": _now(),
                "record_count": count,
                "sha256": digest,
            }
        )
        self._write_ledger(entries)
        return {
            **record.public(),
            "output_file": str(output),
            "record_count": count,
            "sha256": digest,
        }

    async def shutdown(self) -> None:
        active = [
            item.run_id for item in self.runs.values() if item.process is not None
        ]
        for run_id in active:
            await self.stop(run_id)
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)
