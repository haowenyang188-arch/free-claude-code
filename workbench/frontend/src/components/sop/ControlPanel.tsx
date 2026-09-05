import { useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  CodeBlock,
  DataTable,
  Empty,
  Hint,
  KeyValue,
  KeyValueGrid,
  Panel,
  inputClass,
} from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { useResource } from '../../hooks/useResource'
import {
  controlLegacyRun,
  controlSopRun,
  getLegacyRun,
  listLegacyTasks,
  sendRunMessage,
  startLegacyTask,
} from '../../services/sopApi'
import { errorMessage } from '../../services/errors'
import { legacyStatusTone, runtimeLabel, sopRunTone } from '../../lib/labels'
import { formatDuration, formatTime, shortId } from '../../lib/format'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import type { SopControlAction, SopControlResponse } from '../../types/sop'

const SOP_ACTIONS: { value: SopControlAction; label: string; kind: 'ghost' | 'primary' | 'danger' }[] = [
  { value: 'pause', label: '暂停', kind: 'ghost' },
  { value: 'resume', label: '继续', kind: 'primary' },
  { value: 'cancel', label: '取消', kind: 'danger' },
]

const LEGACY_ACTIONS: {
  value: 'pause' | 'resume' | 'cancel' | 'retry'
  label: string
  kind: 'ghost' | 'primary' | 'danger'
}[] = [
  { value: 'pause', label: '暂停', kind: 'ghost' },
  { value: 'resume', label: '继续', kind: 'primary' },
  { value: 'cancel', label: '取消', kind: 'danger' },
  { value: 'retry', label: '重试', kind: 'ghost' },
]

/**
 * 能力 7 —— 人工暂停 / 继续 / 取消任务（SOP run 级别）
 * 能力 9 —— 失败原因与阻塞状态（run.error / metadata.recovery_*）
 *
 * 主路径：POST /api/sop-runs/{run_id}/control，body = { action, reason? }，
 * action 只允许 pause / resume / cancel；已终结的 run 返回 409。
 * 遗留 run 控制（POST /api/runs/{id}/control，body 必须带 run_id）作为副路径保留。
 */
