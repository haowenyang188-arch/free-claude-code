import { useResource, type ResourceState } from './useResource'
import { getSopAttempts } from '../services/sopApi'
import type { AttemptResponse } from '../types/sop'

/**
 * 某个 SOP run 的全部执行 Attempt（GET /api/sop-runs/{id}/attempts）。
 * 与 useSopRunBundle 中其它资源共用同一轮询/刷新契约。
 */
export function useAttempts(
  runId: string,
  options: { intervalMs?: number | null; enabled?: boolean } = {},
): ResourceState<AttemptResponse[]> {
  return useResource(() => getSopAttempts(runId), [runId], options)
}
