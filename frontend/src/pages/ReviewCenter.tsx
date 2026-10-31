// 审核中心（Task 10）：标签行的五维筛选列表与四动作审核。
// - 顶部筛选栏：状态/标签类型/置信度区间/模式/来源（statuses/modes 与
//   origin 为契约固定枚举，tag_types 动态取自 /review/filters）；默认拉
//   status=pending，后端 confidence 升序返回（低分先审）；分页走列表 API。
// - 行展开 EvidenceCard 展示证据明细，点击跳内容详情并定位场景。
// - 单条 通过/拒绝（Popconfirm 确认）；修改/合并（Modal 表单）；勾选行
//   批量通过/拒绝（逐条独立端点）。
// - 冲突处理（Review Focus 1）：审核操作 409 → 统一提示"该标签已被他人
//   处理"并自动刷新列表；批量失败项（含冲突）去重后汇总提示。
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import {
  batchReviewAssetTags,
  fetchReviewFilters,
  fetchReviewItems,
  fetchTags,
  reviewAssetTag,
  type ReviewActionPayload,
  type ReviewFilters,
  type ReviewItem,
  type TagOption,
} from '../api/review'
import EvidenceCard from '../components/EvidenceCard'

/** 审核状态中文展示（键集与后端契约固定枚举一致）。 */
const STATUS_LABELS: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已拒绝',
  merged: '已合并',
}

/** 审核状态 Tag 颜色。 */
const STATUS_COLORS: Record<string, string> = {
  pending: 'gold',
  approved: 'green',
  rejected: 'red',
  merged: 'purple',
}

/** 筛选项接口不可用时的状态下拉兜底（保持页面可用）。 */
const STATUS_FALLBACK = ['pending', 'approved', 'rejected', 'merged']

/** 生成模式中文展示（契约固定枚举 closed/open）。 */
const MODE_LABELS: Record<string, string> = { closed: '闭环', open: '开放' }

/** 来源下拉固定选项（origin 为数据模型固定取值，/review/filters 不返回）。 */
const ORIGIN_OPTIONS = [
  { value: 'generated', label: '生成' },
  { value: 'catalog', label: '目录' },
]

/** 修改弹窗表单值（tag_name/tag_type/description 至少其一 + 可选备注）。 */
interface ModifyFormValues {
  tag_name?: string
  tag_type?: string
  description?: string
  comment?: string
}

/** 合并弹窗表单值（merge_to_tag_id 必填，备注可选）。 */
interface MergeFormValues {
  merge_to_tag_id: string
  comment?: string
}

