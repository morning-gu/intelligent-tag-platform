// 审核 API（Task 10）：/review 列表与筛选项、/asset-tags 审核操作（单条
// 与批量）、/tags 标准标签搜索的类型契约与取数函数 + 证据时间戳格式化。
// 类型形状与后端 tagplatform/api/routers/review.py、tags.py 的 Pydantic
// 模型一致；审核操作的 409 并发冲突经 client 的 ApiError.status 识别。
import { apiFetch } from './client'

/** 审核动作（与后端 ReviewRequest.action 的 Literal 取值一致）。 */
export type ReviewAction = 'approve' | 'reject' | 'modify' | 'merge'

/** 审核动作 payload：modify 用展示字段别名（tag_name/tag_type/description——
 * generated 行落 candidate_*、catalog 行改展示字段，status 回 pending），
 * merge 用 merge_to_tag_id（必填）；comment 对两动作均可选。 */
export interface ReviewActionPayload {
  tag_name?: string
  tag_type?: string
  description?: string
  comment?: string
  merge_to_tag_id?: string
}

/** 单条证据明细（与后端 EvidenceOut 一致）。 */
export interface ReviewEvidence {
  scene_id: string
  start_ms: number
  end_ms: number
  description: string
}

/** 审核列表单行（与后端 ReviewItemOut 一致）：tag_name/tag_type 已按 origin
 * 感知解析（catalog/merged 行取目录字典、generated 行取候选字段），
 * confidence/relevance 缺失时为 null。 */
export interface ReviewItem {
  id: number
  asset_id: string
  asset_title: string | null
  tag_name: string
  origin: string
  mode: string | null
  tag_type: string | null
  confidence: number | null
  relevance: number | null
  review_status: string
  evidence: ReviewEvidence[]
  task_id: string | null
}

/** GET /review/items 响应：当前页行 + 过滤后总数 + 页码（分页走列表 API）。 */
export interface ReviewItemsResponse {
  items: ReviewItem[]
  total: number
  page: number
}

/** GET /review/filters 响应：筛选下拉选项（tag_types 库内动态去重）。 */
export interface ReviewFilters {
  tag_types: string[]
  statuses: string[]
  modes: string[]
}

/** 审核列表查询参数：undefined 字段不拼入查询串（0 为合法边界值，保留）。 */
export interface ReviewItemQuery {
  status?: string
  confidence_min?: number
  confidence_max?: number
  tag_type?: string
  mode?: string
  asset_id?: string
  origin?: string
  page?: number
  page_size?: number
}

/** 审核列表：后端默认 confidence 升序（低分先审），page/page_size 分页。 */
export function fetchReviewItems(query: ReviewItemQuery = {}): Promise<ReviewItemsResponse> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== '') {
      params.set(key, String(value))
    }
  }
  const search = params.toString()
  return apiFetch<ReviewItemsResponse>(`/api/v1/review/items${search ? `?${search}` : ''}`)
}

/** 筛选下拉选项（tag_types 动态，statuses/modes 契约固定枚举）。 */
export function fetchReviewFilters(): Promise<ReviewFilters> {
  return apiFetch<ReviewFilters>('/api/v1/review/filters')
}

/** 单条审核：四动作即时落库，返回更新后的行摘要；行已被处理时后端
 * 409（ApiError.status === 409，调用方按冲突分支处理）。 */
export function reviewAssetTag(
  id: number,
  action: ReviewAction,
  payload?: ReviewActionPayload,
): Promise<unknown> {
  return apiFetch<unknown>(`/api/v1/asset-tags/${id}/review`, {
    method: 'POST',
    body: JSON.stringify(payload !== undefined ? { action, payload } : { action }),
  })
}

/** 批量审核结果单条：ok=false 时 detail 携带失败原因（逐条独立不中断）。 */
export interface BatchReviewItemResult {
  id: number
  ok: boolean
  detail?: string | null
}

/** 批量审核：逐条独立执行，单条失败（冲突/载荷非法/未知行）不影响其余。 */
export function batchReviewAssetTags(
  ids: number[],
  action: ReviewAction,
  payload?: ReviewActionPayload,
): Promise<{ results: BatchReviewItemResult[] }> {
  return apiFetch<{ results: BatchReviewItemResult[] }>('/api/v1/asset-tags/batch-review', {
    method: 'POST',
    body: JSON.stringify(payload !== undefined ? { ids, action, payload } : { ids, action }),
  })
}

/** 标准标签目录项（合并目标选择用，与后端 TagOut 的取用字段一致）。 */
export interface TagOption {
  tag_id: string
  tag_name: string
  tag_type: string
  description: string
}

/** 标准标签搜索：q 对 tag_name 与 aliases 子串匹配（合并弹窗数据源）。 */
export function fetchTags(q: string, limit = 20): Promise<{ items: TagOption[] }> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (q !== '') {
    params.set('q', q)
  }
  return apiFetch<{ items: TagOption[] }>(`/api/v1/tags?${params.toString()}`)
}

/** 毫秒时间戳 → mm:ss（分钟位不封顶，超一小时自然进位；秒向下取整）。 */
export function formatTimestamp(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}
