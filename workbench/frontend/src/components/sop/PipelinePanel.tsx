import { useMemo } from 'react'
import { Badge, Button, DataTable, Empty, KeyValue, KeyValueGrid, Panel } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import { roleLabel, roleShort, sopRunTone, sopTaskTone, stepTone } from '../../lib/labels'
import { formatDuration, formatTime, payloadString, shortId } from '../../lib/format'
import type { SopEventResponse, StepRunResponse, TaskResponse } from '../../types/sop'

const ACTIVE_STEP_STATUSES = ['running', 'validating', 'ready', 'waiting_review']
const PROBLEM_STEP_STATUSES = new Set(['failed', 'blocked', 'rejected', 'rework'])
const PROBLEM_TASK_STATUSES = new Set(['failed', 'rejected', 'rework'])

interface StepView {
  step: StepRunResponse
  task: TaskResponse | undefined
  order: number
}

/** StepRunResponse 没有序号，顺序只能从事件流里还原。 */
function orderFromEvents(events: SopEventResponse[]): Map<string, number> {
  const order = new Map<string, number>()
  const sorted = [...events].sort((a, b) => a.sequence - b.sequence)
  for (const event of sorted) {
    const stepId = payloadString(event.payload, 'step_id')
    if (stepId && !order.has(stepId)) order.set(stepId, event.sequence)
  }
  return order
}

/**
 * 能力 2 —— 实时流程状态（Claude → DSH → Codex，当前阶段一眼可见）
 * 能力 9 —— 失败原因与阻塞状态（失败 / 阻塞 / 返工汇总）
 */
