import { afterEach, describe, expect, it, vi } from 'vitest'
import { getCanvasEvents } from './canvasIntegrations'

describe('canvasIntegrations event history', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('follows agent-server pagination until the complete history is loaded', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{ id: 'event-1', timestamp: '2026-09-04T00:00:00Z', kind: 'MessageEvent' }],
        next_page_id: 'page-2',
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{ id: 'event-2', timestamp: '2026-09-04T00:00:01Z', kind: 'MessageEvent' }],
        next_page_id: null,
      }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getCanvasEvents('conv-1')).resolves.toEqual([
      { id: 'event-1', timestamp: '2026-09-04T00:00:00Z', kind: 'MessageEvent' },
      { id: 'event-2', timestamp: '2026-09-04T00:00:01Z', kind: 'MessageEvent' },
    ])
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/integrations/canvas/conversations/conv-1/events/search?limit=100',
      { credentials: 'include', headers: {} },
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/integrations/canvas/conversations/conv-1/events/search?limit=100&page_id=page-2',
      { credentials: 'include', headers: {} },
    )
  })
})
