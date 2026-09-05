/**
 * Conversation 域 API client —— SOP Workbench × Agent Canvas 整合的**共用边界**（P0）。
 *
 * 数据源是 agent-server(18000)；P1-A 由 8000 提供 `/integrations/canvas/*` 同源只读转发并
 * 注入 Session Key（ADR：8000 独立挂载点，与冻结业务路由隔离，可整体开关）。
 * 本 client 的 base 指向 `/integrations/canvas`（P1-A 挂载点），请求路径形状与
 * agent-server /openapi.json 的 conversations 端点一一对应；失败显式暴露给调用方，
 * 不做本地兜底或假数据。
 *
 * 架构铁律（见 E:\WorkBuddy\WEBUI_ARCH_ADOPTED.md）：
 *   Canvas / Extension / 本 client 不得成为 SOP 状态源、不得保存权威状态副本、
 *   不得承担 Orchestrator —— 这里只读取会话自身的执行摘要，供 UI 展示。
 */

import type { ConversationSession } from '../types/conversation'

/** P1-A 由 8000 注册的透传挂载点（本机 loopback 注入 Session Key）。 */
export const CANVAS_API_BASE = '/integrations/canvas'

export interface CanvasSessionClient {
  /**
   * GET /api/conversations/{id}（经 /integrations/canvas）—— 单会话详情与运行摘要。
   */
  getSession(id: string): Promise<ConversationSession>
  /**
   * GET /api/conversations?ids=a&ids=b —— 会话状态卡的批量摘要读取（P1-A）。
   * 说明：agent-server 该端点按 ids 精确查询（无参返回 422），无全量列表；
   * 会话状态卡需要展示的 ids 由 Workbench 侧 run↔会话关联提供。
   */
  listSessions(ids: string[]): Promise<ConversationSession[]>
}

export function createCanvasSessionClient(fetchImpl: typeof fetch = fetch): CanvasSessionClient {
  return {
    async getSession(id) {
      const response = await fetchImpl(`${CANVAS_API_BASE}/conversations/${encodeURIComponent(id)}`, {
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) {
        throw new Error(`canvas session request failed (${response.status})`)
      }
      return (await response.json()) as ConversationSession
    },

    async listSessions(ids) {
      const params = new URLSearchParams()
      ids.forEach((id) => params.append('ids', id))
      const response = await fetchImpl(
        `${CANVAS_API_BASE}/conversations?${params.toString()}`,
        { headers: { Accept: 'application/json' } },
      )
      if (!response.ok) {
        throw new Error(`canvas session list request failed (${response.status})`)
      }
      const data = (await response.json()) as ConversationSession[]
      return Array.isArray(data) ? data : []
    },
  }
}

/** 单例（P1-A 会话状态卡消费；P0 无调用方，仅导出边界）。 */
export const canvasSessionClient = createCanvasSessionClient()
