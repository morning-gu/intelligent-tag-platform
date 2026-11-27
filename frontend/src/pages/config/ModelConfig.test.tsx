// 模型配置中心页测试（Task 14 只读化后）：mock apiFetch 按路径分发，覆盖：
// Prompt 只读契约渲染（模板正文逐字展示、必需占位符/可用字段动态来自
// GET /prompts——P0 #4 根治，页面零手写文案）、closed 卡组装说明与置信度
// prompt 内层卡（公式序三分区 Ccons → Cver → Cmod + 卡头携带资产总标注，
// 文案本体按常量名动态来自 GET /prompts/registry，指明原始资产插入位置；
// registry 缺条目时静默跳过）、只读语义（无新建版本/激活/版本列/编辑
// 入口）、config 卡十三字段渲染（布尔是/否 Tag）与 Prompt/config 各自
// 加载失败提示。
// 写端点契约锁定（POST → 405 / activate → 404 / 无写函数导出）在
// api/prompts.test.ts，本文件不重复。
import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ModelConfig from './ModelConfig'
import { apiFetch } from '../../api/client'
import type { PromptContractItem, PromptRegistryGroup } from '../../api/prompts'
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
]

const CONFIG: RuntimeConfig = {
  llm_model: 'qwen-plus',
  llm_temperature: 0.2,
  mtg_judge_model: 'judge-x',
  mtg_judge_enabled: true,
  embedding_backend: 'local',
  embedding_model: 'BAAI/bge-m3',
  open_eval_review_model: 'review-x',
  recall_top_k_per_type: 20,
  open_library_suspect_cosine: 0.75,
  open_library_name_overlap: 0.4,
  generation_workers: 4,
  use_mock_llm: true,
  use_mock_embedder: false,
  database_path: '/data/tagplatform/tagplatform.db',
}

/** closed 组装说明条目（夹具文本为精简版，保留后端常量的特征文案——
 * 「原始资产插入位置」标题与 {content} 占位符，页面按常量名取条目渲染）。 */
const CLOSED_ASSEMBLY_NOTE_TEXT =
  '组装说明（closed 生成 · 原始资产插入位置）：\n\n' +
  '1. 模板渲染：{content} 即原始资产文本的插入位置。\n' +
  '2. 候选标签注入：以「## 候选标签」节插入在渲染后正文之前。\n' +
  '3. 关键帧：帧目录可用时随请求发送；无帧退化为纯文本请求。'

const CLOSED_CONFIDENCE_CCONS_TEXT =
  '组装说明（置信度 Ccons 评分一致性重评）：\n\n' +
  '1. 材料：复用生成时的 spec.prompt_text（原始资产已在 {content} 就位）。\n' +
  '2. 调用：同一 run_id 重复调用 2 次，取三次 Sevi 的方差 As，Ccons = exp(−As/b)。'

const CLOSED_CONFIDENCE_CVER_TEXT =
  '组装说明（置信度 Cver 证据可核验性 · 原始资产插入位置）：\n\n' +
  '1. 材料：spec.prompt_text + CLOSED_VERIFICATION_PROMPT（五级可核验表）。\n' +
  '2. 调用：带上与生成时同一组关键帧图片；Cver = (S−1)/4。'

const CLOSED_CONFIDENCE_CMOD_TEXT =
  '组装说明（置信度 Cmod 跨模态一致性）：\n\n' +
  '1. 纯本地计算，不调用 LLM、不注入资产文本；缺模态记 0.5 占位。\n' +
  '2. Cmod = j·Cs + (1−j)·(1−Cc)，j 默认 0.5。'

/** 置信度卡头携带资产总标注（精简版，保留特征文案：spec.prompt_text、
 * {content} 插入位置、关键帧、Cmod 不调用 LLM）。 */
const CLOSED_CONFIDENCE_ASSETS_TEXT =
  '携带资产：Ccons 重评与 Cver 核验的 LLM 消息均以 spec.prompt_text ' +
  '起头——原始资产文本已在模板 {content} 位置渲染就位，资产帧可用时随' +
  '消息附关键帧图片；Cmod 为纯本地算术，不调用 LLM、不携带资产。'

/** 流水线文案 registry 夹具（与后端 RegistryGroupOut 契约同形）。 */
const REGISTRY: PromptRegistryGroup[] = [
  {
    module: 'closed',
    title: '闭环生成',
    items: [
      { name: 'CLOSED_TEMPLATE_TEXT', text: '闭卷生成模板正文' },
      {
        name: 'CLOSED_VERIFICATION_PROMPT',
        text: 'CLOSED_VERIFICATION_PROMPT 正文（五级可核验表）',
      },
      {
        name: 'CLOSED_ASSEMBLY_NOTE',
        text: CLOSED_ASSEMBLY_NOTE_TEXT,
      },
      {
        name: 'CLOSED_CONFIDENCE_CCONS_NOTE',
        text: CLOSED_CONFIDENCE_CCONS_TEXT,
      },
      {
        name: 'CLOSED_CONFIDENCE_CVER_NOTE',
        text: CLOSED_CONFIDENCE_CVER_TEXT,
      },
      {
        name: 'CLOSED_CONFIDENCE_CMOD_NOTE',
        text: CLOSED_CONFIDENCE_CMOD_TEXT,
      },
      {
        name: 'CLOSED_CONFIDENCE_ASSETS_NOTE',
        text: CLOSED_CONFIDENCE_ASSETS_TEXT,
      },
    ],
  },
  {
    module: 'open_review',
    title: '开放评审',
    items: [{ name: 'REVIEW_PROMPT_VERSION', text: 'open-review-v8' }],
  },
]

