/**
 * SOP 控制台的全部后端调用。
 *
 * 全部复用 `services/api.ts` 的 requestJson（credentials: 'include'、
 * 错误解析 detail.error.message / detail.detail），不再自建 fetch 封装。
 * 路径与 payload 形状严格对齐 workbench/backend/main.py（后端已冻结）。
 */

import { requestJson, api } from './api'
import type {
  ApprovalDecision,
  ApprovalDecisionRequest,
  ApprovalProvider,
  ApprovalRecordPayload,
  ArtifactContentResponse,
  ArtifactResponse,
  AttemptChainResponse,
  AttemptResponse,
  HandoffResponse,
  JobRecordPayload,
  RegisterSopDefinitionResponse,
  ReviewEvidenceResponse,
  SopControlAction,
  SopControlResponse,
  SopDefinition,
  SopDefinitionSummary,
  SopEventResponse,
  SopHandoffDispatchResponse,
  SopRunStatusResponse,
  SopRunSummary,
  StartSopRunRequest,
  StartSopRunResponse,
  StepRunResponse,
  SopTraceResponse,
  TaskResponse,
} from '../types/sop'

const enc = encodeURIComponent

/* ------------------------------------------------------- SOP 定义 */

/** GET /api/sop-definitions —— 下发前必须先有一个已注册的定义。 */
export const listSopDefinitions = () =>
  requestJson<SopDefinitionSummary[]>('/sop-definitions')

/**
 * POST /api/sop-definitions
 * body 为完整 SopDefinition（domain/models.py），注册后才能被 POST /api/sop-runs 找到。
 */
export const registerSopDefinition = (definition: SopDefinition) =>
  requestJson<RegisterSopDefinitionResponse>('/sop-definitions', {
    method: 'POST',
    body: JSON.stringify(definition),
  })

/* ---------------------------------------------------------- SOP run */

/** GET /api/sop-runs —— run 列表（此前无此接口，靠它做 run 选择器）。 */
export const listSopRuns = () => requestJson<SopRunSummary[]>('/sop-runs')

/** POST /api/sop-runs */
export const startSopRun = (body: StartSopRunRequest) =>
  requestJson<StartSopRunResponse>('/sop-runs', {
    method: 'POST',
    body: JSON.stringify(body),
  })

/**
 * POST /api/sop-runs/{run_id}/control
 * body 为 { action, reason? }；action 只允许 pause / resume / cancel。
 * 已终结（completed / cancelled）的 run 会返回 409。
 */
export const controlSopRun = (
  runId: string,
  action: SopControlAction,
  reason?: string,
) =>
  requestJson<SopControlResponse>(`/sop-runs/${enc(runId)}/control`, {
    method: 'POST',
    body: JSON.stringify({ action, reason: reason?.trim() ? reason.trim() : null }),
  })

export const getSopRun = (runId: string) =>
  requestJson<SopRunStatusResponse>(`/sop-runs/${enc(runId)}`)

export const getSopSteps = (runId: string) =>
  requestJson<StepRunResponse[]>(`/sop-runs/${enc(runId)}/steps`)

export const getSopTasks = (runId: string) =>
  requestJson<TaskResponse[]>(`/sop-runs/${enc(runId)}/tasks`)

export const getSopArtifacts = (runId: string) =>
  requestJson<ArtifactResponse[]>(`/sop-runs/${enc(runId)}/artifacts`)

export const getSopHandoffs = (runId: string) =>
  requestJson<HandoffResponse[]>(`/sop-runs/${enc(runId)}/handoffs`)

/** POST /api/sop-runs/{run_id}/handoffs/{handoff_id}/dispatch. */
export const dispatchSopHandoff = (runId: string, handoffId: string) =>
  requestJson<SopHandoffDispatchResponse>(
    `/sop-runs/${enc(runId)}/handoffs/${enc(handoffId)}/dispatch`,
    { method: 'POST' },
  )

