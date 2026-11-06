// 模型配置中心页（阶段 4 Task 3，路由层 admin 守卫已保证仅 admin 可达）。
// 两区布局：
// ①Prompt 版本管理：closed/open 两个 mode 分组 Tab（Tabs 仅作模式切换器，
//   列表由 activeMode 驱动单一 Table 渲染，避免隐藏面板残留旧数据），
//   版本 Table（版本号/激活 Tag/创建时间/操作列——仅非激活行提供"激活"，
//   已激活行操作列为空）。
//   "新建版本"Modal：textarea 编辑 template_text，mode 取当前 Tab，打开时
//   预填当前 Tab 激活版本的正文（后端列表已带 template_text——基于现版微调，
//   非从零转写；无激活版本则从空开始）→ POST /prompts → 成功提示 + 关闭
//   弹窗 + 刷新当前 Tab（新版本落库即激活，同 mode 旧版本自动停用，后端
//   保证恰一个 is_active）。
//   422（空文本/缺占位符）/ 403（非 admin）经 ApiError 透出后端 detail，
//   弹窗保持打开便于修正重试。历史版本不可变：无编辑/删除操作
//   （换提示词 = 追加新版本 + 切换激活）。
// ②当前运行配置：Descriptions 只读卡（GET /config 九字段，布尔用是/否 Tag）
//   + 说明文案——修改走环境变量 + 重启服务，页面不提供写配置入口。
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { fetchRuntimeConfig, type RuntimeConfig } from '../api/config'
import {
  activatePromptVersion,
  createPromptVersion,
  fetchPrompts,
  type PromptCreatePayload,
  type PromptTemplateSummary,
} from '../api/prompts'

/** mode → Tab 展示名（语义与 db/models.py 注释一致：closed 目录内封闭生成）。 */
const MODE_TABS = [
  { key: 'closed', label: '封闭生成（closed）' },
  { key: 'open', label: '开放生成（open）' },
] as const

type PromptMode = (typeof MODE_TABS)[number]['key']

/** 各 mode 必需占位符说明（与后端 prompts.py 服务层校验规则一致，供弹窗提示）。 */
const MODE_PLACEHOLDER_HINTS: Record<PromptMode, string> = {
  closed: 'closed 模板必需占位符：{content}（影片内容）与 {candidates}（候选标签清单）',
  open: 'open 模板必需占位符：{content}（影片内容）',
}

/** 新建弹窗表单值：仅模板正文一项（mode 由当前 Tab 决定，不进表单）。 */
interface CreateFormValues {
  template_text: string
}

/** ISO 时间 → "YYYY-MM-DD HH:mm:ss"；null → "—"。 */
function formatTime(v: string | null): string {
  return v === null ? '—' : v.replace('T', ' ').slice(0, 19)
}

/** 运行配置布尔字段的通用渲染：是/否 Tag。 */
function renderBool(v: boolean) {
  return v ? <Tag color="green">是</Tag> : <Tag>否</Tag>
}

