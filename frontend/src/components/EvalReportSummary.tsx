import { Statistic } from 'antd'
import type { BarItem } from './BarChart'

export default function EvalReportSummary({ items }: {
  items: { key: string; title: string; value: number | string | null }[]
}) {
  return (
    <div data-testid="summary-row" style={{ display: 'flex', gap: '16px 48px', flexWrap: 'wrap' }}>
      {items.map(({ key, title, value }) => (
        <div key={key} data-testid={`summary-${key}`}>
          <Statistic title={title} value={value ?? '暂无'} />
        </div>
      ))}
    </div>
  )
}

export function coverageBars(values: number[]): BarItem[] {
  // 左闭右开，末档含 100%。
  return [0, 0.2, 0.4, 0.6, 0.8].map((lower, i) => {
    const upper = (i + 1) / 5
    const last = upper === 1
    return {
      label: `[${lower},${last ? '1.0]' : `${upper})`}`,
      value: values.filter((v) => v >= lower && (last ? v <= upper : v < upper)).length,
    }
  })
}
