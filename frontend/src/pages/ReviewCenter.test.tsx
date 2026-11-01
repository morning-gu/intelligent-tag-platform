// 审核中心测试（Task 10）：mock apiFetch 按路径/方法分发（未命中即抛错），
// 覆盖：列表渲染与默认筛选参数、筛选变更传参（置信度区间/状态下拉）、
// 分页传参、单条通过载荷、409 冲突提示与自动刷新（Review Focus 1）、
// 非冲突错误透出、修改/合并弹窗载荷、批量操作载荷与失败汇总、行展开证据卡。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ReviewCenter from './ReviewCenter'
import { ApiError, apiFetch } from '../api/client'
import type {
  BatchReviewItemResult,
  ReviewFilters,
  ReviewItem,
  ReviewItemsResponse,
  TagOption,
} from '../api/review'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 ReviewItemOut / ReviewFiltersOut 契约同形） ----------

const FILTERS: ReviewFilters = {
  tag_types: ['mood', 'genre'],
  statuses: ['pending', 'approved', 'rejected', 'merged'],
  modes: ['closed', 'open'],
}

const TAGS: TagOption[] = [
  { tag_id: 'tag-crime', tag_name: '犯罪', tag_type: 'genre', description: '犯罪题材' },
  { tag_id: 'tag-romance', tag_name: '爱情', tag_type: 'genre', description: '爱情题材' },
]

/** generated 行：有置信度有证据（可展开）。 */
const ROW_A: ReviewItem = {
  id: 1,
  asset_id: 'a1',
  asset_title: '星际穿越',
  tag_name: '主角争执',
  origin: 'generated',
  mode: 'open',
  tag_type: 'mood',
  confidence: 0.42,
  relevance: 0.77,
  review_status: 'pending',
  evidence: [{ scene_id: 's2', start_ms: 65_000, end_ms: 71_000, description: '主角争执推搡' }],
  task_id: 'task-1',
}

/** catalog 行：置信度/相关度为 null（兜底"暂无"）、无证据（不可展开）。 */
const ROW_B: ReviewItem = {
  id: 2,
  asset_id: 'a2',
  asset_title: '盗梦空间',
  tag_name: '枪战',
  origin: 'catalog',
  mode: 'closed',
  tag_type: 'genre',
  confidence: null,
  relevance: null,
  review_status: 'pending',
  evidence: [],
  task_id: null,
}

/** mode=null 行：模式列兜底"—"（任务缺失/被删的行经 outerjoin 得 null）。 */
const ROW_NULL_MODE: ReviewItem = {
  id: 3,
  asset_id: 'a3',
  asset_title: '无模式资产',
  tag_name: '慢镜头',
  origin: 'catalog',
  mode: null,
  tag_type: 'style',
  confidence: 0.8,
  relevance: 0.8,
  review_status: 'pending',
  evidence: [],
  task_id: null,
}

/** total=60 > page_size=50：分页器出现第二页按钮。 */
const PAGE_ONE: ReviewItemsResponse = { items: [ROW_A, ROW_B], total: 60, page: 1 }

// ---------- mock 装配与查询辅助 ----------

interface MockOptions {
  items?: ReviewItemsResponse
  /** 单条审核端点的拒绝行为（409/422 等分支）。 */
  reviewError?: Error
  batchResults?: BatchReviewItemResult[]
}

/** 按路径与方法分发的 apiFetch 桩。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (path === '/api/v1/review/filters') return FILTERS
    if (path.startsWith('/api/v1/review/items')) return options.items ?? PAGE_ONE
    if (method === 'POST' && /^\/api\/v1\/asset-tags\/\d+\/review$/.test(path)) {
      if (options.reviewError) throw options.reviewError
      return { id: 1, tag_name: '主角争执' }
    }
    if (method === 'POST' && path === '/api/v1/asset-tags/batch-review') {
      return { results: options.batchResults ?? [] }
    }
    if (path.startsWith('/api/v1/tags')) return { items: TAGS }
    throw new Error(`未预期的请求：${method} ${path}`)
  })
}

/** 审核列表端点的请求路径（按调用顺序，断言筛选/分页参数用）。 */
function itemsCallPaths(): string[] {
  return mockedApiFetch.mock.calls
    .map((call) => call[0])
    .filter((p): p is string => typeof p === 'string' && p.startsWith('/api/v1/review/items'))
}

