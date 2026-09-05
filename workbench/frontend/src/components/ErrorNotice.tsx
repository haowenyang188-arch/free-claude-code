import { backendStateOf, errorMessage } from '../services/errors'

const TONE = {
  auth: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
  unreachable: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
  error: 'border-rose-500/40 bg-rose-500/10 text-rose-200',
  ok: 'border-slate-700 bg-slate-800/60 text-slate-300',
}

/**
 * 非阻塞的显式提示：鉴权 / 连通性 / 真实 API 错误都收敛在这里，
 * 保证后端不可用时页面其余部分照常渲染。
 */
export function ErrorNotice({
  error,
  context,
  compact = false,
}: {
  error: unknown
  context: string
  compact?: boolean
}) {
  if (error === undefined || error === null) return null
  const state = backendStateOf(error)

  if (state === 'auth') {
    return (
      <div className={`rounded-lg border px-3 py-2 text-xs ${TONE.auth} ${compact ? 'py-1.5' : ''}`} role="status">
        <strong className="font-semibold">需要鉴权</strong>
        <span className="ml-2">
          {context}：后端返回 401 AUTH_REQUIRED（launcher TOKEN_FILE 已启用）。请先在登录页输入 token。
        </span>
      </div>
    )
  }

  if (state === 'unreachable') {
    return (
      <div
        className={`rounded-lg border px-3 py-2 text-xs ${TONE.unreachable} ${compact ? 'py-1.5' : ''}`}
        role="status"
      >
        <strong className="font-semibold">后端未就绪</strong>
        <span className="ml-2">
          {context}：无法连接后端 /api（vite 代理目标 127.0.0.1:8000）。页面其余部分仍可浏览。
        </span>
      </div>
    )
  }

  return (
    <div className={`rounded-lg border px-3 py-2 text-xs ${TONE.error} ${compact ? 'py-1.5' : ''}`} role="alert">
      <strong className="font-semibold">{context}失败</strong>
      <span className="ml-2 break-words">{errorMessage(error)}</span>
    </div>
  )
}
