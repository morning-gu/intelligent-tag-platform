// 质量评估中心测试：mock apiFetch 按请求路径分发三个指标端点夹具，
// 断言审核动作分布/生成质量/任务统计卡片字段与两张条形图的数据渲染；
// 率/占比字段 None → "暂无"。
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Quality from './Quality'
import { apiFetch } from '../api/client'
import type { ConfidenceBucket, MetricsSummary, TagTypeCoverageRow } from '../api/metrics'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

/** 与后端 MetricsSummaryOut 契约同形的夹具（各率字段非 None）。 */
const SUMMARY: MetricsSummary = {
  pending_count: 6,
  review_stats: {
    approved: { count: 12, ratio: 0.6 },
    rejected: { count: 5, ratio: 0.25 },
    merged: { count: 2, ratio: 0.1 },
    modified: { count: 1, ratio: 0.05 },
  },
  generated_pass_rate: 0.8,
  evidence_coverage: 0.5,
  task_stats: {
    total: 20,
    succeeded: 15,
    failed: 4,
    success_rate: 0.75,
    avg_duration_ms: 3000,
  },
}

/** 置信度分桶夹具（真实接口恒 10 桶，这里取代表桶断言渲染即可）。 */
const CONFIDENCE: ConfidenceBucket[] = [
  { bucket: '0.0-0.1', count: 3 },
  { bucket: '0.1-0.2', count: 0 },
  { bucket: '0.9-1.0', count: 7 },
]

/** tag_type 覆盖夹具（后端按 total 降序返回）。 */
const COVERAGE: TagTypeCoverageRow[] = [
  { tag_type: 'genre', total: 10, approved: 4 },
  { tag_type: 'mood', total: 8, approved: 3 },
]

/** 按路径分发三个指标端点的 mock 装配。 */
function mockMetricsApis(
  summary: MetricsSummary,
  confidence: ConfidenceBucket[],
  coverage: TagTypeCoverageRow[],
) {
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/metrics/summary') return summary
    if (path === '/api/v1/metrics/confidence-distribution') return confidence
    if (path === '/api/v1/metrics/tag-type-coverage') return coverage
    throw new Error(`未预期的请求路径：${path}`)
  })
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('Quality 质量评估中心', () => {
  it('渲染审核动作分布四卡片（计数+占比）与生成质量', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    render(<Quality />)

    expect(await screen.findByText('通过（60.0%）')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('驳回（25.0%）')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('合并（10.0%）')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('修改（5.0%）')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
    expect(screen.getByText('新标签通过率')).toBeInTheDocument()
    expect(screen.getByText('80.0%')).toBeInTheDocument()
  })

  it('渲染任务统计卡片五个字段', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    render(<Quality />)

    expect(await screen.findByText('任务总数')).toBeInTheDocument()
    expect(screen.getByText('20')).toBeInTheDocument()
    expect(screen.getByText('成功')).toBeInTheDocument()
    expect(screen.getByText('15')).toBeInTheDocument()
    expect(screen.getByText('失败')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('成功率')).toBeInTheDocument()
    expect(screen.getByText('75.0%')).toBeInTheDocument()
    expect(screen.getByText('平均耗时')).toBeInTheDocument()
    expect(screen.getByText('3000 ms')).toBeInTheDocument()
  })

  it('渲染置信度分布与标签类型覆盖两张条形图', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    render(<Quality />)

    // 图卡片标题与桶标签/数值
    expect(await screen.findByText('置信度分布')).toBeInTheDocument()
    expect(screen.getByText('0.0-0.1')).toBeInTheDocument()
    expect(screen.getByText('0.9-1.0')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    // 覆盖图：类型、总数与"通过 N"附加文本
    expect(screen.getByText('标签类型覆盖')).toBeInTheDocument()
    expect(screen.getByText('genre')).toBeInTheDocument()
    expect(screen.getByText('mood')).toBeInTheDocument()
    expect(screen.getByText('10')).toBeInTheDocument()
    expect(screen.getByText('通过 4')).toBeInTheDocument()
    // 三个指标端点各被请求一次
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/metrics/summary')
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/metrics/confidence-distribution')
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/metrics/tag-type-coverage')
  })

  it('率/占比/耗时字段为 None 时显示"暂无"', async () => {
    mockMetricsApis(
      {
        pending_count: 0,
        review_stats: {
          approved: { count: 0, ratio: null },
          rejected: { count: 0, ratio: null },
          merged: { count: 0, ratio: null },
          modified: { count: 0, ratio: null },
        },
        generated_pass_rate: null,
        evidence_coverage: null,
        task_stats: { total: 0, succeeded: 0, failed: 0, success_rate: null, avg_duration_ms: null },
      },
      CONFIDENCE,
      COVERAGE,
    )

    render(<Quality />)

    // 四个审核动作占比 + 新标签通过率 + 成功率 + 平均耗时，共 7 处"暂无"
    expect(await screen.findAllByText(/暂无/)).toHaveLength(7)
  })

  it('覆盖数据为空时展示空态提示', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, [])

    render(<Quality />)

    expect(await screen.findByText('暂无数据')).toBeInTheDocument()
  })
})
