// 审核中心（v2 行即日志）：标签行的筛选列表与四动作审核。
// - 顶部筛选栏：状态/目录/置信度区间/来源（statuses/origins 为契约固定
//   枚举，dirs 动态取自 /review/filters）；默认拉 status=pending，后端
//   confidence 升序返回（低分先审）；分页走列表 API。v1 的 tag_type 与
//   mode 维度随词表退役——origin 即模式（closed 闭环 / open 开放）。
// - 表格列：资产标题/标签名/目录（dir_name，null 兜底"—"）/来源
//   （closed→Tag 闭环 / open→Tag 开放）/置信度/相关度/状态（三态 Tag，
//   v1 的 merged 紫 随词表退役）。
// - 行动作按形态分支（后端语义注入）：行有 tag_id（closed 行 / open 归一
//   命中行）出 通过/拒绝/修改/合并；open 自由候选行（tag_id 为空）只出
//   拒绝/合并——直接 approve/modify 后端 422，修改弹窗仅在 tag_id 行出现。
// - 修改弹窗：候选名/候选描述/备注，提交即通过（后端 modify=更新候选后
//   直接走 approve 流程，无需二次点击）。
// - 合并弹窗两支互斥（Radio 切换，备注两支共用）：并入既有=搜索选择既有
//   标签（fetchTags kind=tag）；合入新建=选目录（fetchTagCatalog kind=dir
//   lazily 拉取）+新标签名+描述。payload 走 never 互斥类型，不会混。
// - 行展开 EvidenceCard 展示证据明细，点击跳内容详情并定位场景。
// - 勾选行批量通过/拒绝（逐条独立端点）；冲突处理（Review Focus 1）：
//   审核操作 409 → 统一提示"该标签已被他人处理"并自动刷新列表；批量
//   失败项（含冲突）去重后汇总提示（Review Focus 4）。
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { fetchTagCatalog, type TagNode } from '../api/catalog'
import {
  batchReviewAssetTags,
  fetchReviewFilters,
  fetchReviewItems,
  fetchTags,
  reviewAssetTag,
  type ReviewActionPayload,
  type ReviewFilters,
  type ReviewItem,
  type ReviewStatus,
  type TagOrigin,
} from '../api/review'
import EvidenceCard from '../components/EvidenceCard'

/** 审核状态中文展示（键集与后端契约固定枚举一致；merged 已退役）。 */
const STATUS_LABELS: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已拒绝',
}

/** 审核状态 Tag 颜色（pending 金 / approved 绿 / rejected 红）。 */
const STATUS_COLORS: Record<string, string> = {
  pending: 'gold',
  approved: 'green',
  rejected: 'red',
}

/** 筛选项接口不可用时的状态下拉兜底（保持页面可用）。 */
const STATUS_FALLBACK = ['pending', 'approved', 'rejected']

/** 来源中文展示（origin 即模式：closed 闭环 / open 开放）。 */
const ORIGIN_LABELS: Record<string, string> = { closed: '闭环', open: '开放' }

/** 来源 Tag 颜色（沿 v1 模式列配色：闭环蓝 / 开放紫）。 */
const ORIGIN_COLORS: Record<string, string> = { closed: 'blue', open: 'purple' }

/** 来源下拉兜底（origins 为契约固定枚举，filters 失败时仍可筛）。 */
const ORIGIN_FALLBACK = ['closed', 'open']

/** 修改弹窗表单值（候选名/候选描述至少其一 + 可选备注；提交即通过）。 */
interface ModifyFormValues {
  candidate_name?: string
  candidate_description?: string
  comment?: string
}

/** 合并弹窗模式：并入既有 / 合入新建（两支 payload 互斥，Radio 切换）。 */
type MergeMode = 'existing' | 'new'

/** 合并弹窗表单值（按模式取用字段：支一 merge_to_tag_id；支二 new_*）。 */
interface MergeFormValues {
  merge_to_tag_id?: string
  new_tag_name?: string
  new_dir_id?: string
  new_description?: string
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
  const [dir, setDir] = useState<string | undefined>()
  const [confidenceMin, setConfidenceMin] = useState<number | undefined>()
  const [confidenceMax, setConfidenceMax] = useState<number | undefined>()
  const [origin, setOrigin] = useState<string | undefined>()
  const [filters, setFilters] = useState<ReviewFilters | null>(null)

