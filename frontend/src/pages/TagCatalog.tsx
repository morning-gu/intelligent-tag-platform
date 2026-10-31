// 标签库页（Task 13）：标准标签目录的查询与详情查看（阶段 3 无标签编辑/
// 删除契约，仅读）。
// - 目录 Table：type 精确筛选（动态类型取自 /review/filters，与审核中心
//   同源）+ q 名称/别名子串搜索（提交触发）；后端 limit/offset 分页且列表
//   响应无 total 字段——每页多取一条探测后续页（探测行不展示），分页 total
//   以"当前页末 + 是否还有后续"拼装，翻到末页时自然收敛为精确值。
// - 行点击 → 详情 Drawer：GET /tags/{id}，含 aliases 列表与父标签/状态。
import {
  Alert,
  Card,
  Descriptions,
  Drawer,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { fetchTagCatalog, fetchTagDetail, type TagCatalogDetail, type TagCatalogItem } from '../api/catalog'
import { fetchReviewFilters } from '../api/review'

/** 标签状态中文展示与 Tag 颜色（active/deprecate 与治理流水线入库语义一致）。 */
const TAG_STATUS_LABELS: Record<string, string> = {
  active: '启用',
  deprecate: '废弃',
}
const TAG_STATUS_COLORS: Record<string, string> = {
  active: 'green',
  deprecate: 'red',
}

const PAGE_SIZE_DEFAULT = 20

export default function TagCatalog() {
  // —— 目录数据与服务端分页（limit = pageSize + 1 探测后续页）——
  const [items, setItems] = useState<TagCatalogItem[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(PAGE_SIZE_DEFAULT)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  // —— 筛选状态：类型变更即拉取；名称搜索在提交（回车/按钮）时生效 ——
  const [type, setType] = useState<string | undefined>()
  const [searchQ, setSearchQ] = useState('')
  const [inputQ, setInputQ] = useState('')
  const [tagTypes, setTagTypes] = useState<string[]>([])

  // —— 详情 Drawer ——
  const [selectedTagId, setSelectedTagId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TagCatalogDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  const loadCatalog = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetchTagCatalog({
        type,
        q: searchQ,
        limit: pageSize + 1,
        offset: (page - 1) * pageSize,
      })
      // 探测行：多取的一条仅用于判断有无下一页，不进表格
      const rows = res.items.length > pageSize ? res.items.slice(0, pageSize) : res.items
      setItems(rows)
      setHasMore(res.items.length > pageSize)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '标签库加载失败')
    } finally {
      setLoading(false)
    }
  }, [type, searchQ, page, pageSize])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void loadCatalog()
    })
    return () => {
      cancelled = true
    }
  }, [loadCatalog])

  useEffect(() => {
    // 类型下拉候选（库内动态去重）；筛选接口失败不阻塞列表，静默忽略
    let cancelled = false
    fetchReviewFilters().then(
      (f) => {
        if (!cancelled) setTagTypes(f.tag_types)
      },
      () => {},
    )
    return () => {
      cancelled = true
    }
  }, [])

  /** 打开标签详情 Drawer：在事件内重置详情态（含上次残留）并记录标签 ID，
   * 拉取交给下方 effect（effect 内不做同步 setState）。 */
  function openTag(tagId: string) {
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setSelectedTagId(tagId)
  }

  // selectedTagId 变化即拉取标签详情（仅异步置 state）
  useEffect(() => {
    if (selectedTagId === null) return
    let cancelled = false
    fetchTagDetail(selectedTagId).then(
      (d) => {
        if (!cancelled) {
          setDetail(d)
          setDetailLoading(false)
        }
      },
      (err) => {
        if (!cancelled) {
          setDetailError(err instanceof Error ? err.message : '标签详情加载失败')
          setDetailLoading(false)
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [selectedTagId])

  const columns: TableProps<TagCatalogItem>['columns'] = [
    { title: '标签名', dataIndex: 'tag_name' },
    {
      title: '类型',
      dataIndex: 'tag_type',
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    { title: '描述', dataIndex: 'description' },
    {
      title: '状态',
      dataIndex: 'status',
      render: (v: string | null) =>
        v === null ? (
          '—'
        ) : (
          <Tag color={TAG_STATUS_COLORS[v] ?? 'default'}>{TAG_STATUS_LABELS[v] ?? v}</Tag>
        ),
    },
  ]

  return (
    <Card title="标签库">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Space wrap>
          <Select
            aria-label="类型筛选"
            value={type}
            onChange={(v) => {
              setPage(1)
              setType(v)
            }}
            options={tagTypes.map((t) => ({ value: t, label: t }))}
            allowClear
            placeholder="标签类型"
            style={{ width: 160 }}
          />
          <Input.Search
            aria-label="名称搜索"
            placeholder="名称/别名搜索"
            enterButton="搜索"
            value={inputQ}
            onChange={(e) => setInputQ(e.target.value)}
            onSearch={(v) => {
              setPage(1)
              setSearchQ(v)
            }}
            style={{ width: 280 }}
          />
        </Space>
        {loadError !== null && (
          <Alert type="error" showIcon message="标签库加载失败" description={loadError} />
        )}
        <Table<TagCatalogItem>
          rowKey="tag_id"
          columns={columns}
          dataSource={items}
          loading={loading}
          onRow={(record) => ({
            onClick: () => openTag(record.tag_id),
            style: { cursor: 'pointer' },
          })}
          pagination={{
            current: page,
            pageSize,
            // 后端无 total 契约：hasMore 时多给一页占位（探测行保证下一页可达），
            // 末页收敛为 offset + 实际行数；总数仅在末页展示（中间页的
            // hasMore 占位 total 是下界，展示会误导）
            total: hasMore ? page * pageSize + 1 : (page - 1) * pageSize + items.length,
            showSizeChanger: true,
            showTotal: hasMore ? undefined : (t) => `共 ${t} 条`,
            onChange: (p, ps) => {
              setPage(p)
              setPageSize(ps)
            },
          }}
        />
      </div>
      <Drawer
        title="标签详情"
        open={selectedTagId !== null}
        width={520}
        destroyOnHidden
        onClose={() => setSelectedTagId(null)}
      >
        {detailError !== null && (
          <Alert type="error" showIcon message="标签详情加载失败" description={detailError} />
        )}
        {detailLoading && <Typography.Text type="secondary">详情加载中…</Typography.Text>}
        {detail !== null && (
          <Descriptions
            column={1}
            items={[
              { key: 'id', label: '标签 ID', children: detail.tag_id },
              { key: 'name', label: '标签名', children: detail.tag_name },
              { key: 'type', label: '类型', children: detail.tag_type },
              { key: 'desc', label: '描述', children: detail.description },
              {
                key: 'parent',
                label: '父标签',
                children: detail.parent_id ?? '—',
              },
              {
                key: 'status',
                label: '状态',
                children:
                  detail.status === null ? (
                    '—'
                  ) : (
                    <Tag color={TAG_STATUS_COLORS[detail.status] ?? 'default'}>
                      {TAG_STATUS_LABELS[detail.status] ?? detail.status}
                    </Tag>
                  ),
              },
              {
                key: 'aliases',
                label: '别名',
                children:
                  detail.aliases.length === 0 ? (
                    '—'
                  ) : (
                    <Space wrap size={4}>
                      {detail.aliases.map((a) => (
                        <Tag key={a}>{a}</Tag>
                      ))}
                    </Space>
                  ),
              },
            ]}
          />
        )}
      </Drawer>
    </Card>
  )
}
