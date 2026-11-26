// 模型配置中心页（admin 只读；路由层守卫保证仅 admin 可达）。
// ①Prompt 模板（Task 14 只读化）：prompt_template 表已随迁移 0018 drop，
//   文案是代码常量（git 即版本机制）——页面按 GET /prompts 只读契约清单
//   逐 mode 渲染模板正文 + 必需占位符 + 可用字段；占位符说明动态来自 API
//   （P0 #4 根治：提示文案不再手写）。closed 卡额外按常量名（如
//   CLOSED_ASSEMBLY_NOTE）从 GET /prompts/registry 查找组装说明动态渲染
//   ——原始资产插入位置（{content}）与关键帧/候选分组注入方式一处可看，
//   页面零手写文案；registry 未加载或无该条目时静默跳过。同区块还渲染
//   置信度 prompt 内层卡：按计算公式序三分区（Ccons → Cver → Cmod，
//   见 CONFIDENCE_SECTIONS），Cver 分区聚合核验 prompt 本体与其组装
//   说明；卡头以高亮段落展示携带资产总标注（CLOSED_CONFIDENCE_ASSETS_
//   NOTE——指明 Ccons/Cver 的 LLM 消息携带 spec.prompt_text 原始资产
//   文本与关键帧图片、Cmod 纯本地不携带），文案本体同样动态来自
//   registry。无新建版本/
//   激活入口——写端点已删（POST /prompts → 405、/prompts/{id}/activate →
//   404，契约锁定见 api/prompts.test.ts 与后端 test_api_prompts_admin）。
// ②当前运行配置：Descriptions 只读卡（GET /config 十三字段，布尔用是/否 Tag）
//   + 说明文案——修改走环境变量 + 重启服务，页面不提供写配置入口。
// ③流水线文案（2026-11-22 文案集中新增）：GET /prompts/registry 四组只读
//   清单（closed/mtg/assets/open_review），按流水线分组折叠展示常量名与文案
//   本体——全部指令性 prompt 一处可看；置信度三段组装说明（Ccons/Cver/
//   Cmod）随 closed 组条目展示（常量 CLOSED_CONFIDENCE_*_NOTE）。独立
//   错误态（失败不拖死 ①②）。
import { Alert, Card, Collapse, Descriptions, Spin, Tag, Typography } from 'antd'
import { useCallback, useEffect, useState, type CSSProperties } from 'react'
import { fetchRuntimeConfig, type RuntimeConfig } from '../../api/config'
import {
  fetchPromptRegistry,
  fetchPrompts,
  type PromptContractItem,
  type PromptRegistryGroup,
  type PromptRegistryItem,
} from '../../api/prompts'

/** mode → 展示名（语义与后端 prompts 常量注释一致：closed 目录内封闭生成）。 */
const MODE_TITLES: Record<string, string> = { closed: '封闭生成', open: '开放生成' }

/** 只读正文 pre 的共用样式：保留换行、继承字体。模板正文、组装说明与
 * registry 条目共用（registry 条目处覆写 margin）。 */
const PRE_STYLE: CSSProperties = {
  margin: 0,
  padding: 12,
  background: '#fafafa',
  border: '1px solid #f0f0f0',
  borderRadius: 4,
  whiteSpace: 'pre-wrap',
  fontFamily: 'inherit',
}

/** 置信度内层卡的分区定义：按计算公式 Craw = w1·Ccons + w2·Cver +
 * w3·Cmod 的顺序三分区，各分区标题指明环节名；entries 为该分区展示的
 * registry 常量名（文案本体全部来自 GET /prompts/registry closed 组，
 * 页面零手写）——Cver 分区聚合核验 prompt 本体（CLOSED_VERIFICATION_
 * PROMPT，Cver 环节的 LLM 调用指令）与组装说明；ASSETS_NAME 为卡头
 * 携带资产总标注常量名。 */
const CONFIDENCE_SECTIONS = [
  {
    title: '① Ccons · 评分一致性重评',
    entryNames: ['CLOSED_CONFIDENCE_CCONS_NOTE'],
  },
  {
    title: '② Cver · 证据可核验性核验',
    entryNames: ['CLOSED_VERIFICATION_PROMPT', 'CLOSED_CONFIDENCE_CVER_NOTE'],
  },
  {
    title: '③ Cmod · 跨模态一致性（本地计算）',
    entryNames: ['CLOSED_CONFIDENCE_CMOD_NOTE'],
  },
] as const

/** 置信度卡头的携带资产总标注（registry 常量名）。 */
const CONFIDENCE_ASSETS_NAME = 'CLOSED_CONFIDENCE_ASSETS_NOTE'

/** 从 registry 清单取 closed 组条目（未加载/无 closed 组 → 空数组，
 * 调用方静默跳过组装说明渲染）。 */
