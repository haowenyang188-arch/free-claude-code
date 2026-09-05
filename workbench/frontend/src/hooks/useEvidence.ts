import { useResource, type ResourceState } from './useResource'
import { getSopEvidence } from '../services/sopApi'
import type { ReviewEvidenceResponse } from '../types/sop'

/**
 * 某个 SOP run 的评审证据投影（GET /api/sop-runs/{id}/evidence）。
 * evidence_valid 由后端判定；解析失败 / 跨 Attempt 一律 invalid。
 */
export function useEvidence(
  runId: string,
  options: { intervalMs?: number | null; enabled?: boolean } = {},
): ResourceState<ReviewEvidenceResponse[]> {
  return useResource(() => getSopEvidence(runId), [runId], options)
}
