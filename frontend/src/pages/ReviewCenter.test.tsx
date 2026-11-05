// 审核中心测试（v2 行即日志）：mock apiFetch 按路径/方法分发（未命中即抛
// 错），覆盖：列表渲染与默认筛选参数（status=pending、page_size=50）、
// 目录/来源/置信度/状态筛选传参（dir/origin 替代 v1 tag_type/mode）、分页
// 传参、候选行（无 tag_id）只出 拒绝+合并、单条通过载荷、409 冲突提示与
// 自动刷新（Review Focus 1）、非冲突错误透出、修改弹窗载荷（提交即通过）、
// 合并弹窗两支载荷（并入既有 merge_to_tag_id / 合入新建 new_tag_name+
// new_dir_id）、批量操作载荷与失败汇总、行展开证据卡、加载失败。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ReviewCenter from './ReviewCenter'
import { ApiError, apiFetch } from '../api/client'
import type { TagNode } from '../api/catalog'
import type {
  BatchReviewItemResult,
  ReviewFilters,
  ReviewItem,
  ReviewItemsResponse,
} from '../api/review'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 ReviewItemOut / ReviewFiltersOut 契约同形） ----------

const FILTERS: ReviewFilters = {
  statuses: ['pending', 'approved', 'rejected'],
  origins: ['closed', 'open'],
  dirs: ['情感', '动作'],
}

/** fetchTags（kind=tag）返回的标准标签节点（混合树 TagNode 形状）。 */
const TAG_NODES: TagNode[] = [
  {
    id: 'tag-crime', name: '犯罪', kind: 'tag', dir_type: null, description: '犯罪题材',
    parent_id: 'dir-crime', status: 'active', has_children: false, aliases: [],
  },
  {
    id: 'tag-romance', name: '爱情', kind: 'tag', dir_type: null, description: '爱情题材',
    parent_id: 'dir-crime', status: 'active', has_children: false, aliases: [],
  },
]

/** fetchTagCatalog（kind=dir）返回的目录节点（合并弹窗"合入新建"数据源）。 */
const DIR_NODES: TagNode[] = [
  {
    id: 'dir-emotion', name: '情感', kind: 'dir', dir_type: 'dir_static', description: '',
    parent_id: null, status: null, has_children: true, aliases: [],
  },
  {
    id: 'dir-action', name: '动作', kind: 'dir', dir_type: 'dir_static', description: '',
    parent_id: null, status: null, has_children: true, aliases: [],
  },
]

/** closed 行：有 tag_id（四动作全出），有置信度有证据（可展开）。 */
const ROW_A: ReviewItem = {
  id: 1,
  asset_id: 'a1',
  asset_title: '星际穿越',
  tag_name: '犯罪',
  tag_id: 'tag-crime',
  candidate_name: null,
  origin: 'closed',
  dir_name: '情感',
  confidence: 0.42,
  relevance: 0.77,
  review_status: 'pending',
  evidence: [{ scene_id: 's2', start_ms: 65_000, end_ms: 71_000, description: '主角争执推搡' }],
  task_id: 'task-1',
}

/** open 自由候选行：tag_id null、candidate_name 非空——approve/modify 后端
 * 422，页面只出 拒绝+合并；dir_name/置信度/相关度均 null（兜底"—"/"暂无"）。 */
const ROW_CANDIDATE: ReviewItem = {
  id: 2,
  asset_id: 'a2',
  asset_title: '盗梦空间',
  tag_name: '主角争执',
  tag_id: null,
  candidate_name: '主角争执',
  origin: 'open',
  dir_name: null,
  confidence: null,
  relevance: null,
  review_status: 'pending',
  evidence: [],
  task_id: 'task-1',
}

