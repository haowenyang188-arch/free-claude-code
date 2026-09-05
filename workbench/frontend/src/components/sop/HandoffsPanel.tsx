import { useMemo } from 'react'
import { Badge, Button, Empty, Hint, KeyValue, KeyValueGrid, Panel } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import {
  HANDOFF_SEMANTIC_LABELS,
  HANDOFF_SEMANTIC_TYPES,
  handoffTone,
  inferHandoffSemantic,
  roleLabel,
} from '../../lib/labels'
import { formatTime, payloadString, shortId } from '../../lib/format'
import type { HandoffResponse, SopEventResponse, TaskResponse } from '../../types/sop'

/**
 * 能力 4 —— Handoff 时间线。
 *
 * P0 契约核对（2026-09-03）：HandoffResponse 现已由后端返回完整投影
 * （含 message_type / brief / role+runtime / dispatchable，见 types/sop.ts）。
 * 本组件渲染逻辑仍沿用「事件流推断」路径以保证行为不变；
 * 直接消费 message_type 属于 P1 UI 增强（届时可简化 infer 兜底）。
 * inferHandoffSemantic 保留用于旧记录 / 事件缺失时的兜底标注。
 */

interface HandoffView {
  handoff: HandoffResponse
  fromTask: TaskResponse | undefined
  events: SopEventResponse[]
}

export function HandoffsPanel({ bundle }: { bundle: SopRunBundle }) {
  const { handoffs, tasks, events, enabled } = bundle

  const views = useMemo<HandoffView[]>(() => {
    const taskById = new Map<string, TaskResponse>()
    for (const task of tasks.data ?? []) taskById.set(task.id, task)

    const eventsByHandoff = new Map<string, SopEventResponse[]>()
    for (const event of events.data ?? []) {
      const handoffId = payloadString(event.payload, 'handoff_id')
      if (!handoffId) continue
      const list = eventsByHandoff.get(handoffId) ?? []
      list.push(event)
      eventsByHandoff.set(handoffId, list)
    }

    return (handoffs.data ?? [])
      .map((handoff) => ({
        handoff,
        fromTask: taskById.get(handoff.from_task_id),
        events: (eventsByHandoff.get(handoff.id) ?? []).sort((a, b) => a.sequence - b.sequence),
      }))
      .sort((a, b) => {
        const left = a.events[0]?.sequence ?? Number.MAX_SAFE_INTEGER
        const right = b.events[0]?.sequence ?? Number.MAX_SAFE_INTEGER
        return left - right
      })
  }, [handoffs.data, tasks.data, events.data])

  if (!enabled) return <Empty>请先在左侧选择或创建一个 SOP Run。</Empty>

  return (
    <div className="space-y-4">
      <ErrorNotice error={handoffs.error ?? tasks.error ?? events.error} context="读取 handoff 列表" />

      <Panel
        title="Handoff 语义图例"
        subtitle="message_type 已由后端返回；图例保留（旧记录兜底用事件推断）"
        dense
      >
        <div className="flex flex-wrap gap-2">
          {HANDOFF_SEMANTIC_TYPES.map((type) => (
            <span key={type} className="rounded bg-slate-800 px-2 py-0.5 text-[11px] text-slate-400">
              {HANDOFF_SEMANTIC_LABELS[type]}
            </span>
          ))}
        </div>
      </Panel>

      <Panel
        title="Handoff 时间线"
        subtitle={`${views.length} 条 · GET /api/sop-runs/{id}/handoffs`}
        actions={
          <Button
            kind="ghost"
            onClick={() => {
              handoffs.refresh()
              tasks.refresh()
              events.refresh()
            }}
          >
            刷新
          </Button>
        }
      >
        {views.length === 0 ? (
          <Empty>该 run 暂无 handoff 记录。</Empty>
        ) : (
          <ol className="space-y-3">
            {views.map((view) => {
              const semantic = inferHandoffSemantic(view.events.map((event) => event.event_type))
              return (
                <li
                  key={view.handoff.id}
                  className="rounded-lg border border-slate-800 bg-slate-950/40 p-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <code className="font-mono text-xs text-slate-200">
                      {shortId(view.handoff.id, 10)}
                    </code>
                    <Badge tone={handoffTone(String(view.handoff.status))} mono>
                      {String(view.handoff.status)}
                    </Badge>
                    {semantic ? (
                      <Badge tone="accent">{HANDOFF_SEMANTIC_LABELS[semantic]}（推断）</Badge>
                    ) : (
                      <Badge tone="idle">语义未知</Badge>
                    )}
                    <span className="text-xs text-slate-500">
                      {view.fromTask ? roleLabel(view.fromTask.role_id) : '来源任务未知'} →{' '}
                      <code className="font-mono">{view.handoff.to_step_id}</code>
                    </span>
                  </div>

                  <KeyValueGrid>
                    <KeyValue label="来源任务">
                      {view.fromTask ? (
                        <span className="text-xs">
                          {view.fromTask.title}{' '}
                          <code className="font-mono">{shortId(view.fromTask.id, 6)}</code>
                        </span>
                      ) : (
                        <code className="font-mono text-xs">{shortId(view.handoff.from_task_id, 10)}</code>
                      )}
                    </KeyValue>
                    <KeyValue label="携带 artifact">{view.handoff.artifact_ids.length} 个</KeyValue>
                    <KeyValue label="created_at">{formatTime(view.handoff.created_at)}</KeyValue>
                    <KeyValue label="accepted_at">{formatTime(view.handoff.accepted_at)}</KeyValue>
                  </KeyValueGrid>

                  {view.handoff.artifact_ids.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5">
                      {view.handoff.artifact_ids.map((id) => (
                        <code
                          key={id}
                          className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-400"
                        >
                          {shortId(id, 8)}
                        </code>
                      ))}
                    </div>
                  ) : null}

                  {view.events.length > 0 ? (
                    <div className="flex flex-wrap gap-2 text-[11px] text-slate-400">
                      {view.events.map((event) => (
                        <span key={event.id} className="rounded bg-slate-800/70 px-1.5 py-0.5">
                          <span className="font-mono text-slate-500">#{event.sequence}</span>{' '}
                          {event.event_type}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <Hint>
                      事件流中没有与该 handoff 关联的 handoff_created / handoff_accepted 事件，无法旁证语义。
                    </Hint>
                  )}
                </li>
              )
            })}
          </ol>
        )}
      </Panel>
    </div>
  )
}
