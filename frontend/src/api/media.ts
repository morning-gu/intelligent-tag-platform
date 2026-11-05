// 上传与资产生成进度 API（阶段 4 任务 5 + 资产构建进度展示）：
// /media/match 选文件预检、/media/upload multipart 上传、
// /media/build-tasks/{id} 进度查询。类型形状与后端
// tagplatform/api/routers/media.py（MediaMatchResponse / MediaUploadResponse /
// BuildTaskOut）一致。
//
// 上传走 XMLHttpRequest 而不是 fetch：fetch 不暴露上传进度，几百 MB 的片子
// 传几分钟时界面只能干转圈；XHR 的 upload.onprogress 能给出真实百分比。
// 失败映射与 client.ts 对齐（401 → 跳登录并抛 UnauthorizedError，
// 其余非 2xx → ApiError，message 取后端 detail；后缀 422、超限 413）。
import { ApiError, UnauthorizedError, apiFetch } from './client'

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

/** 选文件预检结果：命中哪部片、该片是否已有可直接复用的内容。 */
export interface MediaMatchResult {
  matched: boolean
  movie_id: string | null
  matched_title: string | null
  match_how: string | null
  asset_id: string | null
  /** 已有**当前口径**的内容 → 无需再上传视频 */
  has_content: boolean
  content_source: string | null
  scene_count: number
  built_at: string | null
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

/** 上传选项：进度回调 + 强制重生成（已有同口径内容也重跑）。 */
export interface UploadOptions {
  onProgress?: (percent: number) => void
  force?: boolean
}

/** 从 XHR 错误响应体提取 {"detail": "..."}，与 client.readErrorDetail 同口径。 */
function errorDetailFromXhr(xhr: XMLHttpRequest): string {
  try {
    const body = JSON.parse(xhr.responseText) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail !== '') return body.detail
  } catch {
    // 非 JSON（如网关 HTML），走默认文案
  }
  return `请求失败（HTTP ${xhr.status}）`
}

/** 按文件名预检：命中哪部片、是否已有可用内容（只读，不建任务）。 */
export function matchMedia(filename: string): Promise<MediaMatchResult> {
  return apiFetch<MediaMatchResult>(
    `/api/v1/media/match?filename=${encodeURIComponent(filename)}`,
  )
}

/** 上传视频（multipart，字段 file；force=true 时带 force 字段）。
 *
 * onProgress 覆盖"浏览器 → 服务器"的传输段；到 100% 后服务端还要把文件
 * 复制进 media_root 并登记资产，那一段没有进度可汇报（由调用方提示文案）。 */
export function uploadMedia(
  file: File,
  { onProgress, force = false }: UploadOptions = {},
): Promise<MediaUploadResult> {
  const form = new FormData()
  form.append('file', file)
  if (force) form.append('force', 'true')

  return new Promise<MediaUploadResult>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/v1/media/upload')
    xhr.withCredentials = true
    xhr.upload.onprogress = (event: ProgressEvent) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.min(100, Math.round((100 * event.loaded) / event.total)))
      }
    }
    xhr.onload = () => {
      if (xhr.status === 401) {
        window.location.href = '/login'
        reject(new UnauthorizedError())
        return
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new ApiError(xhr.status, errorDetailFromXhr(xhr)))
        return
      }
      if (onProgress) onProgress(100)
      try {
        resolve(JSON.parse(xhr.responseText) as MediaUploadResult)
      } catch {
        reject(new ApiError(xhr.status, '上传响应解析失败'))
      }
    }
    xhr.onerror = () => reject(new Error('上传中断（网络错误）'))
    xhr.send(form)
  })
}

/** 查询单个生成任务进度（未知 id → 404）。 */
export function fetchBuildTask(taskId: string): Promise<BuildTask> {
  return apiFetch<BuildTask>(`/api/v1/media/build-tasks/${encodeURIComponent(taskId)}`)
}
