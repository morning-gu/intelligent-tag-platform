// 导入任务页测试（v2）：mock apiFetch 按路径/方法分发，覆盖：任务历史渲染
// （task_id 列/状态 Tag/时间 null 兜底）、Excel 上传成功（FormData 载荷 +
// 摘要数字 + issues/parse_errors 明细行 + 任务历史刷新）、上传失败（422 透出
// detail 且不刷新）、任务详情 Drawer（摘要展示与 summary=null 降级）、撤销
// 双确认流程（导出并撤销/直接撤销）与撤销 409 透出后端 detail。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Modal } from 'antd'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TagImports from './TagImports'
import { ApiError, apiFetch } from '../api/client'
import type { ImportTaskDetail, ImportTaskSummary } from '../api/imports'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 ImportTaskOut/ImportTaskDetailOut/ImportResponse 契约同形） ----------

const TASKS: ImportTaskSummary[] = [
  {
    task_id: 't-1',
    file_name: '标签库-v1.xlsx',
    status: 'completed',
    created_at: '2026-10-30T08:30:00',
  },
  {
    task_id: 't-2',
    file_name: '坏文件.xlsx',
    status: 'failed',
    created_at: null,
  },
]

const IMPORT_RESULT = {
  task_id: 't-new',
  summary: {
    total: 10,
    inserted: 8,
    skipped: 2,
    issues: [
      { tag_name: '打斗', code: 'duplicate', detail: '与现有标签重复', similar_to: '搏斗' },
    ],
    parse_errors: [
      { row_number: 33, tag_name: null, code: 'missing_tag_name', detail: '标签名缺失' },
    ],
  },
}

const TASK_DETAIL: ImportTaskDetail = {
  task_id: 't-1',
  file_name: '标签库-v1.xlsx',
  status: 'completed',
  created_at: '2026-10-30T08:30:00',
  summary: {
    total: 6,
    inserted: 5,
    skipped: 1,
    issues: [],
    parse_errors: [],
  },
}

// ---------- mock 装配与查询辅助 ----------

interface MockOptions {
  tasks?: ImportTaskSummary[]
  importResult?: typeof IMPORT_RESULT
  importError?: Error
  taskDetail?: ImportTaskDetail
  revokeResult?: ImportTaskSummary
  revokeError?: Error
}

/** 按路径与方法分发的 apiFetch 桩。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST' && path === '/api/v1/tags/import') {
      if (options.importError) throw options.importError
      return options.importResult ?? IMPORT_RESULT
    }
    if (path === '/api/v1/import/tasks') return options.tasks ?? TASKS
    if (method === 'POST' && /^\/api\/v1\/import\/tasks\/[^/]+\/revoke$/.test(path)) {
      if (options.revokeError) throw options.revokeError
      return options.revokeResult ?? { ...TASK_DETAIL, status: 'revoked' }
    }
    if (method === 'GET' && /^\/api\/v1\/import\/tasks\/[^/]+$/.test(path)) {
      return options.taskDetail ?? TASK_DETAIL
    }
    throw new Error(`未预期的请求：${method} ${path}`)
  })
}

/** 上传端点的调用（未请求过返回 undefined）。 */
function importCall(): [string, RequestInit] | undefined {
  return mockedApiFetch.mock.calls.find(
    ([p, init]) => p === '/api/v1/tags/import' && (init as RequestInit | undefined)?.method === 'POST',
  ) as [string, RequestInit] | undefined
}

/** 任务历史列表端点的调用次数（成功刷新后递增）。 */
function listCallCount(): number {
  return mockedApiFetch.mock.calls.filter(([p]) => p === '/api/v1/import/tasks').length
}

/** 撤销端点的调用（未请求过返回 undefined）。 */
function revokeCall(): [string, RequestInit] | undefined {
  return mockedApiFetch.mock.calls.find(
    ([p, init]) =>
      typeof p === 'string' &&
      p.endsWith('/revoke') &&
      (init as RequestInit | undefined)?.method === 'POST',
  ) as [string, RequestInit] | undefined
}

/** 桩掉原生 fetch（downloadImportTask 走 fetch 下载压缩包）与 Blob 下载
 * 所需的 URL.createObjectURL/revokeObjectURL（jsdom 无实现）。 */
function stubBlobDownload() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(['fake-zip']),
    }),
  )
  URL.createObjectURL = vi.fn(() => 'blob:mock-url')
  URL.revokeObjectURL = vi.fn()
}

/** 断言指定标题的 Statistic 显示指定值（Statistic 结构：title + content）。 */
function expectStatistic(title: string, value: string) {
  expect(screen.getByText(title).closest('.ant-statistic')).toHaveTextContent(value)
}

function renderTagImports() {
  return render(<TagImports />)
}

/** 模拟选择 Excel 文件（触发 antd Upload 的 beforeUpload 拦截路径）。 */
function selectExcelFile(name = 'tags.xlsx') {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  expect(input).not.toBeNull()
  const file = new File(['fake-xlsx-bytes'], name)
  fireEvent.change(input, { target: { files: [file] } })
  return file
}

