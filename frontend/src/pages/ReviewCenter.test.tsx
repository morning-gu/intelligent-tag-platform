// 审核中心测试（v2 行即日志，按影片分组）：mock apiFetch 按路径/方法分发
// （未命中即抛错），覆盖：按影片分组（组头片名+条数、默认折叠、展开见行）、
// 一次取回 page_size=500、目录（dir_id）/来源（origin）/置信度/状态筛选传参、
// 四动作行内按钮、单条通过载荷、409 冲突提示与自动刷新、非冲突错误透出、
// 修改弹窗载荷（提交即通过）、合并弹窗载荷（merge_to_tag_id 单支）、批量
// 操作载荷与失败汇总、行展开证据卡、相关度列（库内最高相似度）、加载失败。
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
  tag_types: ['val_fixed'],
  statuses: ['pending', 'approved', 'rejected'],
  modes: ['closed', 'open'],
  dirs: [
    {
      dir_id: 'dir-emotion', dir_name: '情感', path: '情感',
      dir_type: 'dir_dynamic', applicable_scope: 'content',
    },
    {
      dir_id: 'dir-action', dir_name: '动作', path: '内容特征标签/动作',
      dir_type: 'dir_static', applicable_scope: 'content',
    },
  ],
}

/** /tags?kind=tag 返回的标准标签节点（混合树 TagNode 形状）。 */
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

/** 库内命中行：有置信度有证据（可展开），相关度即对齐余弦。 */
const ROW_A: ReviewItem = {
  id: 1,
  asset_id: 'a1',
  asset_title: '星际穿越',
  tag_name: '犯罪',
  origin: 'closed',
  mode: 'closed',
  tag_type: 'val_fixed',
  confidence: 0.42,
  relevance: 0.77,
  review_status: 'pending',
  target_type: 'content',
  dir_id: 'dir-emotion',
  dir_name: '情感',
  dir_path: '内容特征标签/情感/父子关系',
  evidence: [{ scene_id: 's2', start_ms: 65_000, end_ms: 71_000, description: '主角争执推搡' }],
  task_id: 'task-1',
  reviewer_id: null,
  review_comment: null,
}

/** 新词候选行：目录/置信度/相关度均 null（兜底"—"/"暂无"），无可展开证据。 */
const ROW_CANDIDATE: ReviewItem = {
  id: 2,
  asset_id: 'a2',
  asset_title: '盗梦空间',
  tag_name: '主角争执',
  origin: 'open',
  mode: 'open',
  tag_type: null,
  confidence: null,
  relevance: null,
  review_status: 'pending',
  target_type: 'content',
  dir_id: null,
  dir_name: null,
  dir_path: null,
  evidence: [],
  task_id: 'task-1',
  reviewer_id: null,
  review_comment: null,
}

/** total=60 > 2 部影片：组头条数按行数算，总数走页脚文案。 */
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
    // 混合树列表端点：kind=tag 只取标签（合并弹窗数据源）
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

