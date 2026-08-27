export enum AgentType {
  CODEX = 'codex',
  CLAUDE_CODE = 'claude_code',
  OPENHANDS = 'openhands',
  CUSTOM = 'custom',
}

export enum AgentStatus {
  OFFLINE = 'offline',
  ONLINE = 'online',
  BUSY = 'busy',
  ERROR = 'error',
}

export enum TaskStatus {
  PENDING = 'pending',
  RUNNING = 'running',
  WAITING_HUMAN = 'waiting_human',
  PAUSED = 'paused',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
}

export enum RunStatus {
  STARTED = 'started',
  RUNNING = 'running',
  WAITING_HUMAN = 'waiting_human',
  PAUSED = 'paused',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
}

export enum EventType {
  RUN_STARTED = 'run_started',
  AGENT_MESSAGE = 'agent_message',
  TOOL_STARTED = 'tool_started',
  TOOL_FINISHED = 'tool_finished',
  FILE_CHANGED = 'file_changed',
  TERMINAL_OUTPUT = 'terminal_output',
  TASK_UPDATED = 'task_updated',
  USER_INPUT_REQUIRED = 'user_input_required',
  RUN_FAILED = 'run_failed',
  RUN_FINISHED = 'run_finished',
  RUN_PAUSED = 'run_paused',
  RUN_CANCELLED = 'run_cancelled',
}

export interface Agent {
  id: string
  name: string
  type: AgentType
  status: AgentStatus
  version?: string
  capabilities?: string[]
  current_task_id?: string
  workspace_path?: string
  created_at: string
  last_active: string
}

export interface Task {
  id: string
  title: string
  description: string
  agent_id?: string
  agent_type?: AgentType
  status: TaskStatus
  workspace_path: string
  created_at: string
  updated_at: string
  completed_at?: string
  runs: string[]
  metadata: Record<string, any>
}

export interface Run {
  id: string
  task_id: string
  agent_id: string
  status: RunStatus
  started_at: string
  completed_at?: string
  events: string[]
  output?: string
  error?: string
  tokens_used: number
  cost: number
  metadata: Record<string, any>
}

export interface Event {
  id: string
  run_id: string
  type: EventType
  timestamp: string
  data: Record<string, any>
  message?: string
  sequence?: number
  backend?: string
  session_id?: string
}

export interface ApprovalIdentity {
  session_id: string
  call_id: string
  normalized_command: string
  argv: string[]
  cwd: string
  requested_permission: string
  command_hash: string
  risk: string
  status: string
}

export interface ApprovalRecord extends ApprovalIdentity {
  created_at: string
  expires_at: string
  approved_at?: string
  consumed_at?: string
  reason?: string
}

export interface CreateTaskRequest {
  title: string
  description: string
  agent_type: AgentType
  workspace_path?: string
}
