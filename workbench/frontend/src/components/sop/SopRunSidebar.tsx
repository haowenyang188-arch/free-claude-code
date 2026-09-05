import { useState } from 'react'
import { Badge, Button, Empty, KeyValue, KeyValueGrid, Panel, inputClass } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { useResource } from '../../hooks/useResource'
import { listAgents, listSopRuns } from '../../services/sopApi'
import { agentTone, runtimeLabel, sopRunTone } from '../../lib/labels'
import { shortId } from '../../lib/format'
import type { RecentRun } from '../../lib/storage'

/**
 * 左侧：SOP run 选择器 + Runtime 概览。
 * GET /api/sop-runs 提供了列表（此前没有），本地历史仅用于记住手动载入过的 run。
 */
export function SopRunSidebar({
  runId,
  recentRuns,
  onSelectRun,
  onForgetRun,
  pollMs,
  refreshToken,
}: {
  runId: string
  recentRuns: RecentRun[]
  onSelectRun: (id: string) => void
  onForgetRun: (id: string) => void
  pollMs: number
  refreshToken: number
}) {
  const [draft, setDraft] = useState('')

  const runs = useResource(() => listSopRuns(), [refreshToken], { intervalMs: pollMs })
  const agents = useResource(() => listAgents(), [refreshToken], { intervalMs: pollMs })

  const runList = runs.data ?? []
  const agentList = agents.data ?? []

  function submitManual() {
    const value = draft.trim()
    if (!value) return
    onSelectRun(value)
    setDraft('')
  }

  return (
    <div className="space-y-4">
      <Panel
        title="SOP Run"
        subtitle={`${runList.length} 个 · GET /api/sop-runs`}
        dense
        actions={
          <Button kind="ghost" onClick={runs.refresh}>
            刷新
          </Button>
        }
      >
        <ErrorNotice error={runs.error} context="读取 SOP run 列表" compact />

        {runList.length === 0 ? (
          <Empty>后端还没有 SOP run。先注册定义并下发一个任务。</Empty>
        ) : (
          <ul className="space-y-1.5">
            {runList.map((run) => (
              <li key={run.sop_run_id}>
                <button
                  type="button"
                  onClick={() => onSelectRun(run.sop_run_id)}
                  className={`w-full rounded-lg border px-3 py-2 text-left transition ${
                    run.sop_run_id === runId
                      ? 'border-cyan-500/60 bg-cyan-500/10'
                      : 'border-slate-800 bg-slate-950/40 hover:border-slate-700'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <code className="truncate font-mono text-xs text-slate-200">
                      {shortId(run.sop_run_id, 10)}
                    </code>
                    <Badge tone={sopRunTone(String(run.status))} mono>
                      {String(run.status)}
                    </Badge>
                  </div>
                  <div className="mt-1 truncate text-[11px] text-slate-500">
                    {run.sop_definition_id}
                    {run.current_step_id ? ` · ${run.current_step_id}` : ''}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}

        <form
          className="mt-3 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            submitManual()
          }}
        >
          <input
            className={inputClass}
            value={draft}
            placeholder="手动载入 sop run id"
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
          />
          <Button kind="default" onClick={submitManual} disabled={!draft.trim()}>
            载入
          </Button>
        </form>

        {recentRuns.length > 0 ? (
          <div className="mt-3">
            <p className="mb-1.5 text-[11px] uppercase tracking-wide text-slate-500">本地历史</p>
            <ul className="space-y-1">
              {recentRuns.map((item) => (
                <li key={item.id} className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => onSelectRun(item.id)}
                    className="min-w-0 flex-1 truncate rounded px-2 py-1 text-left font-mono text-xs text-slate-400 hover:bg-slate-800"
                  >
                    {shortId(item.id, 12)}
                    {item.note ? <span className="ml-2 text-slate-600">{item.note}</span> : null}
                  </button>
                  <button
                    type="button"
                    onClick={() => onForgetRun(item.id)}
                    title="从历史移除"
                    className="rounded px-1.5 text-slate-600 hover:text-rose-300"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </Panel>

      <Panel
        title="Runtime 概览"
        subtitle={`${agentList.length} 个 · GET /api/agents`}
        dense
        actions={
          <Button kind="ghost" onClick={agents.refresh}>
            刷新
          </Button>
        }
      >
        <ErrorNotice error={agents.error} context="读取 /api/agents" compact />
        {agentList.length === 0 ? (
          <Empty>没有 Runtime 上报。</Empty>
        ) : (
          <ul className="space-y-2">
            {agentList.map((agent) => (
              <li key={agent.id} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm text-slate-200">
                    {runtimeLabel(String(agent.type))}
                  </span>
                  <Badge tone={agentTone(String(agent.status))} mono>
                    {String(agent.status)}
                  </Badge>
                </div>
                <KeyValueGrid>
                  <KeyValue label="id">
                    <code className="font-mono text-xs">{shortId(agent.id)}</code>
                  </KeyValue>
                  <KeyValue label="当前任务">
                    <code className="font-mono text-xs">
                      {agent.current_task_id ? shortId(agent.current_task_id) : '空闲'}
                    </code>
                  </KeyValue>
                </KeyValueGrid>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}