function closedRegistryItemsOf(
  registry: PromptRegistryGroup[] | null,
): PromptRegistryItem[] {
  return registry?.find((group) => group.module === 'closed')?.items ?? []
}

/** 运行配置布尔字段的通用渲染：是/否 Tag。 */
function renderBool(v: boolean) {
  return v ? <Tag color="green">是</Tag> : <Tag>否</Tag>
}

/** 单个 mode 的只读契约卡：模板正文逐字展示（pre 保留换行），必需占位符
 * 与可用字段全部来自 API 契约项，页面零手写文案。closed 卡动态追加组装
 * 说明（registry 常量名 → 文案），指明原始资产插入位置与注入方式。 */
function PromptContractCard({
  item,
  assemblyNotes,
}: {
  item: PromptContractItem
  assemblyNotes?: PromptRegistryItem[]
}) {
  return (
    <Card
      type="inner"
      title={`Prompt 模板 · ${MODE_TITLES[item.mode] ?? item.mode}（${item.mode}）`}
    >
      <pre style={PRE_STYLE}>{item.template_text}</pre>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
        {`必需占位符：${
          item.required_placeholders.length > 0
            ? item.required_placeholders.join('、')
            : '（无）'
        }`}
      </Typography.Paragraph>
      <div style={{ marginTop: 8 }}>
        <Typography.Text type="secondary">可用字段：</Typography.Text>
        {item.allowed_fields.map((field) => (
          <Tag key={field}>{field}</Tag>
        ))}
      </div>
      {item.mode === 'closed' && assemblyNotes && assemblyNotes.length > 0 && (
        <div
          style={{
            marginTop: 12,
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          {assemblyNotes.map((note) => (
            <div key={note.name}>
              <Typography.Text strong>{note.name}</Typography.Text>
              <pre style={{ ...PRE_STYLE, marginTop: 4 }}>{note.text}</pre>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export default function ModelConfig() {
  // —— Prompt 只读契约区状态 ——
  const [prompts, setPrompts] = useState<PromptContractItem[] | null>(null)
  const [promptsError, setPromptsError] = useState<string | null>(null)
  // —— 流水线文案区状态（独立于 ①，失败不拖死其他区块）——
  const [registry, setRegistry] = useState<PromptRegistryGroup[] | null>(null)
  const [registryError, setRegistryError] = useState<string | null>(null)
  // —— 运行配置区状态 ——
  const [config, setConfig] = useState<RuntimeConfig | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)

  const loadPrompts = useCallback(async () => {
    try {
      const rows = await fetchPrompts()
      setPrompts(rows)
      setPromptsError(null)
    } catch (err) {
      setPromptsError(err instanceof Error ? err.message : 'Prompt 模板契约加载失败')
    }
  }, [])

  const loadRegistry = useCallback(async () => {
    try {
      const groups = await fetchPromptRegistry()
      setRegistry(groups)
      setRegistryError(null)
    } catch (err) {
      setRegistryError(err instanceof Error ? err.message : '流水线文案加载失败')
    }
  }, [])

  const loadConfig = useCallback(async () => {
    try {
      const cfg = await fetchRuntimeConfig()
      setConfig(cfg)
      setConfigError(null)
    } catch (err) {
      setConfigError(err instanceof Error ? err.message : '运行配置加载失败')
    }
  }, [])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      void loadPrompts()
      void loadRegistry()
      void loadConfig()
    })
    return () => {
      cancelled = true
    }
  }, [loadPrompts, loadRegistry, loadConfig])

  // closed 专属展示条目（registry 未加载或常量缺失 → 空数组，相关区块
  // 静默跳过）：生成组装说明（指明原始资产插入位置）与置信度 prompt
  // （公式序三分区：Ccons/Cver/Cmod，卡头携带资产总标注）。
  const closedItems = closedRegistryItemsOf(registry)
  const closedAssemblyNotes = closedItems.filter(
    (entry) => entry.name === 'CLOSED_ASSEMBLY_NOTE',
  )
  // 置信度分区（公式序）：逐分区解析常量名 → registry 条目，缺条目的
  // 分区跳过；卡头携带资产总标注（缺条目时省略，分区条目仍展示）。
  const confidenceSections = CONFIDENCE_SECTIONS.map((section) => {
    const entries = section.entryNames
      .map((name) => closedItems.find((entry) => entry.name === name))
      .filter((entry): entry is PromptRegistryItem => entry !== undefined)
    return { title: section.title, entries }
  }).filter((section) => section.entries.length > 0)
  const confidenceAssetsNote = closedItems.find(
    (entry) => entry.name === CONFIDENCE_ASSETS_NAME,
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="Prompt 模板" extra={<Tag color="blue">只读 · 文案随代码版本</Tag>}>
        {promptsError !== null && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="Prompt 模板契约加载失败"
            description={promptsError}
          />
        )}
        {prompts === null && promptsError === null ? (
          <Spin />
        ) : (
          prompts?.map((item) => (
            <PromptContractCard
              key={item.mode}
              item={item}
              assemblyNotes={item.mode === 'closed' ? closedAssemblyNotes : undefined}
            />
          ))
        )}
        {prompts !== null && prompts.length === 0 && (
          <Typography.Text type="secondary">暂无模板契约</Typography.Text>
        )}
        {confidenceSections.length > 0 && (
          <Card type="inner" title="置信度计算 · 提示词（closed）" style={{ marginTop: 16 }}>
            {confidenceAssetsNote !== undefined && (
              <Typography.Paragraph
                style={{
                  marginTop: 0,
                  marginBottom: 4,
                  padding: 12,
                  background: '#e6f4ff',
                  border: '1px solid #91caff',
                  borderRadius: 4,
                }}
              >
                {confidenceAssetsNote.text}
              </Typography.Paragraph>
            )}
            {confidenceSections.map((section) => (
              <div key={section.title} style={{ marginTop: 12 }}>
                <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 4 }}>
                  {section.title}
                </Typography.Title>
                {section.entries.map((entry) => (
                  <div key={entry.name} style={{ marginTop: 8 }}>
                    <Typography.Text type="secondary">{entry.name}</Typography.Text>
                    <pre style={{ ...PRE_STYLE, marginTop: 4 }}>{entry.text}</pre>
                  </div>
                ))}
              </div>
            ))}
          </Card>
        )}
      </Card>
      <Card
        title="流水线文案"
        extra={<Tag color="blue">只读 · 文案随代码版本</Tag>}
      >
        {registryError !== null && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="流水线文案加载失败"
            description={registryError}
          />
        )}
        {registry === null && registryError === null ? (
          <Spin />
        ) : (
          registry !== null && registry.length > 0 && (
            <Collapse
              // 只读清单：默认全展开（组数固定为四，收起留给需要时手动）
              defaultActiveKey={registry.map((group) => group.module)}
              items={registry.map((group) => ({
                key: group.module,
                label: group.title,
                children: (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                    {group.items.map((item) => (
                      <div key={item.name}>
                        <Typography.Text strong>{item.name}</Typography.Text>
                        <pre style={{ ...PRE_STYLE, margin: '4px 0 0' }}>
                          {item.text}
                        </pre>
                      </div>
                    ))}
                  </div>
                ),
              }))}
            />
          )
        )}
        {registry !== null && registry.length === 0 && (
          <Typography.Text type="secondary">暂无流水线文案</Typography.Text>
        )}
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 16 }}>
          全部指令性文案（生成 / judge / 释义 / 复核 / 资产构建 / 开放评审）集中于
          tagplatform/capabilities/prompts，与各解析端互为协议；修改随代码评审走 git。
        </Typography.Text>
      </Card>
      <Card title="当前运行配置">
        {configError !== null && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="运行配置加载失败"
            description={configError}
          />
        )}
        {config !== null && (
          <Descriptions bordered column={1} size="small" labelStyle={{ width: 220 }}>
            <Descriptions.Item label="LLM 模型">{config.llm_model}</Descriptions.Item>
            <Descriptions.Item label="LLM 温度">{config.llm_temperature}</Descriptions.Item>
            <Descriptions.Item label="Judge 模型">{config.mtg_judge_model}</Descriptions.Item>
            <Descriptions.Item label="Judge 启用">
              {renderBool(config.mtg_judge_enabled)}
            </Descriptions.Item>
            <Descriptions.Item label="嵌入后端">{config.embedding_backend}</Descriptions.Item>
            <Descriptions.Item label="嵌入模型">{config.embedding_model}</Descriptions.Item>
            <Descriptions.Item label="开放评测评审模型">
              {config.open_eval_review_model === ''
                ? '同生成模型（空值回落）'
                : config.open_eval_review_model}
            </Descriptions.Item>
            <Descriptions.Item label="每类型召回 Top-K">
              {config.recall_top_k_per_type}
            </Descriptions.Item>
            <Descriptions.Item label="开放匹配阈值">
              {config.open_match_threshold}
            </Descriptions.Item>
            <Descriptions.Item label="生成并发数">{config.generation_workers}</Descriptions.Item>
            <Descriptions.Item label="Mock LLM">{renderBool(config.use_mock_llm)}</Descriptions.Item>
            <Descriptions.Item label="Mock Embedder">
              {renderBool(config.use_mock_embedder)}
            </Descriptions.Item>
            <Descriptions.Item label="数据库路径">{config.database_path}</Descriptions.Item>
          </Descriptions>
        )}
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 16 }}>
          修改运行配置：编辑环境变量后重启服务（详见 deploy/deploy.md）。
        </Typography.Text>
      </Card>
    </div>
  )
}
