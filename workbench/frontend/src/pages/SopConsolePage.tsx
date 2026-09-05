import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge, Button } from '../components/ui'
import { ErrorNotice } from '../components/ErrorNotice'
import { useResource } from '../hooks/useResource'
import { useSopRunBundle } from '../hooks/useSopRunBundle'
import { api } from '../services/api'
import { dispatchSopHandoff, listSopDefinitions } from '../services/sopApi'
import { backendStateOf, type BackendState } from '../services/errors'
import {
  forgetRun,
  loadRecentRuns,
  loadSelectedRun,
  rememberRun,
  saveSelectedRun,
  type RecentRun,
} from '../lib/storage'
import { relativeTime } from '../lib/format'
import { SopRunSidebar } from '../components/sop/SopRunSidebar'
import { RegisterSopDefinitionForm } from '../components/sop/RegisterSopDefinitionForm'
import { CreateSopRunForm } from '../components/sop/CreateSopRunForm'
import { PipelinePanel } from '../components/sop/PipelinePanel'
import { ArtifactsPanel } from '../components/sop/ArtifactsPanel'
import { ReviewEvidencePanel } from '../components/sop/ReviewEvidencePanel'
import { HandoffsPanel } from '../components/sop/HandoffsPanel'
import { EventsPanel } from '../components/sop/EventsPanel'
import { RuntimePanel } from '../components/sop/RuntimePanel'
import { ApprovalsPanel } from '../components/sop/ApprovalsPanel'
import { ControlPanel } from '../components/sop/ControlPanel'
import { UsageGuidePanel } from '../components/sop/UsageGuidePanel'
import { CanvasSessionsCard } from '../components/sop/CanvasSessionsCard'
import { CollaborationTracePanel } from '../components/sop/CollaborationTracePanel'
import { SopConversationPanel } from '../components/sop/SopConversationPanel'
import type { HandoffResponse, SopHandoffDispatchResponse } from '../types/sop'

type TabKey =
  | 'collaboration'
  | 'conversation'
  | 'pipeline'
  | 'artifacts'
  | 'evidence'
  | 'handoffs'
  | 'events'
  | 'runtime'
  | 'approvals'
  | 'control'
  | 'dispatch'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'collaboration', label: '协作' },
  { key: 'conversation', label: '讨论' },
  { key: 'pipeline', label: '流程' },
  { key: 'artifacts', label: 'Artifact' },
  { key: 'evidence', label: '证据' },
  { key: 'handoffs', label: 'Handoff' },
  { key: 'events', label: '事件' },
  { key: 'runtime', label: 'Runtime' },
  { key: 'approvals', label: '审批' },
  { key: 'control', label: '控制' },
  { key: 'dispatch', label: '下发' },
]

const POLL_OPTIONS = [
  { label: '1s', value: 1000 },
  { label: '3s', value: 3000 },
  { label: '10s', value: 10000 },
  { label: '关闭', value: 0 },
]

const STATE_TEXT: Record<BackendState, string> = {
  checking: '检测中',
  ok: '后端已就绪',
  auth: '后端需要鉴权',
  unreachable: '后端未启动',
  error: '后端异常',
}

/**
 * SOP 控制台主入口：Claude → DSH → Codex 固定流水线的可视化与人工操控。
 * 后端不可用时仍然完整渲染（各面板自行降级），不白屏。
 */
