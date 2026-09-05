import { useEffect, useMemo, useState } from 'react'
import { Badge, Empty, Panel } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { formatTime, shortId } from '../../lib/format'
import { getArtifactContent } from '../../services/sopApi'
import type { ArtifactResponse } from '../../types/sop'

/** 与 useSopRunBundle 的资源形态对齐（只消费需要的字段）。 */
export interface ConversationResources {
  data: ArtifactResponse[] | undefined
  error: unknown
  loading: boolean
}

export interface SopConversationPanelProps {
  artifacts: ConversationResources
}

/** 角色视觉标识：Claude=紫 / DSH=蓝 / Codex=青 / Engine=灰。 */
const ROLE_STYLE: Record<string, string> = {
  claude: 'bg-violet-500/15 text-violet-200 border-violet-500/40',
  codex: 'bg-cyan-500/15 text-cyan-200 border-cyan-500/40',
  dsh: 'bg-sky-500/15 text-sky-200 border-sky-500/40',
}

function roleName(roleId: string | null): string {
  if (!roleId) return 'SOP Engine'
  const v = roleId.toLowerCase()
  if (v.includes('claude')) return 'Claude · 方案设计'
  if (v.includes('codex') || v.includes('review')) return 'Codex · 审核'
  if (v.includes('dsh') || v.includes('deepseek') || v.includes('harness')) return 'DSH · 执行'
  return roleId
}

function roleStyle(roleId: string | null): string {
  if (!roleId) return 'bg-slate-500/15 text-slate-200 border-slate-500/40'
  const v = roleId.toLowerCase()
  if (v.includes('claude')) return ROLE_STYLE.claude
  if (v.includes('codex') || v.includes('review')) return ROLE_STYLE.codex
  if (v.includes('dsh') || v.includes('deepseek') || v.includes('harness')) return ROLE_STYLE.dsh
  return 'bg-slate-500/15 text-slate-200 border-slate-500/40'
}

function typeLabel(t: string): string {
  const map: Record<string, string> = {
    plan: '方案 PLAN',
    implementation: '实现 CODE',
    test_report: '验证 TEST',
    review_report: '评审 REVIEW',
    diff: '变更 DIFF',
    text: '文本',
    file: '文件',
  }
  return map[t] ?? t
}

/** 解析 REVIEW_REPORT 契约 JSON → 人类可读的结论块。 */
interface ReviewData {
  result?: string
  blocking?: unknown
  non_blocking?: unknown
  evidence?: unknown
}

function parseReview(content: string): ReviewData | null {
  try {
    const raw = JSON.parse(content) as ReviewData
    return raw && typeof raw === 'object' ? raw : null
  } catch {
    return null
  }
}

function asList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

