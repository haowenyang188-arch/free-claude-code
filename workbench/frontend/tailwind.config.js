/**
 * Design Tokens 接线层（P0）。
 *
 * 语义 token 全部以 CSS 变量定义在 src/index.css 的 :root，
 * 此处用 rgb(var(--c-*) / <alpha-value>) 引用以支持 /15、/80 透明度语法。
 * 颜色值唯一权威来源：src/index.css。存量 slate/cyan 色板保持可用，视觉不变。
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        /* 版面层 */
        page: 'rgb(var(--c-page) / <alpha-value>)',
        panel: 'rgb(var(--c-panel) / <alpha-value>)',
        inset: 'rgb(var(--c-inset) / <alpha-value>)',
        edge: 'rgb(var(--c-edge) / <alpha-value>)',
        'edge-strong': 'rgb(var(--c-edge-strong) / <alpha-value>)',
        /* 文本层 */
        fg: {
          main: 'rgb(var(--c-fg-main) / <alpha-value>)',
          secondary: 'rgb(var(--c-fg-secondary) / <alpha-value>)',
          muted: 'rgb(var(--c-fg-muted) / <alpha-value>)',
        },
        /* 品牌强调（cyan 系，现行为主） */
        primary: {
          DEFAULT: 'rgb(var(--c-primary) / <alpha-value>)',
          fg: 'rgb(var(--c-primary-fg) / <alpha-value>)',
          soft: 'rgb(var(--c-primary-soft) / <alpha-value>)',
        },
        /* 状态语义 7 态（与 src/lib/labels.ts Tone 一一对应） */
        state: {
          neutral: {
            DEFAULT: 'rgb(var(--c-state-neutral) / <alpha-value>)',
            fg: 'rgb(var(--c-state-neutral-fg) / <alpha-value>)',
            ring: 'rgb(var(--c-state-neutral-ring) / <alpha-value>)',
            dot: 'rgb(var(--c-state-neutral-dot) / <alpha-value>)',
          },
          idle: {
            DEFAULT: 'rgb(var(--c-state-idle) / <alpha-value>)',
            fg: 'rgb(var(--c-state-idle-fg) / <alpha-value>)',
            ring: 'rgb(var(--c-state-idle-ring) / <alpha-value>)',
            dot: 'rgb(var(--c-state-idle-dot) / <alpha-value>)',
          },
          active: {
            DEFAULT: 'rgb(var(--c-state-active) / <alpha-value>)',
            fg: 'rgb(var(--c-state-active-fg) / <alpha-value>)',
            ring: 'rgb(var(--c-state-active-ring) / <alpha-value>)',
            dot: 'rgb(var(--c-state-active-dot) / <alpha-value>)',
          },
          success: {
            DEFAULT: 'rgb(var(--c-state-success) / <alpha-value>)',
            fg: 'rgb(var(--c-state-success-fg) / <alpha-value>)',
            ring: 'rgb(var(--c-state-success-ring) / <alpha-value>)',
            dot: 'rgb(var(--c-state-success-dot) / <alpha-value>)',
          },
          warn: {
            DEFAULT: 'rgb(var(--c-state-warn) / <alpha-value>)',
            fg: 'rgb(var(--c-state-warn-fg) / <alpha-value>)',
            ring: 'rgb(var(--c-state-warn-ring) / <alpha-value>)',
            dot: 'rgb(var(--c-state-warn-dot) / <alpha-value>)',
          },
          danger: {
            DEFAULT: 'rgb(var(--c-state-danger) / <alpha-value>)',
            fg: 'rgb(var(--c-state-danger-fg) / <alpha-value>)',
            ring: 'rgb(var(--c-state-danger-ring) / <alpha-value>)',
            dot: 'rgb(var(--c-state-danger-dot) / <alpha-value>)',
          },
          accent: {
            DEFAULT: 'rgb(var(--c-state-accent) / <alpha-value>)',
            fg: 'rgb(var(--c-state-accent-fg) / <alpha-value>)',
            ring: 'rgb(var(--c-state-accent-ring) / <alpha-value>)',
            dot: 'rgb(var(--c-state-accent-dot) / <alpha-value>)',
          },
        },
      },
      boxShadow: {
        float: '0 10px 25px -5px rgb(0 0 0 / 0.35)', // 浮层/下拉
        modal: '0 20px 50px -12px rgb(0 0 0 / 0.5)', // 弹窗（P1 起使用）
      },
    },
  },
  plugins: [],
}
