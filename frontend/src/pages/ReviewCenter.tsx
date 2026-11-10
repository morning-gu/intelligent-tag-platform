// 审核中心（v2 行即日志）：按影片分组的标签行审核。
// - 顶部筛选栏：状态/目录/置信度区间/来源（statuses/modes 为契约固定
//   枚举，dirs 动态取自 /review/filters，按 dir_id 精确过滤）；默认拉
//   status=pending，后端 confidence 升序返回（低分先审）；一次取回
//   GROUPED_PAGE_SIZE 行供分组，不分页。
// - 列表按影片分组（Collapse）：组头是片名 + 条数，默认全部折叠，逐部
//   展开审核；排序沿用后端顺序（低置信度优先，片内同序）。
// - 表格列：标签名/目录（dir_name，null 兜底"—"）/来源（closed→Tag 闭环 /
//   open→Tag 开放）/置信度/相关度/状态（三态 Tag）。
// - 行内动作：通过/拒绝/修改/合并（通过=按候选目录入库；合并=候选名记成
//   库内标签别名，后端仅此一支，新建标签走"通过入库"）。
// - 相关度 = 与库内最相近标签的余弦（后端对齐 top-1）：判成新词也有值，
//   是"离库里的东西最近有多近"的信号。
// - 行展开 EvidenceCard 展示证据明细，点击跳内容详情并定位场景。
// - 勾选行批量通过/拒绝（逐条独立端点，跨影片共享选择）；冲突处理：审核
//   操作 409 → 统一提示"该标签已被他人处理"并自动刷新列表。
import {
  Alert,
  Button,
  Collapse,
  Empty,
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
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError } from '../api/client'
import { fetchTagCatalog, type TagNode } from '../api/catalog'
import {
  batchReviewAssetTags,
  fetchReviewFilters,
  fetchReviewItems,
  reviewAssetTag,
  type ReviewActionPayload,
  type ReviewFilters,
  type ReviewItem,
} from '../api/review'
import EvidenceCard from '../components/EvidenceCard'

/** 一次拉取上限：审核队列按影片分组展示，分组需要完整行集，故用大
 * page_size 一次取回；超出时页面提示收窄筛选。 */
const GROUPED_PAGE_SIZE = 500

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

/** 合并弹窗表单值：目标标签（merge_to_tag_id）+ 可选备注。 */
interface MergeFormValues {
  merge_to_tag_id?: string
  comment?: string
}

