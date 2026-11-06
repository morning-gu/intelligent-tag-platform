// 标签库 API（v2 混合树）：/tags 目录+标签合并列表（q/parent_id/root/
// scope_id/kind/limit/offset）、单条详情、生命周期三端点与导出的类型契约
// 与取数函数。类型形状与后端 tagplatform/api/routers/tags.py 的
// TagNodeOut/TagNodeDetail 一致：目录与标签统一节点形状（kind 区分种类），
// 列表视图即展开 aliases；列表无 total 字段，分页由调用方按"多取一条
// 探测"策略组装。v1 的自由 type 分类随 tag_type 枚举化退役。
import { apiFetch } from './client'

/** 节点种类：目录或标签（后端 kind Query 参数同词表）。 */
export type TagKind = 'dir' | 'tag'

/** 目录类型：静态/动态（仅目录节点有值，标签节点恒为 null）。 */
export type DirType = 'dir_static' | 'dir_dynamic'

/** 混合树节点（与后端 TagNodeOut 一致）：id 对目录是 dir_id、对标签是
 * tag_id；目录无 status 列恒为 null（标签 active|deprecated）；has_children
 * 对目录 = 存在子目录或挂有标签（按全库判定），标签恒 false；aliases
 * 列表视图即展开。 */
export interface TagNode {
  id: string
  name: string
  kind: TagKind
  dir_type: DirType | null
  description: string
  parent_id: string | null
  status: string | null
  has_children: boolean
  aliases: string[]
}

/** 节点详情：列表字段之上补从根到本节点的名称/ID 路径（含本节点）。 */
export interface TagNodeDetail extends TagNode {
  path: string[]
  path_ids: string[]
}

/** 目录列表查询参数：undefined/空串字段不拼入查询串；root=false 与后端
 * 默认同值不发送（仅显式 true 序列化）；limit/offset 为合法边界值 0/
 * 正整数时保留（offset=0 显式发送）。 */
export interface TagCatalogQuery {
  q?: string
  parent_id?: string
  root?: boolean
  scope_id?: string
  kind?: TagKind
  limit?: number
  offset?: number
}

/** GET /tags 响应：仅 items（无 total，分页探测由调用方处理）。 */
export interface TagCatalogListResponse {
  items: TagNode[]
}

/** 混合树列表：目录与标签合并（目录先、名字序），q 对 name 与 aliases
 * 子串匹配，limit/offset 分页（后端默认 100/0）。 */
export function fetchTagCatalog(query: TagCatalogQuery = {}): Promise<TagCatalogListResponse> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === '' || value === false) {
      continue
    }
    params.set(key, String(value))
  }
  const search = params.toString()
  return apiFetch<TagCatalogListResponse>(`/api/v1/tags${search ? `?${search}` : ''}`)
}

/** 单条节点详情（含 aliases 与路径）；id 可为 dir_id 或 tag_id，未知 → 404。 */
export function fetchTagDetail(nodeId: string): Promise<TagNodeDetail> {
  return apiFetch<TagNodeDetail>(`/api/v1/tags/${nodeId}`)
}

/** 停用标签定义（仅 tag_define；目录 id → 404），返回更新后的节点详情。 */
export function deactivateTag(tagId: string): Promise<TagNodeDetail> {
  return apiFetch<TagNodeDetail>(`/api/v1/tags/${tagId}/deactivate`, { method: 'POST' })
}

/** 启用标签定义（仅 tag_define；目录 id → 404），返回更新后的节点详情。 */
export function activateTag(tagId: string): Promise<TagNodeDetail> {
  return apiFetch<TagNodeDetail>(`/api/v1/tags/${tagId}/activate`, { method: 'POST' })
}

/** 标签删除结果及目录页应跳转的上级目录。 */
export interface TagDeleteResponse {
  tag_id: string
  parent_id: string | null
  parent_name: string | null
}

/** 删除标签定义（仅 tag_define；目录 id → 404；被挂载/候选引用 → 409）。 */
export function deleteTag(tagId: string): Promise<TagDeleteResponse> {
  return apiFetch<TagDeleteResponse>(`/api/v1/tags/${tagId}`, { method: 'DELETE' })
}

/** 下载当前目录作用域或名称筛选结果的 Excel/XML 压缩包。 */
export async function downloadTagCatalog(query: Pick<TagCatalogQuery, 'q' | 'scope_id'> = {}): Promise<Blob> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== '') params.set(key, value)
  }
  const search = params.toString()
  const response = await fetch(`/api/v1/tags/export${search ? `?${search}` : ''}`, {
    credentials: 'include',
  })
  if (!response.ok) {
    throw new Error(`标签导出失败（HTTP ${response.status}）`)
  }
  return response.blob()
}
