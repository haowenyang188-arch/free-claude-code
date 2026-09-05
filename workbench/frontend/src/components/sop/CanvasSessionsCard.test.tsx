import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CanvasSessionsCard } from './CanvasSessionsCard'

/**
 * Canvas 会话状态卡（P1-A）：
 *  - 健康+列表都 OK → 渲染运行摘要（状态/模型/成本）
 *  - 点击行 → onOpenSession(id)
 *  - 503（集成未启用）→ 提示未启用，不渲染空态误导
 */
function mockFetchRoutes(routes: { health?: unknown; search?: unknown; status?: number }) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (routes.status && routes.status >= 400) {
      return new Response(
        JSON.stringify({ error: { code: 'canvas_integration_disabled', message: 'disabled' } }),
        { status: routes.status, headers: { 'Content-Type': 'application/json' } },
      )
    }
    if (url.includes('/canvas/health')) {
      return new Response(JSON.stringify(routes.health), { status: 200 })
    }
    if (url.includes('/canvas/conversations/search')) {
      return new Response(JSON.stringify(routes.search), { status: 200 })
    }
    return new Response('{"items":[]}', { status: 200 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const RUNNING = {
  id: 'conv-running-1',
  workspace: { working_dir: '/home/gnen/workspace/project/conv-running-1', kind: 'LocalWorkspace' },
  execution_status: 'running',
  stats: {
    usage_to_metrics: { default: { model_name: 'gpt-5.6-sol', accumulated_cost: 2.15 } },
  },
}

const FINISHED = {
  id: 'conv-finished-2',
  workspace: { working_dir: '/ws/finished-2', kind: 'LocalWorkspace' },
  execution_status: 'finished',
  stats: {},
}

const LINKED_RUN = {
  canvasAcpSessionId: 'acp-session-1',
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('CanvasSessionsCard', () => {
  it('健康且有条目：渲染状态/模型/成本，点击回调会话 id', async () => {
    const user = userEvent.setup()
    mockFetchRoutes({
      health: { enabled: true, agent_base: 'http://127.0.0.1:18000' },
      search: { items: [RUNNING, FINISHED] },
    })
    const onOpen = vi.fn()
    render(<CanvasSessionsCard onOpenSession={onOpen} />)

    await waitFor(() => {
      expect(screen.getByText(/gpt-5\.6-sol/)).toBeInTheDocument()
    })
    expect(screen.getByText(/\$2\.1500/)).toBeInTheDocument()
    expect(screen.getAllByText(/running|finished/).length).toBeGreaterThan(0)

    await user.click(screen.getByTitle(/打开会话 conv-running-1/))
    expect(onOpen).toHaveBeenCalledWith('conv-running-1')
  })

  it('503（集成未启用）：提示配置，不显示空态误导', async () => {
    mockFetchRoutes({ status: 503 })
    render(<CanvasSessionsCard onOpenSession={() => {}} />)

    await waitFor(() => {
      expect(screen.getByText(/WORKBENCH_CANVAS_SESSION_API_KEY/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/暂无 Canvas 会话/)).not.toBeInTheDocument()
  })

  it('按 SOP run 的 ACP session 关联并标记 Canvas 会话', async () => {
    mockFetchRoutes({
      health: { enabled: true, agent_base: 'http://127.0.0.1:18000' },
      search: {
        items: [
          { ...FINISHED, agent_state: { acp_session_id: 'acp-session-1' } },
        ],
      },
    })
    render(
      <CanvasSessionsCard
        onOpenSession={() => {}}
        linkedAcpSessionId={LINKED_RUN.canvasAcpSessionId}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('当前 run')).toBeInTheDocument()
    })
  })
})
