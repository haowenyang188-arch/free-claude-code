import { useState } from 'react'
import { Button, Field, Hint, Panel, inputClass } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { registerSopDefinition } from '../../services/sopApi'
import { errorMessage } from '../../services/errors'
import type { SopDefinition, StageDefinition } from '../../types/sop'

/**
 * 能力 1（前半）——注册 SOP 定义：POST /api/sop-definitions。
 *
 * 后端 `POST /api/sop-runs` 只接受已经存在于 service.sop_definitions 里的定义 id，
 * 所以「先注册、再下发」是唯一可行路径。body 即完整的 domain SopDefinition。
 *
 * 这里的默认模板是 Claude(设计) → DSH(执行) → Codex(审核) → DSH(返工) → Codex(终审)
 * 五步闭环，仅作起点，stages 可在文本框里改。
 */

const DEFAULT_STAGES: StageDefinition[] = [
  {
    id: 'stage-plan',
    name: '方案设计',
    steps: [
      {
        id: 'step-plan',
        name: '产出 PLAN',
        role_id: 'claude',
        output_type: 'plan',
        execution_mode: 'sequential',
        requires_review: false,
        retry_limit: 1,
        handoff_to: 'step-implement',
        instructions: '基于目标与验收标准产出可执行的 PLAN，明确改动范围与验收口径。',
        acceptance_criteria: [
          { id: 'ac-plan-1', description: 'PLAN 覆盖全部验收标准', required: true },
        ],
      },
    ],
  },
  {
    id: 'stage-execute',
    name: '实现',
    steps: [
      {
        id: 'step-implement',
        name: '按 PLAN 实现',
        role_id: 'dsh',
        output_type: 'implementation',
        execution_mode: 'sequential',
        requires_review: true,
        retry_limit: 1,
        depends_on: ['step-plan'],
        handoff_to: 'step-review',
        instructions: '严格按 PLAN 实现，不得扩大范围；完成后提交 Review 请求。',
        acceptance_criteria: [
          { id: 'ac-impl-1', description: '实现与 PLAN 一致', required: true },
        ],
      },
    ],
  },
  {
    id: 'stage-review',
    name: '审核与返工',
    steps: [
      {
        id: 'step-review',
        name: 'Codex 审核',
        role_id: 'codex',
        output_type: 'review_report',
        execution_mode: 'sequential',
        requires_review: true,
        depends_on: ['step-implement'],
        handoff_to: 'step-rework',
        instructions: '对照验收标准审核实现，不通过则给出可执行的返工项。',
        acceptance_criteria: [
          { id: 'ac-review-1', description: '给出明确的 pass / rework 结论', required: true },
        ],
      },
      {
        id: 'step-rework',
        name: '同会话返工',
        role_id: 'dsh',
        output_type: 'implementation',
        execution_mode: 'sequential',
        requires_review: true,
        depends_on: ['step-review'],
        handoff_to: 'step-final-review',
        instructions: '针对审核意见在同一次会话内返工，避免上下文丢失。',
      },
      {
        id: 'step-final-review',
        name: '二次审核',
        role_id: 'codex',
        output_type: 'final_summary',
        execution_mode: 'sequential',
        requires_review: true,
        depends_on: ['step-rework'],
        instructions: '复核返工结果，通过则 PASS 并收尾。',
      },
    ],
  },
]

export function RegisterSopDefinitionForm({
  onRegistered,
}: {
  onRegistered?: (id: string) => void
}) {
  const [id, setId] = useState('sop-claude-dsh-codex')
  const [name, setName] = useState('Claude → DSH → Codex 固定流水线')
  const [version, setVersion] = useState('1')
  const [stagesText, setStagesText] = useState(() => JSON.stringify(DEFAULT_STAGES, null, 2))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(undefined)
  const [note, setNote] = useState<string | null>(null)

  const parsed = ((): { stages: StageDefinition[] } | { error: string } => {
    try {
      const value = JSON.parse(stagesText) as unknown
      if (!Array.isArray(value)) return { error: 'stages 必须是数组' }
      return { stages: value as StageDefinition[] }
    } catch (err) {
      return { error: `stages 不是合法 JSON：${errorMessage(err)}` }
    }
  })()

  async function submit() {
    if ('error' in parsed) {
      setError(new Error(parsed.error))
      return
    }
    setBusy(true)
    setError(undefined)
    setNote(null)
    try {
      const body: SopDefinition = {
        id: id.trim(),
        name: name.trim() || id.trim(),
        version: Number(version) || 1,
        origin: 'fixed',
        description: '由 SOP 控制台注册的固定流水线定义',
        stages: parsed.stages,
      }
      const result = await registerSopDefinition(body)
      setNote(`已注册定义 ${result.id}（v${String(result.version)}），现在可以下发任务。`)
      onRegistered?.(result.id)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel
      title="注册 SOP 定义"
      subtitle="POST /api/sop-definitions（下发前必须先注册）"
      dense
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Field label="定义 ID">
          <input className={inputClass} value={id} onChange={(e) => setId(e.target.value)} spellCheck={false} />
        </Field>
        <Field label="名称">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="version">
          <input className={inputClass} value={version} onChange={(e) => setVersion(e.target.value)} />
        </Field>
      </div>

      <Field label="stages（JSON 数组，StepDefinition 结构）" span={2} hint={STAGES_FIELD_HINT}>
        <textarea
          className={`${inputClass} h-56 font-mono text-[11px] leading-relaxed`}
          value={stagesText}
          onChange={(e) => setStagesText(e.target.value)}
          spellCheck={false}
        />
      </Field>

      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" onClick={() => void submit()} disabled={busy || !id.trim()}>
          {busy ? '注册中…' : '注册定义'}
        </Button>
        <Button kind="ghost" onClick={() => setStagesText(JSON.stringify(DEFAULT_STAGES, null, 2))}>
          重置为默认模板
        </Button>
      </div>

      <ErrorNotice error={error} context="注册 SOP 定义" compact />
      {'error' in parsed ? <Hint tone="error">{parsed.error}</Hint> : null}
      {note ? (
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
          {note}
        </div>
      ) : null}
    </Panel>
  )
}

export const STAGES_FIELD_HINT =
  '字段：id / name / role_id / output_type / depends_on / execution_mode / requires_review / handoff_to / instructions / acceptance_criteria'
