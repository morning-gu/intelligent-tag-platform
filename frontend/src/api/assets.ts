// 资产 API（v2 内容合并）：/assets 列表与详情（recognized 识别状态）、
// /assets/{id}/content 完整内容 JSON、/assets/{id}/tags 正式挂载标签的
// 类型契约与取数函数。类型形状与后端 tagplatform/api/routers/content.py
// （AssetOut/AssetDetail/ContentJSON）、generation.py（AssetTagOut）一致；
// 未知资产/未完成内容识别 → 404（ApiError.status）。v2 语义：content_json
// 直写影片级 media_asset 行，context 概念退役——contexts 数组与 contexts
// 端点随之移除。
import { apiFetch } from './client'
import type { TagOrigin } from './review'

/** 资产列表项（与后端 AssetOut 一致，均非大字段）。 */
export interface AssetSummary {
  asset_id: string
  asset_type: string
  parent_id: string | null
  title: string
  duration_ms: number | null
  video_uri: string | null
  /** 是否已有内容（影片行 content_json 非空；场景行恒 false）。 */
  has_content: boolean
  created_at: string | null
  /** 最近一次写入内容的时间（没内容时等于上传时间）。 */
  updated_at: string | null
}

/** 场景内单条台词（与后端 Dialogue 一致；时间可缺失）。 */
export interface SceneDialogue {
  speaker: string
  text: string
  start_ms: number | null
  end_ms: number | null
}

/** 场景内识别出的事件（与后端 Event 一致）。 */
export interface SceneEvent {
  event_type: string
  description: string
}

/** 角色项（内容 JSON 的 characters[]）。 */
export interface AssetCharacter {
  character_id: string
  name: string
  aliases: string[]
  description: string | null
}

/** 单个场景（与后端 Scene 一致：**扁平** start_ms/end_ms + 描述 + 多模态
 * 识别结果；旧的嵌套 time_range 形状已随 v2 内容 JSON 退役）。 */
export interface AssetScene {
  scene_id: string
  start_ms: number
  end_ms: number
  description: string | null
  /** 影片内虚拟时间（如"1945年夏末，婚礼当天午后"）。 */
  virtual_time?: string | null
  location?: string | null
  /** 场景涉及角色（对应 content.characters[].character_id）。 */
  character_ids?: string[]
  emotion?: string | null
  audio_events?: string[]
  dialogues: SceneDialogue[]
  events: SceneEvent[]
}

/** 完整内容 JSON（与后端 ContentJSON 一致，schema v1.0）：剧情描述在
 * content.description，人物表在 content.characters。 */
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
    description: string | null
    characters: AssetCharacter[]
    scenes: AssetScene[]
  }
}

/** 资产详情：列表字段之上附内容识别状态（content_json 非空 → true）。 */
export interface AssetDetailData extends AssetSummary {
  recognized: boolean
}

/** 资产正式挂载行（与后端 AssetTagOut 一致）：asset_tag 只收审核通过的
 * 复制行，仅挂载关系与展示值——评分/证据/review_status 留在 pre 侧不出现；
 * origin 经同键 pre 行解析，无对应 pre 行时为 null。 */
export interface AssetMount {
  id: number
  tag_id: string
  tag_name: string
  origin: TagOrigin | null
  created_at: string | null
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

/** 资产详情 + 内容识别状态；未知 asset_id → 404。 */
export function fetchAssetDetail(assetId: string): Promise<AssetDetailData> {
  return apiFetch<AssetDetailData>(`/api/v1/assets/${assetId}`)
}

/** 资产的完整内容 JSON（场景时间轴数据源）；未知资产或未完成内容识别
 * （content_json 为空，如上传占位资产/场景子资产）→ 404。 */
export function fetchAssetContent(assetId: string): Promise<ContentContext> {
  return apiFetch<ContentContext>(`/api/v1/assets/${assetId}/content`)
}

/** 资产全部正式挂载标签（仅 approved，按入库行序）；未知资产 → 404。 */
export function fetchAssetTags(assetId: string): Promise<AssetMount[]> {
  return apiFetch<AssetMount[]>(`/api/v1/assets/${assetId}/tags`)
}
