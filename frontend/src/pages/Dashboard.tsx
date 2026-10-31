// 工作台首页（Task 9）：四张概况统计卡片（待审核 / 任务成功率 / 证据覆盖率 /
// 平均任务耗时）+ "进入审核"快捷入口。数据来自 GET /metrics/summary；
// 率/耗时字段后端可能返回 null（分母为 0），展示为"暂无"。
import { Alert, Button, Card, Spin, Statistic } from 'antd'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  fetchMetricsSummary,
  formatDuration,
  formatPercent,
  type MetricsSummary,
} from '../api/metrics'

export default function Dashboard() {
  const navigate = useNavigate()
  const [summary, setSummary] = useState<MetricsSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // StrictMode 会双调 effect：以 cancelled 丢弃过期请求结果
    let cancelled = false
    fetchMetricsSummary().then(
      (data) => {
        if (!cancelled) setSummary(data)
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
  if (summary === null) {
    return <Spin />
  }

  const cardStyle = { flex: '1 1 200px' }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <Card style={cardStyle}>
          <Statistic title="待审核标签" value={String(summary.pending_count)} />
        </Card>
        <Card style={cardStyle}>
          <Statistic
            title="任务成功率"
            value={formatPercent(summary.task_stats.success_rate)}
          />
        </Card>
        <Card style={cardStyle}>
          <Statistic title="证据覆盖率" value={formatPercent(summary.evidence_coverage)} />
        </Card>
        <Card style={cardStyle}>
          <Statistic
            title="平均任务耗时"
            value={formatDuration(summary.task_stats.avg_duration_ms)}
          />
        </Card>
      </div>
      <Card title="快捷入口">
        <Button type="primary" onClick={() => navigate('/review')}>
          进入审核
        </Button>
      </Card>
    </div>
  )
}
