// 模型配置中心页测试：mock apiFetch 按路径/方法分发，覆盖：
// 版本列表渲染（版本号/激活 Tag/时间/激活按钮仅非激活行）、Tab 切换拉对应
// mode 列表、激活操作 POST 与刷新（404 不刷新）、新建 Modal 载荷（{mode,
// template_text}，随 Tab 变化）与 422 提示（弹窗保持打开且不刷新）、
// config 卡九字段渲染（布尔是/否 Tag）与加载失败提示。
// 注意：antd 对两个汉字的按钮自动插入空隙（"激活"渲染为"激 活"），按钮
// 可访问名查询一律用宽容正则（与 Users 测试的 /创\s*建$/ 同理）。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ModelConfig from './ModelConfig'
import { ApiError, apiFetch } from '../api/client'
import type { PromptTemplateSummary } from '../api/prompts'
import type { RuntimeConfig } from '../api/config'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 PromptOut / ConfigOut 契约同形，列表直接带正文） ----------

const CLOSED_PROMPTS: PromptTemplateSummary[] = [
  {
    id: 1,
    mode: 'closed',
    version: 1,
    template_text: '闭卷 v1：{content}\n候选：{candidates}',
    is_active: false,
    created_at: '2026-10-30T08:00:00',
  },
  {
    id: 2,
    mode: 'closed',
    version: 2,
    template_text: '闭卷 v2：{content}\n候选清单：{candidates}\n只输出 JSON。',
    is_active: true,
    created_at: '2026-10-31T09:30:00',
  },
]

// open 夹具用版本 5，避免与 closed 的 v1 在"切换后旧行消失"断言中混淆
const OPEN_PROMPTS: PromptTemplateSummary[] = [
  {
    id: 3,
    mode: 'open',
    version: 5,
    template_text: '开卷 v5：{content}\n自由生成标签。',
    is_active: true,
    created_at: null,
  },
]

const CONFIG: RuntimeConfig = {
  llm_model: 'qwen-plus',
  llm_temperature: 0.2,
  recall_top_k_per_type: 20,
  open_match_threshold: 0.55,
  semantic_similar_threshold: 0.82,
  generation_workers: 4,
  use_mock_llm: true,
  use_mock_embedder: false,
  database_path: '/data/tagplatform/tagplatform.db',
}

// ---------- mock 装配与查询辅助 ----------

interface MockOptions {
  closed?: PromptTemplateSummary[]
  open?: PromptTemplateSummary[]
  config?: RuntimeConfig
  configError?: Error
  createError?: Error
  createResult?: PromptTemplateSummary
  activateError?: Error
}

/** 按路径与方法分发的 apiFetch 桩。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST' && path === '/api/v1/prompts') {
      if (options.createError) throw options.createError
      return (
        options.createResult ?? {
          id: 3,
          mode: 'closed',
          version: 3,
          template_text: '闭卷 v3（新建成功返回）',
          is_active: true,
          created_at: '2026-11-01T00:00:00',
        }
      )
    }
    if (method === 'POST' && /^\/api\/v1\/prompts\/\d+\/activate$/.test(path)) {
      if (options.activateError) throw options.activateError
      return { mode: 'closed', version: 1, is_active: true }
    }
    if (path === '/api/v1/prompts?mode=closed') return options.closed ?? CLOSED_PROMPTS
    if (path === '/api/v1/prompts?mode=open') return options.open ?? OPEN_PROMPTS
    if (path === '/api/v1/config') {
      if (options.configError) throw options.configError
      return options.config ?? CONFIG
    }
    throw new Error(`未预期的请求：${method} ${path}`)
  })
}

/** 版本列表端点（指定 mode）的调用次数（成功刷新后递增）。 */
function listCallCount(mode: 'closed' | 'open'): number {
  return mockedApiFetch.mock.calls.filter(
    ([p, init]) =>
      p === `/api/v1/prompts?mode=${mode}` && (init?.method ?? 'GET') === 'GET',
  ).length
}

/** 新建版本端点的请求体（JSON 字符串；未请求过返回 undefined）。 */
function postedCreateBody(): string | undefined {
  const call = mockedApiFetch.mock.calls.find(
    ([p, init]) =>
      p === '/api/v1/prompts' && (init as RequestInit | undefined)?.method === 'POST',
  )
  return call === undefined ? undefined : String((call[1] as RequestInit).body)
}