/** 指定路径的 POST 请求体（JSON 字符串；未请求过返回 undefined）。 */
function postedBody(path: string): string | undefined {
  const call = mockedApiFetch.mock.calls.find(
    ([p, init]) => p === path && (init as RequestInit | undefined)?.method === 'POST',
  )
  return call === undefined ? undefined : String((call[1] as RequestInit).body)
}

function renderReviewCenter() {
  return render(
    <MemoryRouter initialEntries={['/review']}>
      <Routes>
        <Route path="/review" element={<ReviewCenter />} />
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

// ---------- 用例 ----------

describe('ReviewCenter 审核中心', () => {
  it('渲染列表行与默认筛选参数（status=pending、page_size=50）', async () => {
    mockApis()
    renderReviewCenter()

    expect(await screen.findByText('星际穿越')).toBeInTheDocument()
    const rowA = screen.getByText('星际穿越').closest('tr')!
    const rowB = screen.getByText('盗梦空间').closest('tr')!
    // 行内容：标签名 / 来源 Tag / 模式 Tag / 状态 Tag / 置信度进度条 / null 兜底
    expect(rowA).toHaveTextContent('主角争执')
    expect(rowA).toHaveTextContent('生成')
    expect(rowA).toHaveTextContent('开放')
    expect(rowA).toHaveTextContent('待审核')
    expect(rowA).toHaveTextContent('42%')
    expect(screen.getByText('目录')).toBeInTheDocument()
    expect(rowB).toHaveTextContent('闭环')
    expect(within(rowB).getAllByText('暂无')).toHaveLength(2)

    // 默认拉待审核，分页参数走列表 API
    const paths = itemsCallPaths()
    expect(paths).toHaveLength(1)
    expect(paths[0]).toContain('status=pending')
    expect(paths[0]).toContain('page=1')
    expect(paths[0]).toContain('page_size=50')
  })

  it('模式列：mode 为 null 时兜底显示"—"', async () => {
    mockApis({ items: { items: [ROW_NULL_MODE], total: 1, page: 1 } })
    renderReviewCenter()

    await screen.findByText('慢镜头')
    // 该行其余列均有值（标题/类型/置信度/相关度/状态），"—"只可能来自模式列
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('修改置信度下限筛选：携带 confidence_min 重新拉取', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.change(screen.getByPlaceholderText('最低置信度'), { target: { value: '0.2' } })

    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
    expect(itemsCallPaths()[1]).toContain('confidence_min=0.2')
    expect(itemsCallPaths()[1]).toContain('page=1')
  })

  it('切换状态下拉为已通过：携带 status=approved 重新拉取', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '状态筛选' }))
    // antd v6 下拉存在隐藏的无障碍克隆（role=option、无点击处理器），
    // 真实可点选项带 title 属性——以 title 定位点击
    fireEvent.click(await screen.findByTitle('已通过'))

    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
    expect(itemsCallPaths()[1]).toContain('status=approved')
  })

  it('点击第二页：携带 page=2 重新拉取', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getByText('2'))

    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
    expect(itemsCallPaths()[1]).toContain('page=2')
  })

  it('单条通过：Popconfirm 确认后 POST approve 载荷并自动刷新', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^通\s*过$/ })[0])
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    expect(await screen.findByText('已通过')).toBeInTheDocument()
    await waitFor(() => {
      expect(postedBody('/api/v1/asset-tags/1/review')).toBe(JSON.stringify({ action: 'approve' }))
    })
    // 成功后自动刷新列表（第二次拉取）
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('409 冲突：提示"该标签已被他人处理"并自动刷新列表', async () => {
    mockApis({ reviewError: new ApiError(409, '该标签已被处理') })
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^通\s*过$/ })[0])
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    expect(await screen.findByText('该标签已被他人处理')).toBeInTheDocument()
    // 冲突后自动刷新列表回显最新状态（Review Focus 1）
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('非冲突错误：透出后端 detail 文案且不刷新列表', async () => {
    mockApis({ reviewError: new ApiError(422, 'merge 动作必须提供 merge_to_tag_id') })
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^拒\s*绝$/ })[0])
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    expect(await screen.findByText('merge 动作必须提供 merge_to_tag_id')).toBeInTheDocument()
    expect(itemsCallPaths()).toHaveLength(1)
  })

  it('修改弹窗：预填当前行，提交 POST modify 载荷', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^修\s*改$/ })[0])
    const nameInput = await screen.findByLabelText('标签名')
    expect(nameInput).toHaveValue('主角争执')
    fireEvent.change(nameInput, { target: { value: '主角冲突' } })
    fireEvent.click(screen.getByRole('button', { name: /确\s*定/ }))

    await waitFor(() => expect(postedBody('/api/v1/asset-tags/1/review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/1/review')!)).toEqual({
      action: 'modify',
      payload: { tag_name: '主角冲突', tag_type: 'mood' },
    })
    // 成功后关弹窗并刷新列表
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('合并弹窗：搜索选择目标标签，提交 POST merge 载荷', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^合\s*并$/ })[0])
    // 打开弹窗即预载候选标签（空关键词）
    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(([p]) => p === '/api/v1/tags?limit=20')).toBe(true)
    })

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '搜索标准标签' }))
    // 同状态筛选：点击真实选项（title 定位，避开隐藏无障碍克隆）
    fireEvent.click(await screen.findByTitle('犯罪（genre）'))

    fireEvent.click(screen.getByRole('button', { name: /确\s*定/ }))
    await waitFor(() => expect(postedBody('/api/v1/asset-tags/1/review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/1/review')!)).toEqual({
      action: 'merge',
      payload: { merge_to_tag_id: 'tag-crime' },
    })
  })

  it('批量通过：POST 批量载荷，失败项汇总提示并刷新', async () => {
    mockApis({
      batchResults: [{ id: 1, ok: true }, { id: 2, ok: false, detail: '该标签已被处理' }],
    })
    renderReviewCenter()
    await screen.findByText('星际穿越')

    const checkboxes = screen.getAllByRole('checkbox')
    fireEvent.click(checkboxes[1]) // 第一行（0 为表头全选）
    fireEvent.click(checkboxes[2]) // 第二行
    fireEvent.click(screen.getByRole('button', { name: /^批量通过$/ }))
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    await waitFor(() => expect(postedBody('/api/v1/asset-tags/batch-review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/batch-review')!)).toEqual({
      ids: [1, 2],
      action: 'approve',
    })
    // 部分失败汇总提示（Review Focus 4：逐条独立不中断）
    expect(await screen.findByText(/成功 1 条，失败 1 条/)).toBeInTheDocument()
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('批量拒绝：全选后批量操作，全部成功时提示成功计数', async () => {
    mockApis({ batchResults: [{ id: 1, ok: true }, { id: 2, ok: true }] })
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('checkbox')[0]) // 表头全选
    fireEvent.click(screen.getByRole('button', { name: /^批量拒绝$/ }))
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    await waitFor(() => expect(postedBody('/api/v1/asset-tags/batch-review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/batch-review')!)).toEqual({
      ids: [1, 2],
      action: 'reject',
    })
    expect(await screen.findByText('批量拒绝成功（2 条）')).toBeInTheDocument()
  })

  it('行展开渲染证据卡：场景号/时间戳/描述', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    // 仅 ROW_A 有证据可展开（ROW_B rowExpandable=false）
    const expandIcon = document.querySelector('.ant-table-row-expand-icon')
    expect(expandIcon).not.toBeNull()
    fireEvent.click(expandIcon as Element)

    expect(await screen.findByText('主角争执推搡')).toBeInTheDocument()
    expect(screen.getByText('s2')).toBeInTheDocument()
    expect(screen.getByText('01:05 - 01:11')).toBeInTheDocument()
  })

  it('列表加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/v1/review/filters') return FILTERS
      throw new Error('后端不可用')
    })
    renderReviewCenter()

    expect(await screen.findByText('审核列表加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
