/**
 * Design Tokens —— SOP Workbench × Agent Canvas 统一 UI 地基（P0）。
 *
 * 单一来源约定：
 *   - 颜色 / 阴影等**可被 Tailwind 消费**的 token 以 CSS 变量定义在 `src/index.css` 的 `:root`，
 *     tailwind.config.js 通过 `rgb(var(--c-*) / <alpha-value>)` 引用（支持 /15、/80 等透明度）。
 *   - 本文件是**类型与登记清单**（供组件、文档、未来 Canvas 风格对齐使用），不重复存颜色值；
 *     需要拿具体色值时应读 index.css 的 :root。
 *
 * 语义命名（前缀 c- = color token）：
 *   --c-page / --c-panel / --c-inset / --c-edge / --c-edge-strong     版面层
 *   --c-fg-main / --c-fg-secondary / --c-fg-muted                      文本层
 *   --c-primary / --c-primary-fg / --c-primary-soft                    品牌强调（cyan）
 *   --c-state-<tone>[/-fg/-ring/-dot]                                  状态语义（7 态）
 *
 * 设计意图（延续 v1 方案 §5）：
 *   1. 语义状态色只此一套（灰=空闲/蓝=进行/绿=成功/琥珀=等待返工/红=失败/青=强调），
 *      组件不私自造色 —— labels.ts 已迁移为消费 state-* 实用类。
 *   2. 存量 slate/cyan 字面量类（bg-slate-900 等）仍可用且视觉不变；新页面（/agent、
 *      会话状态卡）一律用本 token 层，避免两套视觉。
 *   3. 圆角/间距/字号不新造档位：统一沿用 Tailwind 默认档位，下表仅为**使用纪律登记**
 *      （排版梯度：11px 仅 mono 注记 / 12px 次要 / 13-14px 正文 / 16/20px 标题）。
 */

export const FONT_STACK = {
  sans: [
    '-apple-system',
    'BlinkMacSystemFont',
    '"Segoe UI"',
    'Roboto',
    'Oxygen',
    'Ubuntu',
    'Cantarell',
    '"Helvetica Neue"',
    'sans-serif',
  ],
  mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
} as const

/** 语义状态 7 态 —— 与 labels.ts 的 Tone 一一对应。 */
export const STATUS_TONES = ['neutral', 'idle', 'active', 'success', 'warn', 'danger', 'accent'] as const
export type StatusTone = (typeof STATUS_TONES)[number]

/**
 * 状态色 → tailwind 语义类前缀。动态拼接类时使用（例如按后端状态取值，
 * 而 labels.ts 无法覆盖的场景）；拼接结果必须是完整字面量才能被 Tailwind JIT 扫描到。
 */
export const toneClassPrefix: Record<StatusTone, string> = {
  neutral: 'state-neutral',
  idle: 'state-idle',
  active: 'state-active',
  success: 'state-success',
  warn: 'state-warn',
  danger: 'state-danger',
  accent: 'state-accent',
}

/** Tailwind 默认档位使用纪律（不新增档位；登记供评审与未来页面统一）。 */
export const RADIUS_SCALE = {
  card: 'rounded-lg', // 面板/卡片 8px（存量既有）
  panel: 'rounded-xl', // 大面板 12px
  control: 'rounded-lg', // 按钮/输入 8px
  pill: 'rounded-full', // 徽标/圆点
} as const

export const TYPOGRAPHY_SCALE = {
  monoNote: 'text-[11px]', // 仅 mono 注记（短 ID/代码注脚）
  secondary: 'text-xs', // 12px 次要信息
  body: 'text-sm', // 13-14px 正文（默认）
  bodyStrong: 'text-sm font-medium',
  heading: 'text-lg font-semibold tracking-tight', // 面板标题档
  pageTitle: 'text-xl font-semibold tracking-tight sm:text-2xl', // 页头标题
} as const

/** 焦点可见态统一（P1 组件迁移时全站套用；存量按钮已在迁移清单）。 */
export const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-page'
