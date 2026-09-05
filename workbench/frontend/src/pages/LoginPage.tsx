import { FormEvent, useState } from 'react'
import { api } from '../services/api'

interface Props {
  onAuthenticated: () => void
}

export default function LoginPage({ onAuthenticated }: Props) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!token.trim()) return
    setSubmitting(true)
    setError('')
    try {
      await api.login(token.trim())
      onAuthenticated()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="min-h-screen bg-slate-950 px-4 py-10 text-slate-100 sm:flex sm:items-center sm:justify-center">
      <section className="mx-auto w-full max-w-md rounded-xl border border-slate-800 bg-slate-900 p-6 shadow-xl sm:p-8">
        <div className="mb-8">
          <p className="mb-2 text-xs font-semibold uppercase tracking-[0.2em] text-cyan-400">WSL Control Plane</p>
          <h1 className="text-2xl font-semibold tracking-tight">智能体工作台</h1>
          <p className="mt-2 text-sm leading-6 text-slate-400">输入本机 Workbench token，建立安全的手机控制会话。</p>
        </div>
        <aside
          data-testid="login-token-help"
          className="mb-6 space-y-2 rounded-lg border border-cyan-900/60 bg-cyan-950/20 px-3 py-3 text-xs leading-5 text-slate-400"
          aria-labelledby="login-token-help-title"
        >
          <h2 id="login-token-help-title" className="text-sm font-medium text-slate-200">访问 token 是什么？</h2>
          <p>它是启动脚本会在用户运行时目录生成的 Workbench 登录凭证，只用于 <code className="font-mono text-cyan-300">http://127.0.0.1:3000</code>。</p>
          <ol className="list-decimal space-y-1 pl-4">
            <li>先进入仓库根目录：<code className="font-mono text-cyan-300">cd /home/gnen/free-claude-code</code>。</li>
            <li>运行 <code className="font-mono text-cyan-300">./workbench/launcher.sh start</code>。</li>
            <li>启动命令会在当前终端显示 token；已运行时再次执行 <code className="font-mono text-cyan-300">./workbench/launcher.sh start</code> 也会显示当前 token。</li>
            <li>只读查看服务状态可运行 <code className="font-mono text-cyan-300">./workbench/launcher.sh status</code>。</li>
          </ol>
          <p className="text-slate-500">它不是 Agent Canvas 的会话密钥（Agent Canvas 入口是 8010）。本地模式会自动注入自己的密钥，不要把两个 token 互相粘贴。</p>
          <p className="text-slate-500">完整说明见仓库中的 <code className="font-mono text-cyan-300">workbench/docs/AGENT-CANVAS-OPERATIONS.md</code>。</p>
        </aside>
        <form onSubmit={submit} className="space-y-4">
          <input
            type="text"
            name="username"
            autoComplete="username"
            value="workbench"
            readOnly
            tabIndex={-1}
            aria-hidden="true"
            className="sr-only"
          />
          <div>
            <label htmlFor="workbench-token" className="mb-2 block text-sm font-medium text-slate-200">访问 token</label>
            <input
              id="workbench-token"
              type="password"
              autoComplete="current-password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-3 text-sm outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-400/20"
              placeholder="WORKBENCH_AUTH_TOKEN"
            />
          </div>
          {error && <p role="alert" className="rounded-lg border border-rose-900/70 bg-rose-950/40 px-3 py-2 text-sm text-rose-300">{error}</p>}
          <button
            type="submit"
            disabled={!token.trim() || submitting}
            className="w-full rounded-lg bg-cyan-400 px-4 py-3 text-sm font-semibold text-slate-950 transition hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? '验证中…' : '进入工作台'}
          </button>
        </form>
      </section>
    </main>
  )
}
