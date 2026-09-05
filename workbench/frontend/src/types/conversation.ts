/**
 * Conversation（Agent Canvas 会话）域契约 —— **数据源为 agent-server(18000)**，
 * 不是 8000。SOP Workbench 与 Canvas 的会话状态联动（run ↔ conversation）在
 * P1-A 由 8000 提供 `/canvas-api/*` 同源只读转发并注入 Session Key。
 *
 * P0 只做契约与 client 边界（不实现真实调用）。字段以 2026-09-03 实测为准
 * （GET /api/conversations/{id} 与 GET /api/conversations?ids=... 返回同构对象），
 * 完整 schema 见 agent-server /openapi.json 的 ConversationInfo；P1 可对照
 * @openhands/typescript-client 收窄。禁止为凑 UI 臆造本文件之外的字段。
 */

/** workspace.kind（实测 LocalWorkspace） */
export type ConversationWorkspaceKind = 'LocalWorkspace' | 'RemoteWorkspace' | string

export interface ConversationWorkspace {
  working_dir: string
  kind: ConversationWorkspaceKind
}

/**
 * 执行状态。实测出现过 "finished"；其余取值以 agent-server 枚举为准，
 * 保守声明为 string 联合（未来对照 openhands 类型收窄）。
 */
export type ConversationExecutionStatus =
  | 'starting'
  | 'running'
  | 'finished'
  | 'paused'
  | 'aborted'
  | 'error'
  | string

export interface ConversationTokenUsage {
  model?: string
  prompt_tokens?: number
  completion_tokens?: number
  cache_read_tokens?: number
  cache_write_tokens?: number
  reasoning_tokens?: number
  context_window?: number
  per_turn_token?: number
}

export interface ConversationCostEntry {
  model: string
  cost: number
  timestamp: number
}

/** stats.usage_to_metrics 下的单模型指标（key 通常为 "default"） */
export interface ConversationModelMetric {
  model_name: string
  accumulated_cost: number
  max_budget_per_task: number | null
  accumulated_token_usage?: ConversationTokenUsage
  costs?: ConversationCostEntry[]
  [k: string]: unknown
}

export interface ConversationStats {
  usage_to_metrics?: Record<string, ConversationModelMetric>
  [k: string]: unknown
}

/**
 * 会话对象（detail 与 list 同构，2026-09-03 实测）。
 * 字段只声明实证过的子集；顶层结构以 ConversationInfo 为准。
 */
export interface ConversationSession {
  id: string
  workspace: ConversationWorkspace
  persistence_dir: string
  max_iterations: number
  stuck_detection: boolean
  /** execution_status / cost / model 摘要由此取（会话状态卡消费面） */
  execution_status: ConversationExecutionStatus
  confirmation_policy?: { kind?: string; [k: string]: unknown }
  security_analyzer?: { kind?: string; [k: string]: unknown }
  activated_knowledge_skills?: string[]
  invoked_skills?: string[]
  blocked_actions?: Record<string, unknown>
  blocked_messages?: Record<string, unknown>
  last_user_message_id?: string | null
  leaf_event_id?: string | null
  stats?: ConversationStats
  /** ACP subprocess session id; used as the explicit SOP run correlation key. */
  agent_state?: {
    acp_session_id?: string | null
    [k: string]: unknown
  }
  /** 会话语义标题（有则优先于 id 展示；旧会话可能无） */
  title?: string | null
  [k: string]: unknown
}

export interface ConversationEvent {
  id: string
  timestamp: string
  source?: 'agent' | 'user' | 'environment' | 'hook' | string
  kind: string
  parent_id?: string | null
  llm_message?: {
    role?: string
    content?: Array<{ type?: string; text?: string }>
  }
  action?: {
    kind?: string
    message?: string
    [k: string]: unknown
  } | null
  tool_name?: string | null
  observation?: {
    content?: Array<{ type?: string; text?: string }>
    is_error?: boolean
    [k: string]: unknown
  } | null
  [k: string]: unknown
}

export interface ConversationEventPage {
  items: ConversationEvent[]
  next_page_id?: string | null
}

/**
 * 会话摘要的降级规则（供状态卡 / run 联动使用）：
 *  - model：stats.usage_to_metrics.default?.model_name
 *  - 累计成本：stats.usage_to_metrics.default?.accumulated_cost
 *  - 执行态：execution_status（UI 映射沿用运行时语义：running=active 等）
 * 缺失时 UI 显示「—」，不得猜测。
 */
export interface ConversationRunSummaryView {
  session: ConversationSession
  modelName: string | null
  accumulatedCost: number | null
  running: boolean
}

export function toConversationRunSummaryView(session: ConversationSession): ConversationRunSummaryView {
  const metric = session.stats?.usage_to_metrics?.default
  const modelName = metric?.model_name ?? null
  const accumulatedCost = typeof metric?.accumulated_cost === 'number' ? metric.accumulated_cost : null
  const running = session.execution_status === 'running'
  return { session, modelName, accumulatedCost, running }
}
