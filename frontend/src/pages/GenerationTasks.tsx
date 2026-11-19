// 生产任务页（Task 12）：批量生成任务的列表/详情/重试/创建。
// - 任务 Table：状态 Tag 色映射（queued 蓝/running 橙/succeeded 绿/failed 红）、
//   模式 Tag、错误列 Tooltip 展示、创建/完成时间；created_at 降序由后端保证，
//   列表一次拉默认 100 条（前端分页仅做展示分页）。
// - 行展开任务详情：标签产出、评分和证据在同一张表浏览，明细在侧栏查看。
// - failed 行"重试"：POST retry → 成功提示 + 刷新列表；422（非 failed）/
//   404 透出后端 detail。
// - 创建任务 Modal：资产多选（GET /assets，选完即收起下拉）+ mode 单选
//   （默认开放）+ 可选策略两个数字输入 → POST tasks → 成功提示 + 刷新；策略仅在填写时携带，
//   未填字段交由后端默认值（30 / 0.7）。
// - 场景资产（asset_type='scene'）：v2 内容识别只落影片级，场景行
//   content_json 恒为 NULL——后端会把选中场景上溯归并到所属影片建任务，
//   因此下拉里场景紧跟父影片排列并标注"归并到影片"，避免误解为按场景出
//   标签（场景级打标本期不支持）。
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { fetchAssets, type AssetSummary } from '../api/assets'
import {
  createGenerationTasks,
  DEFAULT_QUALITY_MODEL,
  fetchGenerationTask,
  fetchGenerationTasks,
  retryGenerationTask,
  type GenerationPolicyPayload,
  type GenerationTaskDetail,
  type GenerationTaskSummary,
} from '../api/tasks'
import TaskTagResults from '../components/TaskTagResults'
import TagQualityEvaluation from '../components/TagQualityEvaluation'

/** 任务状态中文展示（键集与后端任务状态机一致）。 */
const TASK_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已成功',
  failed: '已失败',
}

/** 任务状态 Tag 颜色（queued 蓝/running 橙/succeeded 绿/failed 红）。 */
const TASK_STATUS_COLORS: Record<string, string> = {
  queued: 'blue',
  running: 'orange',
  succeeded: 'green',
  failed: 'red',
}

/** 生成模式中文展示与 Tag 颜色（契约固定枚举 closed/open）。 */
const MODE_LABELS: Record<string, string> = { closed: '闭环', open: '开放' }
const MODE_COLORS: Record<string, string> = { closed: 'blue', open: 'purple' }

/** 创建弹窗表单值：资产非空多选 + mode（默认开放）+ 可选策略两项。 */
interface CreateFormValues {
  asset_ids: string[]
  mode: string
  quality_model: string
  max_tags?: number
  min_confidence?: number
}

/** 资产下拉单项文案：场景行标注归并语义（影片/剧集保持原格式）。 */
function assetOptionLabel(a: AssetSummary): string {
  const base = `${a.title}（${a.asset_id}）`
  return a.asset_type === 'scene' ? `${base}· 场景，任务归并到所属影片` : base
}

/** 资产下拉候选：同一父级聚合——影片/剧集在前、其场景紧随其后。
 * 后端归并（场景 → 父影片）在 UI 上必须可见：光标选中场景时用户看到的
 * 是"会归并到影片"，任务列表出现的 asset_id 也就能对得上。 */
function assetOptions(assets: AssetSummary[]) {
  const groupOf = (a: AssetSummary) =>
    a.asset_type === 'scene' && a.parent_id !== null ? a.parent_id : a.asset_id
  return [...assets]
    .sort((a, b) => {
      const ga = groupOf(a)
      const gb = groupOf(b)
      if (ga !== gb) return ga < gb ? -1 : 1
      if (a.asset_type !== b.asset_type) return a.asset_type === 'scene' ? 1 : -1
      return a.asset_id < b.asset_id ? -1 : 1
    })
    .map((a) => ({ value: a.asset_id, label: assetOptionLabel(a) }))
}

/** ISO 时间 → "YYYY-MM-DD HH:mm:ss"；null → "—"。 */
function formatTime(v: string | null): string {
  return v === null ? '—' : v.replace('T', ' ').slice(0, 19)
}

/** 行展开的任务详情面板：按 task_id 拉取详情，展示模型/Prompt 版本与
 * 结果行列表；详情加载失败透出错误文案（不影响外层列表）。 */
