// 清空/删除 API（clear-reset）：五个 DELETE 端点 + 计数预览。类型形状与
// 后端 orchestration/_shared.py 的 ClearCountsOut/ClearResultOut/
// ClearPreviewOut 一致（九键全量，缺省 0 不省略）；权限均为 admin（非
// admin → 403，未登录 → 401，经 ApiError 抛出）；running/processing 任务
// 在途 → 409 detail 文案透出。asset-tags/asset scope 预览必须带 asset_id。
import { apiFetch } from './client'

/** 清空/删除操作的行数快照（与后端 ClearCountsOut 同形，九键固定）。 */
export interface ClearCounts {
  pre_asset_tag: number
  asset_tag: number
  generation_tasks: number
  quality_evaluations: number
  asset_build_task: number
  media_assets: number
  scenes: number
  tag_define: number
  tag_catalog: number
}

/** DELETE 响应：ok + counts。 */
export interface ClearResult {
  ok: boolean
  counts: ClearCounts
}

/** 预览 scope 枚举（library 走 tags 预览端点，其余走 content 预览端点）。 */
export type ClearPreviewScope =
  | 'asset-tags'
  | 'all-tags'
  | 'asset'
  | 'all-assets'
  | 'library'

/** 预览响应：scope + counts（与对应 DELETE 的 counts 同源同形）。 */
export interface ClearPreview {
  scope: string
  counts: ClearCounts
}

/** 计数预览（只读）：确认弹窗"将删除 N 行"的数据源。 */
export function fetchClearPreview(
  scope: ClearPreviewScope,
  assetId?: string,
): Promise<ClearPreview> {
  if (scope === 'library') {
    return apiFetch<ClearPreview>('/api/v1/tags/clear-preview')
  }
  const params = new URLSearchParams({ scope })
  if (assetId !== undefined) {
    params.set('asset_id', assetId)
  }
  return apiFetch<ClearPreview>(`/api/v1/content/clear-preview?${params.toString()}`)
}

/** ① 清空单内容标签数据（含其生成任务历史）。 */
export function clearAssetTags(assetId: string): Promise<ClearResult> {
  return apiFetch<ClearResult>(`/api/v1/assets/${assetId}/tags`, {
    method: 'DELETE',
  })
}

/** ② 清空全部内容的标签数据（标签库与资产幸存）。 */
export function clearAllContentTags(): Promise<ClearResult> {
  return apiFetch<ClearResult>('/api/v1/content/tags', { method: 'DELETE' })
}

/** ③ 清空标签库（级联全部内容标签数据与任务历史）。 */
export function clearTagLibrary(): Promise<ClearResult> {
  return apiFetch<ClearResult>('/api/v1/tags', { method: 'DELETE' })
}

/** ④ 删除单内容资产（含子场景与全部关联行）。 */
export function deleteAsset(assetId: string): Promise<ClearResult> {
  return apiFetch<ClearResult>(`/api/v1/assets/${assetId}`, {
    method: 'DELETE',
  })
}

/** ⑤ 删除全部内容资产（标签库幸存）。 */
export function deleteAllAssets(): Promise<ClearResult> {
  return apiFetch<ClearResult>('/api/v1/assets', { method: 'DELETE' })
}
