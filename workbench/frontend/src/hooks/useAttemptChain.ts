import { useResource, type ResourceState } from './useResource'
import { getAttemptChain } from '../services/sopApi'
import type { AttemptChainResponse } from '../types/sop'

/**
 * 沿 previous_attempt_id 回溯的 REWORK 重试链（GET /api/attempts/{id}/chain）。
 * attemptId 为 null 时不发请求（useResource.enabled=false）。
 */
export function useAttemptChain(
  attemptId: string | null,
  options: { intervalMs?: number | null; enabled?: boolean } = {},
): ResourceState<AttemptChainResponse> {
  const enabled = options.enabled !== false && Boolean(attemptId)
  return useResource(
    () => getAttemptChain(attemptId as string),
    [attemptId],
    { ...options, enabled },
  )
}