export function ControlPanel({
  bundle,
  pollMs,
  refreshToken,
}: {
  bundle: SopRunBundle
  pollMs: number
  refreshToken: number
}) {
  const { runId, run, enabled } = bundle

  const [reason, setReason] = useState('')
  const [sopNote, setSopNote] = useState<string | null>(null)
  const [sopError, setSopError] = useState<unknown>(undefined)
  const [sopBusy, setSopBusy] = useState<SopControlAction | null>(null)
  const [sopResult, setSopResult] = useState<SopControlResponse | null>(null)

  const [taskId, setTaskId] = useState<string | null>(null)
  const [legacyRunId, setLegacyRunId] = useState('')
  const [message, setMessage] = useState('')
  const [legacyNote, setLegacyNote] = useState<string | null>(null)
  const [legacyError, setLegacyError] = useState<unknown>(undefined)
  const [legacyBusy, setLegacyBusy] = useState<string | null>(null)

  const tasks = useResource(() => listLegacyTasks(), [refreshToken], { intervalMs: pollMs })
  const legacyRun = useResource(
    () => (legacyRunId ? getLegacyRun(legacyRunId) : Promise.resolve(undefined)),
    [legacyRunId, refreshToken],
    { intervalMs: pollMs, enabled: Boolean(legacyRunId) },
  )

  const taskList = tasks.data ?? []
  const selectedTask = useMemo(
    () => taskList.find((task) => task.id === taskId),
    [taskList, taskId],
  )

  // 选中任务后默认切到它最新的一次 run。
  useEffect(() => {
    if (!selectedTask) return
    if (selectedTask.runs.length === 0) {
      setLegacyRunId('')
      return
    }
    setLegacyRunId((current) =>
      current && selectedTask.runs.includes(current)
        ? current
        : selectedTask.runs[selectedTask.runs.length - 1],
    )
  }, [selectedTask])

  async function actOnSopRun(action: SopControlAction) {
    if (!runId) return
    setSopBusy(action)
    setSopError(undefined)
    setSopNote(null)
    setSopResult(null)
    try {
      const result = await controlSopRun(runId, action, reason)
      setSopResult(result)
      setSopNote(`${action} -> status=${result.status}`)
      setReason('')
      run.refresh()
    } catch (err) {
      setSopError(err)
    } finally {
      setSopBusy(null)
    }
  }

  async function actOnLegacyRun(action: 'pause' | 'resume' | 'cancel' | 'retry') {
    if (!legacyRunId) return
    setLegacyBusy(action)
    setLegacyError(undefined)
    setLegacyNote(null)
    try {
      const result = await controlLegacyRun(legacyRunId, action)
      setLegacyNote(`${action} -> success=${String(result.success)}`)
      legacyRun.refresh()
      tasks.refresh()
    } catch (err) {
      setLegacyError(err)
    } finally {
      setLegacyBusy(null)
    }
  }

  async function send() {
    if (!legacyRunId || !message.trim()) return
    setLegacyBusy('message')
    setLegacyError(undefined)
    setLegacyNote(null)
    try {
      const result = await sendRunMessage(legacyRunId, message.trim())
      setLegacyNote(`消息已提交 success=${String(result.success)}`)
      setMessage('')
      legacyRun.refresh()
    } catch (err) {
      setLegacyError(err)
    } finally {
      setLegacyBusy(null)
    }
  }

  async function startSelected() {
    if (!selectedTask) return
    setLegacyBusy('start')
    setLegacyError(undefined)
    setLegacyNote(null)
    try {
      const created = await startLegacyTask(selectedTask.id)
      setLegacyRunId(created.id)
      setLegacyNote(`已启动 run ${created.id}`)
      tasks.refresh()
    } catch (err) {
      setLegacyError(err)
    } finally {
      setLegacyBusy(null)
    }
  }

  return (
    <div className="space-y-4">
      {/* ---------------- 能力 7：SOP run 级别控制 ---------------- */}
      <Panel
        title="人工暂停 / 继续 / 取消（SOP Run）"
        subtitle="POST /api/sop-runs/{run_id}/control · body { action, reason? }"
      >
        {!enabled ? (
          <Empty>请先在左侧选择或创建一个 SOP Run。</Empty>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs text-slate-500">
                当前 run <code className="font-mono">{shortId(runId, 12)}</code>
              </span>
              {run.data ? (
                <Badge tone={sopRunTone(String(run.data.status))} mono>
                  {String(run.data.status)}
                </Badge>
              ) : null}
            </div>

            <div className="flex flex-wrap gap-2">
              <input
                className={`${inputClass} w-64`}
                placeholder="reason（可选）"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              {SOP_ACTIONS.map((action) => (
                <Button
                  key={action.value}
                  kind={action.kind}
                  disabled={sopBusy !== null}
                  onClick={() => void actOnSopRun(action.value)}
                >
                  {sopBusy === action.value ? '执行中…' : action.label}
                </Button>
              ))}
            </div>

            <ErrorNotice error={sopError} context="控制 SOP run" compact />
            {sopNote ? (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
                {sopNote}
              </div>
            ) : null}
            {sopResult ? (
              <dl className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <KeyValue label="sop_run_id">
                  <code className="break-all font-mono text-xs">{sopResult.sop_run_id}</code>
                </KeyValue>
                <KeyValue label="action">{sopResult.action}</KeyValue>
                <KeyValue label="status">
                  <Badge tone={sopRunTone(sopResult.status)} mono>
                    {sopResult.status}
                  </Badge>
                </KeyValue>
              </dl>
            ) : null}

            <Hint>
              action 只允许 pause / resume / cancel；run 已处于 completed / cancelled 时后端返回 409。
              resume 只能从 paused 触发。
            </Hint>
          </>
        )}
      </Panel>

      {/* ---------------- 能力 9：遗留 run 的失败原因 ---------------- */}
      <Panel
        title="遗留任务"
        subtitle={`${taskList.length} 条 · GET /api/tasks`}
        actions={
          <>
            <Button kind="default" disabled={!selectedTask || legacyBusy !== null} onClick={() => void startSelected()}>
              {legacyBusy === 'start' ? '启动中…' : '启动任务'}
            </Button>
            <Button kind="ghost" onClick={tasks.refresh}>
              刷新
            </Button>
          </>
        }
      >
        <ErrorNotice error={tasks.error} context="读取任务列表" compact />
        {taskList.length === 0 ? (
          <Empty>暂无遗留任务。</Empty>
        ) : (
          <DataTable head={['标题', 'agent', '状态', 'runs', '更新']}>
            {taskList.map((task) => (
              <tr
                key={task.id}
                onClick={() => setTaskId(task.id === taskId ? null : task.id)}
                className={`cursor-pointer ${
                  task.id === taskId ? 'bg-cyan-500/10' : 'bg-slate-900 hover:bg-slate-800/60'
                }`}
              >
                <td className="px-3 py-2">
                  <div className="text-xs text-slate-200">{task.title || '(无标题)'}</div>
                  <code className="font-mono text-[11px] text-slate-500">{shortId(task.id, 10)}</code>
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  {task.agent_type ? runtimeLabel(String(task.agent_type)) : '—'}
                </td>
                <td className="px-3 py-2">
                  <Badge tone={legacyStatusTone(String(task.status))} mono>
                    {String(task.status)}
                  </Badge>
                </td>
                <td className="px-3 py-2 font-mono text-xs text-slate-400">{task.runs.length}</td>
                <td className="px-3 py-2 text-xs text-slate-500">{formatTime(task.updated_at)}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </Panel>

      <Panel
        title="遗留 Run 控制与失败原因"
        subtitle={legacyRunId ? `run ${legacyRunId}` : '未选择 run'}
        actions={LEGACY_ACTIONS.map((action) => (
          <Button
            key={action.value}
            kind={action.kind}
            disabled={!legacyRunId || legacyBusy !== null}
            onClick={() => void actOnLegacyRun(action.value)}
          >
            {legacyBusy === action.value ? '执行中…' : action.label}
          </Button>
        ))}
      >
        {selectedTask ? (
          <div className="flex flex-wrap gap-2">
            <select
              className={`${inputClass} w-56`}
              value={legacyRunId}
              onChange={(e) => setLegacyRunId(e.target.value)}
            >
              <option value="">选择 run…</option>
              {selectedTask.runs.map((id) => (
                <option key={id} value={id}>
                  {shortId(id, 12)}
                </option>
              ))}
            </select>
            <input
              className={`${inputClass} flex-1`}
              placeholder="向该 run 发送消息（POST /api/runs/{id}/message）"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
            />
            <Button
              kind="default"
              disabled={!legacyRunId || !message.trim() || legacyBusy !== null}
              onClick={() => void send()}
            >
              发送
            </Button>
          </div>
        ) : (
          <Hint>先在上表选择一个任务。</Hint>
        )}

        <ErrorNotice error={legacyError ?? legacyRun.error} context="控制 run" compact />
        {legacyNote ? (
          <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
            {legacyNote}
          </div>
        ) : null}

        {legacyRun.data ? (
          <>
            <KeyValueGrid>
              <KeyValue label="run id">
                <code className="break-all font-mono text-xs">{legacyRun.data.id}</code>
              </KeyValue>
              <KeyValue label="状态">
                <Badge tone={legacyStatusTone(String(legacyRun.data.status))} mono>
                  {String(legacyRun.data.status)}
                </Badge>
              </KeyValue>
              <KeyValue label="task_id">
                <code className="font-mono text-xs">{shortId(legacyRun.data.task_id, 10)}</code>
              </KeyValue>
              <KeyValue label="agent_id">
                <code className="font-mono text-xs">{shortId(legacyRun.data.agent_id, 10)}</code>
              </KeyValue>
              <KeyValue label="开始">{formatTime(legacyRun.data.started_at)}</KeyValue>
              <KeyValue label="耗时">
                {formatDuration(legacyRun.data.started_at, legacyRun.data.completed_at)}
              </KeyValue>
              <KeyValue label="事件数">{legacyRun.data.events.length}</KeyValue>
              <KeyValue label="tokens / cost">
                {legacyRun.data.tokens_used} / {legacyRun.data.cost}
              </KeyValue>
              <KeyValue label="session_id">
                <code className="break-all font-mono text-xs">
                  {typeof legacyRun.data.metadata?.session_id === 'string'
                    ? String(legacyRun.data.metadata.session_id)
                    : '—'}
                </code>
              </KeyValue>
              <KeyValue label="stale">
                {legacyRun.data.metadata?.stale === true ? 'true（进程重启后标记为 stale）' : 'false'}
              </KeyValue>
            </KeyValueGrid>

            <div>
              <p className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">失败原因</p>
              {legacyRun.data.error ? (
                <CodeBlock small>{legacyRun.data.error}</CodeBlock>
              ) : (
                <Hint>该 run 没有 error 字段。</Hint>
              )}
              {legacyRun.data.metadata?.recovery_reason ? (
                <Hint>
                  recovery_reason: {String(legacyRun.data.metadata.recovery_reason)} ·{' '}
                  {String(legacyRun.data.metadata.recovery_state ?? '')}
                </Hint>
              ) : null}
            </div>

            {legacyRun.data.events.length > 0 ? (
              <div className="space-y-1.5">
                {legacyRun.data.events.slice(-8).map((event) => (
                  <div
                    key={event.id}
                    className="flex flex-wrap items-center gap-2 rounded bg-slate-950/60 px-2 py-1.5"
                  >
                    <Badge tone={event.type === 'run_failed' ? 'danger' : 'neutral'} mono>
                      {String(event.type)}
                    </Badge>
                    <span className="text-[11px] text-slate-500">{formatTime(event.timestamp)}</span>
                    {event.message ? (
                      <span className="text-[11px] text-slate-400">{event.message}</span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}
          </>
        ) : (
          !legacyRun.error && <Empty>未加载 run 详情。</Empty>
        )}
      </Panel>

      <Panel title="操作提示" dense>
        <Hint>
          遗留 control 的 body 必须同时包含 <code className="font-mono">run_id</code> 与{' '}
          <code className="font-mono">action</code>（后端 ControlRequest 两个字段都无默认值）；
          若适配器当前 run 与请求不一致，后端会返回 409。
        </Hint>
        {legacyError !== undefined ? <Hint tone="error">{errorMessage(legacyError)}</Hint> : null}
      </Panel>
    </div>
  )
}
