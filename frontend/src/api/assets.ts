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

/** 情绪项（上游模板 `emotion[{name, code}]`：词表 code，未知时为 null）。 */
export interface EmotionItem {
  name: string | null
  code: string | null
}

/** 场景内单条台词（与上游模板 new_json.txt 一致）。 */
export interface SceneDialogue {
  dialogue_id: string | null
  speaker_id: string | null
  speaker_name: string | null
  text: string
  start_ms: number | null
  end_ms: number | null
}

/** 事件参与人及其情绪（上游模板 participant_ids[{character_id, emotion}]）。 */
export interface SceneParticipant {
  character_id: string | null
  emotion: EmotionItem[]
}

/** 场景内识别出的事件（上游模板 events[]）。 */
export interface SceneEvent {
  event_id: string | null
  description: string
  participant_ids: SceneParticipant[]
}

/** 场景内音频事件（上游模板 audio_events[]）。 */
export interface AudioEvent {
  event_type: string
  description: string
  start_ms: number | null
  end_ms: number | null
}

/** 单个场景（上游模板形态：时间戳拍平 + 情绪 [{name,code}]）。 */
export interface AssetScene {
  scene_id: string
  start_ms: number
  end_ms: number
  description: string | null
  virtual_time: string | null
  location: string | null
  character_ids: string[]
  events: SceneEvent[]
  emotion: EmotionItem[]
  dialogues: SceneDialogue[]
  audio_events: AudioEvent[]
}

/** 角色项（上游模板 characters[]）。 */
export interface AssetCharacter {
  character_id: string
  name: string
  aliases: string[]
  description: string | null
}

/** 完整内容 JSON（上游模板 new_json.txt 形态，字段全量：缺值 null / []）。 */
export interface ContentContext {
  schema_version: string
  asset: {
    asset_id: string | null
    asset_type: string
    title: string
    duration_ms: number | null
    video_uri: string | null
    language: string | null
  }
  content: {
    description: string | null
    characters: AssetCharacter[]
    scenes: AssetScene[]
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
  /** 标签层级：film（影片级）/ scene（场景级）。 */
  level: string
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
