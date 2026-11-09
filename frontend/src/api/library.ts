// 标签库 API（v2，JSON 模拟）：目录树/标签列表/统计与入库预览。
// 本期正式的 tag_catalog / tag_define 由外部提供，生成与审核都读
// data/taglibrary 的 JSON；这些接口是前端目录选择器的唯一数据源。
import { apiFetch } from './client'

/** 目录节点（与后端 /library/tree 的节点形状一致）。 */
export interface LibraryDirNode {
  dir_id: string
  dir_name: string
  level: number
  dir_type: string
  applicable_scope: string
  allows_new_tags: boolean
  description: string
  path: string
  tag_count: number
  children: LibraryDirNode[]
}

/** 标签行（与后端 /library/tags 的行形状一致）。 */
export interface LibraryTagRow {
  tag_id: string
  tag_name: string
  tag_type: string
  dir_id: string
  dir_name: string | null
  path: string
  description: string
  aliases: string[]
  status: string
}

/** 库统计：目录数/标签数/可扩展目录数/粒度与类型分布。 */
export interface LibraryStats {
  version: string
  source: string
  dirs: number
  tags: number
  dynamic_dirs: number
  scopes: Record<string, number>
  tag_types: Record<string, number>
}

/** 入库预览单行（将来 INSERT tag_define 的形状）。 */
export interface IngestTagRow {
  tag_id: string
  dir_id: string
  dir_name: string | null
  tag_name: string
  tag_type: string
  description: string
  aliases: string[]
  status: string
  source: Record<string, unknown>
}

/** 别名预览单行（将来并入 tag_define.aliases）。 */
export interface IngestAliasRow {
  tag_id: string
  tag_name: string
  dir_id: string
  alias: string
  source: Record<string, unknown>
}

/** 目录树（含可扩展标记与直接标签数）。 */
export function fetchLibraryTree(): Promise<{ version: string; dirs: LibraryDirNode[] }> {
  return apiFetch<{ version: string; dirs: LibraryDirNode[] }>('/api/v1/library/tree')
}

/** 库统计。 */
export function fetchLibraryStats(): Promise<LibraryStats> {
  return apiFetch<LibraryStats>('/api/v1/library/stats')
}

/** 标签列表：可按目录（含子目录）与名称/别名关键字过滤。 */
export function fetchLibraryTags(
  params: { dir_id?: string; query?: string; limit?: number } = {},
): Promise<{ total: number; items: LibraryTagRow[] }> {
  const search = new URLSearchParams()
  if (params.dir_id) search.set('dir_id', params.dir_id)
  if (params.query) search.set('query', params.query)
  if (params.limit !== undefined) search.set('limit', String(params.limit))
  const qs = search.toString()
  return apiFetch(`/api/v1/library/tags${qs ? `?${qs}` : ''}`)
}

/** 入库预览：待写 tag_define 的行 + 待写别名。 */
export function fetchIngestPreview(): Promise<{
  tag_define: IngestTagRow[]
  aliases: IngestAliasRow[]
  tag_define_count: number
  alias_count: number
}> {
  return apiFetch('/api/v1/ingest/preview')
}

/** 目录树 → antd TreeSelect 数据（只列可扩新词的目录可选）。 */
export interface DirSelectNode {
  value: string
  title: string
  selectable: boolean
  children?: DirSelectNode[]
}

export function toDirSelectNodes(nodes: LibraryDirNode[]): DirSelectNode[] {
  return nodes.map((node) => ({
    value: node.dir_id,
    title: node.allows_new_tags ? `${node.dir_name}（可扩展）` : node.dir_name,
    selectable: node.allows_new_tags,
    children: node.children.length > 0 ? toDirSelectNodes(node.children) : undefined,
  }))
}
