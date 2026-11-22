// 标签管理容器测试（Task 2 合一）：标签库 / 导入批次两 tab 收编
// TagCatalog / TagImports（组件零改动）。断言：默认激活标签库；?tab= 深链
// 激活导入批次；非法 ?tab= 回落标签库；点击 tab 写回 URL query。
// apiFetch 桩为永不落定 Promise——tab 容器结构不依赖子页面数据。
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TagManagement from './TagManagement'
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

/** 以 MemoryRouter 挂载标签管理容器（含 404 兜底路由）。 */
function renderAt(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <UrlCapture />
      <Routes>
        <Route path="/tags" element={<TagManagement />} />
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

describe('TagManagement 标签管理容器', () => {
  it('默认激活"标签库" tab，"导入批次"未选中', async () => {
    renderAt('/tags')

    expect(await screen.findByRole('tab', { name: '标签库' })).toBeInTheDocument()
    expectTabSelected('标签库')
    expect(screen.getByRole('tab', { name: '导入批次' })).toHaveAttribute(
      'aria-selected',
      'false',
    )
    // 默认 tab 不携带 query
    expect(lastUrl).toBe('/tags')
  })

  it('?tab=imports 深链直接激活"导入批次" tab', async () => {
    renderAt('/tags?tab=imports')

    expect(await screen.findByRole('tab', { name: '导入批次' })).toBeInTheDocument()
    expectTabSelected('导入批次')
    expect(lastUrl).toBe('/tags?tab=imports')
  })

  it('非法 ?tab=garbage 回落"标签库" tab，不崩溃', async () => {
    renderAt('/tags?tab=garbage')

    expect(await screen.findByRole('tab', { name: '标签库' })).toBeInTheDocument()
    expectTabSelected('标签库')
  })

  it('点击"导入批次"写回 URL 为 ?tab=imports', async () => {
    renderAt('/tags')

    fireEvent.click(await screen.findByRole('tab', { name: '导入批次' }))

    expectTabSelected('导入批次')
    expect(lastUrl).toBe('/tags?tab=imports')
  })
})
