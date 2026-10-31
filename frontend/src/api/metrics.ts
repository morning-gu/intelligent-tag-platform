// 指标 API（Task 9）：/metrics 三个端点的类型契约与取数函数 + 展示格式化。
// 类型形状与后端 tagplatform/api/routers/metrics.py 的 Pydantic 模型一致；
// 占比/率类字段分母为 0 时后端返回 null，前端统一以"暂无"呈现。
import { apiFetch } from './client'

/** 单个审核动作的记录数与占比（分母 0 → ratio 为 null）。 */
export interface ReviewActionStat {
  count: number
  ratio: number | null
}

/** 审核动作分布：四动作各自的计数与占比。 */
export interface ReviewStats {
  approved: ReviewActionStat
  rejected: ReviewActionStat
  merged: ReviewActionStat
  modified: ReviewActionStat
}

/** 生成任务统计（avg_duration_ms 仅终态且有始有终的任务均值，无 → null）。 */
export interface TaskStats {
  total: number
  succeeded: number
  failed: number
  success_rate: number | null
  avg_duration_ms: number | null
}

/** GET /metrics/summary 响应：质量指标总览（承诺二③）。 */
export interface MetricsSummary {
  pending_count: number
  review_stats: ReviewStats
  generated_pass_rate: number | null
  evidence_coverage: number | null
  task_stats: TaskStats
}

/** GET /metrics/confidence-distribution 单条：桶标签（如 "0.0-0.1"）与行数。 */
export interface ConfidenceBucket {
  bucket: string
  count: number
}

/** GET /metrics/tag-type-coverage 单条：类型名、总行数、其中 approved 数。 */
export interface TagTypeCoverageRow {
  tag_type: string
  total: number
  approved: number
}

/** 质量指标总览：pending / 审核动作分布 / 新标签通过率 / evidence 覆盖率 / 任务统计。 */
export function fetchMetricsSummary(): Promise<MetricsSummary> {
  return apiFetch<MetricsSummary>('/api/v1/metrics/summary')
}

/** confidence 等宽分桶分布（默认 10 桶，恒返回全部桶，含 0 计数桶）。 */
export function fetchConfidenceDistribution(): Promise<ConfidenceBucket[]> {
  return apiFetch<ConfidenceBucket[]>('/api/v1/metrics/confidence-distribution')
}

/** 每 tag_type 的标签行数与已通过数（按 total 降序）。 */
export function fetchTagTypeCoverage(): Promise<TagTypeCoverageRow[]> {
  return apiFetch<TagTypeCoverageRow[]>('/api/v1/metrics/tag-type-coverage')
}

/** 占比展示：null（分母为 0）→ "暂无"；否则百分比一位小数（0.756 → "75.6%"）。 */
export function formatPercent(ratio: number | null): string {
  if (ratio === null) {
    return '暂无'
  }
  return `${(ratio * 100).toFixed(1)}%`
}

/** 毫秒时长展示：null → "暂无"；否则四舍五入取整带单位（1234.6 → "1235 ms"）。 */
export function formatDuration(ms: number | null): string {
  if (ms === null) {
    return '暂无'
  }
  return `${Math.round(ms)} ms`
}
