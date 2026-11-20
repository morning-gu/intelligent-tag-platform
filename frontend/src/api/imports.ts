// 标签导入任务 API：multipart 上传、任务列表、详情、撤销与导出。
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
  error: string | null
}

/** 上传标签 Excel/XML 并创建后台任务。 */
export function importTags(file: File): Promise<ImportTaskSummary> {
  const form = new FormData()
  form.append('file', file)
  return apiFetch<ImportTaskSummary>('/api/v1/import/tasks', { method: 'POST', body: form })
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
