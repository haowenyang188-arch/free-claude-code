import { useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  CodeBlock,
  DataTable,
  Empty,
  KeyValue,
  KeyValueGrid,
  Panel,
  inputClass,
} from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { useResource } from '../../hooks/useResource'
import type { SopRunBundle } from '../../hooks/useSopRunBundle'
import { getArtifactContent } from '../../services/sopApi'
import { artifactTone, roleLabel, sopTaskTone } from '../../lib/labels'
import { formatTime, shortId } from '../../lib/format'
import type { ArtifactContentResponse, ArtifactResponse, AttemptResponse } from '../../types/sop'

type ViewMode = 'flat' | 'grouped'

/**
 * 能力 3 —— PLAN / 实现 / Review 等 Artifact：列表 + 点开看正文。
 * Phase 5A：血缘字段（attempt_id / producer_step_run_id / role_id）只从后端
 * Artifact DTO 取，不从 Task 推断；支持按 Attempt 分组（chain 顺序优先），
 * attempt_id === null 的 legacy Artifact 单独标注分组。
 */
export function ArtifactsPanel({ bundle }: { bundle: SopRunBundle }) {
  const { artifacts, attempts, chain, tasks, enabled } = bundle
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [view, setView] = useState<ViewMode>('grouped')

  // run 切换 / 刷新后选中的 Artifact 消失时自动清空选中态
  useEffect(() => {
    if (selectedId === null) return
    const exists = (artifacts.data ?? []).some((item) => item.id === selectedId)
    if (!exists) setSelectedId(null)
  }, [artifacts.data, selectedId])

  const selected = useMemo<ArtifactResponse | undefined>(
    () => (artifacts.data ?? []).find((item) => item.id === selectedId),
    [artifacts.data, selectedId],
  )

  const filtered = useMemo(() => {
    const keyword = filter.trim().toLowerCase()
    const list = artifacts.data ?? []
    if (!keyword) return list
    return list.filter((item) =>
      [
        item.type,
        item.summary ?? '',
        item.id,
        item.attempt_id ?? '',
        item.producer_step_run_id ?? '',
        item.role_id ?? '',
      ]
        .join(' ')
        .toLowerCase()
        .includes(keyword),
    )
  }, [artifacts.data, filter])

  const roleByTask = useMemo(() => {
    const map = new Map<string, string>()
    for (const task of tasks.data ?? []) map.set(task.id, roleLabel(task.role_id))
    return map
  }, [tasks.data])

  const attemptById = useMemo(() => {
    const map = new Map<string, AttemptResponse>()
    for (const attempt of attempts.data ?? []) map.set(attempt.id, attempt)
    return map
  }, [attempts.data])

  // 分组顺序：chain 顺序优先，其次 attempts API 顺序；attempt_id===null 归入 legacy
  const groups = useMemo(() => {
    const items = filtered
    const order = (chain.data?.attempts ?? []).map((attempt) => attempt.id)
    const orderedAttempts = [...(attempts.data ?? [])].sort((a, b) => {
      const ia = order.indexOf(a.id)
      const ib = order.indexOf(b.id)
      if (ia === -1 && ib === -1) return (a.started_at ?? '').localeCompare(b.started_at ?? '')
      if (ia === -1) return 1
      if (ib === -1) return -1
      return ia - ib
    })

    const result: { attempt: AttemptResponse | null; artifacts: ArtifactResponse[] }[] = []
    const usedAttemptIds = new Set<string>()
    const legacy: ArtifactResponse[] = []

    for (const attempt of orderedAttempts) {
      const groupItems = items.filter((item) => item.attempt_id === attempt.id)
      if (groupItems.length === 0) continue
      usedAttemptIds.add(attempt.id)
      result.push({ attempt, artifacts: groupItems })
    }
    for (const item of items) {
      if (item.attempt_id === null) {
        legacy.push(item)
      } else if (!usedAttemptIds.has(item.attempt_id) && !attemptById.has(item.attempt_id)) {
        legacy.push(item) // attempt 记录不存在（悬挂）也归入 legacy 标注
      }
    }
    if (legacy.length > 0) result.push({ attempt: null, artifacts: legacy })
    return result
  }, [filtered, chain.data, attempts.data, attemptById])

  if (!enabled) return <Empty>请先在左侧选择或创建一个 SOP Run。</Empty>

  return (
    <div className="space-y-4">
      <ErrorNotice error={artifacts.error ?? tasks.error ?? attempts.error} context="读取 artifact 列表" />

      <Panel
        title="Artifact 列表"
        subtitle={`${filtered.length} 条 · GET /api/sop-runs/{id}/artifacts`}
        actions={
          <>
            <input
              className={`${inputClass} w-48`}
              placeholder="按 type / summary / 血缘过滤"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <select
              className={`${inputClass} w-28`}
              value={view}
              onChange={(e) => setView(e.target.value as ViewMode)}
              aria-label="视图"
            >
              <option value="grouped">按 Attempt 分组</option>
              <option value="flat">平铺</option>
            </select>
            <Button kind="ghost" onClick={artifacts.refresh}>
              刷新
            </Button>
          </>
        }
      >
        {filtered.length === 0 ? (
          <Empty>该 run 暂无 artifact{filter ? '（与筛选条件不匹配）' : ''}。</Empty>
        ) : view === 'flat' ? (
          <FlatTable filtered={filtered} roleByTask={roleByTask} selectedId={selectedId} onSelect={setSelectedId} />
        ) : (
          <GroupedView
            groups={groups}
            roleByTask={roleByTask}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        )}
      </Panel>

      {selected ? (
        <ArtifactViewer artifact={selected} pollMs={0} onClose={() => setSelectedId(null)} />
      ) : (
        <Panel title="Artifact 正文" dense>
          <Empty>点击上表任意一行，读取 GET /api/artifacts/{'{id}'}/content 查看正文。</Empty>
        </Panel>
      )}
    </div>
  )
}

