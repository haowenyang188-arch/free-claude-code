import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Badge, Hint, Panel } from '../ui'

const COLLABORATION_STEPS = [
  {
    role: 'Claude',
    tone: 'accent' as const,
    title: '方案设计',
    detail: '理解需求、分析代码并产出 PLAN；不直接改代码，也不做最终审核。',
  },
  {
    role: 'Codex',
    tone: 'active' as const,
    title: '审核 / 复审 / 最终门',
    detail: '对方案与交付物做独立审核，只输出 PASS / REWORK / PLAN_INVALID；最终门等用户决策。',
  },
  {
    role: 'Codex 执行',
    tone: 'active' as const,
    title: '输出结果 / 执行',
    detail: '按已批准的交付物落实文件修改、运行命令和测试，并提交 diff / test_report。',
  },
]

const TOOL_ROWS = [
  {
    tab: '下发',
    action: '先注册 SOP 定义，再创建并下发 run。',
    note: '目标、验收标准和约束要在下发前写清楚。',
  },
  {
    tab: '流程',
    action: '查看当前阶段、步骤、任务和 Attempt 状态。',
    note: 'accepted 代表引擎接收产出，不等于任务已经完成。',
  },
  {
    tab: 'Artifact / 证据',
    action: '核对 PLAN、实现、测试报告和审核报告的血缘。',
    note: '评审至少需要同一 Attempt 的 diff + test_report。',
  },
  {
    tab: 'Handoff',
    action: '查看阶段交接、携带的 Artifact 和事件旁证。',
    note: '交接语义以事件流旁证为准，未知时不要自行补全。',
  },
  {
    tab: '事件',
    action: '按类型筛选时间线，必要时展开 payload 或导出 JSON。',
    note: '数据异常先看事件顺序，再判断是执行错误还是证据不足。',
  },
  {
    tab: 'Runtime',
    action: '查看 Agent、Adapter、session 和恢复信息。',
    note: '外部运行时不可用时，保留错误上下文，不要伪造成功。',
  },
  {
    tab: '审批',
    action: '用 session_id + call_id + 64 位 command_hash 查询后再裁决。',
    note: '确认 command、cwd 和权限后，再批准 / 拒绝 / 取消。',
  },
  {
    tab: '控制',
    action: '按需暂停、继续或取消 SOP Run；遗留 run 另有兼容控制。',
    note: '无效状态会返回 409，先回到流程和事件核对当前状态。',
  },
  {
    tab: '刷新 / 轮询',
    action: '数据未更新时点“立即刷新”，或把轮询调到合适的间隔。',
    note: '优先复用当前 run 的数据，重复下发前先确认状态。',
  },
]

/**
 * SOP 主入口的全局使用指南。
 * 这里只解释稳定的角色与工具边界，具体请求错误仍由对应面板展示。
 */
