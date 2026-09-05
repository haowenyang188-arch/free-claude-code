/**
 * SOP 控制台的 TypeScript 契约镜像。
 *
 * 来源（不要自行臆造字段）：
 *   workbench/backend/sop_models.py      -> SOP 请求/响应模型
 *   workbench/backend/domain/models.py   -> 状态与类型枚举、SopDefinition
 *   workbench/backend/main.py            -> 路由形状（_approval_payload / _job_payload）
 *
 * 遗留 run / task / agent 类型复用既有的 `types/index.ts`，这里只补 SOP 侧
 * 与既有文件缺失的部分。
 */

export type JsonRecord = Record<string, unknown>

/* ------------------------------------------------------- SOP 状态枚举 */

/** domain/models.py :: SopRunStatus */
export type SopRunStatus =
  | 'created'
  | 'running'
  | 'paused'
  | 'waiting_review'
  | 'completed'
  | 'failed'
  | 'cancelled'

/** domain/models.py :: StepStatus */
export type StepStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'validating'
  | 'waiting_review'
  | 'completed'
  | 'rejected'
  | 'rework'
  | 'blocked'
  | 'failed'
  | 'paused'

/** domain/models.py :: TaskStatus（SOP 任务） */
export type SopTaskStatus =
  | 'pending'
  | 'assigned'
  | 'running'
  | 'validating'
  | 'accepted'
  | 'rejected'
  | 'rework'
  | 'failed'

/** domain/models.py :: ArtifactType */
export type ArtifactType =
  | 'text'
  | 'research_report'
  | 'architecture_spec'
  | 'implementation'
  | 'test_report'
  | 'review_report'
  | 'final_summary'
  | 'file'
  | 'diff'
  | 'json'
  | 'plan'

/** domain/models.py :: HandoffStatus */
export type HandoffStatus = 'draft' | 'ready' | 'accepted' | 'rejected'

/**
 * domain/models.py :: HandoffMessageType。
 * P0 契约核对（2026-09-03）：GET /api/sop-runs/{id}/handoffs 的 HandoffResponse
 * **现已真实返回** message_type（sop_models.py:102，default "handoff"），
 * 此 union 直接用于类型标注；不再依赖事件流推断作为唯一来源。
 */
export type HandoffMessageType =
  | 'handoff'
  | 'plan_ready'
  | 'review_request'
  | 'rework'
  | 'rework_completed'
  | 'plan_invalid'
  | 'plan_blocked'
  | 'pass'

/** domain/models.py :: ReviewStatus（含 Engine policy gate 的 policy_blocked） */
export type ReviewStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'changes_requested'
  | 'policy_blocked'

/** domain/models.py :: ValidationStatus */
export type ValidationStatus = 'pending' | 'accepted' | 'rejected' | 'error'

/* ------------------------------------------------- SOP 请求 / 响应体 */

/** sop_models.py :: StartSopRunRequest（控制台下发时显式开启 auto_execute） */
export interface StartSopRunRequest {
  goal_description: string
  sop_definition_id: string
  project_id?: string
  acceptance_criteria?: string[]
  constraints?: string[]
  metadata?: JsonRecord
  /** True 时创建后立即触发后台编排（后端 sop_models.py:20） */
  auto_execute?: boolean
}

/** sop_models.py :: StartSopRunResponse */
export interface StartSopRunResponse {
  sop_run_id: string
  goal_id: string
  status: string
  started_at: string
}

/** sop_models.py :: SopRunStatusResponse */
export interface SopRunStatusResponse {
  id: string
  goal_id: string
  sop_definition_id: string
  status: SopRunStatus | string
  started_at: string
  completed_at: string | null
  step_count: number
  steps_completed: number
  steps_ready: number
  steps_running: number
  metadata: JsonRecord
}

/** sop_models.py :: StepRunResponse */
export interface StepRunResponse {
  id: string
  sop_run_id: string
  step_id: string
  stage_run_id: string
  status: StepStatus | string
  task_id: string | null
}

/** sop_models.py :: TaskResponse */
export interface TaskResponse {
  id: string
  step_run_id: string
  title: string
  description: string
  role_id: string
  status: SopTaskStatus | string
  input_artifact_ids: string[]
  output_artifact_ids: string[]
}

