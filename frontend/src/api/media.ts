// 上传占位 API（阶段 4 任务 5，入口 A）：/media/upload multipart 上传的
// 类型契约与取数函数。类型形状与后端 tagplatform/api/routers/media.py
// （MediaUploadResponse）一致；失败映射：后缀非白名单（mp4/webm/mkv 之外）
// → 422、超 2GB 上限 → 413（ApiError.message 透出后端 detail）。
// 占位语义：上传即登记（存文件 + 建资产行），不做识别——识别内容后续经
// 入口 B（识别 JSON 导入）补齐。
import { apiFetch } from './client'

/** 上传响应：新资产标识 + 标题（文件名去后缀）+ 落盘路径。 */
export interface MediaUploadResult {
  asset_id: string
  title: string
  video_uri: string
}

/** 上传视频（multipart，字段 file）：占位登记资产。走 FormData（client 对
 * FormData 不强设 Content-Type，浏览器自动补 multipart boundary）。 */
export function uploadMedia(file: File): Promise<MediaUploadResult> {
  const form = new FormData()
  form.append('file', file)
  return apiFetch<MediaUploadResult>('/api/v1/media/upload', { method: 'POST', body: form })
}
