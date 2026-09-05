import { useMemo, useState } from 'react'
import { Badge, Button, CodeBlock, Empty, Hint, Panel, inputClass } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import { getRunEvents } from '../../services/sopApi'
import { errorMessage } from '../../services/errors'
import { eventTone } from '../../lib/labels'
import { formatTime, payloadString, shortId, stringifyPayload } from '../../lib/format'
import type { SopEventResponse } from '../../types/sop'

/**
 * 能力 10 —— 从 Event Store 重建完整任务过程。
 * 两条流都暴露：
 *   - SOP 引擎流：GET /api/sop-runs/{id}/events?after=N
 *   - 遗留 run 流：GET /api/runs/{id}/events?after=N（增量回放，带 sequence 游标）
 */
export function EventsPanel({ bundle }: { bundle: SopRunBundle }) {
  const { events, enabled, runId } = bundle
  const [typeFilter, setTypeFilter] = useState('')
  const [showPayload, setShowPayload] = useState(true)

  const all = useMemo(
    () => [...(events.data ?? [])].sort((a, b) => a.sequence - b.sequence),
    [events.data],
  )

  const types = useMemo(
    () => Array.from(new Set(all.map((event) => event.event_type))).sort(),
    [all],
  )

  const filtered = useMemo(() => {
    const keyword = typeFilter.trim().toLowerCase()
    if (!keyword) return all
    return all.filter((event) => event.event_type.toLowerCase().includes(keyword))
  }, [all, typeFilter])

  if (!enabled) return <Empty>请先在左侧选择或创建一个 SOP Run。</Empty>

  function exportJson() {
    const blob = new Blob([JSON.stringify(all, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `sop-run-${runId ?? 'unknown'}-events.json`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const lastSequence = all.length > 0 ? all[all.length - 1].sequence : 0

  return (
    <div className="space-y-4">
      <ErrorNotice error={events.error} context="读取 SOP 事件流" />

      <Panel
        title="SOP 事件时间线"
        subtitle={`${filtered.length}/${all.length} 条 · sequence 1..${lastSequence}`}
        actions={
          <>
            <select
              className={`${inputClass} w-40`}
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
            >
              <option value="">全部类型</option>
              {types.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1.5 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={showPayload}
                onChange={(e) => setShowPayload(e.target.checked)}
              />
              展开 payload
            </label>
            <Button kind="ghost" onClick={exportJson} disabled={all.length === 0}>
              导出 JSON
            </Button>
            <Button kind="ghost" onClick={events.refresh}>
              刷新
            </Button>
          </>
        }
      >
        {filtered.length === 0 ? (
          <Empty>事件流为空。后端 Event Store 至少需要 sop_started 事件。</Empty>
        ) : (
          <ol className="space-y-1.5">
            {filtered.map((event) => (
              <EventRow key={event.id} event={event} showPayload={showPayload} />
            ))}
          </ol>
        )}
      </Panel>

      <LegacyRunReplay />
    </div>
  )
}

function EventRow({ event, showPayload }: { event: SopEventResponse; showPayload: boolean }) {
  const stepId = payloadString(event.payload, 'step_id')
  const taskId = payloadString(event.payload, 'task_id')
  const error = payloadString(event.payload, 'error')

  return (
    <li className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-slate-500">#{event.sequence}</span>
        <Badge tone={eventTone(event.event_type)} mono>
          {event.event_type}
        </Badge>
        <span className="text-[11px] text-slate-500">{formatTime(event.occurred_at)}</span>
        {stepId ? (
          <span className="text-[11px] text-slate-500">
            step <code className="font-mono">{stepId}</code>
          </span>
        ) : null}
        {taskId ? (
          <span className="text-[11px] text-slate-500">
            task <code className="font-mono">{shortId(taskId, 6)}</code>
          </span>
        ) : null}
        {error ? <span className="text-[11px] text-rose-300">error: {error}</span> : null}
      </div>
      {showPayload ? (
        <div className="mt-1.5">
          <CodeBlock small>{stringifyPayload(event.payload)}</CodeBlock>
        </div>
      ) : null}
    </li>
  )
}

/** 遗留 run 的增量回放（sequence 游标）。 */
function LegacyRunReplay() {
  const [runId, setRunId] = useState('')
  const [after, setAfter] = useState('0')
  const [rows, setRows] = useState<Record<string, unknown>[] | null>(null)
  const [error, setError] = useState<unknown>(undefined)
  const [busy, setBusy] = useState(false)

  async function replay() {
    setBusy(true)
    setError(undefined)
    try {
      const response = await getRunEvents(runId.trim(), Number(after) || 0)
      setRows(response.events ?? [])
    } catch (err) {
      setRows(null)
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel title="遗留 Run 事件回放" subtitle="GET /api/runs/{run_id}/events?after=N" dense>
      <div className="flex flex-wrap gap-2">
        <input
          className={`${inputClass} w-64`}
          placeholder="run id"
          value={runId}
          onChange={(e) => setRunId(e.target.value)}
          spellCheck={false}
        />
        <input
          className={`${inputClass} w-24`}
          placeholder="after"
          value={after}
          onChange={(e) => setAfter(e.target.value)}
        />
        <Button kind="default" onClick={() => void replay()} disabled={busy || !runId.trim()}>
          {busy ? '回放中…' : '回放'}
        </Button>
      </div>

      <ErrorNotice error={error} context="回放 run 事件" compact />

      {rows ? (
        rows.length === 0 ? (
          <Empty>该游标之后没有新事件。</Empty>
        ) : (
          <ol className="space-y-1.5">
            {rows.map((row, index) => (
              <li key={String(row.id ?? index)} className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[11px] text-slate-500">
                    #{typeof row.sequence === 'number' ? row.sequence : index}
                  </span>
                  <Badge tone={eventTone(String(row.event_type ?? row.type ?? ''))} mono>
                    {String(row.event_type ?? row.type ?? 'unknown')}
                  </Badge>
                  <span className="text-[11px] text-slate-500">
                    {formatTime(String(row.timestamp ?? row.occurred_at ?? ''))}
                  </span>
                  {row.backend ? (
                    <span className="text-[11px] text-slate-500">backend {String(row.backend)}</span>
                  ) : null}
                </div>
                <div className="mt-1.5">
                  <CodeBlock small>{stringifyPayload(row)}</CodeBlock>
                </div>
              </li>
            ))}
          </ol>
        )
      ) : (
        <Hint>遗留 run id 可从「控制」页的任务列表里取到。</Hint>
      )}
      {error !== undefined ? <Hint tone="error">{errorMessage(error)}</Hint> : null}
    </Panel>
  )
}