/** sop_models.py :: ArtifactResponse */
export interface ArtifactResponse {
  id: string
  task_id: string
  type: ArtifactType | string
  uri: string | null
  summary: string | null
  sha256: string | null
  accepted: boolean
  created_at: string
  /** 产出该 Artifact 的 Attempt（schema-v2；legacy 为 null） */
  attempt_id: string | null
  /** Artifact 自身字段（domain/models.py::Artifact.producer_step_run_id） */
  producer_step_run_id: string | null
  /** 路由层从 Task.role_id join 出来的投影（Artifact 模型上不存在 role） */
  role_id: string | null
}

/** sop_models.py :: AttemptResponse（sop_run_id / artifact_ids 为路由投影） */
export interface AttemptResponse {
  id: string
  task_id: string
  sequence: number
  session_id: string | null
  runtime_id: string | null
  previous_attempt_id: string | null
  started_at: string | null
  completed_at: string | null
  status: string
  sop_run_id: string
  artifact_ids: string[]
}

/** sop_models.py :: ReviewEvidenceResponse */
export interface ReviewEvidenceResponse {
  review_id: string
  /** 被评审的执行 Attempt（schema-v2 前记录可能为 null） */
  reviewed_attempt_id: string | null
  /** Review.status（pending / approved / rejected / changes_requested / policy_blocked） */
  review_status: ReviewStatus | string
  /** 解析自 reviewer_report.content 的 result；解析失败为 null */
  outcome: string | null
  blocking_items: string[]
  outcome_parse_error: string | null
  execution_diff: ArtifactResponse | null
  execution_test_report: ArtifactResponse | null
  reviewer_report: ArtifactResponse | null
  reviewer_attempt_id: string | null
  evidence_valid: boolean
  evidence_complete: boolean
  created_at: string
}

/** sop_models.py :: AttemptChainResponse */
export interface AttemptChainResponse {
  attempts: AttemptResponse[]
  root_task_id: string
  chain_length: number
  /** previous_attempt_id 成环被截断时为 true，前端必须显式提示 */
  truncated: boolean
}

/** main.py :: get_artifact_content */
export interface ArtifactContentResponse {
  id: string
  type: string
  sha256: string
  content: string
}

/**
 * sop_models.py :: HandoffResponse —— collaboration_handoff_view 的完整投影。
 * P0 契约核对（2026-09-03）：后端真实返回 19 个字段（main.py:672-692），
 * 包括 message_type / brief / reply_to_handoff_id / correlation_id /
 * source+target role & runtime 投影 / dispatchable / dispatch_reason。
 * 此前的类型只声明了前 8 个字段（缺失字段在运行时真实存在，前端只是没声明）。
 */
export interface HandoffResponse {
  id: string
  from_task_id: string
  to_step_id: string
  status: HandoffStatus | string
  artifact_ids: string[]
  created_at: string | null
  accepted_at: string | null
  message_type: HandoffMessageType | string
  brief: string
  reply_to_handoff_id: string | null
  correlation_id: string | null
  source_role_id: string | null
  target_role_id: string | null
  source_runtime_id: string | null
  target_runtime_id: string | null
  from_role_id: string | null
  to_role_id: string | null
  dispatchable: boolean
  dispatch_reason: string | null
}

/** sop_models.py :: SopHandoffDispatchResponse */
export interface SopHandoffDispatchResponse {
  sop_run_id: string
  handoff_id: string
  status: string
  target_step_id: string
  target_role_id: string
  target_runtime_id: string
  runtime_mode: string
  task_id: string
  attempt_id: string | null
  workflow_session_id: string | null
  session_id: string | null
  event_id: string
  outgoing_handoff_id: string | null
  artifact_ids: string[]
}

/** sop_models.py :: SopEventResponse */
export interface SopEventResponse {
  id: string
  stream_id: string
  sequence: number
  event_type: string
  payload: JsonRecord
  occurred_at: string
}

/** sop_models.py :: SopTraceEntryResponse —— 已脱敏的 SOP Engine 审计条目。 */
export interface SopTraceEntryResponse {
  sequence: number
  event_type: string
  role_id: string | null
  runtime_id: string | null
  phase: string
  message: string
  tool: string | null
  status: string
  occurred_at: string
}

/** sop_models.py :: SopTraceResponse —— 不含 provider payload 的只读过程投影。 */
export interface SopTraceResponse {
  sop_run_id: string
  mode: string
  provider_events_available: boolean
  empty: boolean
  entries: SopTraceEntryResponse[]
}

