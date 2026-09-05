import { useCallback, useEffect, useRef, useState } from 'react'

export interface ResourceState<T> {
  data: T | undefined
  error: unknown
  loading: boolean
  refreshing: boolean
  lastUpdated: number | null
  refresh: () => void
}

export interface ResourceOptions {
  /** 轮询间隔（ms）。null 表示只手动刷新。 */
  intervalMs?: number | null
  /** 为 false 时完全不加载（例如还没选 run）。 */
  enabled?: boolean
}

/**
 * 轮询一个 loader，并在刷新失败时保留上一次成功的数据，
 * 这样一次瞬时 401 / 断连不会把控制台清成空白。
 */
export function useResource<T>(
  loader: () => Promise<T>,
  deps: readonly unknown[],
  options: ResourceOptions = {},
): ResourceState<T> {
  const { intervalMs = null, enabled = true } = options

  const [data, setData] = useState<T | undefined>(undefined)
  const [error, setError] = useState<unknown>(undefined)
  const [loading, setLoading] = useState(enabled)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [nonce, setNonce] = useState(0)

  const loaderRef = useRef(loader)
  loaderRef.current = loader

  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  const requestId = useRef(0)

  const load = useCallback(async () => {
    if (!enabled) return
    const id = ++requestId.current
    setRefreshing(true)
    try {
      const next = await loaderRef.current()
      if (id !== requestId.current) return
      // 资源在请求期间被禁用（例如 run 切换后 chain 入口变 null）：
      // 迟到的响应必须丢弃，否则会覆盖 disable 时清空的数据（跨 run 残留）。
      if (!enabledRef.current) return
      setData(next)
      setError(undefined)
      setLastUpdated(Date.now())
    } catch (err) {
      if (id !== requestId.current) return
      // 资源在请求期间被禁用：迟到的 rejection 也必须丢弃，不能恢复跨 run 残留。
      if (!enabledRef.current) return
      setError(err)
    } finally {
      if (id === requestId.current) {
        setRefreshing(false)
        setLoading(false)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      setData(undefined)
      setError(undefined)
      return
    }
    void load()
  }, [enabled, load])

  useEffect(() => {
    if (!enabled || !intervalMs) return
    const timer = window.setInterval(() => {
      if (document.hidden) return
      void load()
    }, intervalMs)
    return () => window.clearInterval(timer)
  }, [enabled, intervalMs, load])

  const refresh = useCallback(() => setNonce((n) => n + 1), [])

  return { data, error, loading, refreshing, lastUpdated, refresh }
}
