import { Link, useSearchParams } from 'react-router-dom'

const CANVAS_ENTRY = (import.meta.env.VITE_CANVAS_ENTRY as string | undefined) ?? 'http://127.0.0.1:8010'

/**
 * Agent Canvas 会话页：只嵌原生 Canvas（8010），不再自建会话列表/历史面板。
 * 会话的发起、选择、续聊全部在原生 Canvas 内完成；?c=<id> 仅作为深链入口
 * （可直接从 Workbench 其它页面跳到指定会话的全屏视图）。
 */
export default function AgentCanvasPage() {
  const [searchParams] = useSearchParams()
  const selectedId = searchParams.get('c')
  const frameSrc = selectedId
    ? `${CANVAS_ENTRY}/conversations/${encodeURIComponent(selectedId)}?backend=default-local`
    : `${CANVAS_ENTRY}/`

  return (
    <div className="flex h-screen flex-col bg-page text-fg-main">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge bg-panel px-4 py-3">
        <div className="flex items-center gap-3">
          <Link to="/" className="rounded-lg border border-edge px-3 py-1.5 text-xs text-fg-secondary hover:border-edge-strong hover:text-fg-main">
            ← SOP 总览
          </Link>
          <div>
            <h1 className="text-sm font-semibold">Agent Canvas 会话</h1>
            <p className="text-xs text-fg-muted">原生 Canvas · 会话在左侧列表中选择与管理</p>
          </div>
        </div>
        {selectedId ? (
          <a
            href={frameSrc}
            target="_blank"
            rel="noreferrer"
            className="rounded-lg border border-cyan-500/50 px-3 py-1.5 text-xs text-cyan-300 hover:border-cyan-300"
          >
            在原生 Canvas 打开
          </a>
        ) : null}
      </header>

      <main className="min-h-0 flex-1">
        <iframe
          title="Agent Canvas 原生会话"
          src={frameSrc}
          className="block h-full w-full border-0"
          data-testid="agent-canvas-frame"
          allow="clipboard-write; clipboard-read"
        />
      </main>
    </div>
  )
}
