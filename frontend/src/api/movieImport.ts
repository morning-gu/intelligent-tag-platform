// 影片资产目录导入 API：把"已备好的 content.json + frames"直接注册成资产，
// 与上传视频那条路并行（两条路写同一张 media_asset，靠 asset_id 幂等）。
// 类型形状与后端 tagplatform/api/routers/content.py
// （MovieScanResponse / MovieImportRequest / MovieImportResponse）一致。
//
// 目录布局由后端配置决定（settings.movie_asset_root）；前端不拼路径，
// 只在需要时把 root 透传给后端（默认不传，用后端配置值）。
import { apiFetch } from './client'

/** 扫描到的一部影片目录（列表一行）。 */
export interface MovieDirItem {
  dir_name: string
  title: string
  imdb_id: string | null
  asset_id: string | null
  scenes: number
  frames: number
  duration_ms: number | null
  size_bytes: number
  /** 库里已有同 asset_id 的资产 */
  exists: boolean
  has_content: boolean
  /** 已绑定上传的视频（目录导入默认不覆盖它） */
  has_video: boolean
  /** content.json 读不了/格式不合法时的原因（该行不可导入） */
  error: string | null
}

/** 扫描响应。 */
export interface MovieScanResult {
  root: string
  total: number
  items: MovieDirItem[]
}

/** 单部导入结果：imported | skipped | failed。 */
export interface MovieImportItem {
  dir_name: string
  status: string
  asset_id: string | null
  title: string | null
  scenes: number
  frames: number
  reason: string | null
}

/** 批量导入响应（三个计数 + 每部明细）。 */
export interface MovieImportResult {
  imported: number
  skipped: number
  failed: number
  items: MovieImportItem[]
}

/** 扫描影片资产目录（不传 root 用后端配置值）。 */
export function scanMovieDirs(root?: string): Promise<MovieScanResult> {
  const query = root ? `?root=${encodeURIComponent(root)}` : ''
  return apiFetch<MovieScanResult>(`/api/v1/content/import/movie-scan${query}`)
}

/** 把选中的目录注册成资产；dirNames 为空表示全部。 */
export function bulkImportMovieDirs(params: {
  dirNames?: string[] | null
  overwrite?: boolean
  root?: string | null
}): Promise<MovieImportResult> {
  return apiFetch<MovieImportResult>('/api/v1/content/import/movie-bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      dir_names: params.dirNames ?? null,
      overwrite: params.overwrite ?? false,
      root: params.root ?? null,
    }),
  })
}