  // —— 行选择（批量操作）与弹窗（修改/合并）——
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [editing, setEditing] = useState<ReviewItem | null>(null)
  const [merging, setMerging] = useState<ReviewItem | null>(null)
  const [tagOptions, setTagOptions] = useState<TagNode[]>([])
  const [tagSearching, setTagSearching] = useState(false)
  const [mergeMode, setMergeMode] = useState<MergeMode>('existing')
  const [dirOptions, setDirOptions] = useState<TagNode[]>([])
  const [dirLoading, setDirLoading] = useState(false)
  const [batchLoading, setBatchLoading] = useState(false)
  const [modifyForm] = Form.useForm<ModifyFormValues>()
  const [mergeForm] = Form.useForm<MergeFormValues>()

  const loadItems = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetchReviewItems({
        status: status as ReviewStatus | undefined,
        confidence_min: confidenceMin,
        confidence_max: confidenceMax,
        dir,
        origin: origin as TagOrigin | undefined,
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
  }, [status, dir, confidenceMin, confidenceMax, origin, page, pageSize])

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
    // 预填候选名：open 行即候选原文；closed 行 candidate_name 为空，回退
    // 展示名（定义名）——修改落在行内 candidate_name 作展示名覆盖，不动
    // 目录定义（tag_name = candidate_name 优先，回退定义名）
    modifyForm.setFieldsValue({ candidate_name: item.candidate_name ?? item.tag_name })
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
    const candidateName = values.candidate_name?.trim()
    const candidateDescription = values.candidate_description?.trim()
    // 后端要求 modify 至少一个可修改字段（否则 422），前置拦截省一次往返
    if (!candidateName && !candidateDescription) {
      message.warning('请至少填写一个修改字段（候选名/候选描述）')
      return
    }
    const comment = values.comment?.trim() || undefined
    const updates: { candidate_name?: string; candidate_description?: string } = {}
    if (candidateName) updates.candidate_name = candidateName
    if (candidateDescription) updates.candidate_description = candidateDescription
    const payload: ReviewActionPayload =
      comment !== undefined ? { ...updates, comment } : updates
    try {
      await reviewAssetTag(editing.id, 'modify', payload)
      message.success('已修改并通过')
      setEditing(null)
      await loadItems()
    } catch (err) {
      handleReviewError(err)
    }
  }

  /** 标准标签搜索（合并支一数据源）：即查即显（内部工具、limit=20 限流，
   * 不做防抖）；kind=tag 只取标签，不混入目录节点。 */
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

  /** 合入新建的目录下拉（lazily：首次切到"合入新建"才拉取，kind=dir 只取
   * 目录节点）；失败留空不阻塞弹窗，重开弹窗可重试。 */
  async function loadDirOptions() {
    if (dirOptions.length > 0 || dirLoading) return
    setDirLoading(true)
    try {
      const res = await fetchTagCatalog({ kind: 'dir', limit: 200 })
      setDirOptions(res.items)
    } catch {
      setDirOptions([])
    } finally {
      setDirLoading(false)
    }
  }

  function openMerge(item: ReviewItem) {
    mergeForm.resetFields()
    setMergeMode('existing')
    setMerging(item)
    void searchTags('') // 打开弹窗即预载候选标签（默认支一并入既有）
  }

  /** 合并模式切换：清空两支表单（防跨支残留值泄入 payload）；切到合入
   * 新建时 lazily 拉取目录下拉。 */
  function handleMergeModeChange(mode: MergeMode) {
    setMergeMode(mode)
    mergeForm.resetFields()
    if (mode === 'new') void loadDirOptions()
  }

  async function handleMergeSubmit() {
    if (merging === null) return
    let values: MergeFormValues
    try {
      values = await mergeForm.validateFields()
    } catch {
      return // 必填校验失败（合并目标/目录/新标签名）：字段已原位提示
    }
    const comment = values.comment?.trim() || undefined
    // 两支 payload 走 never 互斥类型：键集按支精确构造，不会混
    let payload: ReviewActionPayload
    if (mergeMode === 'existing') {
      // 支一：并入既有标签（merge_to_tag_id 必填已由表单规则校验，备注可选）
      const mergeTo = values.merge_to_tag_id as string
      payload = comment !== undefined
        ? { merge_to_tag_id: mergeTo, comment }
        : { merge_to_tag_id: mergeTo }
    } else {
      // 支二：新建标签入库（new_tag_name + new_dir_id 必填，描述/备注可选）
      payload = {
        new_tag_name: (values.new_tag_name as string).trim(),
        new_dir_id: values.new_dir_id as string,
        ...(values.new_description?.trim()
          ? { new_description: values.new_description.trim() }
          : {}),
        ...(comment !== undefined ? { comment } : {}),
      }
    }
    try {
      await reviewAssetTag(merging.id, 'merge', payload)
      message.success(mergeMode === 'existing' ? '已合并到目标标签' : '已新建标签并合并入库')
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
  const originOptions = (filters?.origins ?? ORIGIN_FALLBACK).map((o) => ({
    value: o,
    label: ORIGIN_LABELS[o] ?? o,
  }))

  const columns: TableProps<ReviewItem>['columns'] = [
    { title: '资产标题', dataIndex: 'asset_title', render: (v: string | null) => v ?? '—' },
    { title: '标签名', dataIndex: 'tag_name' },
    {
      title: '目录',
      dataIndex: 'dir_name',
      render: (v: string | null) => v ?? '—',
    },
    {
      title: '来源',
      dataIndex: 'origin',
      render: (v: string) => (
        <Tag color={ORIGIN_COLORS[v] ?? 'default'}>{ORIGIN_LABELS[v] ?? v}</Tag>
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
          {/* 行有 tag_id（closed 行 / open 归一命中行）才出 通过/修改——
              open 自由候选直接 approve/modify 后端 422 */}
          {item.tag_id !== null && (
            <Popconfirm
              title="确认通过该标签？"
              okText="确定"
              cancelText="取消"
              onConfirm={() => void handleReview(item, 'approve')}
            >
              <Button type="link" size="small">通过</Button>
            </Popconfirm>
          )}
          <Popconfirm
            title="确认拒绝该标签？"
            okText="确定"
            cancelText="取消"
            onConfirm={() => void handleReview(item, 'reject')}
          >
            <Button type="link" size="small" danger>拒绝</Button>
          </Popconfirm>
          {item.tag_id !== null && (
            <Button type="link" size="small" onClick={() => openModify(item)}>修改</Button>
          )}
          {/* 合并对两种行形都可用——候选行经 merge 归置入库后走 approve 流程 */}
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
          aria-label="目录筛选"
          value={dir}
          onChange={(v) => { setPage(1); setDir(v) }}
          options={(filters?.dirs ?? []).map((d) => ({ value: d, label: d }))}
          allowClear
          placeholder="目录"
          style={{ width: 140 }}
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
          aria-label="来源筛选"
          value={origin}
          onChange={(v) => { setPage(1); setOrigin(v) }}
          options={originOptions}
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
          <Form.Item name="candidate_name" label="候选名">
            <Input />
          </Form.Item>
          <Form.Item name="candidate_description" label="候选描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="comment" label="备注">
            <Input.TextArea rows={2} />
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
          <Radio.Group
            value={mergeMode}
            onChange={(e) => handleMergeModeChange(e.target.value as MergeMode)}
            style={{ marginBottom: 12 }}
          >
            <Radio value="existing">并入既有标签</Radio>
            <Radio value="new">合入新建标签</Radio>
          </Radio.Group>
          {mergeMode === 'existing' ? (
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
                options={tagOptions.map((t) => ({ value: t.id, label: t.name }))}
                placeholder="搜索并选择标准标签"
                style={{ width: '100%' }}
              />
            </Form.Item>
          ) : (
            <>
              <Form.Item
                name="new_dir_id"
                label="目标目录"
                rules={[{ required: true, message: '请选择目标目录' }]}
              >
                <Select
                  aria-label="目标目录"
                  loading={dirLoading}
                  options={dirOptions.map((d) => ({ value: d.id, label: d.name }))}
                  placeholder="选择新标签归属目录"
                  style={{ width: '100%' }}
                />
              </Form.Item>
              <Form.Item
                name="new_tag_name"
                label="新标签名"
                rules={[{ required: true, message: '请填写新标签名' }]}
              >
                <Input />
              </Form.Item>
              <Form.Item name="new_description" label="新标签描述">
                <Input.TextArea rows={2} />
              </Form.Item>
            </>
          )}
          {/* 备注两支共用（并入既有/合入新建同款 TextArea） */}
          <Form.Item name="comment" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
