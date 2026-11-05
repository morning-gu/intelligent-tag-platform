// 审核 API（v2 行即日志）：/review 列表与筛选项、/asset-tags 审核操作（单条
// 与批量）、/tags 标签搜索（合并弹窗数据源）的类型契约与取数函数 + 证据时间
// 戳格式化。类型形状与后端 tagplatform/api/routers/review.py、tags.py 的
// Pydantic 模型一致（词表 closed|open / pending|approved|rejected 与库 CHECK
// 约束同源）；审核操作的 409 并发冲突经 client 的 ApiError.status 识别。
// v1 的 mode/tag_type 维度随词表退役：列表行出 tag_id/candidate_name/dir_name，
// 筛选出 dirs，查询出 dir；merge payload 拆两支（并入既有标签 / 新建入库）。
import { apiFetch } from './client'
import type { TagKind, TagNode } from './catalog'

/** 产出来源词表（pre_asset_tag.origin，库 CHECK 约束）。 */
export type TagOrigin = 'closed' | 'open'

/** 审核状态词表（pre_asset_tag.review_status，库 CHECK 约束）。 */
export type ReviewStatus = 'pending' | 'approved' | 'rejected'

/** 审核动作（与后端 ReviewRequest.action 的 Literal 取值一致；批量端点仅
 * 接受 approve/reject）。 */
export type ReviewAction = 'approve' | 'reject' | 'modify' | 'merge'

/** 审核动作 payload（按动作分形，doc §6.2）：
 * - approve/reject：仅可选备注；
 * - modify：候选展示字段两键可选 + 备注（更新后直接 approve）；
 * - merge 两支互斥——并入既有标签（merge_to_tag_id）或新建标签入库
 *   （new_tag_name + new_dir_id 必填，描述/备注可选）。
 * 联合类型使 merge 两支混用（同时给 merge_to_tag_id 与 new_tag_name）与
 * 必填键缺失在编译期报错。 */
export type ReviewActionPayload =
  | { comment?: string }
  | { candidate_name?: string; candidate_description?: string; comment?: string }
  | { merge_to_tag_id: string }
  | {
      new_tag_name: string
      new_dir_id: string
      new_description?: string
      comment?: string
    }

/** 单条证据明细（与后端 EvidenceOut 一致）。 */
export interface ReviewEvidence {
  scene_id: string
  start_ms: number
  end_ms: number
  description: string
}

/** 审核列表单行（与后端 ReviewItemOut 一致）：tag_name = candidate_name
 * （open 候选/审核 modify 落在行内的展示字段）优先，否则回退目录定义名
 * （closed 行/已合并行）；dir_name = 定义目录名 or 候选目录名 or null。 */
export interface ReviewItem {
  id: number
  asset_id: string
  asset_title: string | null
  tag_name: string
  tag_id: string | null
  candidate_name: string | null
  origin: TagOrigin
  dir_name: string | null
  confidence: number | null
  relevance: number | null
  review_status: ReviewStatus
  evidence: ReviewEvidence[]
  task_id: string | null
}

/** GET /review/items 响应：当前页行 + 过滤后总数 + 页码（分页走列表 API）。 */
export interface ReviewItemsResponse {
  items: ReviewItem[]
  total: number
  page: number
}

/** GET /review/filters 响应：筛选下拉选项——statuses/origins 为契约固定
 * 枚举，dirs 为 tag_catalog 库内目录名去重升序（空库空列表）。 */
export interface ReviewFilters {
  statuses: ReviewStatus[]
  origins: TagOrigin[]
  dirs: string[]
}

/** 审核列表查询参数：undefined 字段不拼入查询串（0 为合法边界值，保留）。
 * dir 为目录名精确匹配（定义目录或候选目录任一命中）。 */
export interface ReviewItemQuery {
  status?: ReviewStatus
  confidence_min?: number
  confidence_max?: number
  dir?: string
  origin?: TagOrigin
  asset_id?: string
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

/** 筛选下拉选项（statuses/origins 契约固定枚举，dirs 库内动态推导）。 */
export function fetchReviewFilters(): Promise<ReviewFilters> {
  return apiFetch<ReviewFilters>('/api/v1/review/filters')
}

/** 单条审核：四动作即时落库，返回更新后的行摘要（与任务详情结果行同形）；
 * 行已被处理时后端 409（ApiError.status === 409，调用方按冲突分支处理）。 */
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

/** 批量审核（仅 approve/reject——modify/merge 载荷逐行各异，无批量语义）：
 * 逐条独立执行，单条失败（冲突/载荷非法/未知行）不影响其余。 */
export function batchReviewAssetTags(
  ids: number[],
  action: Extract<ReviewAction, 'approve' | 'reject'>,
  payload?: { comment?: string },
): Promise<{ results: BatchReviewItemResult[] }> {
  return apiFetch<{ results: BatchReviewItemResult[] }>('/api/v1/asset-tags/batch-review', {
    method: 'POST',
    body: JSON.stringify(payload !== undefined ? { ids, action, payload } : { ids, action }),
  })
}

/** 标签搜索（合并弹窗数据源）：走混合树列表端点，kind 默认 'tag' 只取标签，
 * q 对 name 与 aliases 子串匹配；返回节点即 catalog 的 TagNode 形状。 */
export function fetchTags(
  q: string,
  kind: TagKind = 'tag',
  limit = 20,
): Promise<{ items: TagNode[] }> {
  const params = new URLSearchParams()
  if (q !== '') {
    params.set('q', q)
  }
  params.set('kind', kind)
  params.set('limit', String(limit))
  return apiFetch<{ items: TagNode[] }>(`/api/v1/tags?${params.toString()}`)
}

/** 毫秒时间戳 → mm:ss（分钟位不封顶，超一小时自然进位；秒向下取整）。 */
export function formatTimestamp(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}