/** GET /api/sop-runs/{id}/events?after=N —— Event Store 游标读取。 */
export const getSopEvents = (runId: string, after = 0) =>
  requestJson<SopEventResponse[]>(`/sop-runs/${enc(runId)}/events?after=${after}`)

/** GET /api/sop-runs/{run_id}/trace —— 脱敏的 SOP Engine 协作过程。 */
export const getSopTrace = (runId: string) =>
  requestJson<SopTraceResponse>(`/sop-runs/${enc(runId)}/trace`)

/** GET /api/artifacts/{artifact_id}/content */
export const getArtifactContent = (artifactId: string) =>
  requestJson<ArtifactContentResponse>(`/artifacts/${enc(artifactId)}/content`)

/* ------------------------------------------- Phase 5A：Attempt 血缘只读接口 */

const REVIEW_OUTCOMES = new Set(['PASS', 'REWORK', 'PLAN_INVALID'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isString(value: unknown): value is string {
  return typeof value === 'string'
}

function isStringOrNull(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function assertArtifact(value: unknown, where: string): void {
  if (
    !isRecord(value) ||
    !isString(value.id) ||
    !isString(value.task_id) ||
    !isString(value.type) ||
    !isStringOrNull(value.uri) ||
    !isStringOrNull(value.summary) ||
    !isStringOrNull(value.sha256) ||
    typeof value.accepted !== 'boolean' ||
    !isString(value.created_at) ||
    !isStringOrNull(value.attempt_id) ||
    !isStringOrNull(value.producer_step_run_id) ||
    !isStringOrNull(value.role_id)
  ) {
    throw new Error(`${where}: Artifact 字段缺失或类型不合法`)
  }
}

function assertAttempt(value: unknown, where: string): void {
  if (
    !isRecord(value) ||
    !isString(value.id) ||
    !isString(value.task_id) ||
    typeof value.sequence !== 'number' ||
    !isStringOrNull(value.session_id) ||
    !isStringOrNull(value.runtime_id) ||
    !isStringOrNull(value.previous_attempt_id) ||
    !isStringOrNull(value.started_at) ||
    !isStringOrNull(value.completed_at) ||
    !isString(value.status) ||
    !isString(value.sop_run_id) ||
    !Array.isArray(value.artifact_ids) ||
    !value.artifact_ids.every(isString)
  ) {
    throw new Error(`${where}: Attempt 字段缺失或类型不合法`)
  }
}

function assertEvidenceList(value: unknown): ReviewEvidenceResponse[] {
  if (!Array.isArray(value)) {
    throw new Error('GET /evidence: 响应不是数组')
  }
  value.forEach((item, index) => {
    const where = `GET /evidence[${index}]`
    if (
      !isRecord(item) ||
      !isString(item.review_id) ||
      !isStringOrNull(item.reviewed_attempt_id) ||
      !isString(item.review_status) ||
      !isStringOrNull(item.outcome) ||
      !Array.isArray(item.blocking_items) ||
      !item.blocking_items.every(isString) ||
      !isStringOrNull(item.outcome_parse_error) ||
      !isStringOrNull(item.reviewer_attempt_id) ||
      typeof item.evidence_valid !== 'boolean' ||
      typeof item.evidence_complete !== 'boolean' ||
      !isString(item.created_at)
    ) {
      throw new Error(`${where}: 字段缺失或类型不合法`)
    }
    if (item.outcome !== null && !REVIEW_OUTCOMES.has(String(item.outcome))) {
      throw new Error(`${where}: outcome 非法枚举值 ${String(item.outcome)}`)
    }
    for (const [key, artifact] of [
      ['execution_diff', item.execution_diff],
      ['execution_test_report', item.execution_test_report],
      ['reviewer_report', item.reviewer_report],
    ] as const) {
      if (artifact !== null) assertArtifact(artifact, `${where}.${key}`)
    }
  })
  return value as ReviewEvidenceResponse[]
}

/** GET /api/sop-runs/{run_id}/attempts —— 响应经运行时 DTO 校验。 */
export const getSopAttempts = async (runId: string): Promise<AttemptResponse[]> => {
  const value = await requestJson<unknown>(`/sop-runs/${enc(runId)}/attempts`)
  if (!Array.isArray(value)) {
    throw new Error('GET /attempts: 响应不是数组')
  }
  value.forEach((item, index) => assertAttempt(item, `GET /attempts[${index}]`))
  return value as AttemptResponse[]
}

/** GET /api/sop-runs/{run_id}/evidence —— 响应经运行时 DTO 校验。 */
export const getSopEvidence = async (runId: string): Promise<ReviewEvidenceResponse[]> => {
  const value = await requestJson<unknown>(`/sop-runs/${enc(runId)}/evidence`)
  return assertEvidenceList(value)
}

/** GET /api/attempts/{attempt_id}/chain —— 响应经运行时 DTO 校验。 */
export const getAttemptChain = async (attemptId: string): Promise<AttemptChainResponse> => {
  const value = await requestJson<unknown>(`/attempts/${enc(attemptId)}/chain`)
  if (
    !isRecord(value) ||
    !Array.isArray(value.attempts) ||
    !isString(value.root_task_id) ||
    typeof value.chain_length !== 'number' ||
    typeof value.truncated !== 'boolean'
  ) {
    throw new Error('GET /chain: 响应结构不合法')
  }
  value.attempts.forEach((item, index) => assertAttempt(item, `GET /chain.attempts[${index}]`))
  return value as unknown as AttemptChainResponse
}

/* ------------------------------------------------- 遗留 run 事件回放 */

/** GET /api/runs/{run_id}/events?after=N —— 遗留 Event Store 回放。 */
export const getRunEvents = (runId: string, after = 0) =>
  requestJson<{ events: Record<string, unknown>[] }>(
    `/runs/${enc(runId)}/events?after=${after}`,
  )

/* ------------------------------------------------------------- 审批 */

/**
 * GET /api/approvals/{session_id}/{call_id}
 * command_hash（64 位 hex）是必填查询参数，缺失时后端直接 409
 * approval_integrity_mismatch。后端没有「待审批列表」接口，只能精确查询。
 */
export const getApproval = (
  sessionId: string,
  callId: string,
  commandHash: string,
  provider: ApprovalProvider = 'codex_cli',
) =>
  requestJson<ApprovalRecordPayload>(
    `/approvals/${enc(sessionId)}/${enc(callId)}` +
      `?command_hash=${enc(commandHash)}&provider=${enc(provider)}`,
  )

/** POST /api/approvals/{session_id}/{call_id}/{approve|reject|cancel} */
export const decideApproval = (
  sessionId: string,
  callId: string,
  decision: ApprovalDecision,
  body: ApprovalDecisionRequest,
) =>
  requestJson<ApprovalRecordPayload>(
    `/approvals/${enc(sessionId)}/${enc(callId)}/${enc(decision)}`,
    { method: 'POST', body: JSON.stringify(body) },
  )

/* ---------------------------------------------------------------- Job */

export const getJob = (jobId: string) =>
  requestJson<JobRecordPayload>(`/jobs/${enc(jobId)}`)

export const stopJob = (jobId: string) =>
  requestJson<JobRecordPayload>(`/jobs/${enc(jobId)}/stop`, { method: 'POST' })

/* --------------------------------------------------------- 复用既有 */

/** GET /api/agents → { agents: [...] }（包裹对象，不是裸数组） */
export const listAgents = api.getAgents

/** GET /api/tasks → { tasks: [...] }（包裹对象，不是裸数组） */
export const listLegacyTasks = api.getTasks

/** GET /api/runs/{run_id} */
export const getLegacyRun = api.getRun

/**
 * POST /api/runs/{run_id}/control
 * body 必须同时带 run_id 与 action（后端 ControlRequest 两个字段都无默认值）。
 */
export const controlLegacyRun = api.controlRun

/** POST /api/tasks/{id}/start */
export const startLegacyTask = api.startTask

/** POST /api/runs/{run_id}/message */
export const sendRunMessage = api.sendMessage