/** total=60 > page_size=50：分页器出现第二页按钮。 */
const PAGE_ONE: ReviewItemsResponse = { items: [ROW_A, ROW_CANDIDATE], total: 60, page: 1 }

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
      return { id: 1, tag_name: '犯罪' }
    }
    if (method === 'POST' && path === '/api/v1/asset-tags/batch-review') {
      return { results: options.batchResults ?? [] }
    }
    // 混合树列表端点按 kind 分流：dir 只取目录（合入新建下拉），其余为
    // 标签搜索（合并目标）
    if (path.startsWith('/api/v1/tags') && path.includes('kind=dir')) {
      return { items: DIR_NODES }
    }
    if (path.startsWith('/api/v1/tags')) return { items: TAG_NODES }
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
    const rowC = screen.getByText('盗梦空间').closest('tr')!
    // 行内容：标签名 / 目录列 / 来源 Tag（closed 闭环 / open 开放，mode 列
    // 已删）/ 状态 Tag / 置信度进度条 / null 兜底（目录"—"、分数"暂无"）
    expect(rowA).toHaveTextContent('犯罪')
    expect(rowA).toHaveTextContent('情感')
    expect(rowA).toHaveTextContent('闭环')
    expect(rowA).toHaveTextContent('待审核')
    expect(rowA).toHaveTextContent('42%')
    expect(rowC).toHaveTextContent('主角争执')
    expect(rowC).toHaveTextContent('开放')
    expect(within(rowC).getByText('—')).toBeInTheDocument()
    expect(within(rowC).getAllByText('暂无')).toHaveLength(2)

    // 默认拉待审核，分页参数走列表 API
    const paths = itemsCallPaths()
    expect(paths).toHaveLength(1)
    expect(paths[0]).toContain('status=pending')
    expect(paths[0]).toContain('page=1')
    expect(paths[0]).toContain('page_size=50')
  })

  it('目录筛选：选择目录后携带 dir= 重新拉取', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '目录筛选' }))
    // antd v6 下拉存在隐藏的无障碍克隆（role=option、无点击处理器），
    // 真实可点选项带 title 属性——以 title 定位点击
    fireEvent.click(await screen.findByTitle('情感'))

    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
    expect(itemsCallPaths()[1]).toContain(`dir=${encodeURIComponent('情感')}`)
    expect(itemsCallPaths()[1]).toContain('page=1')
  })

  it('来源筛选：选择开放后携带 origin=open 重新拉取', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '来源筛选' }))
    fireEvent.click(await screen.findByTitle('开放'))

    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
    expect(itemsCallPaths()[1]).toContain('origin=open')
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

  it('行动作按形态分支：tag_id 行四动作全出，候选行只出 拒绝+合并', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    const rowA = screen.getByText('星际穿越').closest('tr')!
    const rowC = screen.getByText('盗梦空间').closest('tr')!

    // tag_id 行（closed 行 / open 归一命中行）：四动作全出
    expect(within(rowA).getByRole('button', { name: /^通\s*过$/ })).toBeInTheDocument()
    expect(within(rowA).getByRole('button', { name: /^拒\s*绝$/ })).toBeInTheDocument()
    expect(within(rowA).getByRole('button', { name: /^修\s*改$/ })).toBeInTheDocument()
    expect(within(rowA).getByRole('button', { name: /^合\s*并$/ })).toBeInTheDocument()

    // open 自由候选行（tag_id null）：approve/modify 后端 422——不出口
    expect(within(rowC).queryByRole('button', { name: /^通\s*过$/ })).not.toBeInTheDocument()
    expect(within(rowC).queryByRole('button', { name: /^修\s*改$/ })).not.toBeInTheDocument()
    expect(within(rowC).getByRole('button', { name: /^拒\s*绝$/ })).toBeInTheDocument()
    expect(within(rowC).getByRole('button', { name: /^合\s*并$/ })).toBeInTheDocument()
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
    mockApis({ reviewError: new ApiError(422, '该候选行未关联标签定义（open 自由候选须先合并入库 merge）') })
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^拒\s*绝$/ })[0])
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    expect(await screen.findByText('该候选行未关联标签定义（open 自由候选须先合并入库 merge）')).toBeInTheDocument()
    expect(itemsCallPaths()).toHaveLength(1)
  })

  it('修改弹窗：预填候选名，提交 modify 载荷（提交即通过）并刷新', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^修\s*改$/ })[0])
    const nameInput = await screen.findByLabelText('候选名')
    // closed 行 candidate_name 为空——预填回退展示名（定义名）
    expect(nameInput).toHaveValue('犯罪')
    fireEvent.change(nameInput, { target: { value: '犯罪题材' } })
    fireEvent.change(screen.getByLabelText('候选描述'), { target: { value: '涉及犯罪情节' } })
    fireEvent.change(screen.getByLabelText('备注'), { target: { value: '修正措辞' } })
    fireEvent.click(screen.getByRole('button', { name: /确\s*定/ }))

    await waitFor(() => expect(postedBody('/api/v1/asset-tags/1/review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/1/review')!)).toEqual({
      action: 'modify',
      payload: {
        candidate_name: '犯罪题材',
        candidate_description: '涉及犯罪情节',
        comment: '修正措辞',
      },
    })
    // 提交即通过（后端 modify=更新后 approve）：成功提示 + 自动刷新列表
    expect(await screen.findByText('已修改并通过')).toBeInTheDocument()
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('合并弹窗·并入既有：搜索选择目标标签，提交 merge_to_tag_id+comment 载荷', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^合\s*并$/ })[0])
    // 打开弹窗即预载候选标签（kind=tag 只搜标签，不混入目录）
    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(
        ([p]) => p === '/api/v1/tags?kind=tag&limit=20',
      )).toBe(true)
    })

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '搜索标准标签' }))
    // 同下拉筛选：点击真实选项（title 定位，避开隐藏无障碍克隆）
    fireEvent.click(await screen.findByTitle('犯罪'))

    fireEvent.change(screen.getByLabelText('备注'), { target: { value: '并入既有目标' } })
    fireEvent.click(screen.getByRole('button', { name: /确\s*定/ }))
    await waitFor(() => expect(postedBody('/api/v1/asset-tags/1/review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/1/review')!)).toEqual({
      action: 'merge',
      payload: { merge_to_tag_id: 'tag-crime', comment: '并入既有目标' },
    })
  })

  it('合并弹窗·合入新建：目录下拉懒加载，提交 new_tag_name/new_dir_id 载荷', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.click(screen.getAllByRole('button', { name: /^合\s*并$/ })[0])
    // 支二目录下拉 lazily：切到合入新建才拉 kind=dir
    fireEvent.click(screen.getByRole('radio', { name: /合入新建/ }))
    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(
        ([p]) => typeof p === 'string' && p.includes('kind=dir'),
      )).toBe(true)
    })

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '目标目录' }))
    fireEvent.click(await screen.findByTitle('情感'))

    fireEvent.change(screen.getByLabelText('新标签名'), { target: { value: '亲情' } })
    fireEvent.change(screen.getByLabelText('新标签描述'), { target: { value: '亲情题材' } })
    fireEvent.change(screen.getByLabelText('备注'), { target: { value: '自由候选归置' } })
    fireEvent.click(screen.getByRole('button', { name: /确\s*定/ }))

    await waitFor(() => expect(postedBody('/api/v1/asset-tags/1/review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/1/review')!)).toEqual({
      action: 'merge',
      payload: {
        new_tag_name: '亲情',
        new_dir_id: 'dir-emotion',
        new_description: '亲情题材',
        comment: '自由候选归置',
      },
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

    // 仅 ROW_A 有证据可展开（ROW_C rowExpandable=false）
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
