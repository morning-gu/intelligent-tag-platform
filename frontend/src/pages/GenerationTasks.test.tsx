// 生产任务页测试（Task 12）：mock apiFetch 按路径/方法分发（未命中即抛错），
// 覆盖：列表渲染（状态/模式 Tag、错误列、时间与 null 兜底、failed 行重试按钮）、
// 状态与模式筛选传参、失败行重试 POST 与刷新（含 422 错误透出不刷新）、
// 创建弹窗预载资产与提交载荷（policy 按填写组装/未填省略）、行展开详情
// results 渲染（含证据卡与 null 兜底）。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import GenerationTasks from './GenerationTasks'
import { ApiError, apiFetch } from '../api/client'
import type { AssetSummary } from '../api/assets'
import type {
  GenerationTaskDetail,
  GenerationTaskSummary,
} from '../api/tasks'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 GenerationTaskOut / GenerationTaskDetail 契约同形） ----------

/** 四状态任务各一：succeeded（含版本与时间）、failed（含错误）、
 * queued（asset_id/时间 null 兜底）、running。 */
const TASKS: GenerationTaskSummary[] = [
  {
    task_id: 'task-1',
    asset_id: 'a1',
    mode: 'closed',
    status: 'succeeded',
    model_version: 'mock-vision-v1',
    prompt_version: 'p1',
    error: null,
    created_at: '2026-10-30T08:30:00',
    finished_at: '2026-10-30T08:30:05',
  },
  {
    task_id: 'task-2',
    asset_id: 'a2',
    mode: 'open',
    status: 'failed',
    model_version: null,
    prompt_version: null,
    error: '模型调用超时',
    created_at: '2026-10-30T09:00:00',
    finished_at: null,
  },
  {
    task_id: 'task-3',
    asset_id: null,
    mode: 'closed',
    status: 'queued',
    model_version: null,
    prompt_version: null,
    error: null,
    created_at: null,
    finished_at: null,
  },
  {
    task_id: 'task-4',
    asset_id: 'a2',
    mode: 'open',
    status: 'running',
    model_version: null,
    prompt_version: null,
    error: null,
    created_at: '2026-10-30T09:10:00',
    finished_at: null,
  },
]

/** task-1 详情：generated 行（含置信度/证据）+ catalog 行（双 null 兜底）。 */
const DETAIL: GenerationTaskDetail = {
  ...TASKS[0],
  results: [
    {
      id: 11,
      tag_name: '主角争执',
      origin: 'open',
      relevance_score: 0.87,
      confidence: 0.92,
      review_status: 'pending',
      target_type: 'content',
    dir_id: 'dir_theme',
    dir_name: '细分题材',
      evidence: [
        { scene_id: 's2', start_ms: 65_000, end_ms: 71_000, description: '主角争执推搡' },
      ],
    },
    {
      id: 12,
      tag_name: '犯罪',
      origin: 'closed',
      relevance_score: null,
      confidence: null,
      review_status: 'approved',
      target_type: 'content',
    dir_id: 'dir_theme',
    dir_name: '细分题材',
      evidence: [],
    },
  ],
}

/** 创建弹窗资产候选（GET /assets 全量）。 */
const ASSETS: AssetSummary[] = [
  {
    asset_id: 'a-movie',
    asset_type: 'movie',
    parent_id: null,
    title: '星际穿越',
    duration_ms: 10_800_000,
    video_uri: null,
    has_content: true,
    created_at: null,
    updated_at: null,
  },
  {
    asset_id: 'a-scene',
    asset_type: 'scene',
    parent_id: 'a-movie',
    title: '星际穿越-场景2',
    duration_ms: 65_000,
    video_uri: null,
    has_content: false,
    created_at: null,
    updated_at: null,
  },
]

const CREATE_RESULT = {
  tasks: [{ task_id: 'task-new-1', asset_id: 'a-movie', status: 'queued' }],
}

// ---------- mock 装配与查询辅助 ----------

interface MockOptions {
  tasks?: GenerationTaskSummary[]
  detail?: GenerationTaskDetail
  /** 重试端点的拒绝行为（非 failed → 422 等分支）。 */
  retryError?: Error
  createResult?: typeof CREATE_RESULT
}

/** 按路径与方法分发的 apiFetch 桩。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST' && path === '/api/v1/generation/tasks') {
      return options.createResult ?? CREATE_RESULT
    }
    if (method === 'POST' && /^\/api\/v1\/generation\/tasks\/[^/]+\/retry$/.test(path)) {
      if (options.retryError) throw options.retryError
      return { task_id: 'task-2', status: 'queued' }
    }
    if (path === '/api/v1/generation/tasks' || path.startsWith('/api/v1/generation/tasks?')) {
      return options.tasks ?? TASKS
    }
    if (method === 'GET' && /^\/api\/v1\/generation\/tasks\/[^/]+$/.test(path)) {
      return options.detail ?? DETAIL
    }
    if (path === '/api/v1/assets' || path.startsWith('/api/v1/assets?')) return ASSETS
    throw new Error(`未预期的请求：${method} ${path}`)
  })
}

/** 任务列表端点的请求路径（按调用顺序，断言筛选参数用；仅 GET——排除
 * 同路径的创建 POST 与详情/重试路径）。 */
