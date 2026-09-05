import { Badge, Button, DataTable, Empty, KeyValue, KeyValueGrid, Panel } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { useResource } from '../../hooks/useResource'
import { getLegacyRun, listAgents, listLegacyTasks } from '../../services/sopApi'
import { errorMessage } from '../../services/errors'
import { agentTone, legacyStatusTone, runtimeLabel } from '../../lib/labels'
import { formatDuration, formatTime, shortId } from '../../lib/format'
import type { Agent, Task } from '../../types'

interface RunProbe {
  id: string
  run?: Awaited<ReturnType<typeof getLegacyRun>>
  error?: string
}

interface RuntimeBundle {
  tasks: Task[]
  probes: RunProbe[]
}

/**
 * 能力 5 —— 三个 Runtime / Session 状态。
 *
 * 后端只有 GET /api/agents（适配器层），没有 /api/sessions 路由，
 * 因此 session 身份只能从 `run.metadata.session_id` 恢复（main.py 在适配器上报时写入），
 * 与适配器列表一起展示。
 */
export function RuntimePanel({
  pollMs,
  refreshToken,
}: {
  pollMs: number
  refreshToken: number
}) {
  const agents = useResource<Agent[]>(() => listAgents(), [refreshToken], { intervalMs: pollMs })

  const bundle = useResource<RuntimeBundle>(
    async () => {
      const tasks = await listLegacyTasks()
      const runIds = tasks.flatMap((task) => task.runs).slice(0, 12)
      const probes = await Promise.all(
        runIds.map(async (id) => {
          try {
            return { id, run: await getLegacyRun(id) }
          } catch (err) {
            return { id, error: errorMessage(err) }
          }
        }),
      )
      return { tasks, probes }
    },
    [refreshToken],
    { intervalMs: pollMs },
  )

  const agentList = agents.data ?? []
  const taskTitle = new Map((bundle.data?.tasks ?? []).map((task) => [task.id, task.title]))

  return (
    <div className="space-y-4">
      <ErrorNotice error={agents.error} context="读取 /api/agents" />
      <ErrorNotice error={bundle.error} context="读取遗留 run 会话" />

      <Panel
        title="Runtime / Adapter"
        subtitle={`${agentList.length} 个 · GET /api/agents`}
        actions={
          <Button kind="ghost" onClick={agents.refresh}>
            刷新
          </Button>
        }
      >
        {agentList.length === 0 ? (
          <Empty>没有 Runtime 上报。后端会注册 claude_code / codex / deepseek_harness 三类适配器。</Empty>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {agentList.map((agent) => (
              <div key={agent.id} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <strong className="text-sm text-slate-100">{runtimeLabel(String(agent.type))}</strong>
                  <Badge tone={agentTone(String(agent.status))} mono>
                    {String(agent.status)}
                  </Badge>
                </div>
                <KeyValueGrid>
                  <KeyValue label="name">{agent.name}</KeyValue>
                  <KeyValue label="id">
                    <code className="break-all font-mono text-xs">{agent.id}</code>
                  </KeyValue>
                  <KeyValue label="version">{agent.version ?? '—'}</KeyValue>
                  <KeyValue label="当前任务">
                    <code className="font-mono text-xs">
                      {agent.current_task_id ? shortId(agent.current_task_id) : '空闲'}
                    </code>
                  </KeyValue>
                  <KeyValue label="workspace">
                    <code className="break-all font-mono text-xs">{agent.workspace_path ?? '—'}</code>
                  </KeyValue>
                  <KeyValue label="capabilities">
                    {agent.capabilities?.length ? agent.capabilities.join(', ') : '—'}
                  </KeyValue>
                  <KeyValue label="最近活跃">{formatTime(agent.last_active)}</KeyValue>
                </KeyValueGrid>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="Session 状态"
        subtitle="无 /api/sessions 路由，session_id 从 run.metadata 恢复"
        dense
        actions={
          <Button kind="ghost" onClick={bundle.refresh}>
            刷新
          </Button>
        }
      >
        {(bundle.data?.probes ?? []).length === 0 ? (
          <Empty>没有遗留 run，无法恢复 session 信息。</Empty>
        ) : (
          <DataTable head={['run', '任务', '状态', 'session_id', '耗时', '错误']}>
            {(bundle.data?.probes ?? []).map((probe) => {
              const run = probe.run
              const sessionId =
                run && typeof run.metadata?.session_id === 'string'
                  ? (run.metadata.session_id as string)
                  : null
              return (
                <tr key={probe.id} className="bg-slate-900">
                  <td className="px-3 py-2 font-mono text-xs text-slate-300">{shortId(probe.id)}</td>
                  <td className="max-w-[12rem] truncate px-3 py-2 text-xs text-slate-400">
                    {run ? (taskTitle.get(run.task_id) ?? shortId(run.task_id)) : '—'}
                  </td>
                  <td className="px-3 py-2">
                    {run ? (
                      <Badge tone={legacyStatusTone(String(run.status))} mono>
                        {String(run.status)}
                      </Badge>
                    ) : (
                      <Badge tone="danger" mono>
                        error
                      </Badge>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-400">
                    {sessionId ? shortId(sessionId, 12) : <span className="text-slate-600">—</span>}
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-500">
                    {run ? formatDuration(run.started_at, run.completed_at) : '—'}
                  </td>
                  <td className="max-w-[16rem] truncate px-3 py-2 text-xs text-rose-300">
                    {run?.error ?? probe.error ?? '—'}
                  </td>
                </tr>
              )
            })}
          </DataTable>
        )}
      </Panel>
    </div>
  )
}
