// 标签库页（Task 13）：标准标签目录的查询与详情查看（阶段 3 无标签编辑/
// 删除契约，仅读）。
// - 目录 Table：q 名称/别名子串搜索（提交触发）；后端 limit/offset 分页且列表
//   响应无 total 字段——每页多取一条探测后续页（探测行不展示），分页 total
//   以"当前页末 + 是否还有后续"拼装，翻到末页时自然收敛为精确值。
// - 行点击 → 详情 Drawer：GET /tags/{id}，含 aliases 列表与父标签/状态。
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Input,
  Space,
  Table,
  Tag,
  TreeSelect,
  Typography,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import {
  downloadTagCatalog,
  fetchTagCatalog,
  fetchTagDetail,
  type TagCatalogDetail,
  type TagCatalogItem,
} from '../api/catalog'

/** 标签状态中文展示与 Tag 颜色（active/deprecate 与治理流水线入库语义一致）。 */
const TAG_STATUS_LABELS: Record<string, string> = {
  active: '启用',
  deprecate: '废弃',
}
const TAG_STATUS_COLORS: Record<string, string> = {
  active: 'green',
  deprecate: 'red',
}

function sortDirectoriesFirst(items: TagCatalogItem[]): TagCatalogItem[] {
  return [...items].sort(
    (left, right) => Number(right.has_children === true) - Number(left.has_children === true),
  )
}

