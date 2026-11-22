// 工作台总览测试（Task 1 吸收质量评估）：mock apiFetch 按请求路径分发三个
// 指标端点夹具，断言总览四卡（待审核/任务成功率/证据覆盖率/平均任务耗时）+
// 审核动作分布三卡（通过/驳回/合并，modified 退役）+ 生成质量 + 任务统计 +
// 置信度分布、目录覆盖两张条形图；率/占比字段 null → "暂无"；快捷入口
// 进入审核 → /review、创建任务 → /tasks；单端点 reject → 错误 Alert。
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Dashboard from './Dashboard'
import { apiFetch } from '../api/client'
import type { ConfidenceBucket, DirCoverageRow, MetricsSummary } from '../api/metrics'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

/** 与后端 MetricsSummaryOut 契约同形的夹具（各率字段非 null）。 */
const SUMMARY: MetricsSummary = {
  pending_count: 6,
  review_stats: {
    approved: { count: 12, ratio: 0.6 },
    rejected: { count: 5, ratio: 0.25 },
    merged: { count: 2, ratio: 0.1 },
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

/** 目录覆盖夹具（后端按 total 降序返回；无目录归属候选行归"未归置"桶）。 */
const COVERAGE: DirCoverageRow[] = [
  { dir_name: '叙事结构', total: 10, approved: 4 },
  { dir_name: '未归置', total: 8, approved: 3 },
]

/** 按路径分发三个指标端点的 mock 装配。 */
function mockMetricsApis(
  summary: MetricsSummary,
  confidence: ConfidenceBucket[],
  coverage: DirCoverageRow[],
) {
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/metrics/summary') return summary
    if (path === '/api/v1/metrics/confidence-distribution') return confidence
    if (path === '/api/v1/metrics/dir-coverage') return coverage
    throw new Error(`未预期的请求路径：${path}`)
  })
}

/** 以 MemoryRouter 挂载工作台，/review 与 /tasks 为跳转目标桩。 */
function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/review" element={<div>审核中心页面桩</div>} />
        <Route path="/tasks" element={<div>生产任务页面桩</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('Dashboard 工作台总览', () => {
  it('渲染总览四卡与审核动作分布三卡（计数+占比）及生成质量', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    renderDashboard()

    // 总览四卡（沿用旧工作台口径）
    expect(await screen.findByText('待审核标签')).toBeInTheDocument()
    expect(screen.getByText('6')).toBeInTheDocument()
    expect(screen.getByText('任务成功率')).toBeInTheDocument()
    expect(screen.getByText('证据覆盖率')).toBeInTheDocument()
    expect(screen.getByText('50.0%')).toBeInTheDocument()
    expect(screen.getByText('平均任务耗时')).toBeInTheDocument()
    // 审核动作分布三卡（modified 随"修改即通过"语义退役）
    expect(screen.getByText('审核动作分布')).toBeInTheDocument()
    expect(screen.getByText('通过（60.0%）')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('驳回（25.0%）')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('合并（10.0%）')).toBeInTheDocument()
    expect(screen.queryByText(/修改（/)).not.toBeInTheDocument()
    // 生成质量
    expect(screen.getByText('新标签通过率')).toBeInTheDocument()
    expect(screen.getByText('80.0%')).toBeInTheDocument()
  })

  it('渲染任务统计卡五字段（顶部卡与统计卡的重复值各出现两处）', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    renderDashboard()

    expect(await screen.findByText('任务总数')).toBeInTheDocument()
    expect(screen.getByText('20')).toBeInTheDocument()
    expect(screen.getByText('成功')).toBeInTheDocument()
    expect(screen.getByText('15')).toBeInTheDocument()
    expect(screen.getByText('失败')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    // 成功率 75.0% 与平均耗时 3000 ms 在总览行和任务统计卡各渲染一次
    expect(screen.getAllByText('75.0%')).toHaveLength(2)
    expect(screen.getAllByText('3000 ms')).toHaveLength(2)
  })

  it('渲染置信度分布与目录覆盖两张条形图，三个指标端点各请求一次', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    renderDashboard()

    // 图卡片标题与桶标签/数值
    expect(await screen.findByText('置信度分布')).toBeInTheDocument()
    expect(screen.getByText('0.0-0.1')).toBeInTheDocument()
    expect(screen.getByText('0.9-1.0')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    // 覆盖图：目录名（含"未归置"桶）、总数与"通过 N"附加文本
    expect(screen.getByText('目录覆盖')).toBeInTheDocument()
    expect(screen.getByText('叙事结构')).toBeInTheDocument()
    expect(screen.getByText('未归置')).toBeInTheDocument()
    expect(screen.getByText('10')).toBeInTheDocument()
    expect(screen.getByText('通过 4')).toBeInTheDocument()
    // 三个指标端点各被请求一次
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/metrics/summary')
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/metrics/confidence-distribution')
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/metrics/dir-coverage')
  })

  it('率/占比/耗时字段为 null 时显示"暂无"（总览三处+指标六处共九处）', async () => {
    mockMetricsApis(
      {
        pending_count: 0,
        review_stats: {
          approved: { count: 0, ratio: null },
          rejected: { count: 0, ratio: null },
          merged: { count: 0, ratio: null },
        },
        generated_pass_rate: null,
        evidence_coverage: null,
        task_stats: { total: 0, succeeded: 0, failed: 0, success_rate: null, avg_duration_ms: null },
      },
      CONFIDENCE,
      COVERAGE,
    )

    renderDashboard()

    // 总览行：任务成功率/证据覆盖率/平均任务耗时 3 处
    // 审核动作分布 3 处占比 + 新标签通过率 1 处 + 任务统计成功率/平均耗时 2 处
    expect(await screen.findAllByText(/暂无/)).toHaveLength(9)
  })

  it('覆盖数据为空时展示空态提示', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, [])

    renderDashboard()

    expect(await screen.findByText('暂无数据')).toBeInTheDocument()
  })

  it('快捷入口：点击"进入审核"跳 /review', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    renderDashboard()

    // antd 两字按钮会自动插空隙，用宽容正则断言可访问名
    fireEvent.click(await screen.findByRole('button', { name: /进\s*入\s*审\s*核/ }))
    expect(screen.getByText('审核中心页面桩')).toBeInTheDocument()
  })

  it('快捷入口：点击"创建任务"跳 /tasks', async () => {
    mockMetricsApis(SUMMARY, CONFIDENCE, COVERAGE)

    renderDashboard()

    fireEvent.click(await screen.findByRole('button', { name: /创\s*建\s*任\s*务/ }))
    expect(screen.getByText('生产任务页面桩')).toBeInTheDocument()
  })

  it('单端点 reject（置信度分布）渲染错误提示，不白屏', async () => {
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/v1/metrics/summary') return SUMMARY
      if (path === '/api/v1/metrics/confidence-distribution') {
        throw new Error('置信度接口不可用')
      }
      if (path === '/api/v1/metrics/dir-coverage') return COVERAGE
      throw new Error(`未预期的请求路径：${path}`)
    })

    renderDashboard()

    expect(await screen.findByText('指标加载失败')).toBeInTheDocument()
    expect(screen.getByText('置信度接口不可用')).toBeInTheDocument()
  })
})