function TaskDetailPanel({ taskId }: { taskId: string }) {
  const [detail, setDetail] = useState<GenerationTaskDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let polling = false
    async function load() {
      try {
        const next = await fetchGenerationTask(taskId)
        if (cancelled) return
        setDetail(next)
        setLoadError(null)
        const status = next.quality_evaluation?.status
        polling = status === 'queued' || status === 'running'
      } catch (err) {
        if (cancelled) return
        setLoadError(err instanceof Error ? err.message : '任务详情加载失败')
      }
      if (polling) timer = setTimeout(() => void load(), 3000)
    }
    void load()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [taskId, refresh])

  if (loadError !== null && detail === null) {
    return <Alert type="error" showIcon message="任务详情加载失败" description={loadError} />
  }
  if (detail === null) {
    return <Typography.Text type="secondary">详情加载中…</Typography.Text>
  }

  const report = detail.quality_evaluation
  const film = report?.films.find((item) => item.task_id === detail.task_id)
  const tags = <TaskTagResults assetId={detail.asset_id} results={detail.results} labels={film?.labels} />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
      {loadError && <Alert type="warning" showIcon message="任务详情刷新失败" description={loadError} />}
      <Descriptions
        size="small"
        column={{ xs: 1, sm: 3 }}
        items={[
          { key: 'model', label: '生成模型', children: detail.model_version ?? '暂无' },
          { key: 'prompt', label: '生成提示词', children: detail.prompt_version ?? '暂无' },
          { key: 'count', label: '当前结果', children: `${detail.results.length} 条` },
        ]}
      />
      {report ? (
        <TagQualityEvaluation report={report} taskId={detail.task_id} onRetry={() => setRefresh((value) => value + 1)}>
          {tags}
        </TagQualityEvaluation>
      ) : tags}
    </div>
  )
}