export function UsageGuidePanel() {
  const [open, setOpen] = useState(false)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    originX: number
    originY: number
  } | null>(null)
  const launcherRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const wasOpen = useRef(false)

  useEffect(() => {
    if (open) closeRef.current?.focus()
    else if (wasOpen.current) launcherRef.current?.focus()
    wasOpen.current = open
  }, [open])

  const toggleWithKeyboard = (event: React.KeyboardEvent<HTMLButtonElement>, nextOpen: boolean) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    setOpen(nextOpen)
  }

  const startDragging = (event: ReactPointerEvent<HTMLButtonElement>) => {
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: offset.x,
      originY: offset.y,
    }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }

  const movePanel = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    setOffset({
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY,
    })
  }

  const stopDragging = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null
  }

  if (!open) {
    return (
      <button
        ref={launcherRef}
        type="button"
        aria-label="打开协作与工具指南"
        title="打开协作与工具指南"
        aria-expanded="false"
        onClick={() => setOpen(true)}
        onKeyDown={(event) => toggleWithKeyboard(event, true)}
        className="fixed bottom-4 right-4 z-40 inline-flex h-11 w-11 items-center justify-center rounded-full border border-cyan-400/50 bg-slate-900 text-lg font-semibold text-cyan-300 shadow-float transition hover:border-cyan-300 hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-cyan-400"
      >
        <span aria-hidden="true">?</span>
      </button>
    )
  }

  return (
    <div
      role="dialog"
      aria-labelledby="usage-guide-title"
      aria-describedby="usage-guide-description"
      className="fixed bottom-4 right-4 z-40 w-[min(36rem,calc(100vw-2rem))] max-h-[calc(100vh-2rem)] overflow-y-auto rounded-xl border border-slate-700 bg-slate-950 shadow-modal"
      style={{ transform: `translate3d(${offset.x}px, ${offset.y}px, 0)` }}
    >
      <header className="sticky top-0 z-10 flex items-start gap-2 border-b border-slate-800 bg-slate-900 px-3 py-2.5">
        <button
          type="button"
          data-testid="usage-guide-drag-handle"
          aria-label="拖动协作与工具指南"
          title="拖动协作与工具指南"
          onPointerDown={startDragging}
          onPointerMove={movePanel}
          onPointerUp={stopDragging}
          onPointerCancel={stopDragging}
          onLostPointerCapture={stopDragging}
          className="mt-0.5 inline-flex h-7 w-7 shrink-0 cursor-move items-center justify-center rounded border border-slate-700 text-sm text-slate-400 hover:border-slate-600 hover:text-slate-200 focus:outline-none focus:ring-2 focus:ring-cyan-400"
        >
          <span aria-hidden="true">⠿</span>
        </button>
        <div className="min-w-0 flex-1">
          <h2 id="usage-guide-title" className="text-sm font-semibold text-slate-100">
            协作与工具指南
          </h2>
          <p id="usage-guide-description" className="mt-0.5 text-xs text-slate-500">
            可拖动此面板，关闭后仅保留右下角图标。
          </p>
        </div>
        <button
          ref={closeRef}
          type="button"
          aria-label="关闭协作与工具指南"
          title="关闭协作与工具指南"
          aria-expanded="true"
          onClick={() => setOpen(false)}
          onKeyDown={(event) => toggleWithKeyboard(event, false)}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border border-slate-700 text-lg leading-none text-slate-400 hover:border-slate-600 hover:text-slate-200 focus:outline-none focus:ring-2 focus:ring-cyan-400"
        >
          <span aria-hidden="true">×</span>
        </button>
      </header>

      <div className="space-y-4 p-3">
        <Panel title="怎么协作" subtitle="v2 流程：Claude 方案 → Codex 审核 → Claude 修订 → Codex 复审 → Codex 输出 → 用户决策 → Codex 执行；SOP Engine 负责推进与路由。">
          <p
            data-testid="usage-guide-collaboration-order"
            className="text-sm font-semibold tracking-wide text-slate-100"
            aria-label="协作顺序"
          >
            Claude 方案 <span className="px-1 text-slate-600" aria-hidden="true">→</span> Codex 审核 →
            Claude 修订 <span className="px-1 text-slate-600" aria-hidden="true">→</span> Codex 复审 →
            Codex 输出 → 用户决策 → Codex 执行
          </p>
          <div
            data-testid="usage-guide-collaboration-roles"
            className="grid gap-3 md:grid-cols-3"
            role="list"
            aria-label="固定协作顺序"
          >
            {COLLABORATION_STEPS.map((step, index) => (
              <div key={step.role} className="relative min-w-0" role="listitem">
                <div className="flex items-center gap-2">
                  <Badge tone={step.tone} mono>
                    {step.role}
                  </Badge>
                  <span className="text-xs font-medium text-slate-200">{step.title}</span>
                  {index < COLLABORATION_STEPS.length - 1 ? (
                    <span className="hidden text-slate-600 md:inline" aria-hidden="true">
                      →
                    </span>
                  ) : null}
                </div>
                <p className="mt-2 text-xs leading-5 text-slate-400">{step.detail}</p>
              </div>
            ))}
          </div>

          <div className="border-t border-slate-800 pt-3 text-xs leading-5 text-slate-400">
            <p>
              <strong className="font-medium text-slate-200">SOP Engine</strong>：统一推进状态、创建
              Handoff 并决定 REWORK / PASS 路由；Agent 不能自行改流程或宣布完成。
            </p>
            <p className="mt-2">
              <strong className="font-medium text-slate-200">Artifact + Handoff</strong>：阶段间的事实交接；
              不要把聊天记录当作下一阶段的唯一依据。
            </p>
          </div>

          <Hint>
            审核不通过时回到 Engine 指定的 REWORK 路由；不要让 Agent 之间自由派活，也不要跳过证据门直接收尾。
          </Hint>
        </Panel>

        <Panel title="工具操作速查" subtitle="先看事实，再做控制；每个页签只承担一类观察或操作。">
          <div data-testid="usage-guide-tools" className="divide-y divide-slate-800">
            {TOOL_ROWS.map((row) => (
              <div key={row.tab} className="grid gap-2 py-3 first:pt-0 last:pb-0 sm:grid-cols-[10rem_minmax(0,1fr)]">
                <div>
                  <Badge tone="neutral" mono>
                    {row.tab}
                  </Badge>
                </div>
                <div className="min-w-0">
                  <p className="text-sm text-slate-200">{row.action}</p>
                  <p className="mt-1 text-xs leading-5 text-slate-500">{row.note}</p>
                </div>
              </div>
            ))}
          </div>

          <Hint>
            数据暂时未更新时先点“立即刷新”或等待轮询，不要重复下发同一 run；遇到错误先保留错误信息和事件序号。
          </Hint>
        </Panel>
      </div>
    </div>
  )
}
