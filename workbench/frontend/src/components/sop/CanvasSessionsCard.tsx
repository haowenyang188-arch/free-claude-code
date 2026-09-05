import { useMemo } from 'react'
import { Badge, Button, Empty, Panel } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { useResource } from '../../hooks/useResource'
import { getCanvasIntegrationHealth, searchCanvasSessions } from '../../services/canvasIntegrations'
import { toConversationRunSummaryView, type ConversationSession } from '../../types/conversation'
import type { Tone } from '../../lib/labels'

/**
 * Canvas 会话状态卡 —— A+ 会话状态联动（P1-A 最小版）。
 *
 * 数据源：8000 /api/integrations/canvas/conversations/search（只读转发 18000）。
 * 边界纪律（WEBUI_ARCH_ADOPTED.md）：本卡只读取会话自身的执行摘要用于展示，
 * 不写状态、不缓存权威副本、不做任何 Orchestrator 决策。
 */

const EXECUTION_TONE: Record<string, Tone> = {
  starting: 'active',
  running: 'active',
  paused: 'warn',
  finished: 'success',
  aborted: 'danger',
  error: 'danger',
}

export function conversationExecutionTone(status: string | undefined): Tone {
  if (!status) return 'idle'
  return EXECUTION_TONE[status] ?? 'neutral'
}

/** requestJson 会在 Error 上附带 status（types 未声明，运行时安全读取）。 */
function errorStatus(error: unknown): number | undefined {
  if (error && typeof error === 'object' && 'status' in error) {
    const status = (error as { status?: unknown }).status
    return typeof status === 'number' ? status : undefined
  }
  return undefined
}

function shortId(id: string, len = 10): string {
  return id.length > len ? id.slice(0, len) : id
}

export function CanvasSessionsCard({
  onOpenSession,
  linkedAcpSessionId,
}: {
  onOpenSession: (id: string) => void
  linkedAcpSessionId?: string | null
}) {
  const health = useResource(() => getCanvasIntegrationHealth(), [], { intervalMs: null })
  const sessions = useResource(
    () => searchCanvasSessions({ limit: linkedAcpSessionId ? 100 : 6 }).then((response) => response.items),
    [linkedAcpSessionId],
    { intervalMs: 10000 },
  )

  const linkedSessionId = useMemo(
    () => (sessions.data ?? []).find(
      (item) => item.agent_state?.acp_session_id === linkedAcpSessionId,
    )?.id ?? null,
    [linkedAcpSessionId, sessions.data],
  )
  const rows = useMemo(() => {
    const mapped = (sessions.data ?? []).map(toConversationRunSummaryView)
    if (!linkedSessionId) return mapped.slice(0, 6)
    const linked = mapped.find((item) => item.session.id === linkedSessionId)
    return linked
      ? [linked, ...mapped.filter((item) => item.session.id !== linkedSessionId).slice(0, 5)]
      : mapped.slice(0, 6)
  }, [linkedSessionId, sessions.data])

  return (
    <Panel
      title="Canvas 会话"
      subtitle={health.data?.enabled ? 'agent-server 状态摘要（只读）' : '集成未启用'}
      dense
      actions={
        <Button kind="ghost" onClick={sessions.refresh}>
          刷新
        </Button>
      }
    >
      <ErrorNotice error={health.error} context="读取 Canvas 集成状态" compact />
      <ErrorNotice error={sessions.error} context="读取会话状态" compact />

      {sessions.error ? (
        <p className="text-xs text-fg-muted">
          {errorStatus(sessions.error) === 503
            ? 'Canvas 集成未启用：需在 8000 配置 WORKBENCH_CANVAS_SESSION_API_KEY 后重启。'
            : '暂时无法读取会话摘要（agent-server 不可达或上游异常）。'}
        </p>
      ) : null}

      {!sessions.error && rows.length === 0 ? (
        <Empty>暂无 Canvas 会话。前往「Agent 会话」发起一个。</Empty>
      ) : null}

      {!sessions.error && linkedAcpSessionId && !linkedSessionId ? (
        <p className="mb-2 text-xs text-fg-muted">当前 run 的 Canvas 会话尚未出现在列表中。</p>
      ) : null}

      {rows.length > 0 ? (
        <ul className="space-y-1.5">
          {rows.map(({ session, modelName, accumulatedCost, running }) => (
            <li key={session.id}>
              <button
                type="button"
                onClick={() => onOpenSession(session.id)}
                title={`打开会话 ${session.id}`}
                className="w-full rounded-lg border border-edge bg-inset px-3 py-2 text-left transition hover:border-edge-strong"
              >
                <div className="flex items-center justify-between gap-2">
                  <code className="truncate font-mono text-xs text-fg-main">{shortId(session.id)}</code>
                  <div className="flex items-center gap-1.5">
                    {session.id === linkedSessionId ? <Badge tone="active">当前 run</Badge> : null}
                    <Badge tone={conversationExecutionTone(session.execution_status)} mono>
                      {running ? '● running' : String(session.execution_status)}
                    </Badge>
                  </div>
                </div>
                <div className="mt-1 truncate text-[11px] text-fg-muted">
                  {session.workspace.working_dir.split('/').slice(-2).join('/')}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[11px] text-fg-secondary">
                  {modelName ? <span className="font-mono">{modelName}</span> : <span>—</span>}
                  {accumulatedCost !== null ? (
                    <span className="font-mono">${accumulatedCost.toFixed(4)}</span>
                  ) : null}
                </div>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </Panel>
  )
}

/** 供类型引用导出（组件测试用） */
export type CanvasSessionRow = ReturnType<typeof toConversationRunSummaryView>
export type { ConversationSession }
