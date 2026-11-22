// 工作台总览（Task 1 吸收质量评估）：登录落地页。
// - 总览行四卡：待审核标签 / 任务成功率 / 证据覆盖率 / 平均任务耗时
//   （沿用旧工作台口径，数据同样来自 /metrics/summary）。
// - 指标看板（原质量评估页整体收编）：审核动作分布（通过/驳回/合并，
//   modified 随"修改即通过"语义退役，merged 为变更日志 merge 事件数）/
//   生成质量 / 任务统计三组卡片 + 置信度分布、目录覆盖两张纯 CSS 条形图。
// - 快捷入口：进入审核（/review）、创建任务（/tasks）。
// 三个指标端点并行拉取；占比类字段 null（分母为 0）展示为"暂无"；
// 任一端点失败 → 页面级错误 Alert（沿用原质量评估行为）。
import { Alert, Button, Card, Empty, Space, Spin, Statistic } from 'antd'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import BarChart from '../components/BarChart'
import {
  fetchConfidenceDistribution,
  fetchMetricsSummary,
  fetchTagDirCoverage,
  formatDuration,
  formatPercent,
  type ConfidenceBucket,
  type DirCoverageRow,
  type MetricsSummary,
  type ReviewStats,
} from '../api/metrics'

/** 页面一次装配的全部指标数据（三端点齐活才渲染）。 */
interface OverviewData {
  summary: MetricsSummary
  confidence: ConfidenceBucket[]
  coverage: DirCoverageRow[]
}

/** 审核状态展示键 → 卡片标题（与后端 review_stats 键一一对应；modified
 * 随"修改即通过"语义退役，merged 为变更日志 merge 事件数）。 */
const REVIEW_ACTION_CARDS: { key: keyof ReviewStats; title: string }[] = [
  { key: 'approved', title: '通过' },
  { key: 'rejected', title: '驳回' },
  { key: 'merged', title: '合并' },
]

export default function Dashboard() {
  const navigate = useNavigate()
  const [data, setData] = useState<OverviewData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // StrictMode 会双调 effect：以 cancelled 丢弃过期请求结果
    let cancelled = false
    Promise.all([fetchMetricsSummary(), fetchConfidenceDistribution(), fetchTagDirCoverage()]).then(
      ([summary, confidence, coverage]) => {
        if (!cancelled) setData({ summary, confidence, coverage })
      },
      (err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载失败')
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  if (error !== null) {
    return <Alert type="error" showIcon message="指标加载失败" description={error} />
  }
  if (data === null) {
    return <Spin />
  }

  const { summary, confidence, coverage } = data
  const taskStats = summary.task_stats

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="总览">
        <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap' }}>
          <Statistic title="待审核标签" value={String(summary.pending_count)} />
          <Statistic title="任务成功率" value={formatPercent(taskStats.success_rate)} />
          <Statistic title="证据覆盖率" value={formatPercent(summary.evidence_coverage)} />
          <Statistic title="平均任务耗时" value={formatDuration(taskStats.avg_duration_ms)} />
        </div>
      </Card>
      <Card title="审核动作分布">
        <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap' }}>
          {REVIEW_ACTION_CARDS.map(({ key, title }) => (
            <Statistic
              key={key}
              title={`${title}（${formatPercent(summary.review_stats[key].ratio)}）`}
              value={String(summary.review_stats[key].count)}
            />
          ))}
        </div>
      </Card>
      <Card title="生成质量">
        <Statistic title="新标签通过率" value={formatPercent(summary.generated_pass_rate)} />
      </Card>
      <Card title="任务统计">
        <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap' }}>
          <Statistic title="任务总数" value={String(taskStats.total)} />
          <Statistic title="成功" value={String(taskStats.succeeded)} />
          <Statistic title="失败" value={String(taskStats.failed)} />
          <Statistic title="成功率" value={formatPercent(taskStats.success_rate)} />
          <Statistic title="平均耗时" value={formatDuration(taskStats.avg_duration_ms)} />
        </div>
      </Card>
      <Card title="置信度分布">
        {/* 分桶恒返回全部桶（含 0 计数），直接作图基线 */}
        <BarChart items={confidence.map((row) => ({ label: row.bucket, value: row.count }))} />
      </Card>
      <Card title="目录覆盖">
        {coverage.length === 0 ? (
          <Empty description="暂无数据" />
        ) : (
          <BarChart
            items={coverage.map((row) => ({
              label: row.dir_name,
              value: row.total,
              extra: `通过 ${row.approved}`,
            }))}
          />
        )}
      </Card>
      <Card title="快捷入口">
        <Space>
          <Button type="primary" onClick={() => navigate('/review')}>
            进入审核
          </Button>
          <Button onClick={() => navigate('/tasks')}>创建任务</Button>
        </Space>
      </Card>
    </div>
  )
}
