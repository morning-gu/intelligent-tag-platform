// 模型配置中心页测试（Task 14 只读化后）：mock apiFetch 按路径分发，覆盖：
// Prompt 只读契约渲染（模板正文逐字展示、必需占位符/可用字段动态来自
// GET /prompts——P0 #4 根治，页面零手写文案）、只读语义（无新建版本/
// 激活/版本列/编辑入口）、config 卡八字段渲染（布尔是/否 Tag）与
// Prompt/config 各自加载失败提示。
// 写端点契约锁定（POST → 405 / activate → 404 / 无写函数导出）在
// api/prompts.test.ts，本文件不重复。
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ModelConfig from './ModelConfig'
import { apiFetch } from '../../api/client'
import type { PromptContractItem } from '../../api/prompts'
import type { RuntimeConfig } from '../../api/config'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

// ---------- 夹具（与后端 PromptContractOut / ConfigOut 契约同形，无版本字段） ----------

const PROMPTS: PromptContractItem[] = [
  {
    mode: 'closed',
    template_text: '闭卷：{content}\n候选清单：{candidates}\n只输出 JSON。',
    allowed_fields: ['title', 'genres'],
    required_placeholders: ['{content}', '{candidates}'],
  },
  {
    mode: 'open',
    template_text: '开卷：{content}\n自由生成标签。',
    allowed_fields: ['title'],
    required_placeholders: ['{content}'],
  },
]

const CONFIG: RuntimeConfig = {
  llm_model: 'qwen-plus',
  llm_temperature: 0.2,
  recall_top_k_per_type: 20,
  open_match_threshold: 0.55,
  generation_workers: 4,
  use_mock_llm: true,
  use_mock_embedder: false,
  database_path: '/data/tagplatform/tagplatform.db',
}

// ---------- mock 装配 ----------

interface MockOptions {
  prompts?: PromptContractItem[]
  promptsError?: Error
  config?: RuntimeConfig
  configError?: Error
}

/** 按路径分发的 apiFetch 桩（页面只有 GET /prompts 与 GET /config 两个读端点）。 */
function mockApis(options: MockOptions = {}) {
  mockedApiFetch.mockImplementation(async (path: string) => {
    if (path === '/api/v1/prompts') {
      if (options.promptsError) throw options.promptsError
      return options.prompts ?? PROMPTS
    }
    if (path === '/api/v1/config') {
      if (options.configError) throw options.configError
      return options.config ?? CONFIG
    }
    throw new Error(`未预期的请求：${path}`)
  })
}

function promptsCallCount(): number {
  return mockedApiFetch.mock.calls.filter(([p]) => p === '/api/v1/prompts').length
}

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('ModelConfig 模型配置中心页（只读）', () => {
  it('Prompt 只读契约渲染：逐 mode 内层卡，模板正文逐字展示（pre 保留换行）', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(
      await screen.findByText('Prompt 模板 · 封闭生成（closed）'),
    ).toBeInTheDocument()
    expect(screen.getByText('Prompt 模板 · 开放生成（open）')).toBeInTheDocument()
    // 模板正文与 API 契约逐字一致（toHaveTextContent 对实际文本做空白
    // 归一化，换行折叠为空格——期望串按归一化口径书写）
    const closedPre = screen.getByText(/闭卷：\{content\}/)
    expect(closedPre).toHaveTextContent('闭卷：{content} 候选清单：{candidates} 只输出 JSON。')
    const openPre = screen.getByText(/开卷：\{content\}/)
    expect(openPre).toHaveTextContent('开卷：{content} 自由生成标签。')
    // 页头只读标识
    expect(screen.getByText('只读 · 文案随代码版本')).toBeInTheDocument()
  })

  it('必需占位符说明动态来自 GET /prompts 契约（不手写文案）', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(await screen.findByText('必需占位符：{content}、{candidates}')).toBeInTheDocument()
    expect(screen.getByText('必需占位符：{content}')).toBeInTheDocument()
  })

  it('可用字段 Tag 动态来自契约（closed 两枚、open 一枚）', async () => {
    mockApis()
    render(<ModelConfig />)

    // "可用字段："标签逐 mode 卡各渲染一次
    expect(await screen.findAllByText('可用字段：')).toHaveLength(2)
    expect(screen.getByText('genres')).toBeInTheDocument()
    // title 在 closed/open 两卡各渲染一次
    expect(screen.getAllByText('title')).toHaveLength(2)
  })

  it('GET /api/v1/prompts 无 mode 过滤一次拉全（不再逐 Tab 拉版本清单）', async () => {
    mockApis()
    render(<ModelConfig />)
    await screen.findByText(/闭卷：\{content\}/)

    expect(promptsCallCount()).toBe(1)
    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/prompts')
    // 也不再请求 mode 过滤的版本列表（旧页签交互退役）
    expect(mockedApiFetch).not.toHaveBeenCalledWith('/api/v1/prompts?mode=closed')
    expect(mockedApiFetch).not.toHaveBeenCalledWith('/api/v1/prompts?mode=open')
  })

  it('只读语义：无新建版本/激活按钮、无版本列与编辑入口', async () => {
    mockApis()
    render(<ModelConfig />)
    await screen.findByText(/闭卷：\{content\}/)

    expect(screen.queryByRole('button', { name: '新建版本' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /激\s*活/ })).not.toBeInTheDocument()
    // 旧版本表的痕迹全部消失：激活 Tag、版本列、新建弹窗的模板正文输入
    expect(screen.queryByText(/激活中|已停用/)).not.toBeInTheDocument()
    expect(screen.queryByText('模板正文')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    // 旧弹窗不存在
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('当前运行配置卡：八字段渲染，布尔以是/否 Tag 展示', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(await screen.findByText('qwen-plus')).toBeInTheDocument()
    expect(screen.getByText('LLM 模型')).toBeInTheDocument()
    expect(screen.getByText('LLM 温度')).toBeInTheDocument()
    expect(screen.getByText('每类型召回 Top-K')).toBeInTheDocument()
    expect(screen.getByText('开放匹配阈值')).toBeInTheDocument()
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

  it('Prompt 契约加载失败：渲染错误提示（透出错误文案），不影响 config 卡', async () => {
    mockApis({ promptsError: new Error('后端不可用') })
    render(<ModelConfig />)

    expect(await screen.findByText('Prompt 模板契约加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
    // config 卡独立加载不受影响
    expect(await screen.findByText('qwen-plus')).toBeInTheDocument()
  })

  it('空契约清单（后端未返回任何 mode）：渲染暂无文案', async () => {
    mockApis({ prompts: [] })
    render(<ModelConfig />)

    expect(await screen.findByText('暂无模板契约')).toBeInTheDocument()
  })

  it('运行配置加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/v1/config') throw new Error('后端不可用')
      if (path === '/api/v1/prompts') return PROMPTS
      throw new Error(`未预期的请求：${path}`)
    })
    render(<ModelConfig />)

    expect(await screen.findByText('运行配置加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
