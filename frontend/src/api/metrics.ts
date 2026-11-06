// 指标 API（v2）：/metrics 三个端点的类型契约与取数函数 + 展示格式化。
// 类型形状与后端 tagplatform/api/routers/metrics.py 的 Pydantic 模型一致；
// 占比/率类字段分母为 0 时后端返回 null，前端统一以"暂无"呈现。v2 口径：
// review_stats 为 review_status 终态分布三键（modified 随"修改即通过"语义
// 退役）、覆盖维度由 tag_type 改目录（tag-type-coverage → dir-coverage）。
import { apiFetch } from './client'

/** 单个审核状态键的计数与占比（分母 0 → ratio 为 null）。 */
export interface ReviewStatusStat {
  count: number
  ratio: number | null
}

/** review_status 终态分布：approved/rejected 按 pre 行状态计数，merged 为
 * 变更日志 merge 事件数（不是行数）；三键同分母（pre 总行数）。 */
export interface ReviewStats {
  approved: ReviewStatusStat
  rejected: ReviewStatusStat
  merged: ReviewStatusStat
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

/** GET /metrics/dir-coverage 单条：目录名、总行数、其中 approved 数
 * （无目录归属的候选行归"未归置"桶）。 */
export interface DirCoverageRow {
  dir_name: string
  total: number
  approved: number
}

/** 质量指标总览：pending / review_status 终态分布 / 新标签通过率 /
 * evidence 覆盖率 / 任务统计。 */
export function fetchMetricsSummary(): Promise<MetricsSummary> {
  return apiFetch<MetricsSummary>('/api/v1/metrics/summary')
}

/** confidence 等宽分桶分布（默认 10 桶，恒返回全部桶，含 0 计数桶）。 */
export function fetchConfidenceDistribution(): Promise<ConfidenceBucket[]> {
  return apiFetch<ConfidenceBucket[]>('/api/v1/metrics/confidence-distribution')
}

/** 每目录的标签行数与已通过数（按 total 降序）。 */
export function fetchTagDirCoverage(): Promise<DirCoverageRow[]> {
  return apiFetch<DirCoverageRow[]>('/api/v1/metrics/dir-coverage')
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
