// 标签导入 API（Task 13）：/tags/import（multipart 上传）与 /import/batches
// 批次历史/详情的类型契约与取数函数。类型形状与后端 tagplatform/api/routers/
// tags.py（ImportSummary/ImportResponse）与 batches.py（BatchOut/BatchDetailOut）
// 一致；坏文件/坏表头 → 422（ApiError.message 透出后端 detail）。
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

/** 导入结果摘要（与后端 ImportSummary 一致）：total 为传入治理的行数
 * （行级解析错误的行不计入），skipped = total - inserted。 */
export interface ImportSummary {
  total: number
  inserted: number
  skipped: number
  issues: ImportIssue[]
  parse_errors: ImportParseError[]
}

/** 上传导入响应：本批批次号 + 结果摘要。 */
export interface ImportResponse {
  batch_id: string
  summary: ImportSummary
}

/** 导入批次列表项（与后端 BatchOut 一致，created_at 降序由后端保证）。 */
export interface ImportBatchSummary {
  batch_id: string
  file_name: string
  status: string
  created_at: string | null
}

/** 批次详情：列表字段之上附 result_summary 解析后的摘要（损坏时为 null）。 */
export interface ImportBatchDetail extends ImportBatchSummary {
  summary: ImportSummary | null
}

/** 上传标签 Excel（multipart，字段 file）：解析 → 治理入库 → 摘要。
 * 注意走 FormData（client 对 FormData 不强设 Content-Type）。 */
export function importTags(file: File): Promise<ImportResponse> {
  const form = new FormData()
  form.append('file', file)
  return apiFetch<ImportResponse>('/api/v1/tags/import', { method: 'POST', body: form })
}

/** 全部导入批次（created_at 降序，最近导入在前）。 */
export function fetchImportBatches(): Promise<ImportBatchSummary[]> {
  return apiFetch<ImportBatchSummary[]>('/api/v1/import/batches')
}

/** 单批详情（含解析后的结果摘要）；未知 batch_id → 404。 */
export function fetchImportBatch(batchId: string): Promise<ImportBatchDetail> {
  return apiFetch<ImportBatchDetail>(`/api/v1/import/batches/${batchId}`)
}
