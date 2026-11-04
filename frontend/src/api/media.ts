// 上传与资产生成进度 API（阶段 4 任务 5 + 资产构建进度展示）：
// /media/upload multipart 上传的类型契约、/media/build-tasks/{id} 进度查询。
// 类型形状与后端 tagplatform/api/routers/media.py（MediaUploadResponse /
// BuildTaskOut）一致；失败映射：后缀非白名单（mp4/webm/mkv 之外）→ 422、
// 超 2GB 上限 → 413（ApiError.message 透出后端 detail）。
// 上传成功且文件名命中数据集影片时返回 build_task_id，前端据此轮询进度。
import { apiFetch } from './client'

/** 上传响应：资产标识 + 匹配结果 + 后台生成任务 id（未命中为 null）。 */
export interface MediaUploadResult {
  asset_id: string
  title: string
  video_uri: string
  movie_id: string | null
  matched_title: string | null
  match_how: string | null
  build_task_id: string | null
}

/** 资产生成任务进度（与后端 BuildTaskOut 一致）。 */
export interface BuildTask {
  task_id: string
  asset_id: string
  movie_id: string
  status: string
  total_scenes: number
  done_scenes: number
  error: string | null
}

/** 上传视频（multipart，字段 file）：命中数据集时后台开始生成资产。走
 * FormData（client 对 FormData 不强设 Content-Type，浏览器自动补 boundary）。 */
export function uploadMedia(file: File): Promise<MediaUploadResult> {
  const form = new FormData()
  form.append('file', file)
  return apiFetch<MediaUploadResult>('/api/v1/media/upload', { method: 'POST', body: form })
}

/** 查询单个生成任务进度（未知 id → 404）。 */
export function fetchBuildTask(taskId: string): Promise<BuildTask> {
  return apiFetch<BuildTask>(`/api/v1/media/build-tasks/${encodeURIComponent(taskId)}`)
}
