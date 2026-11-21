// 模型配置中心页（admin 只读；路由层守卫保证仅 admin 可达）。
// ①Prompt 模板（Task 14 只读化）：prompt_template 表已随迁移 0018 drop，
//   文案是代码常量（git 即版本机制）——页面按 GET /prompts 只读契约清单
//   逐 mode 渲染模板正文 + 必需占位符 + 可用字段；占位符说明动态来自 API
//   （P0 #4 根治：提示文案不再手写）。无新建版本/激活入口——写端点已删
//   （POST /prompts → 405、/prompts/{id}/activate → 404，契约锁定见
//   api/prompts.test.ts 与后端 test_api_prompts_admin）。
// ②当前运行配置：Descriptions 只读卡（GET /config 八字段，布尔用是/否 Tag）
//   + 说明文案——修改走环境变量 + 重启服务，页面不提供写配置入口。
import { Alert, Card, Descriptions, Spin, Tag, Typography } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { fetchRuntimeConfig, type RuntimeConfig } from '../../api/config'
import { fetchPrompts, type PromptContractItem } from '../../api/prompts'

/** mode → 展示名（语义与后端 prompts 常量注释一致：closed 目录内封闭生成）。 */
const MODE_TITLES: Record<string, string> = { closed: '封闭生成', open: '开放生成' }

/** 运行配置布尔字段的通用渲染：是/否 Tag。 */
function renderBool(v: boolean) {
  return v ? <Tag color="green">是</Tag> : <Tag>否</Tag>
}

/** 单个 mode 的只读契约卡：模板正文逐字展示（pre 保留换行），必需占位符
 * 与可用字段全部来自 API 契约项，页面零手写文案。 */
function PromptContractCard({ item }: { item: PromptContractItem }) {
  return (
    <Card
      type="inner"
      title={`Prompt 模板 · ${MODE_TITLES[item.mode] ?? item.mode}（${item.mode}）`}
    >
      <pre
        style={{
          margin: 0,
          padding: 12,
          background: '#fafafa',
          border: '1px solid #f0f0f0',
          borderRadius: 4,
          whiteSpace: 'pre-wrap',
          fontFamily: 'inherit',
        }}
      >
        {item.template_text}
      </pre>
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
    </Card>
  )
}

export default function ModelConfig() {
  // —— Prompt 只读契约区状态 ——
  const [prompts, setPrompts] = useState<PromptContractItem[] | null>(null)
  const [promptsError, setPromptsError] = useState<string | null>(null)
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
      void loadConfig()
    })
    return () => {
      cancelled = true
    }
  }, [loadPrompts, loadConfig])

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
          prompts?.map((item) => <PromptContractCard key={item.mode} item={item} />)
        )}
        {prompts !== null && prompts.length === 0 && (
          <Typography.Text type="secondary">暂无模板契约</Typography.Text>
        )}
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 16 }}>
          模板文案是代码常量（tagplatform/capabilities/prompts），修改随代码评审走 git；
          DB 版本机制已退役（写端点不再存在）。
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
