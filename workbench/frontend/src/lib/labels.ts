/**
 * 状态 -> Tailwind 语义 token 类的映射（P0 起消费 state-* 语义层，
 * 颜色值定义在 index.css :root 的 --c-state-*，见 src/lib/tokens.ts）。
 * 配色刻意克制：灰=空闲，蓝=进行中，绿=成功，琥珀=等待/返工，红=失败，青=强调。
 */

export type Tone = 'neutral' | 'idle' | 'active' | 'success' | 'warn' | 'danger' | 'accent'

export const TONE_CLASS: Record<Tone, string> = {
  neutral:
    'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset bg-state-neutral text-state-neutral-fg ring-state-neutral-ring',
  idle: 'bg-state-idle/80 text-state-idle-fg ring-state-idle-ring',
  active: 'bg-state-active/15 text-state-active-fg ring-state-active/30',
  success: 'bg-state-success/15 text-state-success-fg ring-state-success/30',
  warn: 'bg-state-warn/15 text-state-warn-fg ring-state-warn/30',
  danger: 'bg-state-danger/15 text-state-danger-fg ring-state-danger/30',
  accent: 'bg-state-accent/15 text-state-accent-fg ring-state-accent/30',
}

/** 状态点（圆点）配色，用于时间线 / 表格行首。 */
export const TONE_DOT: Record<Tone, string> = {
  neutral: 'bg-state-neutral-dot',
  idle: 'bg-state-idle-dot',
  active: 'bg-state-active-dot',
  success: 'bg-state-success-dot',
  warn: 'bg-state-warn-dot',
  danger: 'bg-state-danger-dot',
  accent: 'bg-state-accent-dot',
}

const STEP_TONES: Record<string, Tone> = {
  pending: 'idle',
  ready: 'accent',
  running: 'active',
  validating: 'active',
  waiting_review: 'warn',
  completed: 'success',
  rejected: 'danger',
  rework: 'warn',
  blocked: 'danger',
  failed: 'danger',
  paused: 'warn',
}

const SOP_TASK_TONES: Record<string, Tone> = {
  pending: 'idle',
  assigned: 'accent',
  running: 'active',
  validating: 'active',
  accepted: 'success',
  rejected: 'danger',
  rework: 'warn',
  failed: 'danger',
}

const SOP_RUN_TONES: Record<string, Tone> = {
  created: 'idle',
  running: 'active',
  paused: 'warn',
  waiting_review: 'warn',
  completed: 'success',
  failed: 'danger',
  cancelled: 'idle',
}

const LEGACY_STATUS_TONES: Record<string, Tone> = {
  started: 'accent',
  running: 'active',
  waiting_human: 'warn',
  paused: 'warn',
  completed: 'success',
  failed: 'danger',
  cancelled: 'idle',
  pending: 'idle',
}

const AGENT_TONES: Record<string, Tone> = {
  offline: 'idle',
  online: 'success',
  busy: 'active',
  error: 'danger',
}

const ARTIFACT_TONES: Record<string, Tone> = {
  text: 'neutral',
  plan: 'accent',
  architecture_spec: 'accent',
  research_report: 'neutral',
  implementation: 'active',
  test_report: 'warn',
  review_report: 'warn',
  final_summary: 'success',
  file: 'neutral',
  diff: 'active',
  json: 'neutral',
}

const HANDOFF_TONES: Record<string, Tone> = {
  draft: 'idle',
  ready: 'accent',
  accepted: 'success',
  rejected: 'danger',
}

function lookup(map: Record<string, Tone>, value: string | null | undefined): Tone {
  if (!value) return 'idle'
  return map[value] ?? 'neutral'
}

export const stepTone = (v: string | null | undefined) => lookup(STEP_TONES, v)
export const sopTaskTone = (v: string | null | undefined) => lookup(SOP_TASK_TONES, v)
export const sopRunTone = (v: string | null | undefined) => lookup(SOP_RUN_TONES, v)
export const legacyStatusTone = (v: string | null | undefined) => lookup(LEGACY_STATUS_TONES, v)
export const agentTone = (v: string | null | undefined) => lookup(AGENT_TONES, v)
export const artifactTone = (v: string | null | undefined) => lookup(ARTIFACT_TONES, v)
export const handoffTone = (v: string | null | undefined) => lookup(HANDOFF_TONES, v)

