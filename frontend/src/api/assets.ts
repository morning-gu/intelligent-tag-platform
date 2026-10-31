// 资产 API（Task 11）：/assets 列表与详情、/contexts 内容 JSON、
// /assets/{id}/tags 资产标签的类型契约与取数函数。类型形状与后端
// tagplatform/api/routers/content.py（AssetOut/AssetDetail/ContentJSON）、
// generation.py（AssetTagOut）一致；未知资产 → 404（ApiError.status）。
import { apiFetch } from './client'
import type { ReviewEvidence } from './review'

/** 资产列表项（与后端 AssetOut 一致，均非大字段）。 */
export interface AssetSummary {
  asset_id: string
  asset_type: string
  parent_id: string | null
  title: string
  duration_ms: number | null
  video_uri: string | null
  created_at: string | null
}

/** 上下文摘要项（与后端 ContextSummaryOut 一致，不含 content_json 大字段）。 */
export interface AssetContextSummary {
  context_id: string
  model_version: string | null
  recognition_status: string | null
  created_at: string | null
}

/** 资产详情：列表字段之上附其全部上下文摘要（按导入时间升序）。 */
export interface AssetDetailData extends AssetSummary {
  contexts: AssetContextSummary[]
}

/** 毫秒时间区间（与后端 TimeRange 一致）。 */
export interface SceneTimeRange {
  start_ms: number
  end_ms: number
}

/** 场景内单条台词（与后端 Dialogue 一致）。 */
export interface SceneDialogue {
  speaker: string
  text: string
  start_ms: number
  end_ms: number
}

/** 场景内识别出的实体（与后端 Entity 一致）。 */
export interface SceneEntity {
  entity_id: string
  name: string
  type: string
}

/** 场景内识别出的事件（与后端 Event 一致）。 */
export interface SceneEvent {
  event_type: string
  description: string
}

/** 单个场景（与后端 Scene 一致：时间区间 + 描述 + 多模态识别结果）。 */
export interface AssetScene {
  scene_id: string
  time_range: SceneTimeRange
  description: string
  visual_features: string[]
  dialogues: SceneDialogue[]
  entities: SceneEntity[]
  events: SceneEvent[]
}

/** 完整内容 JSON（与后端 ContentJSON 一致，schema v1.0）。 */
export interface ContentContext {
  schema_version: string
  asset: {
    asset_id: string | null
    asset_type: string
    title: string
    duration_ms: number | null
    video_uri: string | null
    parent_id: string | null
  }
  content: {
    summary: string
    scenes: AssetScene[]
  }
  metadata: {
    source: string
    model_version: string | null
    recognition_status: string
  }
}

/** 资产标签行（与后端 AssetTagOut 一致）：摘要形状 + 行主键 id +
 * tag_id（catalog 行）/ candidate_name（generated 行）原样。 */
export interface AssetTagRow {
  id: number
  tag_name: string
  origin: string
  relevance_score: number | null
  confidence: number | null
  review_status: string
  evidence: ReviewEvidence[]
  tag_id: string | null
  candidate_name: string | null
}

/** 资产列表查询参数：undefined 字段不拼入查询串。 */
export interface AssetListQuery {
  asset_type?: string
  q?: string
}

/** 资产列表：asset_type 精确过滤、q 对 title 子串匹配，按 asset_id 排序。 */
export function fetchAssets(query: AssetListQuery = {}): Promise<AssetSummary[]> {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== '') {
      params.set(key, value)
    }
  }
  const search = params.toString()
  return apiFetch<AssetSummary[]>(`/api/v1/assets${search ? `?${search}` : ''}`)
}

/** 资产详情 + 全部上下文摘要；未知 asset_id → 404。 */
export function fetchAssetDetail(assetId: string): Promise<AssetDetailData> {
  return apiFetch<AssetDetailData>(`/api/v1/assets/${assetId}`)
}

/** 单个上下文的完整内容 JSON（场景时间轴数据源）；上下文不属于该资产 → 404。 */
export function fetchAssetContext(assetId: string, contextId: string): Promise<ContentContext> {
  return apiFetch<ContentContext>(`/api/v1/assets/${assetId}/contexts/${contextId}`)
}

/** 资产全部生成标签（跨任务，按入库行序）；未知资产 → 404。 */
export function fetchAssetTags(assetId: string): Promise<AssetTagRow[]> {
  return apiFetch<AssetTagRow[]>(`/api/v1/assets/${assetId}/tags`)
}
