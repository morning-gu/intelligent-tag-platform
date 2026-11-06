// 生产任务 API（Task 12）：/generation/tasks 列表/详情/创建/重试的类型契约
// 与取数函数。类型形状与后端 tagplatform/api/routers/generation.py 的
// Pydantic 模型一致；列表按 created_at 降序、limit/offset 分页（后端默认
// 100/0，前端一次拉默认页即可）；重试仅 failed 可用（422/404 经 client 的
// ApiError.message 透出后端 detail）。
import { apiFetch } from './client'
import type { ReviewEvidence } from './review'

/** 任务列表行（与后端 GenerationTaskOut 一致；asset_id 经 media_context
 * 关联解析，无上下文时为 null）。 */
export interface GenerationTaskSummary {
  task_id: string
  asset_id: string | null
  mode: string
  status: string
  model_version: string | null
  prompt_version: string | null
  error: string | null
  created_at: string | null
  finished_at: string | null
}

/** 任务结果行（与后端 GenerationResultOut 一致）：id 为 pre_asset_tag 行
 * 主键（审核中心行操作的定位键）；evidence 与审核列表证据同形。 */
export interface GenerationTaskResult {
  id: number
  tag_name: string
  origin: string
  relevance_score: number | null
  confidence: number | null
  review_status: string
  target_type: string
  evidence: ReviewEvidence[]
}

/** 任务详情：列表字段之上附该任务全部结果摘要行（按入库行序）。 */
export interface GenerationTaskDetail extends GenerationTaskSummary {
  results: GenerationTaskResult[]
}

/** 创建请求的生成策略：未填字段交由后端默认值（max_tags=30 /
 * min_confidence=0.7）；前端仅在用户填写时逐字段携带。 */
export interface GenerationPolicyPayload {
  max_tags?: number
  min_confidence?: number
}

/** 批量创建请求体：资产 id 非空列表 + mode + 可选策略。 */
export interface GenerationTaskCreatePayload {
  asset_ids: string[]
  mode: string
  policy?: GenerationPolicyPayload
}

/** 创建响应单条：任务标识 + 资产 + 提交后状态（同步模式即终态）。 */
export interface GenerationTaskCreateItem {
  task_id: string
  asset_id: string
  status: string
}

/** 任务列表查询参数：undefined 字段不拼入查询串。 */
export interface GenerationTaskQuery {
  status?: string
  mode?: string
  asset_id?: string
}

/** 任务列表：status/mode/asset_id 过滤，created_at 降序（默认 limit=100）。 */
export function fetchGenerationTasks(
  query: GenerationTaskQuery = {},
): Promise<GenerationTaskSummary[]> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== '') {
      params.set(key, value)
    }
  }
  const search = params.toString()
  return apiFetch<GenerationTaskSummary[]>(
    `/api/v1/generation/tasks${search ? `?${search}` : ''}`,
  )
}

/** 任务详情 + 全部结果摘要行；未知 task_id → 404。 */
export function fetchGenerationTask(taskId: string): Promise<GenerationTaskDetail> {
  return apiFetch<GenerationTaskDetail>(`/api/v1/generation/tasks/${taskId}`)
}

/** 批量创建任务：mode 未知/资产无识别内容 → 422、资产不存在 → 404
 * （均经 ApiError.message 透出后端 detail）。 */
export function createGenerationTasks(
  payload: GenerationTaskCreatePayload,
): Promise<{ tasks: GenerationTaskCreateItem[] }> {
  return apiFetch<{ tasks: GenerationTaskCreateItem[] }>('/api/v1/generation/tasks', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** 重试失败任务：仅 failed 可重试（否则 422）；响应为提交受理态 queued，
 * 实际终态经 GET 查询（前端重试成功后直接刷新列表）。 */
export function retryGenerationTask(
  taskId: string,
): Promise<{ task_id: string; status: string }> {
  return apiFetch<{ task_id: string; status: string }>(
    `/api/v1/generation/tasks/${taskId}/retry`,
    { method: 'POST' },
  )
}