function ReviewReport({ content }: { content: string }) {
  const data = parseReview(content)
  if (!data) {
    return <pre className="whitespace-pre-wrap text-xs text-slate-300">{content}</pre>
  }
  const pass = data.result === 'PASS'
  const blk = asList(data.blocking)
  const nblk = asList(data.non_blocking)
  const ev = asList(data.evidence)
  return (
    <div className="space-y-2">
      <Badge tone={pass ? 'success' : 'warn'} mono>
        {data.result ?? 'unknown'}
      </Badge>
      {blk.length > 0 ? (
        <div className="rounded border border-red-500/40 bg-red-500/10 p-2">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-red-300">blocking</p>
          <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-red-100">
            {blk.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {nblk.length > 0 ? (
        <div className="rounded border border-slate-600/40 p-2">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">non-blocking</p>
          <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-slate-300">
            {nblk.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {ev.length > 0 ? (
        <div className="rounded border border-slate-700/40 p-2">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">evidence</p>
          <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-slate-400">
            {ev.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}

/** 单条 Artifact 渲染为一条对话消息（正文按需拉取）。 */
function ArtifactMessage({ artifact }: { artifact: ArtifactResponse }) {
  const [content, setContent] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    let alive = true
    getArtifactContent(artifact.id)
      .then((r) => {
        if (alive) {
          setContent(r?.content ?? null)
          setErr(null)
        }
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : '正文加载失败')
      })
      .finally(() => {
        if (alive) setLoaded(true)
      })
    return () => {
      alive = false
    }
  }, [artifact.id])

  const isReview = String(artifact.type) === 'review_report'
  const long = (content ?? '').length > 400
  return (
    <article className="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${roleStyle(artifact.role_id)}`}>
          {roleName(artifact.role_id)}
        </span>
        <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-300">
          {typeLabel(String(artifact.type))}
        </span>
        <Badge tone={artifact.accepted ? 'success' : 'warn'} mono>
          {artifact.accepted ? 'accepted' : 'rejected'}
        </Badge>
        <span className="ml-auto text-[11px] text-slate-500">
          {artifact.created_at ? formatTime(artifact.created_at) : ''}
        </span>
      </div>
      <p className="mt-1 font-mono text-[10px] text-slate-600">
        #{shortId(artifact.id, 8)}
        {artifact.attempt_id ? ` · attempt ${shortId(artifact.attempt_id, 8)}` : ''}
      </p>

      {!loaded ? (
        <p className="mt-2 text-xs text-slate-500">正文加载中…</p>
      ) : err ? (
        <p className="mt-2 text-xs text-red-300">{err}</p>
      ) : isReview && content ? (
        <div className="mt-2">
          <ReviewReport content={content} />
        </div>
      ) : content ? (
        long && !expanded ? (
          <div className="mt-2">
            <pre className="line-clamp-4 whitespace-pre-wrap rounded bg-slate-950/40 p-2 text-xs leading-relaxed text-slate-300">
              {content.slice(0, 400)}
            </pre>
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="mt-1 text-xs text-cyan-300 hover:underline"
            >
              展开全文（{content.length} 字）▾
            </button>
          </div>
        ) : (
          <div className="mt-2">
            <pre className="whitespace-pre-wrap rounded bg-slate-950/40 p-2 text-xs leading-relaxed text-slate-300">
              {content}
            </pre>
            {long ? (
              <button
                type="button"
                onClick={() => setExpanded(false)}
                className="mt-1 text-xs text-slate-400 hover:underline"
              >
                收起
              </button>
            ) : null}
          </div>
        )
      ) : (
        <p className="mt-2 text-xs italic text-slate-600">（正文为空）</p>
      )}
    </article>
  )
}

/**
 * 「讨论」对话记录视图：SOP run 的 Artifacts 按时间序渲染为
 * Claude / DSH / Codex 的对话消息流（形态与 Canvas 会话一致）。
 */
export function SopConversationPanel({ artifacts }: SopConversationPanelProps) {
  const ordered = useMemo(() => {
    const list = artifacts.data ?? []
    return [...list].sort((a, b) => {
      const t = (a.created_at ?? '').localeCompare(b.created_at ?? '')
      if (t !== 0) return t
      return String(a.type).localeCompare(String(b.type))
    })
  }, [artifacts.data])

  return (
    <div className="space-y-4">
      <ErrorNotice error={artifacts.error} context="读取讨论记录" />
      <Panel
        title="讨论记录"
        subtitle={artifacts.data ? `${ordered.length} 条 · SOP Engine 审计产物` : 'Claude → DSH → Codex'}
      >
        <p className="text-xs text-slate-500">
          每 5 秒自动拉取 SOP Engine 审计产物（Claude 方案 / DSH 执行 / Codex 审核），新 run 完成后会实时出现。
        </p>
        {artifacts.loading && !artifacts.data ? (
          <p className="text-sm text-slate-500">加载中…</p>
        ) : null}
        {ordered.length === 0 ? (
          <Empty>该 run 暂无产物（尚未执行到产出阶段）。</Empty>
        ) : (
          <ol className="space-y-3" aria-label="Agent 讨论记录">
            {ordered.map((artifact) => (
              <li key={artifact.id}>
                <ArtifactMessage artifact={artifact} />
              </li>
            ))}
          </ol>
        )}
      </Panel>
    </div>
  )
}