export default function SopConsolePage() {
  const [runId, setRunId] = useState<string>(() => loadSelectedRun())
  const [recentRuns, setRecentRuns] = useState<RecentRun[]>(() => loadRecentRuns())
  const [tab, setTab] = useState<TabKey>('collaboration')
  const [pollMs, setPollMs] = useState(3000)
  const [refreshToken, setRefreshToken] = useState(0)
  const [dispatchingHandoffId, setDispatchingHandoffId] = useState<string | null>(null)
  const [dispatchResult, setDispatchResult] = useState<SopHandoffDispatchResponse | null>(null)
  const [dispatchError, setDispatchError] = useState<unknown>(undefined)
  const currentRunId = useRef(runId)
  const navigate = useNavigate()

  const auth = useResource(() => api.getAuthStatus(), [refreshToken], { intervalMs: 10000 })

  const backendState: BackendState = useMemo(() => {
    if (auth.error) return backendStateOf(auth.error)
    if (!auth.data) return 'checking'
    if (auth.data.required && !auth.data.authenticated) return 'auth'
    return 'ok'
  }, [auth.data, auth.error])

  const definitions = useResource(() => listSopDefinitions(), [refreshToken], {
    intervalMs: pollMs,
  })

  const bundle = useSopRunBundle(runId, pollMs, refreshToken)

  useEffect(() => {
    saveSelectedRun(runId)
    currentRunId.current = runId
    setDispatchResult(null)
    setDispatchError(undefined)
    setDispatchingHandoffId(null)
  }, [runId])

  const selectRun = useCallback((id: string) => {
    const trimmed = id.trim()
    if (!trimmed) return
    setRunId(trimmed)
    setRecentRuns(rememberRun(trimmed))
  }, [])

  const remember = useCallback((id: string, note?: string) => {
    setRecentRuns(rememberRun(id, note))
  }, [])

  const forget = useCallback((id: string) => {
    setRecentRuns(forgetRun(id))
  }, [])

  const refreshAll = useCallback(() => setRefreshToken((token) => token + 1), [])

  const dispatchPlan = useCallback(
    (handoff: HandoffResponse) => {
      if (!runId || dispatchingHandoffId) return
      setDispatchingHandoffId(handoff.id)
      setDispatchResult(null)
      setDispatchError(undefined)
      void dispatchSopHandoff(runId, handoff.id)
        .then((result) => {
          if (currentRunId.current !== runId) return
          setDispatchResult(result)
          bundle.refreshAll()
        })
        .catch((error: unknown) => {
          if (currentRunId.current === runId) setDispatchError(error)
        })
        .finally(() => {
          if (currentRunId.current === runId) setDispatchingHandoffId(null)
        })
    },
    [bundle, dispatchingHandoffId, runId],
  )

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/95 backdrop-blur">
        <div className="mx-auto max-w-[1600px] px-4 py-4 sm:px-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-cyan-400">
                SOP Workbench Console
              </p>
              <h1 className="mt-1 text-xl font-semibold tracking-tight sm:text-2xl">
                Claude → DSH → Codex 固定流水线
              </h1>
              <p className="mt-1 text-xs text-slate-500">可视化与人工操控 · SOP Engine 统一调度</p>
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <Link to="/agent" className="rounded-lg border border-cyan-400/60 bg-cyan-400/10 px-3 py-1.5 font-medium text-cyan-300 hover:border-cyan-300 hover:text-cyan-200">
                Agent 会话
              </Link>
              <Link to="/collector" className="rounded-lg border border-emerald-400/60 bg-emerald-400/10 px-3 py-1.5 font-medium text-emerald-300 hover:border-emerald-300 hover:text-emerald-200">
                采集中心
              </Link>
              <Link to="/dashboard" className="rounded-lg border border-slate-700 px-3 py-1.5 hover:border-slate-600 hover:text-slate-200">
                旧版仪表盘
              </Link>
              <label className="flex items-center gap-1.5">
                轮询
                <select
                  className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs outline-none focus:border-cyan-400"
                  value={pollMs}
                  onChange={(e) => setPollMs(Number(e.target.value))}
                >
                  {POLL_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <Button kind="ghost" onClick={refreshAll}>
                立即刷新
              </Button>
              <span className="text-slate-500">刷新于 {relativeTime(auth.lastUpdated)}</span>
              <Badge
                tone={
                  backendState === 'ok'
                    ? 'success'
                    : backendState === 'auth'
                      ? 'warn'
                      : backendState === 'checking'
                        ? 'idle'
                        : 'danger'
                }
              >
                {STATE_TEXT[backendState]}
              </Badge>
            </div>
          </div>

          <div className="mt-3 space-y-2">
            {backendState === 'unreachable' ? (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200" role="status">
                后端未启动：控制台已正常渲染，但所有数据请求都会失败。启动后端（127.0.0.1:8000）后点击「立即刷新」。
              </div>
            ) : null}
            {backendState === 'auth' ? (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200" role="status">
                后端已启用鉴权（TOKEN_FILE），所有 /api 请求返回 401 AUTH_REQUIRED。请返回登录页输入 token。
              </div>
            ) : null}
            {backendState === 'error' ? (
              <ErrorNotice error={auth.error} context="探测后端" compact />
            ) : null}
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1600px] grid-cols-1 gap-4 px-4 py-4 sm:px-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
        <aside className="space-y-4">
          <SopRunSidebar
            runId={runId}
            recentRuns={recentRuns}
            onSelectRun={selectRun}
            onForgetRun={forget}
            pollMs={pollMs}
            refreshToken={refreshToken}
          />
          {/* P1-A：Canvas 会话状态联动（A+ 最小版，只读展示） */}
          <CanvasSessionsCard
            linkedAcpSessionId={
              typeof bundle.run.data?.metadata?.canvas_acp_session_id === 'string'
                ? bundle.run.data.metadata.canvas_acp_session_id
                : null
            }
            onOpenSession={(id) => navigate(`/agent?c=${encodeURIComponent(id)}`)}
          />
        </aside>

        <main className="min-w-0">
          <nav
            className="mb-4 flex flex-wrap gap-1.5 border-b border-slate-800 pb-3"
            aria-label="SOP 控制台页签"
            role="tablist"
          >
            {TABS.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setTab(item.key)}
                id={`sop-tab-${item.key}`}
                data-testid={`sop-tab-${item.key}`}
                aria-selected={tab === item.key}
                aria-controls={`sop-panel-${item.key}`}
                role="tab"
                className={`rounded-lg px-3 py-1.5 text-sm transition ${
                  tab === item.key
                    ? 'bg-cyan-400 font-semibold text-slate-950'
                    : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>

          <div
            id={`sop-panel-${tab}`}
            role="tabpanel"
            aria-labelledby={`sop-tab-${tab}`}
            tabIndex={0}
          >
            {tab === 'collaboration' ? (
              <CollaborationTracePanel
                trace={bundle.trace}
                planHandoffs={bundle.handoffs.data}
                onDispatchPlan={dispatchPlan}
                dispatchingHandoffId={dispatchingHandoffId}
                dispatchResult={dispatchResult}
                dispatchError={dispatchError}
              />
            ) : null}
            {tab === 'conversation' ? (
              <SopConversationPanel artifacts={bundle.artifacts} />
            ) : null}
            {tab === 'pipeline' ? <PipelinePanel bundle={bundle} /> : null}
            {tab === 'artifacts' ? <ArtifactsPanel bundle={bundle} /> : null}
            {tab === 'evidence' ? <ReviewEvidencePanel bundle={bundle} /> : null}
            {tab === 'handoffs' ? <HandoffsPanel bundle={bundle} /> : null}
            {tab === 'events' ? <EventsPanel bundle={bundle} /> : null}
            {tab === 'runtime' ? <RuntimePanel pollMs={pollMs} refreshToken={refreshToken} /> : null}
            {tab === 'approvals' ? <ApprovalsPanel pollMs={pollMs} /> : null}
            {tab === 'control' ? (
              <ControlPanel bundle={bundle} pollMs={pollMs} refreshToken={refreshToken} />
            ) : null}
            {tab === 'dispatch' ? (
              <div className="space-y-4">
                <RegisterSopDefinitionForm onRegistered={() => definitions.refresh()} />
                <CreateSopRunForm
                  definitions={definitions.data ?? []}
                  definitionsError={definitions.error}
                  onReloadDefinitions={definitions.refresh}
                  onCreated={remember}
                  onSelect={selectRun}
                />
              </div>
            ) : null}
          </div>
        </main>
      </div>
      <UsageGuidePanel />
    </div>
  )
}
