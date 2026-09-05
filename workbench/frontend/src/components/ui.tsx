import type { ReactNode } from 'react'
import { TONE_CLASS, TONE_DOT, type Tone } from '../lib/labels'

export function Badge({
  tone = 'neutral',
  children,
  mono = false,
}: {
  tone?: Tone
  children: ReactNode
  mono?: boolean
}) {
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${
        TONE_CLASS[tone]
      } ${mono ? 'font-mono' : ''}`}
    >
      {children}
    </span>
  )
}

export function ToneDot({ tone }: { tone: Tone }) {
  return <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${TONE_DOT[tone]}`} aria-hidden="true" />
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
  dense = false,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  dense?: boolean
}) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-800 px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
          {subtitle ? <p className="mt-0.5 break-words text-xs text-slate-500">{subtitle}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </header>
      <div className={dense ? 'px-4 py-3' : 'space-y-3 px-4 py-4'}>{children}</div>
    </section>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-dashed border-slate-800 px-4 py-6 text-center text-sm text-slate-500">
      {children}
    </p>
  )
}

export function KeyValue({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-slate-200">{children}</dd>
    </div>
  )
}

export function KeyValueGrid({ children }: { children: ReactNode }) {
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">{children}</dl>
  )
}

type ButtonKind = 'default' | 'primary' | 'danger' | 'ghost'

const BUTTON_CLASS: Record<ButtonKind, string> = {
  default:
    'border border-slate-700 bg-slate-800 text-slate-200 hover:border-slate-600 hover:bg-slate-700 disabled:opacity-50',
  primary: 'bg-cyan-400 text-slate-950 hover:bg-cyan-300 disabled:opacity-50',
  danger: 'bg-rose-500/90 text-white hover:bg-rose-500 disabled:opacity-50',
  ghost: 'border border-slate-800 text-slate-400 hover:border-slate-700 hover:text-slate-200 disabled:opacity-50',
}

export function Button({
  kind = 'default',
  children,
  className = '',
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: ButtonKind }) {
  return (
    <button
      type="button"
      {...rest}
      className={`rounded-lg px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed ${BUTTON_CLASS[kind]} ${className}`}
    >
      {children}
    </button>
  )
}

export const inputClass =
  'w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none transition placeholder:text-slate-600 focus:border-cyan-400'

export function Field({
  label,
  hint,
  children,
  span = 1,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
  span?: 1 | 2
}) {
  return (
    <label className={`block ${span === 2 ? 'sm:col-span-2' : ''}`}>
      <span className="mb-1 block text-xs font-medium text-slate-300">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-[11px] text-slate-500">{hint}</span> : null}
    </label>
  )
}

export function DataTable({
  head,
  children,
}: {
  head: string[]
  children: ReactNode
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full min-w-[640px] border-collapse text-left text-sm">
        <thead>
          <tr className="bg-slate-950/60 text-[11px] uppercase tracking-wide text-slate-500">
            {head.map((cell) => (
              <th key={cell} className="px-3 py-2 font-medium">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">{children}</tbody>
      </table>
    </div>
  )
}

export function CodeBlock({ children, small = false }: { children: string; small?: boolean }) {
  return (
    <pre
      className={`overflow-x-auto rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-300 ${
        small ? 'max-h-48' : 'max-h-[28rem]'
      }`}
    >
      {children}
    </pre>
  )
}

export function Hint({ children, tone = 'muted' }: { children: ReactNode; tone?: 'muted' | 'error' }) {
  return (
    <p className={`text-xs ${tone === 'error' ? 'text-rose-300' : 'text-slate-500'}`}>{children}</p>
  )
}
