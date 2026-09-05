import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ErrorNotice } from '../components/ErrorNotice'
import { Badge, Button, CodeBlock, Empty, Field, Hint, KeyValue, KeyValueGrid, Panel, inputClass } from '../components/ui'
import {
  runnerApi,
  type RunnerChannel,
  type RunnerHealth,
  type RunnerImportResult,
  type RunnerRun,
} from '../services/runnerApi'

const STATUS_LABEL: Record<RunnerRun['status'], string> = {
  starting: '启动中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  stopped: '已停止',
}

const STATUS_TONE: Record<RunnerRun['status'], 'accent' | 'active' | 'success' | 'danger' | 'warn'> = {
  starting: 'accent',
  running: 'active',
  completed: 'success',
  failed: 'danger',
  stopped: 'warn',
}

const TERMINAL = new Set<RunnerRun['status']>(['completed', 'failed', 'stopped'])

export default function CollectorPage() {
  const [health, setHealth] = useState<RunnerHealth | null>(null)
  const [healthError, setHealthError] = useState<unknown>(undefined)
  const [channel, setChannel] = useState<RunnerChannel>('xhs')
  const [limit, setLimit] = useState(20)
  const [boot, setBoot] = useState(true)
  const [restart, setRestart] = useState(false)
  const [smoke, setSmoke] = useState(false)
  const [killChrome, setKillChrome] = useState(false)
  const [run, setRun] = useState<RunnerRun | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const cursorRef = useRef(0)
  const [importResult, setImportResult] = useState<RunnerImportResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(undefined)

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await runnerApi.health())
      setHealthError(undefined)
    } catch (err) {
      setHealth(null)
      setHealthError(err)
    }
  }, [])

  useEffect(() => {
    void refreshHealth()
    const timer = window.setInterval(() => void refreshHealth(), 5000)
    return () => window.clearInterval(timer)
  }, [refreshHealth])

  useEffect(() => {
    if (!run || TERMINAL.has(run.status)) return
    let active = true
    const poll = async () => {
      try {
        const [nextRun, nextLogs] = await Promise.all([
          runnerApi.getRun(run.run_id),
          runnerApi.getLogs(run.run_id, cursorRef.current),
        ])
        if (!active) return
        setRun(nextRun)
        setLogs((previous) => (
          nextLogs.next < cursorRef.current ? nextLogs.lines : [...previous, ...nextLogs.lines]
        ))
        cursorRef.current = nextLogs.next
        setError(undefined)
      } catch (err) {
        if (active) setError(err)
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [run?.run_id, run?.status])

  async function start() {
    setBusy(true)
    setError(undefined)
    setImportResult(null)
    setLogs([])
    cursorRef.current = 0
    try {
      const next = await runnerApi.start({
        channel,
        boot,
        limit: Math.min(Math.max(limit, 1), 100),
        restart,
        smoke,
        kill_chrome: killChrome,
      })
      setRun(next)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (!run) return
    setBusy(true)
    setError(undefined)
    try {
      setRun(await runnerApi.stop(run.run_id, killChrome))
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function register() {
    if (!run) return
    setBusy(true)
    setError(undefined)
    try {
      setRun(await runnerApi.register(run.run_id))
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function importOutput() {
    if (!run) return
    setBusy(true)
    setError(undefined)
    try {
      const result = await runnerApi.importOutput(run.run_id)
      setRun(result)
      setImportResult(result)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const terminal = Boolean(run && TERMINAL.has(run.status))
  const connected = Boolean(health)

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1200px] flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-cyan-400">Local Execution</p>
            <h1 className="mt-1 text-xl font-semibold tracking-tight sm:text-2xl">采集中心</h1>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <Badge tone={connected ? 'success' : 'danger'}>{connected ? '执行层已连接' : '执行层未连接'}</Badge>
            <Link to="/" className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-500">
              返回工作台
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1200px] space-y-4 p-4 sm:p-6">
        <ErrorNotice error={healthError} context="连接本机执行层" compact />
        <ErrorNotice error={error} context="采集操作" compact />

        <Panel title="运行控制" subtitle="固定 channel → Chrome CDP → WSL scraper">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="平台">
              <select className={inputClass} value={channel} onChange={(event) => setChannel(event.target.value as RunnerChannel)} disabled={Boolean(run && !terminal)}>
                <option value="xhs">小红书 · CDP 9222</option>
                <option value="douyin">抖音 · CDP 9224</option>
              </select>
            </Field>
            <Field label="每关键词上限">
              <input className={inputClass} type="number" min={1} max={100} value={limit} onChange={(event) => setLimit(Number(event.target.value))} disabled={Boolean(run && !terminal)} />
            </Field>
            <label className="flex items-center gap-2 pt-6 text-sm text-slate-300">
              <input type="checkbox" checked={boot} onChange={(event) => setBoot(event.target.checked)} disabled={Boolean(run && !terminal)} />
              缺失时自动启动 Chrome
            </label>
            <label className="flex items-center gap-2 pt-6 text-sm text-slate-300">
              <input type="checkbox" checked={killChrome} onChange={(event) => setKillChrome(event.target.checked)} disabled={Boolean(run && !terminal)} />
              停止时清理本次 Chrome
            </label>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={restart} onChange={(event) => setRestart(event.target.checked)} disabled={Boolean(run && !terminal)} />
              忽略断点重新开始
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={smoke} onChange={(event) => setSmoke(event.target.checked)} disabled={Boolean(run && !terminal)} />
              冒烟运行（固定关键词）
            </label>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button kind="primary" onClick={() => void start()} disabled={busy || Boolean(run && !terminal) || !connected}>
              {busy ? '处理中…' : '开始采集'}
            </Button>
            <Button kind="danger" onClick={() => void stop()} disabled={busy || !run || terminal}>
              停止采集
            </Button>
            <Button kind="ghost" onClick={() => void refreshHealth()} disabled={busy}>刷新执行层</Button>
          </div>
          {!connected ? <Hint tone="error">本机执行层未连接，开始按钮保持禁用；启动 `runner/start_runner.bat` 后刷新。</Hint> : null}
        </Panel>

        <Panel title="真实运行状态" subtitle={run ? `run_id ${run.run_id}` : '尚未启动采集'}>
          {!run ? (
            <Empty>没有运行记录</Empty>
          ) : (
            <>
              <KeyValueGrid>
                <KeyValue label="状态"><Badge tone={STATUS_TONE[run.status]}>{STATUS_LABEL[run.status]}</Badge></KeyValue>
                <KeyValue label="PID">{run.pid ? `PID ${run.pid}` : '—'}</KeyValue>
                <KeyValue label="退出码">{run.exit_code ?? '—'}</KeyValue>
                <KeyValue label="平台">{run.channel}</KeyValue>
                <KeyValue label="Chrome">{run.chrome_booted ? '本次启动' : '复用已有'}</KeyValue>
                <KeyValue label="产出">{run.output_files.length ? run.output_files.join(', ') : '等待产出'}</KeyValue>
              </KeyValueGrid>
              {terminal ? (
                <div className="flex flex-wrap gap-2 border-t border-slate-800 pt-3">
                  <Button kind="ghost" onClick={() => void register()} disabled={busy || run.registered}>
                    {run.registered ? '已登记台账' : '登记到台账'}
                  </Button>
                  <Button kind="primary" onClick={() => void importOutput()} disabled={busy || run.imported}>
                    {run.imported ? '已导入产出' : '导入产出'}
                  </Button>
                </div>
              ) : null}
              {run.chrome_cleanup_error ? <p className="text-xs text-amber-300">Chrome 清理：{run.chrome_cleanup_error}</p> : null}
              {importResult ? <p className="text-xs text-emerald-300">已导入 {importResult.record_count} 条，SHA-256 {importResult.sha256}</p> : null}
            </>
          )}
        </Panel>

        <Panel title="实时日志" subtitle={logs.length ? `${logs.length} 行` : '等待 stdout'}>
          {logs.length ? <CodeBlock small>{logs.join('\n')}</CodeBlock> : <Empty>暂无日志</Empty>}
        </Panel>
      </main>
    </div>
  )
}