// ---------- mock 装配 ----------

interface MockOptions {
  prompts?: PromptContractItem[]
  promptsError?: Error
  config?: RuntimeConfig
  configError?: Error
  registry?: PromptRegistryGroup[]
  registryError?: Error
}

/** 按路径分发的 apiFetch 桩（页面读端点：GET /prompts、GET /config、
 * GET /prompts/registry 三者）。 */
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
    if (path === '/api/v1/prompts/registry') {
      if (options.registryError) throw options.registryError
      return options.registry ?? REGISTRY
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
    // 模板正文与 API 契约逐字一致（toHaveTextContent 对实际文本做空白
    // 归一化，换行折叠为空格——期望串按归一化口径书写）
    const closedPre = screen.getByText(/闭卷：\{content\}/)
    expect(closedPre).toHaveTextContent('闭卷：{content} 候选清单：{candidates} 只输出 JSON。')
    // 页头只读标识（Prompt 模板卡与流水线文案卡各一枚）
    expect(screen.getAllByText('只读 · 文案随代码版本')).toHaveLength(2)
  })

  it('必需占位符说明动态来自 GET /prompts 契约（不手写文案）', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(await screen.findByText('必需占位符：{content}、{candidates}')).toBeInTheDocument()
  })

  it('可用字段 Tag 动态来自契约（closed 两枚）', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(await screen.findByText('可用字段：')).toBeInTheDocument()
    expect(screen.getByText('genres')).toBeInTheDocument()
    expect(screen.getByText('title')).toBeInTheDocument()
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

  it('当前运行配置卡：十四字段渲染，布尔以是/否 Tag 展示', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(await screen.findByText('qwen-plus')).toBeInTheDocument()
    expect(screen.getByText('LLM 模型')).toBeInTheDocument()
    expect(screen.getByText('LLM 温度')).toBeInTheDocument()
    // 模型视图增补字段：judge / 嵌入 / 开放评测评审
    expect(screen.getByText('Judge 模型')).toBeInTheDocument()
    expect(screen.getByText('judge-x')).toBeInTheDocument()
    expect(screen.getByText('Judge 启用')).toBeInTheDocument()
    expect(screen.getByText('嵌入后端')).toBeInTheDocument()
    expect(screen.getByText('local')).toBeInTheDocument()
    expect(screen.getByText('嵌入模型')).toBeInTheDocument()
    expect(screen.getByText('BAAI/bge-m3')).toBeInTheDocument()
    expect(screen.getByText('开放评测评审模型')).toBeInTheDocument()
    expect(screen.getByText('review-x')).toBeInTheDocument()
    expect(screen.getByText('全局召回 Top-K')).toBeInTheDocument()
    expect(screen.getByText('开放疑似余弦阈值')).toBeInTheDocument()
    expect(screen.getByText('0.75')).toBeInTheDocument()
    expect(screen.getByText('开放字面重合阈值')).toBeInTheDocument()
    expect(screen.getByText('0.4')).toBeInTheDocument()
    expect(screen.getByText('生成并发数')).toBeInTheDocument()
    expect(screen.getByText('Mock LLM')).toBeInTheDocument()
    expect(screen.getByText('Mock Embedder')).toBeInTheDocument()
    expect(screen.getByText('数据库路径')).toBeInTheDocument()
    expect(screen.getByText('/data/tagplatform/tagplatform.db')).toBeInTheDocument()
    // 布尔字段：use_mock_llm=true、mtg_judge_enabled=true → 两枚「是」；
    // use_mock_embedder=false → 一枚「否」
    expect(screen.getAllByText('是')).toHaveLength(2)
    expect(screen.getAllByText('否')).toHaveLength(1)
    // 只读语义说明文案
    expect(screen.getByText(/修改运行配置：编辑环境变量后重启服务/)).toBeInTheDocument()
  })

  it('开放评测评审模型为空串：渲染「同生成模型（空值回落）」', async () => {
    mockApis({ config: { ...CONFIG, open_eval_review_model: '' } })
    render(<ModelConfig />)

    expect(await screen.findByText('同生成模型（空值回落）')).toBeInTheDocument()
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
      if (path === '/api/v1/prompts/registry') return REGISTRY
      throw new Error(`未预期的请求：${path}`)
    })
    render(<ModelConfig />)

    expect(await screen.findByText('运行配置加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })

  it('流水线文案区：按分组折叠渲染常量名与文案（默认展开）', async () => {
    mockApis()
    render(<ModelConfig />)

    expect(await screen.findByText('流水线文案')).toBeInTheDocument()
    expect(screen.getByText('开放评审')).toBeInTheDocument()
    expect(screen.getByText('REVIEW_PROMPT_VERSION')).toBeInTheDocument()
    expect(screen.getByText('open-review-v8')).toBeInTheDocument()
  })

  it('registry 加载失败：区内错误提示，Prompt 模板区不受影响', async () => {
    mockApis({ registryError: new Error('后端不可用') })
    render(<ModelConfig />)

    expect(await screen.findByText('流水线文案加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
    // Prompt 模板区独立加载不受影响
    expect(await screen.findByText('Prompt 模板 · 封闭生成（closed）')).toBeInTheDocument()
  })

  it('closed 卡组装说明：按常量名动态渲染 registry 条目（指明原始资产插入位置）', async () => {
    mockApis()
    render(<ModelConfig />)

    const closedCard = await screen
      .findByText('Prompt 模板 · 封闭生成（closed）')
      .then((el) => el.closest('.ant-card') as HTMLElement)
    // 组装说明出现在 closed 卡内（模板正文之后），标题与 {content} 特征串逐字可见
    expect(
      within(closedCard).getByText(/组装说明（closed 生成 · 原始资产插入位置）/, {
        exact: false,
      }),
    ).toBeInTheDocument()
    // 条目常量名作为说明区标题渲染
    expect(within(closedCard).getByText('CLOSED_ASSEMBLY_NOTE')).toBeInTheDocument()
    // 组装说明仅 closed 卡渲染（open 模板已停用，MODES 收敛为 closed，
    // 页面不会再出现 open 模板卡——反证组装说明不会外溢到其他卡）
    expect(screen.queryByText('Prompt 模板 · 开放生成（open）')).not.toBeInTheDocument()
  })

  it('置信度 prompt 内层卡：公式序三分区，核验 prompt 本体与携带资产标注动态渲染', async () => {
    mockApis()
    render(<ModelConfig />)

    const confidenceCard = await screen
      .findByText('置信度计算 · 提示词（closed）')
      .then((el) => el.closest('.ant-card') as HTMLElement)
    // 卡头携带资产总标注（高亮段落）：指明 Ccons/Cver 携带原始资产与关键帧
    expect(
      within(confidenceCard).getByText(/携带资产：Ccons 重评与 Cver 核验/, {
        exact: false,
      }),
    ).toBeInTheDocument()
    // 公式序三分区标题
    expect(
      within(confidenceCard).getByText('① Ccons · 评分一致性重评'),
    ).toBeInTheDocument()
    expect(
      within(confidenceCard).getByText('② Cver · 证据可核验性核验'),
    ).toBeInTheDocument()
    expect(
      within(confidenceCard).getByText('③ Cmod · 跨模态一致性（本地计算）'),
    ).toBeInTheDocument()
    // Cver 分区聚合核验 prompt 本体（常量名 + 文案），open 评审常量不混入
    expect(within(confidenceCard).getByText('CLOSED_VERIFICATION_PROMPT')).toBeInTheDocument()
    expect(within(confidenceCard).queryByText('REVIEW')).not.toBeInTheDocument()
    // 三段组装说明常量名与其文案
    expect(within(confidenceCard).getByText('CLOSED_CONFIDENCE_CCONS_NOTE')).toBeInTheDocument()
    expect(within(confidenceCard).getByText('CLOSED_CONFIDENCE_CVER_NOTE')).toBeInTheDocument()
    expect(within(confidenceCard).getByText('CLOSED_CONFIDENCE_CMOD_NOTE')).toBeInTheDocument()
    expect(
      within(confidenceCard).getByText(
        /置信度 Cver 证据可核验性 · 原始资产插入位置/,
        { exact: false },
      ),
    ).toBeInTheDocument()
  })

  it('registry 无 closed 组装条目：closed 卡与置信度内层卡静默跳过', async () => {
    // 覆盖 fallback 语义：closed 组装说明与置信度卡整体缺席
    mockApis({
      registry: [
        {
          module: 'open_review',
          title: '开放评审',
          items: [{ name: 'REVIEW_PROMPT_VERSION', text: 'open-review-v8' }],
        },
      ],
    })
    render(<ModelConfig />)

    expect(
      await screen.findByText('Prompt 模板 · 封闭生成（closed）'),
    ).toBeInTheDocument()
    // 置信度卡整体不渲染：内层卡标题、分区标题与携带资产标注均缺席
    expect(screen.queryByText('置信度计算 · 提示词（closed）')).not.toBeInTheDocument()
    expect(screen.queryByText('① Ccons · 评分一致性重评')).not.toBeInTheDocument()
    expect(screen.queryByText(/携带资产：Ccons 重评与 Cver 核验/, { exact: false })).not.toBeInTheDocument()
    expect(screen.queryByText('CLOSED_ASSEMBLY_NOTE')).not.toBeInTheDocument()
  })
})
