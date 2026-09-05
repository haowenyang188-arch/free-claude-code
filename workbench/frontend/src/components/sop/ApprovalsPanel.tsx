import { useState } from 'react'
import { Badge, Button, CodeBlock, Empty, Field, Hint, KeyValue, KeyValueGrid, Panel, inputClass } from '../ui'
import { ErrorNotice } from '../ErrorNotice'
import { useResource } from '../../hooks/useResource'
import { decideApproval, getApproval, getJob, stopJob } from '../../services/sopApi'
import { errorMessage } from '../../services/errors'
import { formatTime, shortId } from '../../lib/format'
import type {
  ApprovalDecision,
  ApprovalProvider,
  ApprovalRecordPayload,
  JobRecordPayload,
} from '../../types/sop'

const DECISIONS: { value: ApprovalDecision; label: string; kind: 'primary' | 'danger' | 'ghost' }[] = [
  { value: 'approve', label: '批准', kind: 'primary' },
  { value: 'reject', label: '拒绝', kind: 'danger' },
  { value: 'cancel', label: '取消', kind: 'ghost' },
]

const HEX64 = /^[0-9a-fA-F]{64}$/

interface Query {
  sessionId: string
  callId: string
  commandHash: string
  provider: ApprovalProvider
}

/**
 * 能力 6 —— 审批请求与审批结果
 * 能力 8 —— 对需人工确认的节点批准 / 拒绝
 *
 * 后端没有「待审批列表」接口：GET /api/approvals/{session}/{call} 以
 * session_id + call_id 为键，且 **必须**带 64 位 hex 的 command_hash 查询参数
 * （main.py::get_approval 缺失时返回 409 approval_integrity_mismatch）。
 * 因此这里提供显式查询表单；裁决走
 * POST /api/approvals/{session_id}/{call_id}/{approve|reject|cancel}，
 * decision 的真实取值是 approve / reject / cancel，不是 deny。
 */
export function ApprovalsPanel({ pollMs }: { pollMs: number }) {
  const [form, setForm] = useState<Query>({
    sessionId: '',
    callId: '',
    commandHash: '',
    provider: 'codex_cli',
  })
  const [submitted, setSubmitted] = useState<Query | null>(null)
  const [actionError, setActionError] = useState<unknown>(undefined)
  const [actionNote, setActionNote] = useState<string | null>(null)
  const [busy, setBusy] = useState<ApprovalDecision | null>(null)

  const approval = useResource<ApprovalRecordPayload | undefined>(
    () =>
      submitted
        ? getApproval(submitted.sessionId, submitted.callId, submitted.commandHash, submitted.provider)
        : Promise.resolve(undefined),
    [submitted],
    { intervalMs: submitted ? pollMs : null, enabled: Boolean(submitted) },
  )

  const hashInvalid = form.commandHash.length > 0 && !HEX64.test(form.commandHash)
  const canLookup =
    form.sessionId.trim().length > 0 &&
    form.callId.trim().length > 0 &&
    HEX64.test(form.commandHash.trim())

  async function decide(decision: ApprovalDecision) {
    if (!submitted) return
    setBusy(decision)
    setActionError(undefined)
    setActionNote(null)
    try {
      const record = await decideApproval(submitted.sessionId, submitted.callId, decision, {
        provider: submitted.provider,
        command_hash: submitted.commandHash,
      })
      setActionNote(`已${labelOf(decision)}，当前状态 ${record.status}`)
      approval.refresh()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(null)
    }
  }

  function labelOf(decision: ApprovalDecision) {
    return DECISIONS.find((item) => item.value === decision)?.label ?? decision
  }

  return (
    <div className="space-y-4">
      <Panel
        title="审批请求查询"
        subtitle="GET /api/approvals/{session_id}/{call_id}?command_hash=…&provider=…"
        dense
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="session_id">
            <input
              className={inputClass}
              value={form.sessionId}
              onChange={(e) => setForm({ ...form, sessionId: e.target.value })}
              spellCheck={false}
            />
          </Field>
          <Field label="call_id">
            <input
              className={inputClass}
              value={form.callId}
              onChange={(e) => setForm({ ...form, callId: e.target.value })}
              spellCheck={false}
            />
          </Field>
          <Field
            label="command_hash（64 位十六进制，必填）"
            span={2}
            hint={hashInvalid ? 'command_hash 必须是 64 位十六进制字符串。' : undefined}
          >
            <input
              className={`${inputClass} font-mono ${hashInvalid ? 'border-rose-500' : ''}`}
              value={form.commandHash}
              onChange={(e) => setForm({ ...form, commandHash: e.target.value })}
              spellCheck={false}
            />
          </Field>
          <Field label="provider">
            <select
              className={inputClass}
              value={form.provider}
              onChange={(e) => setForm({ ...form, provider: e.target.value as ApprovalProvider })}
            >
              <option value="codex_cli">codex_cli</option>
              <option value="claude_cli">claude_cli</option>
            </select>
          </Field>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            kind="default"
            disabled={!canLookup}
            onClick={() => setSubmitted({ ...form, commandHash: form.commandHash.trim() })}
          >
            查询
          </Button>
          {hashInvalid ? (
            <Hint tone="error">command_hash 必须是 64 位十六进制字符串。</Hint>
          ) : null}
        </div>

        <Hint>
          后端没有「待审批列表」接口，session_id / call_id / command_hash 需从运行侧或事件流中取得；
          command_hash 缺失时后端直接返回 409 approval_integrity_mismatch。
        </Hint>
      </Panel>

      {submitted ? (
        <Panel
          title="审批详情与裁决"
          subtitle={`${shortId(submitted.sessionId, 12)} / ${shortId(submitted.callId, 12)}`}
          actions={
            <Button kind="ghost" onClick={approval.refresh}>
              刷新
            </Button>
          }
        >
          <ErrorNotice error={approval.error ?? actionError} context="读取审批请求" />
          {actionNote ? (
            <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
              {actionNote}
            </div>
          ) : null}

          {approval.data ? (
            <>
              <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
                <Badge
                  tone={
                    approval.data.status === 'approved'
                      ? 'success'
                      : approval.data.status === 'rejected'
                        ? 'danger'
                        : 'warn'
                  }
                  mono
                >
                  {approval.data.status}
                </Badge>
                <span>risk: {approval.data.risk}</span>
                <span>provider: {approval.data.provider}</span>
                <span>
                  one_shot:{' '}
                  <code className="font-mono">
                    {approval.data.one_shot_id ? shortId(approval.data.one_shot_id, 12) : '—'}
                  </code>
                </span>
              </div>

              <KeyValueGrid>
                <KeyValue label="call_id">
                  <code className="break-all font-mono text-xs">{approval.data.call_id}</code>
                </KeyValue>
                <KeyValue label="command_hash">
                  <code className="break-all font-mono text-xs">{approval.data.command_hash}</code>
                </KeyValue>
                <KeyValue label="permission">{approval.data.requested_permission}</KeyValue>
                <KeyValue label="cwd">
                  <code className="break-all font-mono text-xs">{approval.data.cwd}</code>
                </KeyValue>
                <KeyValue label="created_at">{formatTime(approval.data.created_at)}</KeyValue>
                <KeyValue label="expires_at">{formatTime(approval.data.expires_at)}</KeyValue>
                <KeyValue label="approved_at">{formatTime(approval.data.approved_at)}</KeyValue>
                <KeyValue label="consumed_at">{formatTime(approval.data.consumed_at)}</KeyValue>
                <KeyValue label="reason">{approval.data.reason ?? '—'}</KeyValue>
              </KeyValueGrid>

              <div>
                <p className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">
                  normalized_command
                </p>
                <CodeBlock small>{approval.data.normalized_command ?? '(空)'}</CodeBlock>
                {approval.data.argv.length > 0 ? (
                  <div className="mt-2">
                    <CodeBlock small>{JSON.stringify(approval.data.argv)}</CodeBlock>
                  </div>
                ) : null}
              </div>

              <div className="flex flex-wrap gap-2">
                {DECISIONS.map((decision) => (
                  <Button
                    key={decision.value}
                    kind={decision.kind}
                    disabled={busy !== null}
                    onClick={() => void decide(decision.value)}
                  >
                    {busy === decision.value ? '提交中…' : decision.label}
                  </Button>
                ))}
              </div>
            </>
          ) : (
            !approval.error && <Empty>未查询到审批请求。</Empty>
          )}
        </Panel>
      ) : (
        <Panel title="审批详情与裁决" dense>
          <Empty>填写上方表单并查询后，可在此批准 / 拒绝 / 取消。</Empty>
        </Panel>
      )}

      <JobPanel />
    </div>
  )
}