function FlatTable({
  filtered,
  roleByTask,
  selectedId,
  onSelect,
}: {
  filtered: ArtifactResponse[]
  roleByTask: Map<string, string>
  selectedId: string | null
  onSelect: (id: string | null) => void
}) {
  return (
    <DataTable head={['类型', '摘要', '产出角色', 'Attempt', '血缘 step_run', '接受', '创建时间']}>
      {filtered.map((artifact) => (
        <tr
          key={artifact.id}
          onClick={() => onSelect(artifact.id === selectedId ? null : artifact.id)}
          className={`cursor-pointer ${
            artifact.id === selectedId ? 'bg-cyan-500/10' : 'bg-slate-900 hover:bg-slate-800/60'
          }`}
        >
          <td className="px-3 py-2">
            <Badge tone={artifactTone(String(artifact.type))} mono>
              {String(artifact.type)}
            </Badge>
          </td>
          <td className="max-w-xs truncate px-3 py-2 text-xs text-slate-300">
            {artifact.summary ?? <span className="text-slate-600">无摘要</span>}
          </td>
          <td className="px-3 py-2 text-xs text-slate-500">
            {artifact.role_id ? roleLabel(artifact.role_id) : roleByTask.get(artifact.task_id) ?? '—'}
          </td>
          <td className="px-3 py-2 font-mono text-xs text-slate-500">
            {artifact.attempt_id ? shortId(artifact.attempt_id, 6) : <span className="text-slate-600">—</span>}
          </td>
          <td className="px-3 py-2 font-mono text-xs text-slate-500">
            {artifact.producer_step_run_id ? shortId(artifact.producer_step_run_id, 6) : '—'}
          </td>
          <td className="px-3 py-2">
            <Badge tone={artifact.accepted ? 'success' : 'idle'} mono>
              {artifact.accepted ? 'accepted' : 'pending'}
            </Badge>
          </td>
          <td className="px-3 py-2 text-xs text-slate-500">{formatTime(artifact.created_at)}</td>
        </tr>
      ))}
    </DataTable>
  )
}