/** 激活端点被请求的路径（未请求过返回 undefined）。 */
function postedActivatePath(): string | undefined {
  const call = mockedApiFetch.mock.calls.find(
    ([p, init]) =>
      typeof p === 'string' &&
      /^\/api\/v1\/prompts\/\d+\/activate$/.test(p) &&
      (init as RequestInit | undefined)?.method === 'POST',
  )
  return call === undefined ? undefined : String(call[0])
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('ModelConfig 模型配置中心页', () => {
  it('版本列表渲染：版本号/激活 Tag/创建时间，激活按钮仅出现在非激活行', async () => {
    mockApis()
    render(<ModelConfig />)

    // 初始 Tab 为 closed：v2 激活中（无激活按钮），v1 已停用（有激活按钮）
    const activeRow = (await screen.findByText('v2')).closest('tr')!
    expect(activeRow).toHaveTextContent('激活中')
    expect(activeRow).toHaveTextContent('2026-10-31 09:30:00')
    expect(within(activeRow).queryByRole('button', { name: /激\s*活/ })).not.toBeInTheDocument()
    const oldRow = screen.getByText('v1').closest('tr')!
    expect(oldRow).toHaveTextContent('已停用')
    expect(oldRow).toHaveTextContent('2026-10-30 08:00:00')
    expect(within(oldRow).getByRole('button', { name: /激\s*活/ })).toBeInTheDocument()
  })

  it('Tab 切换：按 mode 拉取对应列表，旧 Tab 行消失', async () => {
    mockApis()
    render(<ModelConfig />)
    await screen.findByText('v2')

    fireEvent.click(screen.getByRole('tab', { name: /开放生成/ }))

    expect(await screen.findByText('v5')).toBeInTheDocument()
    expect(screen.queryByText('v2')).not.toBeInTheDocument()
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/prompts?mode=open')
    expect(listCallCount('open')).toBe(1)
  })

  it('激活操作：POST activate 端点后成功提示并刷新当前 Tab', async () => {
    mockApis()
    render(<ModelConfig />)
    const oldRow = (await screen.findByText('v1')).closest('tr')!

    fireEvent.click(within(oldRow).getByRole('button', { name: /激\s*活/ }))

    await waitFor(() => expect(postedActivatePath()).toBe('/api/v1/prompts/1/activate'))
    expect(await screen.findByText('已激活 v1（closed）')).toBeInTheDocument()
    await waitFor(() => expect(listCallCount('closed')).toBe(2))
  })

  it('激活失败（404 未知 id）：透出后端 detail，不刷新列表', async () => {
    mockApis({ activateError: new ApiError(404, '未知 Prompt 模板 id：1') })
    render(<ModelConfig />)
    const row = (await screen.findByText('v1')).closest('tr')!

    fireEvent.click(within(row).getByRole('button', { name: /激\s*活/ }))

    expect(await screen.findByText('未知 Prompt 模板 id：1')).toBeInTheDocument()
    expect(listCallCount('closed')).toBe(1)
  })

  it('新建版本：提交载荷 {mode,template_text}，成功提示并刷新当前 Tab', async () => {
    mockApis({
      createResult: {
        id: 3,
        mode: 'closed',
        version: 3,
        template_text: '生成标签：{content}\n候选清单：{candidates}',
        is_active: true,
        created_at: '2026-11-01T00:00:00',
      },
    })
    render(<ModelConfig />)
    await screen.findByText('v2')

    fireEvent.click(screen.getByRole('button', { name: '新建版本' }))
    fireEvent.change(screen.getByLabelText('模板正文'), {
      target: { value: '生成标签：{content}\n候选清单：{candidates}' },
    })
    fireEvent.click(screen.getByRole('button', { name: /提\s*交/ }))

    await waitFor(() => expect(postedCreateBody()).toBeDefined())
    expect(JSON.parse(postedCreateBody()!)).toEqual({
      mode: 'closed',
      template_text: '生成标签：{content}\n候选清单：{candidates}',
    })
    expect(await screen.findByText('已新建版本 v3（closed）')).toBeInTheDocument()
    await waitFor(() => expect(listCallCount('closed')).toBe(2))
  })

  it('新建弹窗预填当前 Tab 激活版本文本（基于现版微调，非从零转写）', async () => {
    mockApis()
    render(<ModelConfig />)
    await screen.findByText('v2')

    fireEvent.click(screen.getByRole('button', { name: '新建版本' }))

    const textarea = screen.getByLabelText('模板正文') as HTMLTextAreaElement
    expect(textarea.value).toBe(CLOSED_PROMPTS[1].template_text)
  })

  it('新建版本：在 open Tab 提交时载荷 mode=open', async () => {
    mockApis({
      createResult: {
        id: 4,
        mode: 'open',
        version: 6,
        template_text: '开放生成标签：{content}',
        is_active: true,
        created_at: '2026-11-01T00:00:00',
      },
    })
    render(<ModelConfig />)
    await screen.findByText('v2')
    fireEvent.click(screen.getByRole('tab', { name: /开放生成/ }))
    await screen.findByText('v5')

    fireEvent.click(screen.getByRole('button', { name: '新建版本' }))
    // 预填的是切换后 Tab（open）的激活版本文本
    expect((screen.getByLabelText('模板正文') as HTMLTextAreaElement).value).toBe(
      OPEN_PROMPTS[0].template_text,
    )
    fireEvent.change(screen.getByLabelText('模板正文'), {
      target: { value: '开放生成标签：{content}' },
    })
    fireEvent.click(screen.getByRole('button', { name: /提\s*交/ }))

    await waitFor(() => expect(postedCreateBody()).toBeDefined())
    expect(JSON.parse(postedCreateBody()!)).toEqual({
      mode: 'open',
      template_text: '开放生成标签：{content}',
    })
  })

  it('新建失败（422 缺占位符）：透出后端 detail，弹窗保持打开且不刷新', async () => {
    mockApis({
      createError: new ApiError(422, '模板缺少必需占位符 {candidates}（mode=closed）'),
    })
    render(<ModelConfig />)
    await screen.findByText('v2')

    fireEvent.click(screen.getByRole('button', { name: '新建版本' }))
    fireEvent.change(screen.getByLabelText('模板正文'), {
      target: { value: '没有占位符的文本' },
    })
    fireEvent.click(screen.getByRole('button', { name: /提\s*交/ }))

    expect(
      await screen.findByText('模板缺少必需占位符 {candidates}（mode=closed）'),
    ).toBeInTheDocument()
    // 弹窗保持打开（可修正正文后重试），列表未刷新
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(listCallCount('closed')).toBe(1)
  })

  it('当前运行配置卡：九字段渲染，布尔以是/否 Tag 展示', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(await screen.findByText('qwen-plus')).toBeInTheDocument()
    expect(screen.getByText('LLM 模型')).toBeInTheDocument()
    expect(screen.getByText('LLM 温度')).toBeInTheDocument()
    expect(screen.getByText('每类型召回 Top-K')).toBeInTheDocument()
    expect(screen.getByText('开放匹配阈值')).toBeInTheDocument()
    expect(screen.getByText('语义相似阈值')).toBeInTheDocument()
    expect(screen.getByText('生成并发数')).toBeInTheDocument()
    expect(screen.getByText('Mock LLM')).toBeInTheDocument()
    expect(screen.getByText('Mock Embedder')).toBeInTheDocument()
    expect(screen.getByText('数据库路径')).toBeInTheDocument()
    expect(screen.getByText('/data/tagplatform/tagplatform.db')).toBeInTheDocument()
    // 布尔字段：use_mock_llm=true → 是；use_mock_embedder=false → 否
    expect(screen.getByText('是')).toBeInTheDocument()
    expect(screen.getByText('否')).toBeInTheDocument()
    // 只读语义说明文案
    expect(screen.getByText(/修改运行配置：编辑环境变量后重启服务/)).toBeInTheDocument()
  })

  it('运行配置加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/v1/config') throw new Error('后端不可用')
      if (path === '/api/v1/prompts?mode=closed') return CLOSED_PROMPTS
      throw new Error(`未预期的请求：${path}`)
    })
    render(<ModelConfig />)

    expect(await screen.findByText('运行配置加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
