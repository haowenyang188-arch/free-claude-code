/**
 * Canvas 集成 API（前端）—— 对应 8000 挂载点 /api/integrations/canvas/*（P1-A）。
 *
 * 数据源链：前端 → 8000（同源，Workbench token 会话）→ 18000（agent-server，
 * 由 8000 注入 Session API Key）。前端永不直接访问 18000。
 *
 * 失败信封（与后端约定）：
 *   - 503 canvas_integration_disabled  未配置 WORKBENCH_CANVAS_SESSION_API_KEY（提示用户配置）
 *   - 502 canvas_upstream_*            agent-server 不可达 / 上游错误（提示服务未启动）
 * requestJson 会把非 2xx 解析为带 status 的 Error。
 */

import { requestJson } from './api'
import type {
  ConversationEvent,
  ConversationEventPage,
  ConversationSession,
} from '../types/conversation'

export interface CanvasSearchResponse {
  items: ConversationSession[]
  next_page_id?: string | null
}

export interface CanvasIntegrationHealth {
  enabled: boolean
  agent_base: string
}

/** GET /api/integrations/canvas/health —— enabled=false 时前端隐藏会话状态卡数据。 */
export const getCanvasIntegrationHealth = () =>
  requestJson<CanvasIntegrationHealth>('/integrations/canvas/health')

/**
 * GET /api/integrations/canvas/conversations/search
 * 会话状态卡主数据源：最近会话（CREATED_AT_DESC），limit 1..100。
 */
export const searchCanvasSessions = (
  opts: { limit?: number; status?: string } = {},
): Promise<CanvasSearchResponse> => {
  const params = new URLSearchParams()
  if (opts.limit !== undefined) params.set('limit', String(opts.limit))
  if (opts.status) params.set('status', opts.status)
  const query = params.toString()
  return requestJson<CanvasSearchResponse>(
    `/integrations/canvas/conversations/search${query ? `?${query}` : ''}`,
  )
}

/** GET /api/integrations/canvas/conversations?ids=a,b（run↔会话联动精确读取，G-1 落地后使用）。 */
export const getCanvasSessionsByIds = (ids: string[]) =>
  requestJson<ConversationSession[]>(
    `/integrations/canvas/conversations?ids=${encodeURIComponent(ids.join(','))}`,
  )

export const getCanvasSession = (id: string) =>
  requestJson<ConversationSession>(
    `/integrations/canvas/conversations/${encodeURIComponent(id)}`,
  )

export const searchCanvasEvents = (
  id: string,
  opts: { limit?: number; pageId?: string } = {},
) => {
  const params = new URLSearchParams()
  if (opts.limit !== undefined) params.set('limit', String(opts.limit))
  if (opts.pageId) params.set('page_id', opts.pageId)
  const query = params.toString()
  return requestJson<ConversationEventPage>(
    `/integrations/canvas/conversations/${encodeURIComponent(id)}/events/search${query ? `?${query}` : ''}`,
  )
}

export const getCanvasEvents = async (id: string): Promise<ConversationEvent[]> => {
  const events: ConversationEvent[] = []
  let pageId: string | undefined
  // ponytail: cap at 100 pages; add cursor persistence when histories exceed this ceiling.
  for (let page = 0; page < 100; page += 1) {
    const response = await searchCanvasEvents(id, { limit: 100, pageId })
    events.push(...response.items)
    if (!response.next_page_id) break
    pageId = response.next_page_id
  }
  return events
}

export const sendCanvasMessage = (id: string, text: string) =>
  requestJson<{ ok?: boolean }>(
    `/integrations/canvas/conversations/${encodeURIComponent(id)}/events`,
    {
      method: 'POST',
      body: JSON.stringify({
        role: 'user',
        content: [{ type: 'text', text }],
        run: true,
      }),
    },
  )