export default function ModelConfig() {
  // —— Prompt 版本管理区状态 ——
  const [activeMode, setActiveMode] = useState<PromptMode>('closed')
  const [prompts, setPrompts] = useState<PromptTemplateSummary[]>([])
  const [promptsLoading, setPromptsLoading] = useState(false)
  const [promptsError, setPromptsError] = useState<string | null>(null)
  const [activatingId, setActivatingId] = useState<number | null>(null)
  // —— 新建版本弹窗状态 ——
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createForm] = Form.useForm<CreateFormValues>()
  // —— 运行配置区状态 ——
  const [config, setConfig] = useState<RuntimeConfig | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)

  const loadPrompts = useCallback(async (mode: PromptMode) => {
    setPromptsLoading(true)
    try {
      const rows = await fetchPrompts(mode)
      setPrompts(rows)
      setPromptsError(null)
    } catch (err) {
      setPromptsError(err instanceof Error ? err.message : 'Prompt 版本列表加载失败')
    } finally {
      setPromptsLoading(false)
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
      void loadPrompts('closed')
      void loadConfig()
    })
    return () => {
      cancelled = true
    }
  }, [loadPrompts, loadConfig])

  /** Tab 切换：清空旧列表（避免切换瞬间闪现上一 mode 的数据）并拉取新清单。 */
  function handleTabChange(key: string) {
    const mode = key as PromptMode
    setActiveMode(mode)
    setPrompts([])
    setPromptsError(null)
    void loadPrompts(mode)
  }

  /** 激活指定版本：成功提示 + 刷新当前 Tab；404（未知 id）透出后端 detail。 */
  async function handleActivate(row: PromptTemplateSummary) {
    setActivatingId(row.id)
    try {
      const res = await activatePromptVersion(row.id)
      message.success(`已激活 v${res.version}（${res.mode}）`)
      await loadPrompts(activeMode)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '激活失败')
    } finally {
      setActivatingId(null)
    }
  }

  function openCreate() {
    createForm.resetFields()
    // 预填当前 Tab 激活版本的正文（列表已带 template_text）：运营侧基于
    // 现版微调而非从零转写；无激活版本（理论不可达，后端保证恰一个）则留空
    const active = prompts.find((row) => row.is_active)
    if (active !== undefined) {
      createForm.setFieldsValue({ template_text: active.template_text })
    }
    setCreateOpen(true)
  }

  /** 新建版本：mode 取当前 Tab；成功刷新当前 Tab（新版本落库即激活，
   * 后端返回的 mode 与当前 Tab 一致）；422（空文本/缺占位符）经 ApiError
   * 透出后端 detail，弹窗保持打开便于修正重试。 */
  async function handleCreateSubmit() {
    let values: CreateFormValues
    try {
      values = await createForm.validateFields()
    } catch {
      return // 必填校验失败（空正文）：字段已原位提示
    }
    setCreating(true)
    try {
      const payload: PromptCreatePayload = {
        mode: activeMode,
        template_text: values.template_text,
      }
      const created = await createPromptVersion(payload)
      message.success(`已新建版本 v${created.version}（${created.mode}）`)
      setCreateOpen(false)
      await loadPrompts(created.mode as PromptMode)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '新建版本失败')
    } finally {
      setCreating(false)
    }
  }

  const columns: TableProps<PromptTemplateSummary>['columns'] = [
    { title: '版本', dataIndex: 'version', render: (v: number) => `v${v}` },
    {
      title: '状态',
      dataIndex: 'is_active',
      render: (active: boolean) => (active ? <Tag color="green">激活中</Tag> : <Tag>已停用</Tag>),
    },
    { title: '创建时间', dataIndex: 'created_at', render: formatTime },
    {
      title: '操作',
      key: 'actions',
      render: (_, row) =>
        // 已激活行不提供操作（幂等切换无意义）；仅历史行可激活
        row.is_active ? null : (
          <Button
            size="small"
            loading={activatingId === row.id}
            onClick={() => void handleActivate(row)}
          >
            激活
          </Button>
        ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="Prompt 版本管理"
        extra={
          <Button type="primary" onClick={openCreate}>
            新建版本
          </Button>
        }
      >
        {promptsError !== null && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="Prompt 版本列表加载失败"
            description={promptsError}
          />
        )}
        <Tabs activeKey={activeMode} onChange={handleTabChange} items={[...MODE_TABS]} />
        <Table<PromptTemplateSummary>
          rowKey="id"
          columns={columns}
          dataSource={prompts}
          loading={promptsLoading}
          pagination={{ showTotal: (t) => `共 ${t} 条` }}
          locale={{ emptyText: '暂无版本' }}
        />
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
      <Modal
        title={`新建 Prompt 版本（${activeMode}）`}
        open={createOpen}
        okText="提交"
        cancelText="取消"
        confirmLoading={creating}
        onOk={() => void handleCreateSubmit()}
        onCancel={() => setCreateOpen(false)}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="template_text"
            label="模板正文"
            rules={[{ required: true, message: '请输入模板正文' }]}
            extra={`${MODE_PLACEHOLDER_HINTS[activeMode]}；打开时已预填当前激活版本，可在此基础上微调`}
          >
            <Input.TextArea
              rows={8}
              placeholder="在此编辑模板正文（历史版本不可变，换提示词请追加新版本）"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
