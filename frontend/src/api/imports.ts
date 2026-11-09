// 标签导入 API（v2）：/tags/import（multipart 上传）与 /import/tasks 导入
// 任务历史/详情的类型契约与取数函数。类型形状与后端 tagplatform/api/routers/
// tags.py（ImportSummary/ImportResponse）与 imports.py（ImportTaskOut/
// ImportTaskDetailOut）一致；坏文件/坏表头 → 422（ApiError.message 透出
// 后端 detail）。v1 的 /import/batches 前缀与 batch_id 字段随表结构 v2
// 退役（import_batch → tag_import_task），统一 task_id。
import { apiFetch } from './client'

/** 单条治理问题明细（与后端 IssueOut / governance.Issue 字段一一对应）。 */
export interface ImportIssue {
  tag_name: string
  code: string
  detail: string
  similar_to: string | null
}

/** 单行解析失败明细（与后端 ParseErrorOut / governance.excel.RowError 一致）。 */
export interface ImportParseError {
  row_number: number
  tag_name: string | null
  code: string
  detail: string
}

/** 导入结果摘要（与后端 ImportSummary 一致）：total 为传入治理的标签
 * 定义行数（目录行落 tag_catalog 不计入；行级解析错误的行不计入），
 * skipped = total - inserted。 */
export interface ImportSummary {
  total: number
  inserted: number
  skipped: number
  issues: ImportIssue[]
  parse_errors: ImportParseError[]
}

/** 上传导入响应：本次导入任务号 + 结果摘要（v1 batch_id 退役）。 */
export interface ImportResponse {
  task_id: string
  summary: ImportSummary
}

/** 导入任务列表项（与后端 ImportTaskOut 一致，created_at 降序由后端保证）。 */
export interface ImportTaskSummary {
  task_id: string
  file_name: string
  status: string
  created_at: string | null
}

/** 导入任务详情：列表字段之上附 result_summary 解析后的摘要（无/损坏时为 null）。 */
export interface ImportTaskDetail extends ImportTaskSummary {
  summary: ImportSummary | null
}

/** 上传标签 Excel/XML（multipart，字段 file）：按扩展名解析 → 治理双表
 * 入库 → 任务号与摘要。注意走 FormData（client 对 FormData 不强设
 * Content-Type）。 */
export function importTags(file: File): Promise<ImportResponse> {
  const form = new FormData()
  form.append('file', file)
  return apiFetch<ImportResponse>('/api/v1/tags/import', { method: 'POST', body: form })
}

/** 全部导入任务（created_at 降序，最近导入在前）。 */
export function fetchImportTasks(): Promise<ImportTaskSummary[]> {
  return apiFetch<ImportTaskSummary[]>('/api/v1/import/tasks')
}

/** 单个导入任务详情（含解析后的结果摘要）；未知 task_id → 404。 */
export function fetchImportTask(taskId: string): Promise<ImportTaskDetail> {
  return apiFetch<ImportTaskDetail>(`/api/v1/import/tasks/${taskId}`)
}

/** 下载指定导入任务的 Excel/XML 压缩包。 */
export async function downloadImportTask(taskId: string): Promise<Blob> {
  const response = await fetch(`/api/v1/import/tasks/${taskId}/export`, {
    credentials: 'include',
  })
  if (!response.ok) throw new Error(`导入任务导出失败（HTTP ${response.status}）`)
  return response.blob()
}

/** 撤销已完成的导入任务（批内行被挂载/候选引用 → 409）。 */
export function revokeImportTask(taskId: string): Promise<ImportTaskSummary> {
  return apiFetch<ImportTaskSummary>(`/api/v1/import/tasks/${taskId}/revoke`, {
    method: 'POST',
  })
}
