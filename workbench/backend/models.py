"""数据模型定义"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AgentType(StrEnum):
    """Agent类型"""

    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    DEEPSEEK_HARNESS = "deepseek_harness"
    OPENHANDS = "openhands"
    CUSTOM = "custom"


class AgentStatus(StrEnum):
    """Agent状态"""

    OFFLINE = "offline"
    ONLINE = "online"
    BUSY = "busy"
    ERROR = "error"


class TaskStatus(StrEnum):
    """任务状态"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    """运行状态"""

    STARTED = "started"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    """事件类型"""

    RUN_STARTED = "run_started"
    AGENT_MESSAGE = "agent_message"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    FILE_CHANGED = "file_changed"
    TERMINAL_OUTPUT = "terminal_output"
    TASK_UPDATED = "task_updated"
    USER_INPUT_REQUIRED = "user_input_required"
    RUN_FAILED = "run_failed"
    RUN_FINISHED = "run_finished"
    RUN_PAUSED = "run_paused"
    RUN_CANCELLED = "run_cancelled"


class Agent(BaseModel):
    """Agent模型"""

    id: str
    name: str
    type: AgentType
    status: AgentStatus = AgentStatus.OFFLINE
    version: str | None = None
    capabilities: list[str] = []
    current_task_id: str | None = None
    workspace_path: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    last_active: datetime = Field(default_factory=datetime.now)


class Task(BaseModel):
    """任务模型"""

    id: str
    title: str
    description: str
    agent_id: str | None = None
    agent_type: AgentType | None = None
    status: TaskStatus = TaskStatus.PENDING
    workspace_path: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    runs: list[str] = []  # Run IDs
    metadata: dict[str, Any] = {}


class Run(BaseModel):
    """运行实例模型"""

    id: str
    task_id: str
    agent_id: str
    status: RunStatus = RunStatus.STARTED
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    events: list[str] = []  # Event IDs
    output: str | None = None
    error: str | None = None
    tokens_used: int = 0
    cost: float = 0.0
    metadata: dict[str, Any] = {}


class Event(BaseModel):
    """事件模型"""

    id: str
    run_id: str
    type: EventType
    timestamp: datetime = Field(default_factory=datetime.now)
    data: dict[str, Any] = {}
    message: str | None = None


class ToolCall(BaseModel):
    """工具调用记录"""

    id: str
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    result: Any | None = None
    error: str | None = None


class FileChange(BaseModel):
    """文件变化记录"""

    id: str
    run_id: str
    file_path: str
    change_type: str  # created, modified, deleted
    timestamp: datetime = Field(default_factory=datetime.now)
    diff: str | None = None


class TerminalOutput(BaseModel):
    """终端输出记录"""

    id: str
    run_id: str
    command: str
    output: str
    exit_code: int | None = None
    timestamp: datetime = Field(default_factory=datetime.now)


class CreateTaskRequest(BaseModel):
    """创建任务请求"""

    title: str
    description: str
    agent_type: AgentType
    workspace_path: str | None = None


class SendMessageRequest(BaseModel):
    """发送消息请求"""

    run_id: str
    message: str


class ControlRequest(BaseModel):
    """控制请求"""

    run_id: str
    action: str  # pause, resume, cancel, retry