/** 展开所有影片分组（组头默认折叠，逐部点开才能看到标签行）。 */
async function expandGroups() {
  fireEvent.click(screen.getByText('星际穿越'))
  fireEvent.click(screen.getByText('盗梦空间'))
  await screen.findByText('主角争执')
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('ReviewCenter 审核中心', () => {
  it('按影片分组展示：组头是片名+条数，默认折叠，展开后见该片标签行', async () => {
    mockApis()
    renderReviewCenter()

    // 组头：两部影片各自一条 Collapse 面板，默认全部折叠（行不渲染）
    expect(await screen.findByText('星际穿越')).toBeInTheDocument()
    expect(screen.getByText('盗梦空间')).toBeInTheDocument()
    expect(screen.getByText('共 60 条 · 涉及 2 部影片')).toBeInTheDocument()
    expect(screen.getAllByText('1 条')).toHaveLength(2)
    expect(screen.queryByText('犯罪')).not.toBeInTheDocument()
    // 分组要完整行集：一次取回 500 行，不分页
    expect(itemsCallPaths()[0]).toContain('status=pending')
    expect(itemsCallPaths()[0]).toContain('page_size=500')

    // 展开第一部：只出现该片的行；目录列显示完整路径（不是只有叶子名）
    fireEvent.click(screen.getByText('星际穿越'))
    const tr = (await screen.findByText('犯罪')).closest('tr')!
    expect(tr).toHaveTextContent('内容特征标签/情感/父子关系')
    expect(tr).toHaveTextContent('闭环')
    expect(tr).toHaveTextContent('42%')
    expect(tr).toHaveTextContent('0.77') // 相关度 = 库内最高相似度，两位小数
    expect(screen.queryByText('主角争执')).not.toBeInTheDocument()

    // 再展开第二部：候选行出现，null 兜底"—"/"暂无"
    fireEvent.click(screen.getByText('盗梦空间'))
    const rowC = (await screen.findByText('主角争执')).closest('tr')!
    expect(within(rowC).getByText('—')).toBeInTheDocument()
    expect(within(rowC).getAllByText('暂无')).toHaveLength(2)
    expect(rowC).toHaveTextContent('开放')
  })

  it('目录筛选：选择目录后携带 dir_id= 重新拉取', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '目录筛选' }))
    // antd v6 下拉存在隐藏的无障碍克隆（role=option、无点击处理器），
    // 真实可点选项带 title 属性——以 title 定位点击
    fireEvent.click(await screen.findByTitle('情感'))

    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
    expect(itemsCallPaths()[1]).toContain('dir_id=dir-emotion')
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

  it('行内四动作：通过/拒绝/修改/合并对新词候选行同样可用', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')
    await expandGroups()

    const rowA = screen.getByText('犯罪').closest('tr')!
    const rowC = screen.getByText('主角争执').closest('tr')!

    for (const row of [rowA, rowC]) {
      expect(within(row).getByRole('button', { name: /^通\s*过$/ })).toBeInTheDocument()
      expect(within(row).getByRole('button', { name: /^拒\s*绝$/ })).toBeInTheDocument()
      expect(within(row).getByRole('button', { name: /^修\s*改$/ })).toBeInTheDocument()
      expect(within(row).getByRole('button', { name: /^合\s*并$/ })).toBeInTheDocument()
    }
  })

  it('单条通过：Popconfirm 确认后 POST approve 载荷并自动刷新', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')
    await expandGroups()

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
    await expandGroups()

    fireEvent.click(screen.getAllByRole('button', { name: /^通\s*过$/ })[0])
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    expect(await screen.findByText('该标签已被他人处理')).toBeInTheDocument()
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('非冲突错误：透出后端 detail 文案且不刷新列表', async () => {
    mockApis({ reviewError: new ApiError(422, '目录「主类型」是静态目录（dir_static），不允许扩充新标签') })
    renderReviewCenter()
    await screen.findByText('星际穿越')
    await expandGroups()

    fireEvent.click(screen.getAllByRole('button', { name: /^通\s*过$/ })[0])
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    expect(await screen.findByText('目录「主类型」是静态目录（dir_static），不允许扩充新标签'))
      .toBeInTheDocument()
    expect(itemsCallPaths()).toHaveLength(1)
  })

  it('修改弹窗：预填展示名，提交 modify 载荷（提交即通过）并刷新', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')
    await expandGroups()

    fireEvent.click(screen.getAllByRole('button', { name: /^修\s*改$/ })[0])
    const nameInput = await screen.findByLabelText('候选名')
    // 后端 tag_name 已是"候选名优先、目录名回退"的展示名
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
    expect(await screen.findByText('已修改并通过')).toBeInTheDocument()
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('合并弹窗：搜索库内标签，提交 merge_to_tag_id+comment 载荷（单支别名）', async () => {
    mockApis()
    renderReviewCenter()
    await screen.findByText('星际穿越')
    await expandGroups()

    fireEvent.click(screen.getAllByRole('button', { name: /^合\s*并$/ })[0])
    // 打开弹窗即预载候选标签（kind=tag 只搜标签，不混入目录）
    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(
        ([p]) => typeof p === 'string' && p.startsWith('/api/v1/tags')
          && p.includes('kind=tag'),
      )).toBe(true)
    })
    // 只有一支：没有"合入新建"单选，也没有新建标签字段
    expect(screen.queryByRole('radio', { name: /合入新建/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('新标签名')).not.toBeInTheDocument()

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '搜索标准标签' }))
    fireEvent.click(await screen.findByTitle('犯罪'))

    fireEvent.change(screen.getByLabelText('备注'), { target: { value: '并入既有目标' } })
    fireEvent.click(screen.getByRole('button', { name: /确\s*定/ }))
    await waitFor(() => expect(postedBody('/api/v1/asset-tags/1/review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/1/review')!)).toEqual({
      action: 'merge',
      payload: { merge_to_tag_id: 'tag-crime', comment: '并入既有目标' },
    })
  })

  it('批量通过：跨影片勾选后 POST 批量载荷，失败项汇总提示并刷新', async () => {
    mockApis({
      batchResults: [{ id: 1, ok: true }, { id: 2, ok: false, detail: '该标签已被处理' }],
    })
    renderReviewCenter()
    await screen.findByText('星际穿越')
    await expandGroups()

    // 每部影片一张表，各自表头有全选；勾行用行内 checkbox
    fireEvent.click(within(screen.getByText('犯罪').closest('tr')!).getByRole('checkbox'))
    fireEvent.click(within(screen.getByText('主角争执').closest('tr')!).getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: /^批量通过$/ }))
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))

    await waitFor(() => expect(postedBody('/api/v1/asset-tags/batch-review')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/asset-tags/batch-review')!)).toEqual({
      ids: [1, 2],
      action: 'approve',
    })
    expect(await screen.findByText(/成功 1 条，失败 1 条/)).toBeInTheDocument()
    await waitFor(() => expect(itemsCallPaths()).toHaveLength(2))
  })

  it('批量拒绝：勾选后批量操作，全部成功时提示成功计数', async () => {
    mockApis({ batchResults: [{ id: 1, ok: true }, { id: 2, ok: true }] })
    renderReviewCenter()
    await screen.findByText('星际穿越')
    await expandGroups()

    fireEvent.click(within(screen.getByText('犯罪').closest('tr')!).getByRole('checkbox'))
    fireEvent.click(within(screen.getByText('主角争执').closest('tr')!).getByRole('checkbox'))
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
    await expandGroups()

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