export function PipelinePanel({ bundle }: { bundle: SopRunBundle }) {
  const { run, steps, tasks, events, chain, refreshAll, firstError, enabled } = bundle

  const taskById = useMemo(() => {
    const map = new Map<string, TaskResponse>()
    for (const task of tasks.data ?? []) map.set(task.id, task)
    return map
  }, [tasks.data])

  const stepViews = useMemo<StepView[]>(() => {
    const order = orderFromEvents(events.data ?? [])
    const list = (steps.data ?? []).map((step) => ({
      step,
      task: step.task_id ? taskById.get(step.task_id) : undefined,
      order: order.get(step.step_id) ?? Number.MAX_SAFE_INTEGER,
    }))
    return list.sort((a, b) => a.order - b.order || a.step.step_id.localeCompare(b.step.step_id))
  }, [steps.data, events.data, taskById])

  const currentStep = useMemo(() => {
    for (const status of ACTIVE_STEP_STATUSES) {
      const hit = stepViews.find((view) => view.step.status === status)
      if (hit) return hit
    }
    return undefined
  }, [stepViews])

  const problems = useMemo(() => {
    const stepIssues = stepViews
      .filter((view) => PROBLEM_STEP_STATUSES.has(String(view.step.status)))
      .map((view) => ({
        scope: `step ${view.step.step_id}`,
        status: String(view.step.status),
        detail: view.task ? view.task.title : '无绑定任务',
      }))

    const taskIssues = (tasks.data ?? [])
      .filter((task) => PROBLEM_TASK_STATUSES.has(String(task.status)))
      .map((task) => ({
        scope: `task ${shortId(task.id)}`,
        status: String(task.status),
        detail: task.title || task.description || '无标题',
      }))

    const rejectedValidations = (events.data ?? [])
      .filter((event) => {
        if (event.event_type !== 'validation_completed') return false
        const status = payloadString(event.payload, 'status')
        return status !== null && status !== 'accepted'
      })
      .map((event) => ({
        scope: 'validation',
        status: payloadString(event.payload, 'status') ?? 'rejected',
        detail: `artifact ${shortId(payloadString(event.payload, 'artifact_id'))} · task ${shortId(
          payloadString(event.payload, 'task_id'),
        )}`,
      }))

    return [...stepIssues, ...taskIssues, ...rejectedValidations]
  }, [stepViews, tasks.data, events.data])

  if (!enabled) {
    return <Empty>请先在左侧选择或创建一个 SOP Run。</Empty>
  }

  return (
    <div className="space-y-4">
      <ErrorNotice error={firstError} context="读取 SOP 运行状态" />

      <Panel
        title="运行状态"
        subtitle={run.data ? `sop_definition_id=${run.data.sop_definition_id}` : undefined}
        actions={
          <Button kind="ghost" onClick={refreshAll}>
            刷新
          </Button>
        }
      >
        {run.data ? (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <Badge tone={sopRunTone(String(run.data.status))} mono>
                {String(run.data.status)}
              </Badge>
              <span className="text-sm text-slate-300">
                当前阶段：
                {currentStep ? (
                  <>
                    <strong className="font-mono text-slate-100">{currentStep.step.step_id}</strong>
                    <span className="ml-2 text-slate-500">
                      {currentStep.task ? roleLabel(currentStep.task.role_id) : '未绑定任务'}
                    </span>
                  </>
                ) : (
                  <span className="text-slate-500">
                    {run.data.status === 'completed' ? '已全部完成' : '无活动阶段'}
                  </span>
                )}
              </span>
            </div>
            <KeyValueGrid>
              <KeyValue label="run id">
                <code className="break-all font-mono text-xs">{run.data.id}</code>
              </KeyValue>
              <KeyValue label="goal id">
                <code className="break-all font-mono text-xs">{shortId(run.data.goal_id, 10)}</code>
              </KeyValue>
              <KeyValue label="开始">{formatTime(run.data.started_at)}</KeyValue>
              <KeyValue label="结束">{formatTime(run.data.completed_at)}</KeyValue>
              <KeyValue label="耗时">
                {formatDuration(run.data.started_at, run.data.completed_at)}
              </KeyValue>
              <KeyValue label="步骤">
                {run.data.steps_completed}/{run.data.step_count} 完成 · {run.data.steps_running} 运行中 ·{' '}
                {run.data.steps_ready} 就绪
              </KeyValue>
            </KeyValueGrid>
          </>
        ) : (
          <p className="text-sm text-slate-500">加载中…</p>
        )}
      </Panel>

      <Panel title="流程状态" subtitle="Claude → DSH → Codex（按事件流顺序还原）">
        {stepViews.length === 0 ? (
          <Empty>该 run 暂无 step run。</Empty>
        ) : (
          <ol className="space-y-2">
            {stepViews.map((view, index) => {
              const status = String(view.step.status)
              const isCurrent = currentStep?.step.id === view.step.id
              return (
                <li
                  key={view.step.id}
                  className={`rounded-lg border p-3 ${
                    isCurrent ? 'border-cyan-500/60 bg-cyan-500/5' : 'border-slate-800 bg-slate-950/40'
                  }`}
                >
                  <div className="flex items-start gap-3">
                    <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-800 text-xs text-slate-400">
                      {index + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-sm text-slate-100">{view.step.step_id}</span>
                        <Badge tone={stepTone(status)} mono>
                          {status}
                        </Badge>
                        <span className="text-xs text-slate-400">{roleLabel(view.task?.role_id)}</span>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                        {view.task ? (
                          <>
                            <span className="text-slate-300">{view.task.title}</span>
                            <Badge tone={sopTaskTone(String(view.task.status))} mono>
                              {String(view.task.status)}
                            </Badge>
                          </>
                        ) : (
                          <span className="text-slate-500">未绑定任务</span>
                        )}
                      </div>
                      <div className="mt-1.5 flex flex-wrap gap-3 text-[11px] text-slate-500">
                        <span>
                          step_run <code className="font-mono">{shortId(view.step.id, 6)}</code>
                        </span>
                        <span>产出 {view.task?.output_artifact_ids.length ?? 0} 个</span>
                      </div>
                    </div>
                  </div>
                </li>
              )
            })}
          </ol>
        )}

        {(tasks.data ?? []).length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {Array.from(new Set((tasks.data ?? []).map((task) => task.role_id))).map((roleId) => (
              <span
                key={roleId}
                className="rounded bg-slate-800 px-2 py-0.5 text-[11px] text-slate-400"
              >
                {roleShort(roleId)} · {roleId}
              </span>
            ))}
          </div>
        ) : null}
      </Panel>

      <Panel
        title="重试链（Attempt）"
        subtitle={
          chain.data ? `${chain.data.chain_length} 个 Attempt · 逻辑重试顺序只看 /chain` : undefined
        }
        dense
      >
        {chain.error ? <ErrorNotice error={chain.error} context="读取重试链" compact /> : null}
        {chain.loading && chain.data === undefined ? (
          <p className="text-sm text-slate-500">加载中…</p>
        ) : !chain.data || chain.data.attempts.length === 0 ? (
          <Empty>该 run 暂无 Attempt 血缘链。</Empty>
        ) : (
          <>
            {chain.refreshing ? (
              <p className="text-xs text-slate-500" role="status">
                数据刷新中…
              </p>
            ) : null}
            {chain.data.truncated ? (
              <div
                className="mb-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
                role="status"
              >
                previous_attempt_id 存在环路，链已被截断（不完整），展示内容可能缺项。
              </div>
            ) : null}
            <ol className="space-y-2">
              {chain.data.attempts.map((attempt, index) => {
                const chainTask = taskById.get(attempt.task_id)
                return (
                  <li
                    key={attempt.id}
                    className="rounded-lg border border-slate-800 bg-slate-950/40 p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-slate-500">{index + 1}.</span>
                      <code className="font-mono text-xs text-slate-100">{shortId(attempt.id, 8)}</code>
                      <Badge tone={sopTaskTone(attempt.status)} mono>
                        {attempt.status}
                      </Badge>
                      <span className="text-xs text-slate-400">seq {attempt.sequence}</span>
                      <span className="text-xs text-slate-400">
                        {chainTask ? roleLabel(chainTask.role_id) : '—'}
                      </span>
                    </div>
                    <div className="mt-1 text-[11px] text-slate-500">
                      {formatTime(attempt.started_at)} → {formatTime(attempt.completed_at)} · 产出{' '}
                      {attempt.artifact_ids.length} 个
                    </div>
                  </li>
                )
              })}
            </ol>
            <p className="mt-2 text-xs text-slate-500">
              root_task_id=<code className="font-mono">{shortId(chain.data.root_task_id, 8)}</code>
            </p>
          </>
        )}
      </Panel>

      <Panel title="失败与阻塞" subtitle={`${problems.length} 条`} dense>
        {problems.length === 0 ? (
          <Empty>没有检测到失败 / 阻塞 / 返工节点。</Empty>
        ) : (
          <DataTable head={['范围', '状态', '说明']}>
            {problems.map((item, index) => (
              <tr key={`${item.scope}-${index}`} className="bg-slate-900">
                <td className="px-3 py-2 font-mono text-xs text-slate-300">{item.scope}</td>
                <td className="px-3 py-2">
                  <Badge tone="danger" mono>
                    {item.status}
                  </Badge>
                </td>
                <td className="px-3 py-2 text-xs text-slate-400">{item.detail}</td>
              </tr>
            ))}
          </DataTable>
        )}
        <p className="text-xs text-slate-500">
          错误详情同时出现在「事件时间线」里带 error 字段的事件中。
        </p>
      </Panel>
    </div>
  )
}
