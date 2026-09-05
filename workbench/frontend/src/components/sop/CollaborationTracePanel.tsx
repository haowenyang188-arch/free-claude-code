import { Badge, Button, Empty, Panel } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { eventTone } from '../../lib/labels'
import { formatTime, shortId } from '../../lib/format'
import type {
  HandoffResponse,
  SopHandoffDispatchResponse,
  SopTraceEntryResponse,
  SopTraceResponse,
} from '../../types/sop'

export interface CollaborationTraceResource {
  data: SopTraceResponse | undefined
  error: unknown
  loading: boolean
}

export interface CollaborationTracePanelProps {
  trace: CollaborationTraceResource
  /** 可选：由父层从 bundle 传入；组件不会自行读取或 dispatch API。 */
  planHandoffs?: readonly HandoffResponse[]
  /** 未接入 Engine 时按钮保持 disabled，避免产生假执行。 */
  onDispatchPlan?: (handoff: HandoffResponse) => void
  dispatchingHandoffId?: string | null
  dispatchResult?: SopHandoffDispatchResponse | null
  dispatchError?: unknown
}

const PHASES = [
  { id: 'plan', role: 'claude', label: 'Claude · 方案设计' },
  { id: 'execute', role: 'dsh', label: 'DSH · 主执行' },
  { id: 'review', role: 'codex', label: 'Codex · 最终审核' },
] as const

function traceTone(status: string, eventType: string) {
  if (status === 'running' || status === 'validating') return 'active' as const
  if (status === 'completed' || status === 'accepted' || status === 'approved') return 'success' as const
  if (status === 'failed' || status === 'rejected') return 'danger' as const
  if (status === 'waiting_review' || status === 'rework' || status === 'paused') return 'warn' as const
  return eventTone(eventType)
}

function phaseEntry(entries: readonly SopTraceEntryResponse[], phase: string) {
  return entries
    .filter((entry) => entry.phase === phase)
    .sort((a, b) => b.sequence - a.sequence)[0]
}

function safeRoleLabel(roleId: string | null, fallback: string) {
  if (!roleId) return fallback
  const value = roleId.toLowerCase()
  if (value.includes('claude')) return 'Claude · 方案设计'
  if (value.includes('dsh') || value.includes('deepseek') || value.includes('harness')) return 'DSH · 主执行'
  if (value.includes('codex') || value.includes('review')) return 'Codex · 最终审核'
  return fallback
}

export function CollaborationTracePanel({
  trace,
  planHandoffs = [],
  onDispatchPlan,
  dispatchingHandoffId = null,
  dispatchResult = null,
  dispatchError,
}: CollaborationTracePanelProps) {
  const entries = [...(trace.data?.entries ?? [])].sort((a, b) => a.sequence - b.sequence)
  const dispatchablePlans = planHandoffs.filter(
    (handoff) => handoff.message_type === 'plan_ready' && handoff.dispatchable,
  )

  return (
    <div className="space-y-4">
      <ErrorNotice error={trace.error} context="读取协作过程" />

      <Panel
        title="协作过程"
        subtitle={
          trace.data
            ? `${entries.length} 条 · SOP Engine 审计`
            : 'Claude → DSH → Codex'
        }
      >
        <p className="text-xs text-slate-500">
          当前仅显示 SOP Engine 审计事件，不展示模型 provider、runtime、tool 或原始 payload 内容。
        </p>

        {trace.loading && !trace.data ? <p className="text-sm text-slate-500">加载中…</p> : null}

        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          {PHASES.map((phase) => {
            const entry = phaseEntry(entries, phase.id)
            return (
              <div key={phase.id} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-slate-100">
                    {safeRoleLabel(entry?.role_id ?? phase.role, phase.label)}
                  </span>
                  <Badge tone={entry ? traceTone(entry.status, entry.event_type) : 'idle'} mono>
                    {entry?.status ?? 'pending'}
                  </Badge>
                </div>
                <p className="mt-1.5 text-xs text-slate-400">
                  {entry?.message ?? '尚无该阶段的审计记录。'}
                </p>
                {entry ? (
                  <p className="mt-1 text-[11px] text-slate-600">
                    #{entry.sequence} · {formatTime(entry.occurred_at)}
                  </p>
                ) : null}
              </div>
            )
          })}
        </div>

        {entries.length === 0 ? (
          <Empty>该 run 暂无协作过程记录。</Empty>
        ) : (
          <ol className="space-y-1.5" aria-label="协作审计时间线">
            {entries.map((entry) => (
              <li key={`${entry.sequence}-${entry.event_type}`} className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[11px] text-slate-500">#{entry.sequence}</span>
                  <Badge tone={traceTone(entry.status, entry.event_type)} mono>
                    {entry.status}
                  </Badge>
                  <Badge tone={eventTone(entry.event_type)} mono>
                    {entry.event_type}
                  </Badge>
                  <span className="text-[11px] text-slate-400">
                    {safeRoleLabel(entry.role_id, 'SOP Engine')}
                  </span>
                  <span className="text-[11px] text-slate-500">{formatTime(entry.occurred_at)}</span>
                </div>
                <p className="mt-1 text-xs text-slate-300">{entry.message}</p>
              </li>
            ))}
          </ol>
        )}

        {dispatchablePlans.length > 0 ? (
          <div className="space-y-2 border-t border-slate-800 pt-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400">已完成方案</h3>
            {dispatchablePlans.map((handoff) => (
              <div key={handoff.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-cyan-500/30 bg-cyan-500/5 px-3 py-2">
                <div className="min-w-0">
                  <p className="text-sm text-slate-200">方案已就绪 · Claude → DSH</p>
                  <p className="mt-0.5 break-words text-xs text-slate-500">
                    {handoff.brief || '已生成可执行方案'} · handoff {shortId(handoff.id, 8)}
                  </p>
                </div>
                <Button
                  kind="primary"
                  disabled={
                    !onDispatchPlan ||
                    dispatchingHandoffId === handoff.id ||
                    dispatchResult?.handoff_id === handoff.id
                  }
                  title={onDispatchPlan ? undefined : '等待父层接入 Engine dispatch 回调'}
                  onClick={() => onDispatchPlan?.(handoff)}
                >
                  {dispatchingHandoffId === handoff.id
                    ? '发送中…'
                    : dispatchResult?.handoff_id === handoff.id
                      ? '已发送'
                      : '发送给 DSH'}
                </Button>
              </div>
            ))}
          </div>
        ) : null}

        <ErrorNotice error={dispatchError} context="发送方案给 DSH" compact />
        {dispatchResult ? (
          <div
            className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200"
            role="status"
          >
            方案已发送给 DSH，已创建执行任务{' '}
            <code className="font-mono">{shortId(dispatchResult.task_id, 10)}</code>。
          </div>
        ) : null}
      </Panel>
    </div>
  )
}
