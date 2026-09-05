import { describe, expect, it } from 'vitest'
import { STATUS_TONES, toneClassPrefix } from './tokens'
import { TONE_CLASS, TONE_DOT, type Tone } from './labels'

/**
 * Design Token 一致性（P0）：
 * labels.ts 的 Tone 语义集与 tokens.ts 的 STATUS_TONES 必须完全一致，
 * 且每个 tone 都必须产出非空的 badge/dot 类 —— 防止 token 层与语义层漂移。
 */
describe('design token 一致性', () => {
  it('labels.Tone 与 tokens.STATUS_TONES 一一对应', () => {
    const tones: Tone[] = ['neutral', 'idle', 'active', 'success', 'warn', 'danger', 'accent']
    expect(STATUS_TONES).toEqual(tones)
    expect(Object.keys(TONE_CLASS)).toEqual(tones)
    expect(Object.keys(TONE_DOT)).toEqual(tones)
    expect(Object.keys(toneClassPrefix)).toEqual(tones)
  })

  it('每个 tone 的徽标类都引用了 state-* 语义实用类（非空且非旧的字面量色板）', () => {
    for (const tone of STATUS_TONES) {
      const badge = TONE_CLASS[tone]
      const dot = TONE_DOT[tone]
      expect(badge.length).toBeGreaterThan(0)
      expect(dot.length).toBeGreaterThan(0)
      expect(badge).toContain(`state-${tone}`)
      expect(badge).not.toContain('bg-slate-800 text-slate-300 ring-slate-700')
      expect(dot).toContain(`state-${tone}-dot`)
      expect(dot).not.toContain('bg-slate-500')
    }
  })

  it('toneClassPrefix 是 state-* 前缀（不含重复 state-）', () => {
    // toneClassPrefix 返回「state-success」这类前缀；动态拼接时需保证不出现
    // 「state-state-*」双前缀，且产出完整字面量才能被 Tailwind JIT 扫描到。
    for (const tone of STATUS_TONES) {
      const prefix = toneClassPrefix[tone]
      expect(prefix).toMatch(/^state-[a-z]+$/)
      expect(prefix).not.toContain('state-state-')
    }
    const sample: Record<string, string> = {
      success: 'bg-state-success/15 text-state-success-fg',
      active: 'bg-state-active/15 text-state-active-fg',
      danger: 'bg-state-danger/15 text-state-danger-fg',
    }
    // 供动态场景参考的完整字面量（写全类名，勿用模板拼接漏扫）。
    expect(Object.values(sample).join(' ')).toContain('bg-state-success/15')
  })
})
