import { useCallback, useMemo } from 'react'
import { useResource, type ResourceState } from './useResource'
import {
  getAttemptChain,
  getSopArtifacts,
  getSopAttempts,
  getSopEvents,
  getSopEvidence,
  getSopHandoffs,
  getSopRun,
  getSopSteps,
  getSopTasks,
  getSopTrace,
} from '../services/sopApi'
import type {
  ArtifactResponse,
  AttemptChainResponse,
  AttemptResponse,
  HandoffResponse,
  ReviewEvidenceResponse,
  SopEventResponse,
  SopRunStatusResponse,
  SopTraceResponse,
  StepRunResponse,
  TaskResponse,
} from '../types/sop'

export interface SopRunBundle {
  runId: string
  enabled: boolean
  run: ResourceState<SopRunStatusResponse>
  steps: ResourceState<StepRunResponse[]>
  tasks: ResourceState<TaskResponse[]>
  artifacts: ResourceState<ArtifactResponse[]>
  attempts: ResourceState<AttemptResponse[]>
  evidence: ResourceState<ReviewEvidenceResponse[]>
  chain: ResourceState<AttemptChainResponse>
  handoffs: ResourceState<HandoffResponse[]>
  events: ResourceState<SopEventResponse[]>
  trace: ResourceState<SopTraceResponse>
  /** 任一子请求的错误优先展示，用于「后端不可用」提示。 */
  firstError: unknown
  refreshAll: () => void
}

/**
 * 一个 SOP run 的全部只读数据集中在这里拉取，
 * 各面板共享同一份结果，避免每个 tab 各发一遍请求。
 */
export function useSopRunBundle(
  runId: string,
  pollMs: number,
  refreshToken: number,
): SopRunBundle {
  const enabled = Boolean(runId)
  const options = { intervalMs: pollMs, enabled }

  const run = useResource(() => getSopRun(runId), [runId, refreshToken], options)
  const steps = useResource(() => getSopSteps(runId), [runId, refreshToken], options)
  const tasks = useResource(() => getSopTasks(runId), [runId, refreshToken], options)
  const artifacts = useResource(() => getSopArtifacts(runId), [runId, refreshToken], options)
  const attempts = useResource(() => getSopAttempts(runId), [runId, refreshToken], options)
  const evidence = useResource(() => getSopEvidence(runId), [runId, refreshToken], options)
  const handoffs = useResource(() => getSopHandoffs(runId), [runId, refreshToken], options)
  const events = useResource(() => getSopEvents(runId), [runId, refreshToken], options)
  const trace = useResource(() => getSopTrace(runId), [runId, refreshToken], options)

  // 重试链的入口取本 run 最新开始的 Attempt（链会沿 previous_attempt_id 回溯到链首）。
  const chainStartAttemptId = useMemo(() => {
    const list = attempts.data ?? []
    let best: AttemptResponse | null = null
    for (const attempt of list) {
      if (best === null || (attempt.started_at ?? '') > (best.started_at ?? '')) {
        best = attempt
      }
    }
    return best?.id ?? null
  }, [attempts.data])

  const chain = useResource(
    () => getAttemptChain(chainStartAttemptId as string),
    [runId, refreshToken, chainStartAttemptId],
    { ...options, enabled: enabled && Boolean(chainStartAttemptId) },
  )

  const refreshAll = useCallback(() => {
    run.refresh()
    steps.refresh()
    tasks.refresh()
    artifacts.refresh()
    attempts.refresh()
    evidence.refresh()
    chain.refresh()
    handoffs.refresh()
    events.refresh()
    trace.refresh()
  }, [run, steps, tasks, artifacts, attempts, evidence, chain, handoffs, events, trace])

  const firstError =
    run.error ??
    steps.error ??
    tasks.error ??
    artifacts.error ??
    attempts.error ??
    evidence.error ??
    chain.error ??
    handoffs.error ??
    events.error
    ?? trace.error

  return {
    runId,
    enabled,
    run,
    steps,
    tasks,
    artifacts,
    attempts,
    evidence,
    chain,
    handoffs,
    events,
    trace,
    firstError,
    refreshAll,
  }
}
