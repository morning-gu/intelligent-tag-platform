// BarChart 纯 CSS 条形图测试：label/数值/extra 渲染 + 行内宽度百分比按
// value/max 比例。宽度断言直接读 data-testid="bar-fill" 填充条的
// style.width（行内样式，jsdom 下 getComputedStyle 可靠回读）。
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import BarChart from './BarChart'

/** 取指定 label 行的填充条元素。 */
function fillOf(label: string): HTMLElement {
  return within(screen.getByTestId(`bar-row-${label}`)).getByTestId('bar-fill')
}

describe('BarChart 渲染', () => {
  it('渲染每行 label、数值与 extra 附加文本', () => {
    render(
      <BarChart
        items={[
          { label: '剧情', value: 7 },
          { label: '动作', value: 3, extra: '通过 2' },
        ]}
      />,
    )
    expect(screen.getByText('剧情')).toBeInTheDocument()
    expect(screen.getByText('动作')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('通过 2')).toBeInTheDocument()
  })

  it('缺省 max 取各行 value 最大值：最大行 100%，其余按比例', () => {
    render(<BarChart items={[{ label: 'A', value: 4 }, { label: 'B', value: 1 }]} />)
    expect(fillOf('A')).toHaveStyle({ width: '100%' })
    expect(fillOf('B')).toHaveStyle({ width: '25%' })
  })

  it('显式 max 覆盖缺省比例（50/200 → 25%）', () => {
    render(<BarChart items={[{ label: 'X', value: 50, max: 200 }]} />)
    expect(fillOf('X')).toHaveStyle({ width: '25%' })
  })

  it('全 0 数据：宽度为 0%（不产生 NaN/Infinity）', () => {
    render(<BarChart items={[{ label: 'A', value: 0 }, { label: 'B', value: 0 }]} />)
    expect(fillOf('A')).toHaveStyle({ width: '0%' })
    expect(fillOf('B')).toHaveStyle({ width: '0%' })
  })

  it('value 超过 max 时 clamp 到 100%', () => {
    render(<BarChart items={[{ label: 'A', value: 150, max: 100 }]} />)
    expect(fillOf('A')).toHaveStyle({ width: '100%' })
  })

  it('比例换算含两位小数舍入与负值 clamp', () => {
    render(
      <BarChart
        items={[
          { label: 'C', value: 1 },
          { label: 'D', value: 3 },
          { label: 'N', value: -1, max: 5 },
        ]}
      />,
    )
    expect(fillOf('C')).toHaveStyle({ width: '33.33%' }) // 1/3 舍入到两位小数
    expect(fillOf('D')).toHaveStyle({ width: '100%' })
    expect(fillOf('N')).toHaveStyle({ width: '0%' }) // 负值 clamp 到 0
  })

  it('空列表渲染为空容器', () => {
    const { container } = render(<BarChart items={[]} />)
    expect(container.firstElementChild).toBeEmptyDOMElement()
  })
})