/** 批准后的命令以后台 job 形式运行；stop 是操作员的紧急开关。 */
function JobPanel() {
  const [jobId, setJobId] = useState('')
  const [job, setJob] = useState<JobRecordPayload | undefined>(undefined)
  const [error, setError] = useState<unknown>(undefined)
  const [busy, setBusy] = useState(false)

  async function load() {
    setBusy(true)
    setError(undefined)
    try {
      setJob(await getJob(jobId.trim()))
    } catch (err) {
      setJob(undefined)
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (!job) return
    setBusy(true)
    setError(undefined)
    try {
      setJob(await stopJob(job.job_id))
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel title="Job 控制" subtitle="GET /api/jobs/{job_id} · POST /api/jobs/{job_id}/stop" dense>
      <div className="flex flex-wrap gap-2">
        <input
          className={`${inputClass} w-64`}
          placeholder="job id"
          value={jobId}
          onChange={(e) => setJobId(e.target.value)}
          spellCheck={false}
        />
        <Button kind="default" disabled={busy || !jobId.trim()} onClick={() => void load()}>
          查询
        </Button>
        <Button kind="danger" disabled={busy || !job} onClick={() => void stop()}>
          停止
        </Button>
      </div>

      <ErrorNotice error={error} context="操作 job" compact />

      {job ? (
        <KeyValueGrid>
          <KeyValue label="job_id">
            <code className="break-all font-mono text-xs">{job.job_id}</code>
          </KeyValue>
          <KeyValue label="状态">
            <Badge tone={job.status === 'failed' ? 'danger' : 'active'} mono>
              {job.status}
            </Badge>
          </KeyValue>
          <KeyValue label="pid">{job.pid ?? '—'}</KeyValue>
          <KeyValue label="exit_code">{job.exit_code ?? '—'}</KeyValue>
          <KeyValue label="port">{job.port ?? '—'}</KeyValue>
          <KeyValue label="ready">{job.ready ? 'true' : 'false'}</KeyValue>
          <KeyValue label="started_at">{formatTime(job.started_at)}</KeyValue>
        </KeyValueGrid>
      ) : (
        <Hint>{error === undefined ? '未查询 job。' : errorMessage(error)}</Hint>
      )}
    </Panel>
  )
}