function listCallPaths(): string[] {
  return mockedApiFetch.mock.calls
    .filter(
      ([p, init]) =>
        typeof p === 'string' &&
        (init?.method ?? 'GET') === 'GET' &&
        (p === '/api/v1/generation/tasks' || p.startsWith('/api/v1/generation/tasks?')),
    )
    .map((call) => call[0] as string)
}

/** 指定路径的 POST 请求体（JSON 字符串；未请求过返回 undefined）。 */
function postedBody(path: string): string | undefined {
  const call = mockedApiFetch.mock.calls.find(
    ([p, init]) => p === path && (init as RequestInit | undefined)?.method === 'POST',
  )
  return call === undefined ? undefined : String((call[1] as RequestInit).body)
}

function renderGenerationTasks() {
  return render(
    <MemoryRouter initialEntries={['/tasks']}>
      <Routes>
        <Route path="/tasks" element={<GenerationTasks />} />
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

describe('GenerationTasks 生产任务页', () => {
  it('渲染任务行：状态/模式 Tag、错误列、时间与 null 兜底、failed 行重试按钮', async () => {
    mockApis()
    renderGenerationTasks()

    const row1 = (await screen.findByText('task-1')).closest('tr')!
    expect(row1).toHaveTextContent('a1')
    expect(row1).toHaveTextContent('闭环')
    expect(row1).toHaveTextContent('已成功')
    expect(row1).toHaveTextContent('2026-10-30 08:30:00')
    expect(row1).toHaveTextContent('2026-10-30 08:30:05')
    const row2 = screen.getByText('task-2').closest('tr')!
    expect(row2).toHaveTextContent('开放')
    expect(row2).toHaveTextContent('已失败')
    expect(row2).toHaveTextContent('模型调用超时')
    const row3 = screen.getByText('task-3').closest('tr')!
    expect(row3).toHaveTextContent('排队中')
    expect(row3).toHaveTextContent('—') // asset_id/created_at/finished_at/error null 兜底
    expect(screen.getByText('task-4').closest('tr')).toHaveTextContent('执行中')

    // 仅 failed 行有重试按钮
    expect(within(row2).getByRole('button', { name: /重\s*试/ })).toBeInTheDocument()
    expect(within(row1).queryByRole('button', { name: /重\s*试/ })).not.toBeInTheDocument()

    // 默认无筛选：不带查询串
    expect(listCallPaths()).toEqual(['/api/v1/generation/tasks'])
  })

  it('状态筛选选已失败：携带 status=failed 重新拉取', async () => {
    mockApis()
    renderGenerationTasks()
    await screen.findByText('task-1')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '状态筛选' }))
    // antd v6 下拉存在隐藏的无障碍克隆（role=option、无点击处理器），
    // 真实可点选项带 title 属性——以 title 定位点击
    fireEvent.click(await screen.findByTitle('已失败'))

    await waitFor(() => expect(listCallPaths()).toHaveLength(2))
    expect(listCallPaths()[1]).toBe('/api/v1/generation/tasks?status=failed')
  })

  it('模式筛选选开放：携带 mode=open 重新拉取', async () => {
    mockApis()
    renderGenerationTasks()
    await screen.findByText('task-1')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '模式筛选' }))
    fireEvent.click(await screen.findByTitle('开放'))

    await waitFor(() => expect(listCallPaths()).toHaveLength(2))
    expect(listCallPaths()[1]).toBe('/api/v1/generation/tasks?mode=open')
  })

  it('失败行重试：POST retry 后提示成功并刷新列表', async () => {
    mockApis()
    renderGenerationTasks()
    await screen.findByText('task-2')

    fireEvent.click(
      within(screen.getByText('task-2').closest('tr')!).getByRole('button', { name: /重\s*试/ }),
    )

    await waitFor(() => {
      const call = mockedApiFetch.mock.calls.find(
        ([p]) => p === '/api/v1/generation/tasks/task-2/retry',
      )
      expect(call?.[1]?.method).toBe('POST')
    })
    expect(await screen.findByText(/已重新排队/)).toBeInTheDocument()
    // 成功后自动刷新列表（第二次拉取）
    await waitFor(() => expect(listCallPaths()).toHaveLength(2))
  })

  it('重试失败（非 failed 任务）：透出后端 detail 且不刷新列表', async () => {
    mockApis({ retryError: new ApiError(422, '仅失败任务可重试') })
    renderGenerationTasks()
    await screen.findByText('task-2')

    fireEvent.click(
      within(screen.getByText('task-2').closest('tr')!).getByRole('button', { name: /重\s*试/ }),
    )

    expect(await screen.findByText('仅失败任务可重试')).toBeInTheDocument()
    expect(listCallPaths()).toHaveLength(1)
  })

  it('创建弹窗：预载资产候选，多选资产 + mode + policy 提交载荷并刷新', async () => {
    mockApis()
    renderGenerationTasks()
    await screen.findByText('task-1')

    fireEvent.click(screen.getByRole('button', { name: '创建任务' }))
    // 打开弹窗即预载资产候选（GET /assets）
    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(([p]) => p === '/api/v1/assets')).toBe(true)
    })

    // 选项仅在下拉展开后渲染：先 mouseDown 再按 title 定位点击
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '资产多选' }))
    fireEvent.click(await screen.findByTitle('星际穿越（a-movie）'))
    fireEvent.click(await screen.findByTitle('星际穿越-场景2（a-scene）'))
    // 模式切为开放、最大标签数填 20（最低置信度留空走后端默认）
    fireEvent.click(screen.getByRole('radio', { name: '开放' }))
    fireEvent.change(screen.getByPlaceholderText('默认 30'), { target: { value: '20' } })

    // 弹窗确定按钮为两字"创建"（antd 渲染为"创 建"），与触发按钮区分
    fireEvent.click(screen.getByRole('button', { name: /创\s*建$/ }))

    await waitFor(() => expect(postedBody('/api/v1/generation/tasks')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/generation/tasks')!)).toEqual({
      asset_ids: ['a-movie', 'a-scene'],
      mode: 'open',
      policy: { max_tags: 20 },
    })
    expect(await screen.findByText('已创建 1 个任务')).toBeInTheDocument()
    // 成功后关弹窗并刷新列表
    await waitFor(() => expect(listCallPaths()).toHaveLength(2))
  })

  it('创建弹窗：未填策略时不携带 policy 字段（mode 默认闭环）', async () => {
    mockApis()
    renderGenerationTasks()
    await screen.findByText('task-1')

    fireEvent.click(screen.getByRole('button', { name: '创建任务' }))
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: '资产多选' }))
    fireEvent.click(await screen.findByTitle('星际穿越（a-movie）'))
    fireEvent.click(screen.getByRole('button', { name: /创\s*建$/ }))

    await waitFor(() => expect(postedBody('/api/v1/generation/tasks')).toBeDefined())
    expect(JSON.parse(postedBody('/api/v1/generation/tasks')!)).toEqual({
      asset_ids: ['a-movie'],
      mode: 'closed',
    })
  })

  it('创建失败（资产无识别内容 422）：透出后端 detail 且不关弹窗', async () => {
    mockApis()
    mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === 'POST' && path === '/api/v1/generation/tasks') {
        throw new ApiError(422, '资产 a-movie 无识别内容')
      }
      if (path.startsWith('/api/v1/generation/tasks')) return TASKS
      if (path === '/api/v1/assets' || path.startsWith('/api/v1/assets?')) return ASSETS
      throw new Error(`未预期的请求：${init?.method ?? 'GET'} ${path}`)
    })
    renderGenerationTasks()
    await screen.findByText('task-1')

    fireEvent.click(screen.getByRole('button', { name: '创建任务' }))
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: '资产多选' }))
    fireEvent.click(await screen.findByTitle('星际穿越（a-movie）'))
    fireEvent.click(screen.getByRole('button', { name: /创\s*建$/ }))

    expect(await screen.findByText('资产 a-movie 无识别内容')).toBeInTheDocument()
  })

  it('行展开详情：results 列表（来源/审核状态/置信度/相关度/证据）与版本展示', async () => {
    mockApis()
    renderGenerationTasks()
    await screen.findByText('task-1')

    // 展开第一行（task-1）
    const expandIcon = document.querySelector('.ant-table-row-expand-icon')
    expect(expandIcon).not.toBeNull()
    fireEvent.click(expandIcon as Element)

    // 详情端点按 task_id 请求
    await waitFor(() => {
      expect(
        mockedApiFetch.mock.calls.some(([p]) => p === '/api/v1/generation/tasks/task-1'),
      ).toBe(true)
    })
    // generated 行：标签名/来源/审核状态/置信度/相关度/证据描述与时间戳
    expect(await screen.findByText('主角争执')).toBeInTheDocument()
    expect(screen.getByText('开放生成')).toBeInTheDocument()
    expect(screen.getByText('待审核')).toBeInTheDocument()
    expect(screen.getByText('0.92')).toBeInTheDocument()
    expect(screen.getByText('0.87')).toBeInTheDocument()
    expect(screen.getByText('主角争执推搡')).toBeInTheDocument()
    expect(screen.getByText('01:05 - 01:11')).toBeInTheDocument()
    // catalog 行：目录/已通过，relevance/confidence 双 null 兜底
    expect(screen.getByText('犯罪')).toBeInTheDocument()
    expect(screen.getByText('目录召回')).toBeInTheDocument()
    expect(screen.getByText('已通过')).toBeInTheDocument()
    expect(screen.getAllByText('暂无')).toHaveLength(2)
    // 详情头部：模型/Prompt 版本展示
    expect(screen.getByText('mock-vision-v1')).toBeInTheDocument()
    expect(screen.getByText('p1')).toBeInTheDocument()
  })

  it('列表加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw new Error('后端不可用')
    })
    renderGenerationTasks()

    expect(await screen.findByText('生产任务列表加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