export default function ReviewCenter() {
  // —— 列表数据：按影片分组展示，一次取回全部匹配行 ——
  const [items, setItems] = useState<ReviewItem[]>([])
  const [total, setTotal] = useState(0)
  // 展开的影片分组：默认全部折叠（先看影片与待审条数，逐部展开审核）
  const [openKeys, setOpenKeys] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  // —— 筛选状态（默认拉待审核）——
  const [status, setStatus] = useState<string | undefined>('pending')
  const [dirId, setDirId] = useState<string | undefined>()
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
        dir_id: dirId,
        origin,
        page: 1,
        page_size: GROUPED_PAGE_SIZE,
      })
      setItems(res.items)
      setTotal(res.total)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '审核列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [status, dirId, confidenceMin, confidenceMax, origin])

  /** 按影片分组（保持后端返回顺序：低置信度优先，影片内同样低分在前）。 */
  const groups = useMemo(() => {
    const map = new Map<
      string,
      { assetId: string; title: string; rows: ReviewItem[] }
    >()
    for (const item of items) {
      const group = map.get(item.asset_id) ?? {
        assetId: item.asset_id,
        title: item.asset_title ?? item.asset_id,
        rows: [],
      }
      group.rows.push(item)
      map.set(item.asset_id, group)
    }
    return [...map.values()]
  }, [items])

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
    // 预填展示名（后端 tag_name 已是"候选名优先、目录名回退"）；提交落在
    // 行内 candidate_name 作展示名覆盖，不动目录定义
    modifyForm.setFieldsValue({ candidate_name: item.tag_name })
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

  /** 标准标签搜索（合并弹窗数据源）：即查即显（内部工具、limit=20 限流，
   * 不做防抖）；kind=tag 只取标签，不混入目录节点。 */
  async function searchTags(q: string) {
    setTagSearching(true)
    try {
      const res = await fetchTagCatalog({ kind: 'tag', q: q || undefined, limit: 20 })
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
    void searchTags('') // 打开弹窗即预载候选标签
  }

  async function handleMergeSubmit() {
    if (merging === null) return
    let values: MergeFormValues
    try {
      values = await mergeForm.validateFields()
    } catch {
      return // 必填校验失败（合并目标）：字段已原位提示
    }
    const comment = values.comment?.trim() || undefined
    // 合并只有一支：候选名记为库内标签的别名（后端 merge_to_tag_id）；
    // "新建标签"走通过入库（approve 按目录建 tag_define），不在这里绕。
    const mergeTo = values.merge_to_tag_id as string
    const payload: ReviewActionPayload = comment !== undefined
      ? { merge_to_tag_id: mergeTo, comment }
      : { merge_to_tag_id: mergeTo }
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
  const originOptions = (filters?.modes ?? ORIGIN_FALLBACK).map((o) => ({
    value: o,
    label: ORIGIN_LABELS[o] ?? o,
  }))
  // 目录下拉按 dir_id 精确过滤（后端 dir_id 入参），展示用全路径
  const dirOptions = (filters?.dirs ?? []).map((d) => ({
    value: d.dir_id,
    label: d.path || d.dir_name,
  }))

  const columns: TableProps<ReviewItem>['columns'] = [
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
          {/* 通过 = 按候选目录入库（后端 record_tag_definition）：新词、库内
              命中行都能通过；目录校验不过返回 422，前端透出 detail */}
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
          {/* 合并 = 候选名记为库内标签别名（不改标准名） */}
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
          onChange={(v) => { setStatus(v) }}
          options={statusOptions}
          allowClear
          placeholder="状态"
          style={{ width: 140 }}
        />
        <Select
          aria-label="目录筛选"
          value={dirId}
          onChange={(v) => { setDirId(v) }}
          options={dirOptions}
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="目录"
          style={{ width: 200 }}
        />
        <InputNumber
          placeholder="最低置信度"
          min={0}
          max={1}
          step={0.1}
          value={confidenceMin}
          onChange={(v) => { setConfidenceMin(v ?? undefined) }}
          style={{ width: 130 }}
        />
        <InputNumber
          placeholder="最高置信度"
          min={0}
          max={1}
          step={0.1}
          value={confidenceMax}
          onChange={(v) => { setConfidenceMax(v ?? undefined) }}
          style={{ width: 130 }}
        />
        <Select
          aria-label="来源筛选"
          value={origin}
          onChange={(v) => { setOrigin(v) }}
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
      <Typography.Text type="secondary">
        {`共 ${total} 条 · 涉及 ${groups.length} 部影片`}
        {total > GROUPED_PAGE_SIZE
          ? `（仅展示前 ${GROUPED_PAGE_SIZE} 条，请用筛选收窄范围）`
          : ''}
      </Typography.Text>
      {groups.length === 0 ? (
        loading ? null : <Empty description="当前筛选下没有标签" />
      ) : (
        <Collapse
          activeKey={openKeys}
          onChange={(keys) => setOpenKeys(keys as string[])}
          items={groups.map((group) => ({
            key: group.assetId,
            label: (
              <Space>
                <Typography.Text strong>{group.title}</Typography.Text>
                <Tag>{`${group.rows.length} 条`}</Tag>
              </Space>
            ),
            children: (
              <Table<ReviewItem>
                rowKey="id"
                columns={columns}
                dataSource={group.rows}
                loading={loading}
                rowSelection={{
                  selectedRowKeys: selectedIds,
                  // 每部影片一张表，跨表共享选择：只替换本表范围内的选择，
                  // 保留其他影片已勾选的行（批量操作按全集提交）
                  onChange: (keys) => {
                    const groupIds = new Set(group.rows.map((row) => row.id))
                    const kept = selectedIds.filter((id) => !groupIds.has(id))
                    setSelectedIds([...kept, ...keys.map(Number)])
                  },
                }}
                expandable={{
                  expandedRowRender: (item) => (
                    <EvidenceCard assetId={item.asset_id} evidence={item.evidence} />
                  ),
                  rowExpandable: (item) => item.evidence.length > 0,
                }}
                pagination={false}
                size="small"
              />
            ),
          }))}
        />
      )}
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
          <Form.Item
            name="merge_to_tag_id"
            label="合并目标（库内标签）"
            extra="合并后库内标签保留为标准名，本行候选名记为它的别名；要新建标签请直接「通过」（按目录入库）"
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
          <Form.Item name="comment" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