export default function ReviewCenter() {
  // —— 列表数据与分页 ——
  const [items, setItems] = useState<ReviewItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  // —— 筛选状态（默认拉待审核）——
  const [status, setStatus] = useState<string | undefined>('pending')
  const [tagType, setTagType] = useState<string | undefined>()
  const [confidenceMin, setConfidenceMin] = useState<number | undefined>()
  const [confidenceMax, setConfidenceMax] = useState<number | undefined>()
  const [mode, setMode] = useState<string | undefined>()
  const [origin, setOrigin] = useState<string | undefined>()
  const [filters, setFilters] = useState<ReviewFilters | null>(null)

  // —— 行选择（批量操作）与弹窗（修改/合并）——
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [editing, setEditing] = useState<ReviewItem | null>(null)
  const [merging, setMerging] = useState<ReviewItem | null>(null)
  const [tagOptions, setTagOptions] = useState<TagOption[]>([])
  const [tagSearching, setTagSearching] = useState(false)
  const [batchLoading, setBatchLoading] = useState(false)
  const [modifyForm] = Form.useForm<ModifyFormValues>()
  const [mergeForm] = Form.useForm<MergeFormValues>()

  const loadItems = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetchReviewItems({
        status,
        confidence_min: confidenceMin,
        confidence_max: confidenceMax,
        tag_type: tagType,
        mode,
        origin,
        page,
        page_size: pageSize,
      })
      setItems(res.items)
      setTotal(res.total)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '审核列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [status, confidenceMin, confidenceMax, tagType, mode, origin, page, pageSize])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void loadItems()
    })
    return () => {
      cancelled = true
    }
  }, [loadItems])

  useEffect(() => {
    // 筛选项加载失败不阻塞列表（下拉留空仍可审核），静默忽略
    let cancelled = false
    fetchReviewFilters().then(
      (f) => {
        if (!cancelled) setFilters(f)
      },
      () => {},
    )
    return () => {
      cancelled = true
    }
  }, [])

  /** 审核操作统一错误处理：409（已被他人处理/并发抢先）→ 固定文案 +
   * 自动刷新列表回显最新状态；其余透出后端 detail（404/422）。 */
  function handleReviewError(err: unknown) {
    if (err instanceof ApiError && err.status === 409) {
      message.error('该标签已被他人处理')
      void loadItems()
    } else {
      message.error(err instanceof Error ? err.message : '操作失败')
    }
  }

  async function handleReview(item: ReviewItem, action: 'approve' | 'reject') {
    try {
      await reviewAssetTag(item.id, action)
      message.success(action === 'approve' ? '已通过' : '已拒绝')
      await loadItems()
    } catch (err) {
      handleReviewError(err)
    }
  }

  function openModify(item: ReviewItem) {
    modifyForm.resetFields()
    // 预填当前展示名/类型（generated 行即候选字段，catalog 行即目录字段）
    modifyForm.setFieldsValue({ tag_name: item.tag_name, tag_type: item.tag_type ?? undefined })
    setEditing(item)
  }

  async function handleModifySubmit() {
    if (editing === null) return
    let values: ModifyFormValues
    try {
      values = await modifyForm.validateFields()
    } catch {
      return // 表单校验失败：字段已原位提示
    }
    const payload: ReviewActionPayload = {}
    if (values.tag_name) payload.tag_name = values.tag_name
    if (values.tag_type) payload.tag_type = values.tag_type
    if (values.description) payload.description = values.description
    if (values.comment) payload.comment = values.comment
    // 后端要求 modify 至少一个可修改字段（否则 422），前置拦截省一次往返
    if (!payload.tag_name && !payload.tag_type && !payload.description) {
      message.warning('请至少填写一个修改字段（标签名/类型/描述）')
      return
    }
    try {
      await reviewAssetTag(editing.id, 'modify', payload)
      message.success('已修改，重新进入待审核队列')
      setEditing(null)
      await loadItems()
    } catch (err) {
      handleReviewError(err)
    }
  }

  /** 标准标签搜索：即查即显（内部工具、limit=20 限流，不做防抖）。 */
  async function searchTags(q: string) {
    setTagSearching(true)
    try {
      const res = await fetchTags(q)
      setTagOptions(res.items)
    } catch {
      setTagOptions([]) // 搜索失败：清空候选不阻塞弹窗，再次搜索可恢复
    } finally {
      setTagSearching(false)
    }
  }

  function openMerge(item: ReviewItem) {
    mergeForm.resetFields()
    setMerging(item)
    void searchTags('') // 打开弹窗即预载候选标签（目录前 20 条）
  }

  async function handleMergeSubmit() {
    if (merging === null) return
    let values: MergeFormValues
    try {
      values = await mergeForm.validateFields()
    } catch {
      return // merge_to 必填校验失败：字段已原位提示
    }
    const payload: ReviewActionPayload = { merge_to_tag_id: values.merge_to_tag_id }
    if (values.comment) payload.comment = values.comment
    try {
      await reviewAssetTag(merging.id, 'merge', payload)
      message.success('已合并到目标标签')
      setMerging(null)
      await loadItems()
    } catch (err) {
      handleReviewError(err)
    }
  }

  async function handleBatch(action: 'approve' | 'reject') {
    setBatchLoading(true)
    try {
      const res = await batchReviewAssetTags(selectedIds, action)
      const failed = res.results.filter((r) => !r.ok)
      const actionLabel = action === 'approve' ? '通过' : '拒绝'
      if (failed.length === 0) {
        message.success(`批量${actionLabel}成功（${res.results.length} 条）`)
      } else {
        // 逐条独立：失败不中断成功项，失败原因去重后汇总（Review Focus 4）
        const details = [...new Set(failed.map((f) => f.detail ?? '未知原因'))].join('；')
        message.warning(
          `批量操作：成功 ${res.results.length - failed.length} 条，失败 ${failed.length} 条（${details}）`,
        )
      }
      setSelectedIds([])
      await loadItems()
    } catch (err) {
      handleReviewError(err)
    } finally {
      setBatchLoading(false)
    }
  }

  const statusOptions = (filters?.statuses ?? STATUS_FALLBACK).map((s) => ({
    value: s,
    label: STATUS_LABELS[s] ?? s,
  }))
  const modeOptions = (filters?.modes ?? []).map((m) => ({
    value: m,
    label: MODE_LABELS[m] ?? m,
  }))
  // 修改弹窗类型下拉：目录动态类型 + 当前行自身类型（generated 候选类型
  // 可能不在目录枚举内，保留可选）
  const modifyTypeOptions = [
    ...new Set([...(filters?.tag_types ?? []), ...(editing?.tag_type ? [editing.tag_type] : [])]),
  ].map((v) => ({ value: v, label: v }))

  const columns: TableProps<ReviewItem>['columns'] = [
    { title: '资产标题', dataIndex: 'asset_title', render: (v: string | null) => v ?? '—' },
    { title: '标签名', dataIndex: 'tag_name' },
    { title: '类型', dataIndex: 'tag_type', render: (v: string | null) => v ?? '—' },
    {
      title: '来源',
      dataIndex: 'origin',
      render: (v: string) => (
        <Tag color={v === 'generated' ? 'blue' : 'green'}>
          {ORIGIN_OPTIONS.find((o) => o.value === v)?.label ?? v}
        </Tag>
      ),
    },
    {
      title: '置信度',
      dataIndex: 'confidence',
      render: (v: number | null) =>
        v === null ? '暂无' : <Progress percent={Math.round(v * 100)} size="small" />,
    },
    {
      title: '相关度',
      dataIndex: 'relevance',
      render: (v: number | null) => (v === null ? '暂无' : v.toFixed(2)),
    },
    {
      title: '状态',
      dataIndex: 'review_status',
      render: (v: string) => (
        <Tag color={STATUS_COLORS[v] ?? 'default'}>{STATUS_LABELS[v] ?? v}</Tag>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      render: (_, item) => (
        <Space size={0}>
          <Popconfirm
            title="确认通过该标签？"
            okText="确定"
            cancelText="取消"
            onConfirm={() => void handleReview(item, 'approve')}
          >
            <Button type="link" size="small">通过</Button>
          </Popconfirm>
          <Popconfirm
            title="确认拒绝该标签？"
            okText="确定"
            cancelText="取消"
            onConfirm={() => void handleReview(item, 'reject')}
          >
            <Button type="link" size="small" danger>拒绝</Button>
          </Popconfirm>
          <Button type="link" size="small" onClick={() => openModify(item)}>修改</Button>
          <Button type="link" size="small" onClick={() => openMerge(item)}>合并</Button>
        </Space>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Space wrap size="middle">
        <Select
          aria-label="状态筛选"
          value={status}
          onChange={(v) => { setPage(1); setStatus(v) }}
          options={statusOptions}
          allowClear
          placeholder="状态"
          style={{ width: 140 }}
        />
        <Select
          aria-label="标签类型筛选"
          value={tagType}
          onChange={(v) => { setPage(1); setTagType(v) }}
          options={(filters?.tag_types ?? []).map((t) => ({ value: t, label: t }))}
          allowClear
          placeholder="标签类型"
          style={{ width: 160 }}
        />
        <InputNumber
          placeholder="最低置信度"
          min={0}
          max={1}
          step={0.1}
          value={confidenceMin}
          onChange={(v) => { setPage(1); setConfidenceMin(v ?? undefined) }}
          style={{ width: 130 }}
        />
        <InputNumber
          placeholder="最高置信度"
          min={0}
          max={1}
          step={0.1}
          value={confidenceMax}
          onChange={(v) => { setPage(1); setConfidenceMax(v ?? undefined) }}
          style={{ width: 130 }}
        />
        <Select
          aria-label="模式筛选"
          value={mode}
          onChange={(v) => { setPage(1); setMode(v) }}
          options={modeOptions}
          allowClear
          placeholder="模式"
          style={{ width: 120 }}
        />
        <Select
          aria-label="来源筛选"
          value={origin}
          onChange={(v) => { setPage(1); setOrigin(v) }}
          options={ORIGIN_OPTIONS}
          allowClear
          placeholder="来源"
          style={{ width: 120 }}
        />
      </Space>
      <Space>
        <Popconfirm
          title={`确认批量通过选中的 ${selectedIds.length} 个标签？`}
          okText="确定"
          cancelText="取消"
          onConfirm={() => void handleBatch('approve')}
        >
          <Button disabled={selectedIds.length === 0} loading={batchLoading}>批量通过</Button>
        </Popconfirm>
        <Popconfirm
          title={`确认批量拒绝选中的 ${selectedIds.length} 个标签？`}
          okText="确定"
          cancelText="取消"
          onConfirm={() => void handleBatch('reject')}
        >
          <Button danger disabled={selectedIds.length === 0} loading={batchLoading}>批量拒绝</Button>
        </Popconfirm>
      </Space>
      {loadError !== null && (
        <Alert type="error" showIcon message="审核列表加载失败" description={loadError} />
      )}
      <Table<ReviewItem>
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        rowSelection={{
          selectedRowKeys: selectedIds,
          onChange: (keys) => setSelectedIds(keys.map(Number)),
        }}
        expandable={{
          expandedRowRender: (item) => (
            <EvidenceCard assetId={item.asset_id} evidence={item.evidence} />
          ),
          rowExpandable: (item) => item.evidence.length > 0,
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => { setPage(p); setPageSize(ps) },
        }}
      />
      <Modal
        title={`修改标签：${editing?.tag_name ?? ''}`}
        open={editing !== null}
        okText="确定"
        cancelText="取消"
        onOk={() => void handleModifySubmit()}
        onCancel={() => setEditing(null)}
      >
        <Form form={modifyForm} layout="vertical">
          <Form.Item name="tag_name" label="标签名">
            <Input />
          </Form.Item>
          <Form.Item name="tag_type" label="标签类型">
            <Select allowClear options={modifyTypeOptions} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="description" label="标签描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="comment" label="备注">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={`合并标签：${merging?.tag_name ?? ''}`}
        open={merging !== null}
        okText="确定"
        cancelText="取消"
        onOk={() => void handleMergeSubmit()}
        onCancel={() => setMerging(null)}
      >
        <Form form={mergeForm} layout="vertical">
          <Form.Item
            name="merge_to_tag_id"
            label="合并目标"
            rules={[{ required: true, message: '请选择合并目标标签' }]}
          >
            <Select
              aria-label="搜索标准标签"
              showSearch
              filterOption={false}
              loading={tagSearching}
              onSearch={(q) => void searchTags(q)}
              options={tagOptions.map((t) => ({
                value: t.tag_id,
                label: `${t.tag_name}（${t.tag_type}）`,
              }))}
              placeholder="搜索并选择标准标签"
              style={{ width: '100%' }}
            />
          </Form.Item>
          <Form.Item name="comment" label="备注">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