function GroupedView({
  groups,
  roleByTask,
  selectedId,
  onSelect,
}: {
  groups: { attempt: AttemptResponse | null; artifacts: ArtifactResponse[] }[]
  roleByTask: Map<string, string>
  selectedId: string | null
  onSelect: (id: string | null) => void
}) {
  return (
    <div className="space-y-4">
      {groups.map((group, index) => {
        const attempt = group.attempt
        const legacy = attempt === null
        const seq = attempt ? attempt.sequence : null
        const roleId = attempt ? roleByTask.get(attempt.task_id) ?? null : null
        return (
          <div key={attempt?.id ?? `legacy-${index}`} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
              {legacy ? (
                <Badge tone="warn" mono>
                  legacy · 无 Attempt 血缘
                </Badge>
              ) : (
                <>
                  <span className="font-mono text-slate-200">{shortId(attempt.id, 8)}</span>
                  <Badge tone="accent" mono>
                    seq {seq}
                  </Badge>
                  <Badge tone={sopTaskTone(attempt.status)} mono>
                    {attempt.status}
                  </Badge>
                  {roleId ? <span className="text-slate-400">{roleLabel(roleId)}</span> : null}
                </>
              )}
              <span className="text-slate-600">{group.artifacts.length} 个</span>
            </div>
            <DataTable head={['类型', '摘要', '血缘 step_run', '接受', '创建时间']}>
              {group.artifacts.map((artifact) => (
                <tr
                  key={artifact.id}
                  onClick={() => onSelect(artifact.id === selectedId ? null : artifact.id)}
                  className={`cursor-pointer ${
                    artifact.id === selectedId ? 'bg-cyan-500/10' : 'bg-slate-900 hover:bg-slate-800/60'
                  }`}
                >
                  <td className="px-3 py-2">
                    <Badge tone={artifactTone(String(artifact.type))} mono>
                      {String(artifact.type)}
                    </Badge>
                  </td>
                  <td className="max-w-xs truncate px-3 py-2 text-xs text-slate-300">
                    {artifact.summary ?? <span className="text-slate-600">无摘要</span>}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-slate-500">
                    {artifact.producer_step_run_id ? shortId(artifact.producer_step_run_id, 6) : '—'}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={artifact.accepted ? 'success' : 'idle'} mono>
                      {artifact.accepted ? 'accepted' : 'pending'}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-500">{formatTime(artifact.created_at)}</td>
                </tr>
              ))}
            </DataTable>
          </div>
        )
      })}
    </div>
  )
}

function ArtifactViewer({
  artifact,
  pollMs,
  onClose,
}: {
  artifact: ArtifactResponse
  pollMs: number
  onClose: () => void
}) {
  const content = useResource<ArtifactContentResponse>(
    () => getArtifactContent(artifact.id),
    [artifact.id],
    { intervalMs: pollMs === 0 ? null : Math.max(pollMs, 15000) },
  )

  return (
    <Panel
      title={
        <span>
          Artifact 正文 <code className="font-mono text-xs">{shortId(artifact.id, 10)}</code>
        </span>
      }
      subtitle={String(artifact.type)}
      actions={
        <>
          <Button
            kind="ghost"
            onClick={() => {
              if (content.data?.content !== undefined) {
                void navigator.clipboard?.writeText(content.data.content)
              }
            }}
            disabled={content.data?.content === undefined}
          >
            复制
          </Button>
          <Button kind="ghost" onClick={content.refresh}>
            刷新
          </Button>
          <Button kind="ghost" onClick={onClose}>
            关闭
          </Button>
        </>
      }
    >
      <KeyValueGrid>
        <KeyValue label="id">
          <code className="break-all font-mono text-xs">{artifact.id}</code>
        </KeyValue>
        <KeyValue label="attempt">
          <code className="break-all font-mono text-xs">{artifact.attempt_id ?? '—'}</code>
        </KeyValue>
        <KeyValue label="血缘 step_run">
          <code className="break-all font-mono text-xs">{artifact.producer_step_run_id ?? '—'}</code>
        </KeyValue>
        <KeyValue label="uri">
          <code className="break-all font-mono text-xs">{artifact.uri ?? '—'}</code>
        </KeyValue>
        <KeyValue label="sha256">
          <code className="break-all font-mono text-xs">
            {content.data?.sha256 ?? artifact.sha256 ?? '—'}
          </code>
        </KeyValue>
        <KeyValue label="accepted">{artifact.accepted ? 'true' : 'false'}</KeyValue>
      </KeyValueGrid>

      <ErrorNotice error={content.error} context="读取 artifact 正文" compact />

      {content.loading && content.data === undefined ? (
        <p className="text-sm text-slate-500">加载正文…</p>
      ) : (
        <CodeBlock>{content.data?.content ?? ''}</CodeBlock>
      )}
    </Panel>
  )
}
