// 工作台测试：mock apiFetch 模块（vi.mock），断言四张统计卡片字段渲染、
// None → "暂无"，以及"进入审核"按钮跳转 /review。
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Dashboard from './Dashboard'
import { apiFetch } from '../api/client'
import type { MetricsSummary } from '../api/metrics'

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

/** 以 MemoryRouter 挂载工作台，/review 为跳转目标桩。 */
function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/review" element={<div>审核中心页面桩</div>} />
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

describe('Dashboard 工作台', () => {
  it('渲染四张统计卡片：待审核/任务成功率/证据覆盖率/平均耗时', async () => {
    // 泛型 apiFetch 的 mock 以 never 断言绕开 T 实例化检查（运行时即夹具）
    mockedApiFetch.mockResolvedValue(SUMMARY as never)

    renderDashboard()

    expect(await screen.findByText('待审核标签')).toBeInTheDocument()
    expect(screen.getByText('6')).toBeInTheDocument()
    expect(screen.getByText('任务成功率')).toBeInTheDocument()
    expect(screen.getByText('75.0%')).toBeInTheDocument()
    expect(screen.getByText('证据覆盖率')).toBeInTheDocument()
    expect(screen.getByText('50.0%')).toBeInTheDocument()
    expect(screen.getByText('平均任务耗时')).toBeInTheDocument()
    expect(screen.getByText('3000 ms')).toBeInTheDocument()
    // 请求走的是指标总览端点
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/metrics/summary')
  })

  it('率/耗时字段为 None 时显示"暂无"，计数字段不受影响', async () => {
    mockedApiFetch.mockResolvedValue({
      ...SUMMARY,
      evidence_coverage: null,
      task_stats: { ...SUMMARY.task_stats, success_rate: null, avg_duration_ms: null },
    } as never)

    renderDashboard()

    expect(await screen.findByText('证据覆盖率')).toBeInTheDocument()
    expect(screen.getAllByText('暂无')).toHaveLength(3)
    expect(screen.getByText('6')).toBeInTheDocument()
  })

  it('点击"进入审核"跳转 /review', async () => {
    mockedApiFetch.mockResolvedValue(SUMMARY as never)

    renderDashboard()

    // antd 两字按钮会自动插空隙，用宽容正则断言可访问名
    fireEvent.click(await screen.findByRole('button', { name: /进\s*入\s*审\s*核/ }))
    expect(screen.getByText('审核中心页面桩')).toBeInTheDocument()
  })

  it('加载失败渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockRejectedValue(new Error('后端不可用'))

    renderDashboard()

    expect(await screen.findByText('指标加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