/** 打开 t-1 的任务详情 Drawer：点行内"详情"按钮，返回 Drawer 容器
 * （后续撤销按钮断言/点击收窄到 Drawer，避开确认弹窗内的同名按钮）。 */
async function openTaskDetail(): Promise<HTMLElement> {
  fireEvent.click(
    within(screen.getByText('t-1').closest('tr')!).getByRole('button', { name: '详情' }),
  )
  const title = await screen.findByText('任务详情：t-1')
  return title.closest('.ant-drawer') as HTMLElement
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  // 命令式 Modal.confirm 挂在 document.body 的 portal 不随 RTL cleanup 卸载；
  // 且 destroyAll 的离场动画在 jsdom 不结束（transitionend 永不触发），DOM
  // 节点会渗入下一用例——destroyAll 触发正规卸载后再直接移除残留容器。
  Modal.destroyAll()
  Array.from(document.body.children).forEach((el) => {
    if (el instanceof HTMLElement && el.querySelector('.ant-modal')) el.remove()
  })
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

// ---------- 用例 ----------

describe('TagImports 导入任务页（v2）', () => {
  it('任务历史渲染：task_id 列、状态 Tag、创建时间与 null 兜底、行详情按钮', async () => {
    mockApis()
    renderTagImports()

    const row1 = (await screen.findByText('t-1')).closest('tr')!
    expect(row1).toHaveTextContent('标签库-v1.xlsx')
    expect(row1).toHaveTextContent('已完成')
    expect(row1).toHaveTextContent('2026-10-30 08:30:00')
    const row2 = screen.getByText('t-2').closest('tr')!
    expect(row2).toHaveTextContent('已失败')
    expect(row2).toHaveTextContent('—')
    // 行内有详情按钮（任务详情入口）
    expect(within(row1).getByRole('button', { name: '详情' })).toBeInTheDocument()
  })

  it('Excel 上传成功：POST 携带 FormData（file 字段），摘要数字与 issues/parse_errors 明细渲染，任务历史刷新', async () => {
    mockApis()
    renderTagImports()
    await screen.findByText('t-1')

    const file = selectExcelFile()

    // 上传载荷：multipart FormData（client 不强设 Content-Type，见 client.test）
    await waitFor(() => expect(importCall()).toBeDefined())
    const body = importCall()![1].body as FormData
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBe(file)

    // 摘要统计卡：total/inserted/skipped（口径：total=目录+标签行合计，inserted 仅标签）
    expect(await screen.findByText('导入结果（任务 t-new）')).toBeInTheDocument()
    expectStatistic('传入治理行数', '10')
    expectStatistic('新增入库', '8')
    expectStatistic('跳过', '2')
    // 有明细的面板默认展开：issues/parse_errors 明细行可见
    expect(screen.getByText('治理问题（1）')).toBeInTheDocument()
    const issueRow = screen.getByText('打斗').closest('tr')!
    expect(issueRow).toHaveTextContent('duplicate')
    expect(issueRow).toHaveTextContent('与现有标签重复')
    expect(issueRow).toHaveTextContent('搏斗')
    expect(screen.getByText('解析失败（1）')).toBeInTheDocument()
    const parseRow = screen.getByText('missing_tag_name').closest('tr')!
    expect(parseRow).toHaveTextContent('33')
    expect(parseRow).toHaveTextContent('—') // tag_name null 兜底
    expect(parseRow).toHaveTextContent('标签名缺失')

    // 成功提示与任务历史刷新（第二次列表拉取）
    expect(await screen.findByText('导入完成：新增 8 个标签')).toBeInTheDocument()
    await waitFor(() => expect(listCallCount()).toBe(2))
  })

  it('Excel 上传失败（422 坏文件）：透出后端 detail 且不刷新任务历史', async () => {
    mockApis({ importError: new ApiError(422, '文件无法解析为 xlsx 表格') })
    renderTagImports()
    await screen.findByText('t-1')

    selectExcelFile()

    expect(await screen.findByText('文件无法解析为 xlsx 表格')).toBeInTheDocument()
    expect(listCallCount()).toBe(1)
    expect(screen.queryByText(/导入结果（任务/)).not.toBeInTheDocument()
  })

  it('任务详情 Drawer：点详情拉取任务并复用摘要视图（无明细面板时提示无问题）', async () => {
    mockApis()
    renderTagImports()
    await screen.findByText('t-1')

    await openTaskDetail()

    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(([p]) => p === '/api/v1/import/tasks/t-1')).toBe(
        true,
      )
    })
    // Descriptions 元数据 + 摘要视图（TASK_DETAIL 无 issues/parse_errors）
    const descriptions = document.querySelector('.ant-drawer .ant-descriptions') as HTMLElement
    const fileItem = within(descriptions).getByText('文件名').closest('.ant-descriptions-item')!
    expect(fileItem).toHaveTextContent('标签库-v1.xlsx')
    expectStatistic('传入治理行数', '6')
    expectStatistic('新增入库', '5')
    expectStatistic('跳过', '1')
    expect(screen.getByText('本批无治理问题与解析失败')).toBeInTheDocument()
  })

  it('任务详情 Drawer：summary=null（result_summary 损坏）降级为无结果摘要', async () => {
    mockApis({
      taskDetail: { ...TASK_DETAIL, summary: null },
    })
    renderTagImports()
    await screen.findByText('t-1')

    await openTaskDetail()

    expect(await screen.findByText('无结果摘要')).toBeInTheDocument()
  })

  it('撤销双确认（导出并撤销）：先下载压缩包再撤销，状态翻转且任务历史刷新', async () => {
    stubBlobDownload()
    mockApis()
    renderTagImports()
    await screen.findByText('t-1')

    const drawer = await openTaskDetail()
    fireEvent.click(within(drawer).getByRole('button', { name: /撤\s*销/ }))

    // 第一层确认弹窗：默认主按钮走"导出并撤销"（antd confirm 标题渲染两处）
    await screen.findAllByText('撤销导入任务')
    const modal = screen.getAllByText('撤销导入任务')[0].closest('.ant-modal') as HTMLElement
    fireEvent.click(within(modal).getByRole('button', { name: '导出并撤销' }))

    // 导出（原生 fetch 下载）→ 撤销（apiFetch POST）
    await waitFor(() => {
      expect(vi.mocked(globalThis.fetch).mock.calls.some(([p]) => p === '/api/v1/import/tasks/t-1/export')).toBe(true)
    })
    await waitFor(() => expect(revokeCall()).toBeDefined())
    expect(revokeCall()![0]).toBe('/api/v1/import/tasks/t-1/revoke')
    expect(await screen.findByText('任务已撤销')).toBeInTheDocument()
    // 详情态本地翻转 + 任务历史刷新（第二次列表拉取）
    expect(within(drawer).getByText('已撤销')).toBeInTheDocument()
    await waitFor(() => expect(listCallCount()).toBe(2))
  })

  it('撤销双确认（直接撤销）：不导出文件直接撤销', async () => {
    stubBlobDownload()
    mockApis()
    renderTagImports()
    await screen.findByText('t-1')

    const drawer = await openTaskDetail()
    fireEvent.click(within(drawer).getByRole('button', { name: /撤\s*销/ }))

    // 第一层弹窗点 footer 自定义"直接撤销"→ 第二层确认"直接撤销"
    await screen.findAllByText('撤销导入任务')
    fireEvent.click(screen.getByRole('button', { name: '直接撤销' }))
    await screen.findAllByText('确认直接撤销')
    const modal2 = screen.getAllByText('确认直接撤销')[0].closest('.ant-modal') as HTMLElement
    fireEvent.click(within(modal2).getByRole('button', { name: '直接撤销' }))

    await waitFor(() => expect(revokeCall()).toBeDefined())
    expect(revokeCall()![0]).toBe('/api/v1/import/tasks/t-1/revoke')
    expect(await screen.findByText('任务已撤销')).toBeInTheDocument()
    // 未走导出下载
    expect(vi.mocked(globalThis.fetch).mock.calls).toHaveLength(0)
  })

  it('撤销 409（批内标签仍被引用）：透出后端 detail，状态保持已完成且不刷新', async () => {
    stubBlobDownload()
    mockApis({
      revokeError: new ApiError(409, '导入任务中的标签仍被资产挂载或候选引用，无法撤销'),
    })
    renderTagImports()
    await screen.findByText('t-1')

    const drawer = await openTaskDetail()
    fireEvent.click(within(drawer).getByRole('button', { name: /撤\s*销/ }))

    await screen.findAllByText('撤销导入任务')
    const modal = screen.getAllByText('撤销导入任务')[0].closest('.ant-modal') as HTMLElement
    fireEvent.click(within(modal).getByRole('button', { name: '导出并撤销' }))

    expect(await screen.findByText('导入任务中的标签仍被资产挂载或候选引用，无法撤销')).toBeInTheDocument()
    // 状态未翻转、历史未刷新（撤销失败不改任何状态）
    expect(within(drawer).getByText('已完成')).toBeInTheDocument()
    expect(listCallCount()).toBe(1)
  })

  it('非 completed 任务不可撤销（撤销按钮禁用）', async () => {
    mockApis({
      tasks: [{ ...TASKS[0], status: 'revoked' }],
      taskDetail: { ...TASK_DETAIL, status: 'revoked' },
    })
    renderTagImports()
    await screen.findByText('t-1')
    expect(screen.getByText('t-1').closest('tr')).toHaveTextContent('已撤销')

    const drawer = await openTaskDetail()
    // 非 completed（已撤销）任务不可撤销：撤销按钮禁用
    expect(within(drawer).getByRole('button', { name: /撤\s*销/ })).toBeDisabled()
  })

  it('任务历史加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw new Error('后端不可用')
    })
    renderTagImports()

    expect(await screen.findByText('任务历史加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
