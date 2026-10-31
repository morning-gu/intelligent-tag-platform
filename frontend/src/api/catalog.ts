// 标签库 API（Task 13）：/tags 目录列表（type/q/limit/offset）与单条详情
// 的类型契约与取数函数。类型形状与后端 tagplatform/api/routers/tags.py 的
// TagOut/TagDetail 一致；列表不展开 aliases（详情才含）。后端列表无 total
// 字段，分页由调用方按"多取一条探测"策略组装。
import { apiFetch } from './client'

/** 标签目录列表项（与后端 TagOut 一致）。 */
export interface TagCatalogItem {
  tag_id: string
  tag_name: string
  tag_type: string
  description: string
  parent_id: string | null
  status: string | null
}

/** 标签详情：列表字段之上展开 aliases 列表（与后端 TagDetail 一致）。 */
export interface TagCatalogDetail extends TagCatalogItem {
  aliases: string[]
}

/** 目录列表查询参数：undefined/空串字段不拼入查询串；limit/offset 为
 * 合法边界值 0/正整数时保留（offset=0 显式发送）。 */
export interface TagCatalogQuery {
  type?: string
  q?: string
  limit?: number
  offset?: number
}

/** GET /tags 响应：仅 items（无 total，分页探测由调用方处理）。 */
export interface TagCatalogListResponse {
  items: TagCatalogItem[]
}

/** 标签目录列表：type 精确过滤、q 对 tag_name 与 aliases 子串匹配，
 * 按 tag_id 排序，limit/offset 分页（后端默认 100/0）。 */
export function fetchTagCatalog(query: TagCatalogQuery = {}): Promise<TagCatalogListResponse> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== '') {
      params.set(key, String(value))
    }
  }
  const search = params.toString()
  return apiFetch<TagCatalogListResponse>(`/api/v1/tags${search ? `?${search}` : ''}`)
}

/** 单条标签详情（含 aliases）；未知 tag_id → 404。 */
export function fetchTagDetail(tagId: string): Promise<TagCatalogDetail> {
  return apiFetch<TagCatalogDetail>(`/api/v1/tags/${tagId}`)
}
