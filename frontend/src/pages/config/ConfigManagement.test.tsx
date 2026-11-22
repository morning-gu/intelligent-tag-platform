// 配置管理容器测试（Task 3 合一）：用户 / 模型与 Prompt 两 tab 收编
// Users / ModelConfig（组件零改动）。断言：默认激活用户；?tab=model 深链
// 激活模型与 Prompt；非法 ?tab= 回落用户；点击 tab 写回 URL query。
// apiFetch 桩为永不落定 Promise——tab 容器结构不依赖子页面数据。
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ConfigManagement from './ConfigManagement'
import { apiFetch } from '../../api/client'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

/** 最近一次渲染观测到的完整 URL（pathname + search），用于断言 query 写回。 */
let lastUrl = ''
function UrlCapture() {
  const location = useLocation()
  lastUrl = `${location.pathname}${location.search}`
  return null
}

/** 以 MemoryRouter 挂载配置管理容器（含 404 兜底路由）。 */
function renderAt(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <UrlCapture />
      <Routes>
        <Route path="/config" element={<ConfigManagement />} />
        <Route path="*" element={<div>未匹配路由</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

/** antd tab 的选中态断言。 */
function expectTabSelected(name: string) {
  expect(screen.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'true')
}

beforeEach(() => {
  mockedApiFetch.mockReset()
  mockedApiFetch.mockImplementation(() => new Promise(() => {}))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('ConfigManagement 配置管理容器', () => {
  it('默认激活"用户" tab，"模型与 Prompt"未选中', async () => {
    renderAt('/config')

    expect(await screen.findByRole('tab', { name: '用户' })).toBeInTheDocument()
    expectTabSelected('用户')
    expect(screen.getByRole('tab', { name: '模型与 Prompt' })).toHaveAttribute(
      'aria-selected',
      'false',
    )
    // 默认 tab 不携带 query
    expect(lastUrl).toBe('/config')
  })

  it('?tab=model 深链直接激活"模型与 Prompt" tab', async () => {
    renderAt('/config?tab=model')

    expect(await screen.findByRole('tab', { name: '模型与 Prompt' })).toBeInTheDocument()
    expectTabSelected('模型与 Prompt')
    expect(lastUrl).toBe('/config?tab=model')
  })

  it('非法 ?tab=garbage 回落"用户" tab，不崩溃', async () => {
    renderAt('/config?tab=garbage')

    expect(await screen.findByRole('tab', { name: '用户' })).toBeInTheDocument()
    expectTabSelected('用户')
  })

  it('点击"模型与 Prompt"写回 URL 为 ?tab=model', async () => {
    renderAt('/config')

    fireEvent.click(await screen.findByRole('tab', { name: '模型与 Prompt' }))

    expectTabSelected('模型与 Prompt')
    expect(lastUrl).toBe('/config?tab=model')
  })
})