export default function TagCatalog() {
  // —— 树状目录：首屏只加载根节点，展开时按父节点加载子节点 ——
  const [items, setItems] = useState<TagCatalogItem[]>([])
  const [directoryItems, setDirectoryItems] = useState<TagCatalogItem[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)

  // —— 筛选状态：名称搜索在提交（回车/按钮）时生效 ——
  const [searchQ, setSearchQ] = useState('')
  const [inputQ, setInputQ] = useState('')
  const [scopeId, setScopeId] = useState<string | undefined>()
  const [scopeLabel, setScopeLabel] = useState<string | undefined>()
  const [expandedRowKeys, setExpandedRowKeys] = useState<string[]>([])

  // —— 详情 Drawer ——
  const [selectedTagId, setSelectedTagId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TagCatalogDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  async function exportCatalog() {
    setExporting(true)
    try {
      const blob = await downloadTagCatalog({ q: searchQ, scope_id: scopeId })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'tags-export.zip'
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '标签导出失败')
    } finally {
      setExporting(false)
    }
  }

  const loadCatalog = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetchTagCatalog({
        q: searchQ,
        parent_id: scopeId,
        // 默认只展示一级目录；搜索时返回所有匹配节点，避免子级标签
        // 因 root=true 被过滤掉，同时保留目录节点的下级展开能力。
        root: scopeId === undefined && searchQ === '' ? true : undefined,
      })
      const sortedItems = sortDirectoriesFirst(res.items)
      setItems(sortedItems)
      if (scopeId === undefined && searchQ === '') setDirectoryItems(sortedItems)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '标签库加载失败')
    } finally {
      setLoading(false)
    }
  }, [scopeId, searchQ])

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

  async function loadDirectoryChildren(node: { value?: string | number }) {
    if (typeof node.value !== 'string') return
    const parentId = node.value
    const res = await fetchTagCatalog({ parent_id: parentId })
    setDirectoryItems((current) => updateDirectoryChildren(current, parentId, res.items))
  }

  function updateDirectoryChildren(
    nodes: TagCatalogItem[],
    parentId: string,
    children: TagCatalogItem[],
  ): TagCatalogItem[] {
    return nodes.map((node) => {
      if (node.tag_id === parentId) {
        return { ...node, children: sortDirectoriesFirst(children) }
      }
      if (node.children) {
        return { ...node, children: updateDirectoryChildren(node.children, parentId, children) }
      }
      return node
    })
  }

  function selectDirectory(directoryId: string | undefined, directoryName?: string) {
    setSelectedTagId(null)
    setSearchQ('')
    setInputQ('')
    setScopeId(directoryId)
    setScopeLabel(directoryName)
    setExpandedRowKeys([])
  }

  function findDirectory(nodes: TagCatalogItem[], id: string): TagCatalogItem | undefined {
    for (const node of nodes) {
      if (node.tag_id === id) return node
      if (node.children) {
        const found = findDirectory(node.children, id)
        if (found) return found
      }
    }
    return undefined
  }

  function goToParentDirectory() {
    if (scopeId === undefined) return
    const current = findDirectory(directoryItems, scopeId)
    const parent = current?.parent_id ? findDirectory(directoryItems, current.parent_id) : undefined
    selectDirectory(parent?.tag_id, parent?.tag_name)
  }

  async function expandSelectedDirectory(record: TagCatalogItem) {
    if (record.has_children !== true) return
    setExpandedRowKeys((current) =>
      current.includes(record.tag_id)
        ? current.filter((key) => key !== record.tag_id)
        : [...current, record.tag_id],
    )
    if (record.children !== undefined) return
    try {
      const res = await fetchTagCatalog({ parent_id: record.tag_id })
      setItems((current) => updateDirectoryChildren(current, record.tag_id, res.items))
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '下级标签加载失败')
    }
  }

  function toTreeData(nodes: TagCatalogItem[]): Array<{
    title: string
    value: string
    children?: Array<{
      title: string
      value: string
      children?: ReturnType<typeof toTreeData>
      isLeaf?: boolean
    }>
    isLeaf?: boolean
  }> {
    return nodes
      .filter((node) => node.has_children === true)
      .map((node) => ({
      title: node.tag_name,
      value: node.tag_id,
      children: node.children ? toTreeData(node.children) : undefined,
      isLeaf: false,
      }))
  }

  const columns: TableProps<TagCatalogItem>['columns'] = [
    {
      title: '名称',
      dataIndex: 'tag_name',
      render: (name: string, record: TagCatalogItem) => (
        record.tag_id === '__back__' ? (
          <Button type="link" onClick={goToParentDirectory} style={{ padding: 0 }}>
            ← {name}
          </Button>
        ) : (
        <Space size={8}>
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              minWidth: 42,
              height: 22,
              padding: '0 6px',
              borderRadius: 11,
              background: record.has_children ? '#e6f4ff' : '#f6ffed',
              color: record.has_children ? '#1677ff' : '#389e0d',
              fontSize: 12,
              lineHeight: '22px',
            }}
          >
            {record.has_children ? '目录' : '标签'}
          </span>
          <span>{name}</span>
        </Space>
        )
      ),
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

  const displayItems: TagCatalogItem[] = scopeId
    ? [
        {
          tag_id: '__back__',
          tag_name: '返回上级目录',
          tag_type: '',
          description: '',
          parent_id: null,
          status: null,
          has_children: false,
        },
        ...items,
      ]
    : items

  return (
    <Card title="标签库">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Space wrap>
          <TreeSelect
            aria-label="目录层级"
            treeData={[
              { title: '所有', value: '__all__', isLeaf: true },
              ...(scopeId && scopeLabel && !directoryItems.some((item) => item.tag_id === scopeId)
                ? [{ title: scopeLabel, value: scopeId, isLeaf: true }]
                : []),
              ...toTreeData(directoryItems),
            ]}
            value={scopeId ?? '__all__'}
            onChange={(value, label) => {
              const nextId = value === '__all__' ? undefined : value
              setScopeId(nextId)
              setScopeLabel(typeof label === 'string' ? label : undefined)
            }}
            loadData={loadDirectoryChildren}
            treeDefaultExpandAll={false}
            placeholder="目录层级"
            style={{ width: 220 }}
          />
          <Input.Search
            aria-label="名称搜索"
            placeholder="名称/别名搜索"
            enterButton="搜索"
            value={inputQ}
            onChange={(e) => setInputQ(e.target.value)}
            onSearch={(v) => {
              setSearchQ(v)
            }}
            style={{ width: 280 }}
          />
          <Button type="primary" onClick={() => void exportCatalog()} loading={exporting}>
            标签导出
          </Button>
        </Space>
        {loadError !== null && (
          <Alert type="error" showIcon message="标签库加载失败" description={loadError} />
        )}
        <Table<TagCatalogItem>
          rowKey="tag_id"
          columns={columns}
          dataSource={displayItems}
          loading={loading}
          expandable={{
            rowExpandable: (record) => record.has_children === true,
            expandedRowKeys,
            expandRowByClick: false,
            expandIcon: ({ expanded, onExpand, record }) =>
              record.has_children === true ? (
                <button
                  type="button"
                  aria-label={expanded ? '收起下级目录' : '展开下级目录'}
                  onClick={(event) => {
                    event.stopPropagation()
                    void expandSelectedDirectory(record)
                    onExpand(record, event)
                  }}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: 24,
                    height: 24,
                    border: '1px solid #91caff',
                    borderRadius: 6,
                    background: expanded ? '#e6f4ff' : '#fff',
                    color: '#1677ff',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                >
                  <span
                    style={{
                      width: expanded ? 11 : 2,
                      height: expanded ? 2 : 11,
                      background: 'currentColor',
                      transition: 'all 0.2s ease',
                    }}
                  />
                </button>
              ) : null,
          }}
          onRow={(record) => ({
            onClick: () => {
              if (record.tag_id === '__back__') {
                goToParentDirectory()
              } else if (record.has_children === true) {
                void expandSelectedDirectory(record)
              } else {
                openTag(record.tag_id)
              }
            },
            style: { cursor: 'pointer' },
          })}
          pagination={false}
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
              {
                key: 'name',
                label: '标签名',
                children: (
                  <Space size={4} wrap>
                    {(detail.path ?? [detail.tag_name]).map((name, index) => {
                      const isTag = index === (detail.path?.length ?? 1) - 1
                      const directoryId = detail.path_ids?.[index]
                      return isTag || directoryId === undefined ? (
                        <span key={`${name}-${index}`}>
                          {index > 0 ? ' / ' : ''}
                          {name}
                        </span>
                      ) : (
                        <span key={`${name}-${index}`}>
                          {index > 0 ? ' / ' : ''}
                          <Button
                            type="link"
                            size="small"
                            onClick={() => selectDirectory(directoryId, name)}
                            style={{ padding: 0 }}
                          >
                            {name}
                          </Button>
                        </span>
                      )
                    })}
                  </Space>
                ),
              },
              { key: 'desc', label: '描述', children: detail.description },
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
