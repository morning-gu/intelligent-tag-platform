// 标签导入页测试（Task 13）：mock apiFetch 按路径/方法分发，覆盖：批次历史
// 渲染（状态 Tag/时间 null 兜底）、Excel 上传成功（FormData 载荷 + 摘要数字
// + issues/parse_errors 明细行 + 批次历史刷新）、上传失败（422 透出 detail
// 且不刷新）、批次详情 Drawer（摘要展示与 summary=null 降级）。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TagImports from './TagImports'
import { ApiError, apiFetch } from '../api/client'
import type { ImportBatchDetail, ImportBatchSummary } from '../api/imports'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 BatchOut/BatchDetail/ImportResponse 契约同形） ----------

const BATCHES: ImportBatchSummary[] = [
  {
    batch_id: 'b-1',
    file_name: '标签库-v1.xlsx',
    status: 'completed',
    created_at: '2026-10-30T08:30:00',
  },
  {
    batch_id: 'b-2',
    file_name: '坏文件.xlsx',
    status: 'failed',
    created_at: null,
  },
]

const IMPORT_RESULT = {
  batch_id: 'b-new',
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

const BATCH_DETAIL: ImportBatchDetail = {
  batch_id: 'b-1',
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
  batches?: ImportBatchSummary[]
  importResult?: typeof IMPORT_RESULT
  importError?: Error
  batchDetail?: ImportBatchDetail
}

/** 按路径与方法分发的 apiFetch 桩。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST' && path === '/api/v1/tags/import') {
      if (options.importError) throw options.importError
      return options.importResult ?? IMPORT_RESULT
    }
    if (path === '/api/v1/import/batches') return options.batches ?? BATCHES
    if (method === 'GET' && /^\/api\/v1\/import\/batches\/[^/]+$/.test(path)) {
      return options.batchDetail ?? BATCH_DETAIL
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

/** 批次历史列表端点的调用次数（成功刷新后递增）。 */
function listCallCount(): number {
  return mockedApiFetch.mock.calls.filter(([p]) => p === '/api/v1/import/batches').length
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

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('TagImports 标签导入页', () => {
  it('批次历史渲染：状态 Tag、创建时间与 null 兜底、行详情按钮', async () => {
    mockApis()
    renderTagImports()

    const row1 = (await screen.findByText('b-1')).closest('tr')!
    expect(row1).toHaveTextContent('标签库-v1.xlsx')
    expect(row1).toHaveTextContent('已完成')
    expect(row1).toHaveTextContent('2026-10-30 08:30:00')
    const row2 = screen.getByText('b-2').closest('tr')!
    expect(row2).toHaveTextContent('已失败')
    expect(row2).toHaveTextContent('—')
    // 行内有详情按钮（批次详情入口）
    expect(within(row1).getByRole('button', { name: '详情' })).toBeInTheDocument()
  })

  it('Excel 上传成功：POST 携带 FormData（file 字段），摘要数字与 issues/parse_errors 明细渲染，批次历史刷新', async () => {
    mockApis()
    renderTagImports()
    await screen.findByText('b-1')

    const file = selectExcelFile()

    // 上传载荷：multipart FormData（client 不强设 Content-Type，见 client.test）
    await waitFor(() => expect(importCall()).toBeDefined())
    const body = importCall()![1].body as FormData
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBe(file)

    // 摘要统计卡：total/inserted/skipped
    expect(await screen.findByText('导入结果（批次 b-new）')).toBeInTheDocument()
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

    // 成功提示与批次历史刷新（第二次列表拉取）
    expect(await screen.findByText('导入完成：新增 8 个标签')).toBeInTheDocument()
    await waitFor(() => expect(listCallCount()).toBe(2))
  })

  it('Excel 上传失败（422 坏文件）：透出后端 detail 且不刷新批次历史', async () => {
    mockApis({ importError: new ApiError(422, '文件无法解析为 xlsx 表格') })
    renderTagImports()
    await screen.findByText('b-1')

    selectExcelFile()

    expect(await screen.findByText('文件无法解析为 xlsx 表格')).toBeInTheDocument()
    expect(listCallCount()).toBe(1)
    expect(screen.queryByText(/导入结果（批次/)).not.toBeInTheDocument()
  })

  it('批次详情 Drawer：点详情拉取批次并复用摘要视图（无明细面板时提示无问题）', async () => {
    mockApis()
    renderTagImports()
    await screen.findByText('b-1')

    fireEvent.click(
      within(screen.getByText('b-1').closest('tr')!).getByRole('button', { name: '详情' }),
    )

    await waitFor(() => {
      expect(mockedApiFetch.mock.calls.some(([p]) => p === '/api/v1/import/batches/b-1')).toBe(
        true,
      )
    })
    expect(await screen.findByText('批次详情：b-1')).toBeInTheDocument()
    // Descriptions 元数据（文件名/列头在批次历史表格同见，以 Descriptions 收窄）
    // + 摘要视图（BATCH_DETAIL 无 issues/parse_errors）
    const descriptions = document.querySelector('.ant-descriptions') as HTMLElement
    const fileItem = within(descriptions).getByText('文件名').closest('.ant-descriptions-item')!
    expect(fileItem).toHaveTextContent('标签库-v1.xlsx')
    expectStatistic('传入治理行数', '6')
    expectStatistic('新增入库', '5')
    expectStatistic('跳过', '1')
    expect(screen.getByText('本批无治理问题与解析失败')).toBeInTheDocument()
  })

  it('批次详情 Drawer：summary=null（result_summary 损坏）降级为无结果摘要', async () => {
    mockApis({
      batchDetail: { ...BATCH_DETAIL, summary: null },
    })
    renderTagImports()
    await screen.findByText('b-1')

    fireEvent.click(
      within(screen.getByText('b-1').closest('tr')!).getByRole('button', { name: '详情' }),
    )

    expect(await screen.findByText('无结果摘要')).toBeInTheDocument()
  })

  it('批次历史加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async () => {
      throw new Error('后端不可用')
    })
    renderTagImports()

    expect(await screen.findByText('批次历史加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })

  it('已撤销批次显示已撤销状态', async () => {
    mockApis({
      batches: [{ ...BATCHES[0], status: 'revoked' }],
      batchDetail: { ...BATCH_DETAIL, status: 'revoked' },
    })
    renderTagImports()
    await screen.findByText('b-1')
    expect(screen.getByText('b-1').closest('tr')).toHaveTextContent('已撤销')
  })
})
