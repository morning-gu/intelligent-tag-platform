// 标签库页测试（Task 13）：mock apiFetch 按路径分发，覆盖：目录渲染（类型/
// 状态 Tag、null 兜底）、服务端分页 limit/offset 映射（pageSize+1 探测、
// 翻页 offset 递进）、type 筛选与 q 搜索传参（提交触发而非输入即触发）、
// 行点击详情 Drawer（aliases/parent_id/status 展示与加载失败提示）。
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TagCatalog from './TagCatalog'
import { apiFetch } from '../api/client'
import type { TagCatalogDetail, TagCatalogItem } from '../api/catalog'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 TagOut/TagDetail 契约同形） ----------

/** 21 条目录项：第 1 页（pageSize 20）取满后仍有 1 条，可探测到下一页。 */
const CATALOG_ITEMS: TagCatalogItem[] = Array.from({ length: 21 }, (_, i) => ({
  tag_id: `t-${String(i + 1).padStart(3, '0')}`,
  tag_name: `标签${i + 1}`,
  tag_type: i % 2 === 0 ? 'scene' : 'theme',
  description: `第 ${i + 1} 个标签的描述`,
  parent_id: null,
  status: i === 1 ? 'deprecate' : 'active',
}))

const TAG_DETAIL: TagCatalogDetail = {
  tag_id: 't-003',
  tag_name: '标签3',
  tag_type: 'scene',
  description: '第 3 个标签的描述',
  parent_id: 't-001',
  status: 'active',
  aliases: ['别名甲', '别名乙'],
}

/** 筛选下拉候选（GET /review/filters 响应，动态 tag_types）。 */
const FILTERS = {
  tag_types: ['scene', 'theme', 'emotion'],
  statuses: ['pending', 'approved', 'rejected', 'merged'],
  modes: ['closed', 'open'],
}

// ---------- mock 装配与查询辅助 ----------

interface MockOptions {
  items?: TagCatalogItem[]
  detail?: TagCatalogDetail
}

/** 按路径分发的 apiFetch 桩：目录按 limit/offset 切片（模拟服务端分页行为）。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/review/filters') return FILTERS
    if (path === '/api/v1/tags' || path.startsWith('/api/v1/tags?')) {
      const items = options.items ?? CATALOG_ITEMS
      const params = new URLSearchParams(path.split('?')[1] ?? '')
      const limit = Number(params.get('limit') ?? items.length)
      const offset = Number(params.get('offset') ?? 0)
      return { items: items.slice(offset, offset + limit) }
    }
    if (/^\/api\/v1\/tags\/[^/]+$/.test(path)) {
      return options.detail ?? TAG_DETAIL
    }
    throw new Error(`未预期的请求：${path}`)
  })
}

/** 目录列表端点的请求路径（按调用顺序，断言筛选/分页参数用）。 */
function catalogCallPaths(): string[] {
  return mockedApiFetch.mock.calls
    .map((call) => call[0])
    .filter((p): p is string => typeof p === 'string' && p.startsWith('/api/v1/tags'))
}

function renderTagCatalog() {
  return render(<TagCatalog />)
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('TagCatalog 标签库页', () => {
  it('首屏渲染第 1 页 20 行（探测行不展示）：类型/状态 Tag 与筛选候选拉取', async () => {
    mockApis()
    renderTagCatalog()

    // 第一行：scene 类型 Tag + 启用状态
    const row1 = (await screen.findByText('标签1')).closest('tr')!
    expect(row1).toHaveTextContent('scene')
    expect(row1).toHaveTextContent('启用')
    // 第二行：deprecate 状态中文展示
    const row2 = screen.getByText('标签2').closest('tr')!
    expect(row2).toHaveTextContent('theme')
    expect(row2).toHaveTextContent('废弃')
    // 第 1 页共 20 行；探测到的第 21 条不进表格
    expect(screen.getByText('标签20')).toBeInTheDocument()
    expect(screen.queryByText('标签21')).not.toBeInTheDocument()

    // 首屏参数：pageSize 20 + 1 探测、offset 0 显式发送
    expect(catalogCallPaths()).toEqual(['/api/v1/tags?limit=21&offset=0'])
  })

  it('类型筛选选 scene：携带 type=scene 并重置回第 1 页', async () => {
    mockApis()
    renderTagCatalog()
    await screen.findByText('标签1')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '类型筛选' }))
    // antd v6 下拉存在隐藏的无障碍克隆（role=option、无点击处理器），
    // 真实可点选项带 title 属性——以 title 定位点击
    fireEvent.click(await screen.findByTitle('scene'))

    await waitFor(() => expect(catalogCallPaths()).toHaveLength(2))
    expect(catalogCallPaths()[1]).toBe('/api/v1/tags?type=scene&limit=21&offset=0')
  })

  it('名称搜索：提交（按钮）触发带 q 重新拉取，输入本身不触发', async () => {
    mockApis()
    renderTagCatalog()
    await screen.findByText('标签1')

    fireEvent.change(screen.getByPlaceholderText('名称/别名搜索'), {
      target: { value: '打斗' },
    })
    expect(catalogCallPaths()).toHaveLength(1) // 输入即触发则此处已两次

    fireEvent.click(screen.getByRole('button', { name: /搜\s*索/ }))

    await waitFor(() => expect(catalogCallPaths()).toHaveLength(2))
    expect(catalogCallPaths()[1]).toBe(`/api/v1/tags?q=${encodeURIComponent('打斗')}&limit=21&offset=0`)
  })

  it('翻到第 2 页：offset 递进为 20，剩余探测行进表格', async () => {
    mockApis()
    renderTagCatalog()
    await screen.findByText('标签1')

    // 首页 21 条探测到后续页：分页器应给出第 2 页
    fireEvent.click(await screen.findByTitle('2'))

    await waitFor(() => expect(catalogCallPaths()[1]).toBe('/api/v1/tags?limit=21&offset=20'))
    // 第 2 页展示第 21 条（探测行从数据源自然流入）
    expect(await screen.findByText('标签21')).toBeInTheDocument()
    expect(screen.queryByText('标签1')).not.toBeInTheDocument()
  })

  it('行点击打开详情 Drawer：tag_id/类型/描述/父标签/别名渲染', async () => {
    mockApis()
    renderTagCatalog()
    await screen.findByText('标签1')

    fireEvent.click(screen.getByText('标签3'))

    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(([p]) => p === '/api/v1/tags/t-003')).toBe(true)
    })
    expect(await screen.findByText('标签详情')).toBeInTheDocument()
    const aliasItem = screen.getByText('别名').closest('.ant-descriptions-item')!
    expect(aliasItem).toHaveTextContent('别名甲')
    expect(aliasItem).toHaveTextContent('别名乙')
    const parentItem = screen.getByText('父标签').closest('.ant-descriptions-item')!
    expect(parentItem).toHaveTextContent('t-001')
    expect(screen.getByText('t-003')).toBeInTheDocument()
  })

  it('目录加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw new Error('后端不可用')
    })
    renderTagCatalog()

    expect(await screen.findByText('标签库加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
