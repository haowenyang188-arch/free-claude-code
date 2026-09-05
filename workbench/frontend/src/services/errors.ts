/**
 * 统一的错误分类。
 *
 * `services/api.ts` 的 requestJson 会抛出一个带 `status` 的 Error，
 * 网络层失败（后端没启动）时 fetch 直接 reject 成 TypeError。
 * 这里把两者归一化，供 UI 区分：
 *   - unreachable：后端未启动 / 代理目标拒绝连接
 *   - auth：后端启用 TOKEN_FILE，所有 /api 返回 401 AUTH_REQUIRED
 *   - error：真实的 4xx / 5xx
 * 目的：后端不可用时页面正常渲染并给出明确提示，绝不白屏。
 */

export const AUTH_REQUIRED = 'AUTH_REQUIRED'
export const BACKEND_UNREACHABLE = 'BACKEND_UNREACHABLE'

export type BackendState = 'checking' | 'ok' | 'auth' | 'unreachable' | 'error'

interface WithStatus {
  status?: number
}

function statusOf(error: unknown): number | undefined {
  if (error && typeof error === 'object') {
    const status = (error as WithStatus).status
    if (typeof status === 'number') return status
  }
  return undefined
}

function isNetworkFailure(error: unknown): boolean {
  if (error instanceof TypeError) return true
  if (error && typeof error === 'object') {
    const name = (error as { name?: unknown }).name
    if (name === 'TypeError') return true
  }
  return false
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  return String(error)
}

export function isAuthError(error: unknown): boolean {
  const status = statusOf(error)
  if (status === 401) return true
  return errorMessage(error).includes(AUTH_REQUIRED)
}

export function isUnreachableError(error: unknown): boolean {
  if (isNetworkFailure(error)) return true
  return statusOf(error) === 0
}

/** 把任意异常映射成顶部横幅所用的后端状态。 */
export function backendStateOf(error: unknown): BackendState {
  if (!error) return 'ok'
  if (isUnreachableError(error)) return 'unreachable'
  if (isAuthError(error)) return 'auth'
  return 'error'
}