/** SOP 引擎事件类型（.workbench/sop/workflow_events.jsonl 中实测出现过）。 */
const EVENT_TONES: Record<string, Tone> = {
  sop_started: 'accent',
  sop_completed: 'success',
  sop_failed: 'danger',
  sop_waiting_review: 'warn',
  step_started: 'active',
  step_ready: 'accent',
  step_completed: 'success',
  step_failed: 'danger',
  step_blocked: 'danger',
  task_created: 'neutral',
  task_started: 'active',
  task_accepted: 'success',
  task_rejected: 'danger',
  task_failed: 'danger',
  task_rework: 'warn',
  artifact_created: 'accent',
  validation_completed: 'warn',
  handoff_created: 'accent',
  handoff_accepted: 'success',
  handoff_rejected: 'danger',
}

export const eventTone = (v: string | null | undefined) => lookup(EVENT_TONES, v)

/**
 * Handoff 语义标签。
 * P0 契约核对（2026-09-03）：HandoffResponse 现已由后端返回 message_type
 * （types/sop.ts），本图例与 infer 仅作为旧记录/事件缺失时的兜底标注，
 * 不作为唯一来源。
 */
export const HANDOFF_SEMANTIC_TYPES = ['plan_ready', 'review_request', 'rework', 'pass'] as const

export type HandoffSemanticType = (typeof HANDOFF_SEMANTIC_TYPES)[number]

export const HANDOFF_SEMANTIC_LABELS: Record<HandoffSemanticType, string> = {
  plan_ready: 'PLAN_READY 方案就绪',
  review_request: 'REVIEW_REQUEST 请求审核',
  rework: 'REWORK 返工',
  pass: 'PASS 通过',
}

/**
 * 从与某个 handoff 关联的事件旁证推断其语义类型。
 * 命中不到就返回 null——宁可显示「未知」，也不臆造。
 */
export function inferHandoffSemantic(eventTypes: string[]): HandoffSemanticType | null {
  const joined = eventTypes.join('|').toLowerCase()
  if (joined.includes('rework')) return 'rework'
  if (joined.includes('review_request') || joined.includes('waiting_review') || joined.includes('review'))
    return 'review_request'
  if (joined.includes('plan_ready') || joined.includes('plan')) return 'plan_ready'
  if (joined.includes('pass') || joined.includes('accepted') || joined.includes('completed'))
    return 'pass'
  return null
}

/**
 * role_id -> 显示名。冻结的 SOP 角色是 Claude（设计者）、DSH（主执行者）、
 * Codex（审核者）；其余回退成原始 role id。
 */
export function roleLabel(roleId: string | null | undefined): string {
  if (!roleId) return '未分配'
  const value = roleId.toLowerCase()
  if (value.includes('claude')) return 'Claude · 方案设计'
  if (value.includes('codex')) return 'Codex · 最终审核'
  if (value.includes('dsh') || value.includes('deepseek') || value.includes('harness'))
    return 'DSH · 主执行'
  if (value.includes('review')) return '审核者'
  if (value.includes('test') || value.includes('tester')) return '执行者'
  return roleId
}

/** 横向流水线里的短徽标。 */
export function roleShort(roleId: string | null | undefined): string {
  if (!roleId) return '—'
  const value = roleId.toLowerCase()
  if (value.includes('claude')) return 'Claude'
  if (value.includes('codex')) return 'Codex'
  if (value.includes('dsh') || value.includes('deepseek') || value.includes('harness'))
    return 'DSH'
  return roleId
}

export function runtimeLabel(kind: string | null | undefined): string {
  switch (kind) {
    case 'claude_code':
      return 'Claude Code'
    case 'codex':
      return 'Codex'
    case 'deepseek_harness':
      return 'DeepSeek Harness'
    case 'openhands':
      return 'OpenHands'
    case null:
    case undefined:
      return '未知'
    default:
      return kind
  }
}