export default function GenerationTasks() {
  // —— 列表数据与筛选状态（status/mode 变更即拉取，created_at 降序由后端保证）——
  const [tasks, setTasks] = useState<GenerationTaskSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [status, setStatus] = useState<string | undefined>()
  const [mode, setMode] = useState<string | undefined>()

  // —— 创建弹窗状态 ——
  const [createOpen, setCreateOpen] = useState(false)
  const [assets, setAssets] = useState<AssetSummary[]>([])
  const [assetsLoading, setAssetsLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  // 资产多选下拉的开合由我们接管：antd 多选默认选中后不收起，弹层会一直挂在
  // 弹窗上挡住下面的字段，这里每选一次就收起（再点输入框可继续加选）。
  const [assetOpen, setAssetOpen] = useState(false)
  const [createForm] = Form.useForm<CreateFormValues>()

  const loadTasks = useCallback(async () => {
    setLoading(true)
    try {
      const rows = await fetchGenerationTasks({ status, mode })
      setTasks(rows)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '生产任务列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [status, mode])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void loadTasks()
    })
    return () => {
      cancelled = true
    }
  }, [loadTasks])

  /** 重试失败任务：成功提示 + 刷新列表；422（非 failed）/404 透出 detail。 */
  async function handleRetry(task: GenerationTaskSummary) {
    try {
      await retryGenerationTask(task.task_id)
      message.success(`任务 ${task.task_id} 已重新排队`)
      await loadTasks()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '重试失败')
    }
  }

  /** 打开创建弹窗并预载资产候选（GET /assets 全量）；加载失败仅提示不阻塞。 */
  function openCreate() {
    createForm.resetFields()
    setAssetsLoading(true)
    fetchAssets()
      .then((rows) => setAssets(rows))
      .catch((err) => message.error(err instanceof Error ? err.message : '资产列表加载失败'))
      .finally(() => setAssetsLoading(false))
    setCreateOpen(true)
  }

  async function handleCreateSubmit() {
    let values: CreateFormValues
    try {
      values = await createForm.validateFields()
    } catch {
      return // 必填校验失败（资产/mode）：字段已原位提示
    }
    // 策略仅在用户填写时逐字段携带（未填字段交由后端默认值 30 / 0.7）
    const policy: GenerationPolicyPayload = {}
    if (values.max_tags !== undefined) policy.max_tags = values.max_tags
    if (values.min_confidence !== undefined) policy.min_confidence = values.min_confidence
    setCreating(true)
    try {
      const res = await createGenerationTasks({
        asset_ids: values.asset_ids,
        mode: values.mode,
        quality_model: values.quality_model.trim(),
        ...(Object.keys(policy).length > 0 ? { policy } : {}),
      })
      message.success(`已创建 ${res.tasks.length} 个任务`)
      setCreateOpen(false)
      await loadTasks()
    } catch (err) {
      // 422（mode 未知/资产及其父级均无识别内容）/404（资产不存在）透出后端 detail
      message.error(err instanceof Error ? err.message : '创建任务失败')
    } finally {
      setCreating(false)
    }
  }

  const columns: TableProps<GenerationTaskSummary>['columns'] = [
    { title: '任务 ID', dataIndex: 'task_id' },
    { title: '资产', dataIndex: 'asset_id', render: (v: string | null) => v ?? '—' },
    {
      title: '模式',
      dataIndex: 'mode',
      render: (v: string) => (
        <Tag color={MODE_COLORS[v] ?? 'default'}>{MODE_LABELS[v] ?? v}</Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      render: (v: string) => (
        <Tag color={TASK_STATUS_COLORS[v] ?? 'default'}>{TASK_STATUS_LABELS[v] ?? v}</Tag>
      ),
    },
    {
      title: '错误',
      dataIndex: 'error',
      render: (v: string | null) =>
        v === null || v === '' ? (
          '—'
        ) : (
          <Tooltip title={v}>
            <span
              style={{
                display: 'inline-block',
                maxWidth: 220,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                verticalAlign: 'bottom',
              }}
            >
              {v}
            </span>
          </Tooltip>
        ),
    },
    { title: '创建时间', dataIndex: 'created_at', render: formatTime },
    { title: '完成时间', dataIndex: 'finished_at', render: formatTime },
    {
      title: '操作',
      key: 'actions',
      render: (_, task) =>
        task.status === 'failed' ? (
          <Button type="link" size="small" danger onClick={() => void handleRetry(task)}>
            重试
          </Button>
        ) : null,
    },
  ]

  return (
    <Card
      title="生产任务"
      extra={<Button type="primary" onClick={openCreate}>创建任务</Button>}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Space wrap>
          <Select
            aria-label="状态筛选"
            value={status}
            onChange={(v) => setStatus(v)}
            options={Object.entries(TASK_STATUS_LABELS).map(([value, label]) => ({
              value,
              label,
            }))}
            allowClear
            placeholder="状态"
            style={{ width: 140 }}
          />
          <Select
            aria-label="模式筛选"
            value={mode}
            onChange={(v) => setMode(v)}
            options={Object.entries(MODE_LABELS).map(([value, label]) => ({ value, label }))}
            allowClear
            placeholder="模式"
            style={{ width: 120 }}
          />
        </Space>
        {loadError !== null && (
          <Alert type="error" showIcon message="生产任务列表加载失败" description={loadError} />
        )}
        <Table<GenerationTaskSummary>
          rowKey="task_id"
          columns={columns}
          dataSource={tasks}
          loading={loading}
          expandable={{
            expandedRowRender: (task, _index, _indent, expanded) =>
              expanded ? <TaskDetailPanel taskId={task.task_id} /> : null,
          }}
          pagination={{ showTotal: (t) => `共 ${t} 条` }}
        />
      </div>
      <Modal
        title="创建生成任务"
        open={createOpen}
        okText="创建"
        cancelText="取消"
        confirmLoading={creating}
        onOk={() => void handleCreateSubmit()}
        onCancel={() => setCreateOpen(false)}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="asset_ids"
            label="资产"
            rules={[{ required: true, message: '请选择至少一个资产' }]}
            extra="选场景资产也会归并到所属影片建任务（内容识别在影片级）。"
          >
            <Select
              aria-label="资产多选"
              mode="multiple"
              open={assetOpen}
              onDropdownVisibleChange={setAssetOpen}
              onChange={() => setAssetOpen(false)}
              loading={assetsLoading}
              options={assetOptions(assets)}
              placeholder="选择要生成标签的资产"
            />
          </Form.Item>
          <Form.Item name="mode" label="模式" initialValue="open">
            <Radio.Group
              options={[
                { value: 'open', label: '开放' },
                { value: 'closed', label: '闭环' },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="quality_model"
            label="评估模型 ID"
            initialValue={DEFAULT_QUALITY_MODEL}
            rules={[{ required: true, whitespace: true, message: '请输入评估模型 ID' }]}
          >
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item name="max_tags" label="最大标签数">
            <InputNumber min={1} placeholder="默认 30" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="min_confidence" label="最低置信度">
            <InputNumber min={0} max={1} step={0.1} placeholder="默认 0.7" style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
