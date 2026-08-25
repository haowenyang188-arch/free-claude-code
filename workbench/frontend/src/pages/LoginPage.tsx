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
        <form onSubmit={submit} className="space-y-4">
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
