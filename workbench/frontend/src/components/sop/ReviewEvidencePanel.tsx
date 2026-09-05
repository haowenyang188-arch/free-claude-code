import { useMemo } from 'react'
import { Badge, Empty, Panel } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import { artifactTone } from '../../lib/labels'
import { formatTime, shortId } from '../../lib/format'
import type { ArtifactResponse, ReviewEvidenceResponse } from '../../types/sop'

const OUTCOME_TONE: Record<string, 'success' | 'warn' | 'danger' | 'idle'> = {
  PASS: 'success',
  REWORK: 'warn',
  PLAN_INVALID: 'warn',
}

/**
 * 评审证据面板（Phase 5A）：直接消费 GET /api/sop-runs/{id}/evidence。
 *
 * 绿色只允许 evidence_valid === true——不看 task.status === "accepted"，
 * 也不看 outcome === "PASS" 单独成立。evidence_complete 为 true 但 valid
 * 为 false 时用警示态并说明原因（跨 Attempt / 解析失败）。
 */
export function ReviewEvidencePanel({ bundle }: { bundle: SopRunBundle }) {
  const { evidence, tasks, enabled } = bundle

  const roleByTask = useMemo(() => {
    const map = new Map<string, string>()
    for (const task of tasks.data ?? []) map.set(task.id, task.role_id)
    return map
  }, [tasks.data])

  if (!enabled) return <Empty>请先在左侧选择或创建一个 SOP Run。</Empty>

  return (
    <div className="space-y-4">
      <ErrorNotice error={evidence.error} context="读取评审证据" />

      <Panel
        title="评审证据"
        subtitle={evidence.data ? `${evidence.data.length} 条 · GET /api/sop-runs/{id}/evidence` : undefined}
      >
        {evidence.loading && evidence.data === undefined ? (
          <p className="text-sm text-slate-500">加载中…</p>
        ) : !evidence.data || evidence.data.length === 0 ? (
          <Empty>该 run 暂无评审证据（没有 Review 记录）。</Empty>
        ) : (
          <div className="space-y-3">
            {evidence.data.map((item) => (
              <EvidenceCard key={item.review_id} item={item} roleByTask={roleByTask} />
            ))}
          </div>
        )}
      </Panel>
    </div>
  )
}

function EvidenceCard({
  item,
  roleByTask,
}: {
  item: ReviewEvidenceResponse
  roleByTask: Map<string, string>
}) {
  const legacy = item.reviewed_attempt_id === null
  const completeButInvalid = item.evidence_complete && !item.evidence_valid

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm text-slate-100">{shortId(item.review_id, 8)}</span>
        <Badge
          tone={
            item.evidence_valid && item.review_status === 'approved'
              ? 'success'
              : item.review_status === 'rejected'
                ? 'danger'
                : 'warn'
          }
          mono
        >
          {item.review_status}
        </Badge>
        {legacy ? (
          <Badge tone="warn" mono>
            legacy · 无 Attempt 血缘
          </Badge>
        ) : null}
        {item.evidence_valid ? (
          <Badge tone="success" mono>
            evidence valid
          </Badge>
        ) : (
          <Badge tone={completeButInvalid ? 'warn' : 'danger'} mono>
            {completeButInvalid ? '证据不完整/无效' : 'invalid'}
          </Badge>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-400">
        <span>
          被评审 Attempt{' '}
          <code className="font-mono text-slate-200">{item.reviewed_attempt_id ? shortId(item.reviewed_attempt_id, 8) : '—'}</code>
        </span>
        <span>
          评审 Attempt{' '}
          <code className="font-mono text-slate-200">{item.reviewer_attempt_id ? shortId(item.reviewer_attempt_id, 8) : '—'}</code>
        </span>
        <span>{formatTime(item.created_at)}</span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-slate-500">结论</span>
        {item.outcome ? (
          <Badge tone={item.evidence_valid ? OUTCOME_TONE[item.outcome] ?? 'idle' : 'warn'} mono>
            {item.outcome}
          </Badge>
        ) : (
          <Badge tone="danger" mono>
            无有效结论
          </Badge>
        )}
        {item.outcome && !item.evidence_valid ? (
          <span className="text-amber-300">（无效证据）</span>
        ) : null}
        {item.outcome_parse_error ? (
          <span className="text-amber-300" title={item.outcome_parse_error}>
            解析失败：{item.outcome_parse_error}
          </span>
        ) : null}
      </div>

      {item.blocking_items.length > 0 ? (
        <ul className="mt-2 list-inside list-disc space-y-0.5 text-xs text-amber-200">
          {item.blocking_items.map((blocking, index) => (
            <li key={index}>{blocking}</li>
          ))}
        </ul>
      ) : null}

      {completeButInvalid ? (
        <div
          className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200"
          role="status"
        >
          证据三件套齐全但无效：DIFF / TEST_REPORT 未绑定到被评审 Attempt，或评审结论解析失败。
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        <ArtifactChip label="DIFF" artifact={item.execution_diff} roleByTask={roleByTask} />
        <ArtifactChip label="TEST" artifact={item.execution_test_report} roleByTask={roleByTask} />
        <ArtifactChip label="REVIEW" artifact={item.reviewer_report} roleByTask={roleByTask} />
      </div>
    </div>
  )
}

function ArtifactChip({
  label,
  artifact,
  roleByTask,
}: {
  label: string
  artifact: ArtifactResponse | null
  roleByTask: Map<string, string>
}) {
  if (!artifact) {
    return (
      <span className="rounded bg-slate-800/60 px-2 py-1 text-[11px] text-slate-500">
        {label}：缺失
      </span>
    )
  }
  const roleId = roleByTask.get(artifact.task_id)
  return (
    <span className="rounded bg-slate-800 px-2 py-1 text-[11px] text-slate-400">
      <Badge tone={artifactTone(String(artifact.type))} mono>
        {label}
      </Badge>{' '}
      <code className="font-mono text-slate-200">{shortId(artifact.id, 8)}</code>
      {roleId ? <span className="ml-1 text-slate-500">· {roleId}</span> : null}
    </span>
  )
}
