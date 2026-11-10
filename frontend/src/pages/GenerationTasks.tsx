// 生产任务页（Task 12）：批量生成任务的列表/详情/重试/创建。
// - 任务 Table：状态 Tag 色映射（queued 蓝/running 橙/succeeded 绿/failed 红）、
//   模式 Tag、错误列 Tooltip 展示、创建/完成时间；created_at 降序由后端保证，
//   列表一次拉默认 100 条（前端分页仅做展示分页）。
// - 行展开任务详情：GET /generation/tasks/{id} 的结果行列表（行形状与审核
//   列表一致：标签名/来源/相关度/置信度/审核状态/证据卡），附模型与 Prompt
//   版本（Prompt 内容管理属阶段 4，此处仅展示版本号）。
// - failed 行"重试"：POST retry → 成功提示 + 刷新列表；422（非 failed）/
//   404 透出后端 detail。
// - 创建任务 Modal：资产多选（GET /assets，选完即收起下拉）+ mode 单选
//   （默认开放）+ 可选策略两个数字输入 → POST tasks → 成功提示 + 刷新；
//   策略仅在填写时携带，未填字段交由后端默认值（30 / 0.7）。
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  TreeSelect,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { fetchAssets, type AssetSummary } from '../api/assets'
import type { ReviewEvidence } from '../api/review'
import {
  createGenerationTasks,
  fetchGenerationTask,
  fetchGenerationTasks,
  retryGenerationTask,
  type GenerationPolicyPayload,
  type GenerationTaskDetail,
  type GenerationTaskResult,
  type GenerationTaskSummary,
} from '../api/tasks'
import { fetchLibraryTree, toDirSelectNodes, type DirSelectNode } from '../api/library'
import EvidenceCard from '../components/EvidenceCard'

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

/** 结果行的审核状态中文展示与 Tag 颜色（键集与审核中心一致）。 */
const REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已拒绝',
  merged: '已合并',
}
const REVIEW_STATUS_COLORS: Record<string, string> = {
  pending: 'gold',
  approved: 'green',
  rejected: 'red',
  merged: 'purple',
}

/** 创建弹窗表单值：资产非空多选 + mode（默认开放）+ 可选策略两项。 */
interface CreateFormValues {
  asset_ids: string[]
  mode: string
  max_tags?: number
  min_confidence?: number
  /** 目录范围（空 = 全库）；只列 dir_dynamic 目录，静态目录不产新词 */
  dir_ids?: string[]
  /** 是否允许产出库外新词（关掉 = 只挑命中库内的标签） */
  allow_new_tags?: boolean
  /** 单目录产出上限（0/未填 = 不限） */
  max_tags_per_dir?: number
}

/** 挂载粒度中文展示（目录 applicable_scope 的落库结果）。 */
const TARGET_TYPE_LABELS: Record<string, string> = {
  content: '影片级',
  scene: '场景级',
  character: '角色级',
}

/** ISO 时间 → "YYYY-MM-DD HH:mm:ss"；null → "—"。 */
function formatTime(v: string | null): string {
  return v === null ? '—' : v.replace('T', ' ').slice(0, 19)
}

/** 结果行单条证据明细（详情面板证据列复用审核列表证据卡）。 */
function ResultEvidence({ assetId, evidence }: { assetId: string; evidence: ReviewEvidence[] }) {
  if (evidence.length === 0) {
    return <Typography.Text type="secondary">暂无证据</Typography.Text>
  }
  return <EvidenceCard assetId={assetId} evidence={evidence} />
}

/** 行展开的任务详情面板：按 task_id 拉取详情，展示模型/Prompt 版本与
 * 结果行列表；详情加载失败透出错误文案（不影响外层列表）。 */
