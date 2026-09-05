import { useEffect, useState } from 'react'
import { Badge, Button, Field, Hint, Panel, inputClass } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { startSopRun } from '../../services/sopApi'
import type { SopDefinitionSummary, StartSopRunResponse } from '../../types/sop'

function linesToArray(value: string): string[] {
  return value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/**
 * 能力 1（后半）——创建并下发任务：POST /api/sop-runs。
 *
 * 定义 ID 必须来自 GET /api/sop-definitions；后端在
 * `sop_definition_id not in service.sop_definitions` 时返回 404
 * 「SOP definition not found」，此时请先用上面的表单注册定义。
 */
export function CreateSopRunForm({
  definitions,
  definitionsError,
  onReloadDefinitions,
  defaultDefinitionId,
  onCreated,
  onSelect,
}: {
  definitions: SopDefinitionSummary[]
  definitionsError: unknown
  onReloadDefinitions: () => void
  defaultDefinitionId?: string
  onCreated: (id: string, note?: string) => void
  onSelect: (id: string) => void
}) {
  const [sopDefinitionId, setSopDefinitionId] = useState(defaultDefinitionId ?? '')
  const [projectId, setProjectId] = useState('default')
  const [goal, setGoal] = useState('')
  const [criteria, setCriteria] = useState('')
  const [constraints, setConstraints] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(undefined)
  const [result, setResult] = useState<StartSopRunResponse | null>(null)

  // 定义列表异步到达后，若当前选择为空则自动选中第一个。
  useEffect(() => {
    if (sopDefinitionId) return
    if (definitions.length === 0) return
    const hit = definitions.find((item) => item.id === defaultDefinitionId) ?? definitions[0]
    setSopDefinitionId(hit.id)
  }, [definitions, defaultDefinitionId, sopDefinitionId])

  const selected = definitions.find((item) => item.id === sopDefinitionId)

  async function submit() {
    setBusy(true)
    setError(undefined)
    setResult(null)
    try {
      const response = await startSopRun({
        goal_description: goal.trim(),
        sop_definition_id: sopDefinitionId.trim(),
        project_id: projectId.trim() || 'default',
        acceptance_criteria: linesToArray(criteria),
        constraints: linesToArray(constraints),
        auto_execute: true,
      })
      setResult(response)
      onCreated(response.sop_run_id, sopDefinitionId.trim())
      onSelect(response.sop_run_id)
      setGoal('')
      setCriteria('')
      setConstraints('')
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel
      title="创建 / 下发任务"
      subtitle="POST /api/sop-runs"
      actions={
        <Button kind="ghost" onClick={onReloadDefinitions}>
          重新拉取定义
        </Button>
      }
    >
      <ErrorNotice error={definitionsError} context="读取 /api/sop-definitions" compact />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="SOP 定义 ID">
          {definitions.length > 0 ? (
            <select
              className={inputClass}
              value={sopDefinitionId}
              onChange={(e) => setSopDefinitionId(e.target.value)}
            >
              {definitions.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.id} · {item.name}（{item.step_count} 步）
                </option>
              ))}
            </select>
          ) : (
            <input
              className={inputClass}
              value={sopDefinitionId}
              onChange={(e) => setSopDefinitionId(e.target.value)}
              spellCheck={false}
            />
          )}
        </Field>
        <Field label="项目 ID">
          <input
            className={inputClass}
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            spellCheck={false}
          />
        </Field>
      </div>

      {selected ? (
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
          <Badge tone="accent">v{String(selected.version)}</Badge>
          <span>{selected.step_count} 步</span>
          <span className="break-all font-mono text-slate-500">{selected.steps.join(' → ')}</span>
        </div>
      ) : (
        <Hint>没有已注册的定义。请先在上方「注册 SOP 定义」里注册一个。</Hint>
      )}

      <Field label="目标描述" hint="必填">
        <textarea
          className={`${inputClass} h-24`}
          value={goal}
          placeholder="让 SOP 引擎下发的目标"
          onChange={(e) => setGoal(e.target.value)}
        />
      </Field>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="验收标准（每行一条）">
          <textarea
            className={`${inputClass} h-20`}
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
          />
        </Field>
        <Field label="约束（每行一条）">
          <textarea
            className={`${inputClass} h-20`}
            value={constraints}
            onChange={(e) => setConstraints(e.target.value)}
          />
        </Field>
      </div>

      <Button
        kind="primary"
        onClick={() => void submit()}
        disabled={busy || !goal.trim() || !sopDefinitionId.trim()}
      >
        {busy ? '下发中…' : '创建并下发'}
      </Button>

      <ErrorNotice error={error} context="创建 SOP run" compact />

      {result ? (
        <dl className="grid grid-cols-1 gap-2 rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 sm:grid-cols-3">
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-slate-500">sop_run_id</dt>
            <dd className="break-all font-mono text-xs text-emerald-200">{result.sop_run_id}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-slate-500">goal_id</dt>
            <dd className="break-all font-mono text-xs text-slate-300">{result.goal_id}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-slate-500">status</dt>
            <dd className="text-xs text-slate-300">{result.status}</dd>
          </div>
        </dl>
      ) : null}
    </Panel>
  )
}
