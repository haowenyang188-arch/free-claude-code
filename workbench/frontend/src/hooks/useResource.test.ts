import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useResource } from './useResource'

describe('useResource（Phase 5A 前端契约：竞态 / 轮询 / 刷新 / 失败保数据）', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('run 切换后迟到响应不得污染新数据（请求竞态）', async () => {
    let resolveOld!: (value: string) => void
    let resolveNew!: (value: string) => void
    const loader = vi
      .fn()
      .mockImplementationOnce(() => new Promise<string>((resolve) => { resolveOld = resolve }))
      .mockImplementationOnce(() => new Promise<string>((resolve) => { resolveNew = resolve }))

    const { result, rerender } = renderHook(
      ({ id }) => useResource(() => loader(id), [id], { intervalMs: null }),
      { initialProps: { id: 'run-1' } },
    )
    await act(async () => {})

    rerender({ id: 'run-2' })
    await act(async () => {})

    // 旧请求（run-1）迟到返回，必须被 requestId 守卫丢弃
    await act(async () => {
      resolveOld('OLD')
      await Promise.resolve()
    })
    expect(result.current.data).toBeUndefined()

    await act(async () => {
      resolveNew('NEW')
      await Promise.resolve()
    })
    expect(result.current.data).toBe('NEW')
  })

  it('intervalMs 为 null 时轮询关闭：只加载一次', async () => {
    vi.useFakeTimers()
    const loader = vi.fn().mockResolvedValue('once')
    const { result } = renderHook(() => useResource(loader, [], { intervalMs: null }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(loader).toHaveBeenCalledTimes(1)
    expect(result.current.data).toBe('once')
  })

  it('手动 refresh 会重新加载', async () => {
    const loader = vi.fn().mockResolvedValue('v1')
    const { result } = renderHook(() => useResource(loader, [], { intervalMs: null }))
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))

    loader.mockResolvedValue('v2')
    await act(async () => {
      result.current.refresh()
    })
    await waitFor(() => expect(result.current.data).toBe('v2'))
    expect(loader).toHaveBeenCalledTimes(2)
  })

  it('资源禁用后 in-flight 响应不得回写（迟到响应防护）', async () => {
    let resolveOld!: (value: string) => void
    const loader = vi
      .fn()
      .mockImplementationOnce(() => new Promise<string>((resolve) => { resolveOld = resolve }))
    const { result, rerender } = renderHook(
      ({ enabled }) => useResource(() => loader('x'), ['x'], { intervalMs: null, enabled }),
      { initialProps: { enabled: true } },
    )
    await act(async () => {}) // 触发首次 load（挂起）

    rerender({ enabled: false }) // 请求期间禁用
    await act(async () => {
      resolveOld('LATE')
      await Promise.resolve()
    })
    // 迟到响应必须被丢弃，不能覆盖禁用时清空的数据
    expect(result.current.data).toBeUndefined()
    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('资源禁用后迟到 rejection 不回写 error（catch 分支守卫）', async () => {
    let rejectOld!: (value: Error) => void
    const loader = vi
      .fn()
      .mockImplementationOnce(() => new Promise<string>((_, reject) => { rejectOld = reject }))
    const { result, rerender } = renderHook(
      ({ enabled }) => useResource(() => loader('x'), ['x'], { intervalMs: null, enabled }),
      { initialProps: { enabled: true } },
    )
    await act(async () => {}) // 触发首次 load（挂起）

    rerender({ enabled: false })
    await act(async () => {
      rejectOld(new Error('late rejection'))
      await Promise.resolve()
    })
    // 迟到的 rejection 必须被丢弃，不能把禁用前状态恢复成 error
    expect(result.current.error).toBeUndefined()
    expect(result.current.data).toBeUndefined()
  })

  it('enabled 变 false 时清空数据（run 切换跨 run 残留防护）', async () => {
    const loader = vi.fn().mockResolvedValue('data-A')
    const { result, rerender } = renderHook(
      ({ id }) => useResource(() => loader(id), [id], { intervalMs: null, enabled: Boolean(id) }),
      { initialProps: { id: 'run-A' } },
    )
    await waitFor(() => expect(result.current.data).toBe('data-A'))

    rerender({ id: '' })
    await waitFor(() => expect(result.current.data).toBeUndefined())
    expect(result.current.loading).toBe(false)
  })

  it('刷新失败时保留上一次成功数据（瞬时 401 不清空页面）', async () => {
    const loader = vi.fn().mockResolvedValueOnce('stable').mockRejectedValueOnce(new Error('boom'))
    const { result } = renderHook(() => useResource(loader, [], { intervalMs: null }))
    await waitFor(() => expect(result.current.data).toBe('stable'))

    await act(async () => {
      result.current.refresh()
    })
    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error))
    expect(result.current.data).toBe('stable')
    expect(result.current.loading).toBe(false)
  })
})