function TaskDetailPanel({ taskId }: { taskId: string }) {
  const [detail, setDetail] = useState<GenerationTaskDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    // StrictMode 会双调 effect：以 cancelled 丢弃过期请求结果
    let cancelled = false
    fetchGenerationTask(taskId).then(
      (d) => {
        if (!cancelled) setDetail(d)
      },
      (err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : '任务详情加载失败')
      },
    )
    return () => {
      cancelled = true
    }
  }, [taskId])

  if (loadError !== null) {
    return <Alert type="error" showIcon message="任务详情加载失败" description={loadError} />
  }
  if (detail === null) {
    return <Typography.Text type="secondary">详情加载中…</Typography.Text>
  }

  const resultColumns: TableProps<GenerationTaskResult>['columns'] = [
    { title: '标签名', dataIndex: 'tag_name' },
    {
      title: '目录',
      dataIndex: 'dir_name',
      render: (v: string | null) => v ?? '未归属',
    },
    {
      title: '粒度',
      dataIndex: 'target_type',
      render: (v: string) => (
        <Tag color={v === 'content' ? 'default' : 'cyan'}>
          {TARGET_TYPE_LABELS[v] ?? v}
        </Tag>
      ),
    },
    {
      title: '来源',
      dataIndex: 'origin',
      render: (v: string) => (
        <Tag color={v === 'closed' ? 'green' : 'blue'}>
          {v === 'closed' ? '目录召回' : '开放生成'}
        </Tag>
      ),
    },
    {
      title: '相关度',
      dataIndex: 'relevance_score',
      render: (v: number | null) => (v === null ? '暂无' : v.toFixed(2)),
    },
    {
      title: '置信度',
      dataIndex: 'confidence',
      render: (v: number | null) => (v === null ? '暂无' : v.toFixed(2)),
    },
    {
      title: '审核状态',
      dataIndex: 'review_status',
      render: (v: string) => (
        <Tag color={REVIEW_STATUS_COLORS[v] ?? 'default'}>{REVIEW_STATUS_LABELS[v] ?? v}</Tag>
      ),
    },
    {
      title: '证据',
      dataIndex: 'evidence',
      render: (v: ReviewEvidence[]) => (
        <ResultEvidence assetId={detail.asset_id ?? ''} evidence={v} />
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Descriptions
        size="small"
        column={3}
        items={[
          { key: 'model', label: '模型版本', children: detail.model_version ?? '暂无' },
          { key: 'prompt', label: 'Prompt 版本', children: detail.prompt_version ?? '暂无' },
          { key: 'count', label: '结果数', children: detail.results.length },
        ]}
      />
      <Table<GenerationTaskResult>
        rowKey="id"
        size="small"
        columns={resultColumns}
        dataSource={detail.results}
        pagination={false}
      />
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
  const [createForm] = Form.useForm<CreateFormValues>()
  const [dirNodes, setDirNodes] = useState<DirSelectNode[]>([])
  // 资产多选下拉的开合由我们接管：antd 多选默认选中后不收起，弹层会一直挂在
  // 弹窗上挡住下面的字段，这里每选一次就收起（再点输入框可继续加选）。
  const [assetOpen, setAssetOpen] = useState(false)

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
    // 目录树只用于"限定范围"：可扩展目录才可选（静态目录不产新词）
    fetchLibraryTree()
      .then((body) => setDirNodes(toDirSelectNodes(body.dirs)))
      .catch((err) => message.error(err instanceof Error ? err.message : '标签库目录加载失败'))
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
    if (values.dir_ids !== undefined && values.dir_ids.length > 0) {
      policy.dir_ids = values.dir_ids
    }
    // 默认 true 与后端缺省一致：只在显式关掉时携带 false，其余不写进 policy
    if (values.allow_new_tags === false) policy.allow_new_tags = false
    if (values.max_tags_per_dir !== undefined && values.max_tags_per_dir > 0) {
      policy.max_tags_per_dir = values.max_tags_per_dir
    }
    setCreating(true)
    try {
      const res = await createGenerationTasks({
        asset_ids: values.asset_ids,
        mode: values.mode,
        ...(Object.keys(policy).length > 0 ? { policy } : {}),
      })
      message.success(`已创建 ${res.tasks.length} 个任务`)
      setCreateOpen(false)
      await loadTasks()
    } catch (err) {
      // 422（mode 未知/资产无识别内容）/404（资产不存在）透出后端 detail
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
            expandedRowRender: (task) => <TaskDetailPanel taskId={task.task_id} />,
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
          >
            <Select
              aria-label="资产多选"
              mode="multiple"
              open={assetOpen}
              onDropdownVisibleChange={setAssetOpen}
              onChange={() => setAssetOpen(false)}
              loading={assetsLoading}
              options={assets.map((a) => ({
                value: a.asset_id,
                label: `${a.title}（${a.asset_id}）`,
              }))}
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
            name="dir_ids"
            label="目录范围"
            extra="留空 = 全库；只列可扩展（dir_dynamic）目录，静态目录不产新词"
          >
            <TreeSelect
              aria-label="目录范围"
              treeData={dirNodes}
              multiple
              treeCheckable
              showCheckedStrategy={TreeSelect.SHOW_PARENT}
              treeDefaultExpandAll
              placeholder="不限定目录"
              allowClear
            />
          </Form.Item>
          <Form.Item
            name="allow_new_tags"
            label="允许产出新词"
            valuePropName="checked"
            initialValue={true}
            extra="关闭后只挑命中标签库的标签；新词进审核队列，通过后写入入库预览"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="max_tags_per_dir"
            label="单目录上限"
            extra="0 或留空 = 不限；限制同一目录最多产出几条新词"
          >
            <InputNumber min={0} placeholder="默认不限" style={{ width: '100%' }} />
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