/** sop_models.py :: SopControlRequest —— 只接受 pause / resume / cancel */
export type SopControlAction = 'pause' | 'resume' | 'cancel'

export interface SopControlRequest {
  action: SopControlAction
  reason?: string | null
}

/** main.py :: control_sop_run 的返回体 */
export interface SopControlResponse {
  sop_run_id: string
  action: string
  status: string
  reason: string | null
}

/* ---------------------------------------------------- SOP 定义注册 */

/** domain/models.py :: AcceptanceCriteria */
export interface AcceptanceCriteria {
  id: string
  description: string
  required?: boolean
}

/** domain/models.py :: ExecutionMode */
export type ExecutionMode = 'sequential' | 'parallel'

/** domain/models.py :: StepDefinition */
export interface StepDefinition {
  id: string
  name: string
  role_id: string
  required_capabilities?: string[]
  allowed_tools?: string[]
  context_scope?: string[]
  output_type?: ArtifactType | string
  depends_on?: string[]
  execution_mode?: ExecutionMode
  requires_review?: boolean
  validator_id?: string | null
  retry_limit?: number
  handoff_to?: string | null
  instructions?: string
  acceptance_criteria?: AcceptanceCriteria[]
}

/** domain/models.py :: StageDefinition */
export interface StageDefinition {
  id: string
  name: string
  steps: StepDefinition[]
}

/** domain/models.py :: SopOrigin */
export type SopOrigin = 'fixed' | 'template' | 'ai_instantiated' | 'ai_replanned'

/** domain/models.py :: SopDefinition —— POST /api/sop-definitions 的 body */
export interface SopDefinition {
  id: string
  name: string
  version?: number
  origin?: SopOrigin
  template_id?: string | null
  description?: string
  input_schema?: JsonRecord
  output_schema?: JsonRecord
  stages: StageDefinition[]
  metadata?: JsonRecord
}

/**
 * GET /api/sop-definitions 的每一项（main.py list_sop_definitions 投影）。
 * P0 契约核对（2026-09-03）：后端真实返回 id/name/version/description/
 * step_count/stages[{id,name}]/steps[step.id] —— **没有 goal_template**（旧类型臆造，
 * 已移除）。注册定义在 service registry 中，重启后不持久化（见 contract gaps）。
 */
export interface SopDefinitionSummary {
  id: string
  name: string
  version: number | string
  description: string | null
  step_count: number
  stages: { id: string; name: string }[]
  steps: string[]
}

/** POST /api/sop-definitions 的返回体 */
export interface RegisterSopDefinitionResponse {
  id: string
  name: string
  version: number | string
}

/** GET /api/sop-runs 的每一项 */
export interface SopRunSummary {
  sop_run_id: string
  sop_definition_id: string
  sop_version: number | string
  status: SopRunStatus | string
  current_step_id: string | null
  started_at: string | null
  completed_at: string | null
}

/* ------------------------------------------------------ 审批 / Job */

/**
 * main.py :: decide_approval —— decision 的真实取值是
 * approve / reject / cancel，不是 deny。
 */
export type ApprovalDecision = 'approve' | 'reject' | 'cancel'

export type ApprovalProvider = 'codex_cli' | 'claude_cli'

/** main.py :: ApprovalDecisionRequest */
export interface ApprovalDecisionRequest {
  provider?: ApprovalProvider
  command_hash: string
  one_shot_id?: string | null
  thread_id?: string | null
  turn_id?: string | null
  item_id?: string | null
  approval_id?: string | null
}

/** main.py :: _approval_payload */
export interface ApprovalRecordPayload {
  provider: string
  session_id: string
  call_id: string
  one_shot_id: string | null
  thread_id: string | null
  item_id: string | null
  approval_id: string | null
  turn_id: string | null
  normalized_command: string | null
  argv: string[]
  cwd: string
  workspace_target: string | null
  requested_permission: string
  permission_scope: string | null
  patch_identity: string | null
  command_hash: string
  risk: string
  status: string
  created_at: string
  expires_at: string
  approved_at: string | null
  consumed_at: string | null
  reason: string | null
}

/** main.py :: _job_payload */
export interface JobRecordPayload {
  job_id: string
  call_id: string
  command_hash: string
  pid: number | null
  status: string
  background: boolean
  port: number | null
  health_url: string | null
  started_at: string
  ready: boolean
  exit_code: number | null
}

/** GET /api/auth/status */
export interface AuthStatusResponse {
  authenticated: boolean
  required: boolean
}
