// 纯 CSS 条形图（Task 9）：不引第三方图表库，每行以「标签 + 轨道 + 填充条 +
// 数值」四段渲染；填充条宽度 = value/max 的百分比（行内 style，便于测试断言）。
// max 可按行显式指定；缺省取全部条目 value 的最大值（全 0 时兜底为 1，避免除零）。

/** 单条数据：label 左侧文本、value 数值、max 显式上限（可选）、extra 附加说明（可选）。 */
export interface BarItem {
  label: string
  value: number
  max?: number
  extra?: string
}

/** 宽度百分比换算：value/max，clamp 到 [0, 100]，保留两位小数；max ≤ 0 恒 0。 */
function barWidthPercent(value: number, max: number): number {
  if (max <= 0) {
    return 0
  }
  const ratio = Math.min(Math.max(value / max, 0), 1)
  return Math.round(ratio * 10000) / 100
}

export default function BarChart({ items }: { items: BarItem[] }) {
  // 缺省上限：全部条目 value 的最大值（空列表/全 0 → 0，逐行再兜底为 1）
  const defaultMax = Math.max(0, ...items.map((item) => item.value))

  return (
    <div>
      {items.map((item) => {
        // 行上限：显式 max（须为正）优先，否则全局缺省；全 0 时取 1 防除零
        const max =
          item.max !== undefined && item.max > 0 ? item.max : defaultMax > 0 ? defaultMax : 1
        const widthPct = barWidthPercent(item.value, max)
        return (
          <div
            key={item.label}
            data-testid={`bar-row-${item.label}`}
            style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}
          >
            <span
              style={{
                width: 120,
                flexShrink: 0,
                textAlign: 'right',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                color: 'rgba(0, 0, 0, 0.65)',
              }}
            >
              {item.label}
            </span>
            <div
              style={{
                flex: 1,
                height: 16,
                background: '#f5f5f5',
                borderRadius: 2,
                overflow: 'hidden',
              }}
            >
              <div
                data-testid="bar-fill"
                style={{
                  width: `${widthPct}%`,
                  height: '100%',
                  background: '#1677ff',
                  borderRadius: 2,
                }}
              />
            </div>
            <span style={{ width: 140, flexShrink: 0, fontSize: 12 }}>
              {item.value}
              {item.extra !== undefined && (
                <span style={{ marginLeft: 8, color: 'rgba(0, 0, 0, 0.45)' }}>{item.extra}</span>
              )}
            </span>
          </div>
        )
      })}
    </div>
  )
}
