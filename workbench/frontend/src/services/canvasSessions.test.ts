import { describe, expect, it, vi } from 'vitest'
import { createCanvasSessionClient } from './canvasSessions'
import { toConversationRunSummaryView, type ConversationSession } from '../types/conversation'

function fakeConversation(overrides: Partial<ConversationSession> = {}): ConversationSession {
  return {
    id: 'conv-1',
    workspace: { working_dir: '/ws/conv-1', kind: 'LocalWorkspace' },
    persistence_dir: '/p/conv-1',
    max_iterations: 100,
    stuck_detection: true,
    execution_status: 'finished',
    ...overrides,
  }
}

/**
 * Conversation client 边界（P0）：
 *  - base 必须是 /integrations/canvas（P1-A 由 8000 透传 18000 的挂载点）；
 *  - 请求形状与 agent-server /openapi.json 对齐（路径 + ids 查询）；
 *  - 失败显式抛错，不做本地兜底（状态属主纪律）。
 */
describe('canvasSessions client 边界', () => {
  it('getSession 命中 /integrations/canvas/conversations/{id}', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(fakeConversation()), { status: 200 }))
    const client = createCanvasSessionClient(fetchMock as unknown as typeof fetch)
    const session = await client.getSession('conv-1')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/integrations/canvas/conversations/conv-1')
    expect(session.id).toBe('conv-1')
  })

  it('listSessions 用 ids 查询（对齐 agent-server：无 ids 会 422）', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([fakeConversation()]), { status: 200 }))
    const client = createCanvasSessionClient(fetchMock as unknown as typeof fetch)
    const sessions = await client.listSessions(['a', 'b'])
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/integrations/canvas/conversations?ids=a&ids=b')
    expect(sessions).toHaveLength(1)
  })

  it('非 2xx 显式抛错（不允许静默假数据）', async () => {
    const fetchMock = vi.fn(async () => new Response('not found', { status: 404 }))
    const client = createCanvasSessionClient(fetchMock as unknown as typeof fetch)
    await expect(client.getSession('missing')).rejects.toThrow(/404/)
  })

  it('摘要视图：model/cost 取自 stats.usage_to_metrics.default，缺失降级为 null', () => {
    const session = fakeConversation({
      execution_status: 'running',
      stats: {
        usage_to_metrics: {
          default: { model_name: 'gpt-5.6-sol', accumulated_cost: 2.15, max_budget_per_task: null },
        },
      },
    })
    const view = toConversationRunSummaryView(session)
    expect(view.running).toBe(true)
    expect(view.modelName).toBe('gpt-5.6-sol')
    expect(view.accumulatedCost).toBeCloseTo(2.15)

    const empty = toConversationRunSummaryView(fakeConversation())
    expect(empty.modelName).toBeNull()
    expect(empty.accumulatedCost).toBeNull()
    expect(empty.running).toBe(false)
  })
})
