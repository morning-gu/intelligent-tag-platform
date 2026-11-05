// 标签库页（v2 混合树）：/tags 目录+标签合并列表的查询与详情查看。
// - 目录 Table：根视图（root=true）/scope 视图（parent_id）/子级展开
//   （parent_id）/q 名称+别名子串搜索（提交触发）。徽标直接取节点 kind
//   （目录/标签），has_children 驱动展开箭头语义；目录节点无 status 列，
//   状态列显示 "—" 且详情无生命周期按钮（v2 后端对目录生命周期操作一律 404）。
// - TreeSelect 目录层级选择器：懒加载沿 parent_id 递归且只拉目录（kind=dir）。
// - 行点击 → 详情 Drawer：GET /tags/{id}（目录/标签通用），含 path 路径
//   （父目录可点击跳 scope）与 aliases；启用/停用 + 删除仅对 kind=tag 渲染，
//   删除后跳转 result.parent_id 对应 scope。
// - 标签导出按钮：downloadTagCatalog 走 /tags/export，与本页筛选联动。
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Input,
  Modal,
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
  activateTag,
  deactivateTag,
  deleteTag,
  type TagNode,
  type TagNodeDetail,
} from '../api/catalog'

/** 标签状态中文展示与 Tag 颜色（active/deprecated 与 v2 治理流水线入库语义
 * 一致；目录节点 status 恒 null，两表均不查值直接显示 "—"）。 */
const TAG_STATUS_LABELS: Record<string, string> = {
  active: '启用',
  deprecated: '停用',
}
const TAG_STATUS_COLORS: Record<string, string> = {
  active: 'green',
  deprecated: 'orange',
}

/** 目录行排前（与后端目录先排序一致；搜索结果兜底）。 */
function sortDirectoriesFirst<T extends TagNode>(items: T[]): T[] {
  return [...items].sort(
    (left, right) => Number(right.kind === 'dir') - Number(left.kind === 'dir'),
  )
}

/** 页内树形装配节点：API TagNode 之上挂客户端下级缓存（仅内存态，不随请求
 * 发送；后端 TagNode 无 children 字段）。 */
type TagTreeNode = TagNode & { children?: TagTreeNode[] }

export default function TagCatalog() {
  // —— 树状目录：首屏只加载根节点，展开时按父节点加载子节点 ——
  const [items, setItems] = useState<TagTreeNode[]>([])
  const [directoryItems, setDirectoryItems] = useState<TagTreeNode[]>([])
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
  const [detail, setDetail] = useState<TagNodeDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [statusUpdating, setStatusUpdating] = useState(false)
  const [deleting, setDeleting] = useState(false)

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
        // 默认只展示一级节点；搜索时返回所有匹配节点，避免子级标签
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

  /** 打开节点详情 Drawer：在事件内重置详情态（含上次残留）并记录节点 ID，
   * 拉取交给下方 effect（effect 内不做同步 setState）。 */
  function openTag(tagId: string) {
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setSelectedTagId(tagId)
  }

  // selectedTagId 变化即拉取节点详情（目录或标签通用，仅异步置 state）
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

  /** TreeSelect 目录懒加载：只拉目录（kind=dir），标签不进层级选择器。 */
  async function loadDirectoryChildren(node: { value?: string | number }) {
    if (typeof node.value !== 'string') return
    const parentId = node.value
    const res = await fetchTagCatalog({ parent_id: parentId, kind: 'dir' })
    setDirectoryItems((current) => updateDirectoryChildren(current, parentId, res.items))
  }

  function updateDirectoryChildren(
    nodes: TagTreeNode[],
    parentId: string,
    children: TagTreeNode[],
  ): TagTreeNode[] {
    return nodes.map((node) => {
      if (node.id === parentId) {
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

  function confirmDeleteTag(tag: TagNodeDetail) {
    Modal.confirm({
      title: '删除标签',
      content: `确定删除标签“${tag.name}”吗？如果目录为空，空目录也会被删除。`,
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setDeleting(true)
        try {
          const result = await deleteTag(tag.id)
          setSelectedTagId(null)
          selectDirectory(result.parent_id ?? undefined, result.parent_name ?? undefined)
          await loadCatalog()
        } catch (err) {
          setDetailError(err instanceof Error ? err.message : '删除标签失败')
        } finally {
          setDeleting(false)
        }
      },
    })
  }

  function findDirectory(nodes: TagTreeNode[], id: string): TagTreeNode | undefined {
    for (const node of nodes) {
      if (node.id === id) return node
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
    selectDirectory(parent?.id, parent?.name)
  }

  async function expandSelectedDirectory(record: TagTreeNode) {
    if (record.has_children !== true) return
    setExpandedRowKeys((current) =>
      current.includes(record.id)
        ? current.filter((key) => key !== record.id)
        : [...current, record.id],
    )
    if (record.children !== undefined) return
    try {
      const res = await fetchTagCatalog({ parent_id: record.id })
      setItems((current) => updateDirectoryChildren(current, record.id, res.items))
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '下级标签加载失败')
    }
  }

  function toTreeData(nodes: TagTreeNode[]): Array<{
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
      .filter((node) => node.kind === 'dir' && node.has_children === true)
      .map((node) => ({
      title: node.name,
      value: node.id,
      children: node.children ? toTreeData(node.children) : undefined,
      isLeaf: false,
      }))
  }

  const columns: TableProps<TagTreeNode>['columns'] = [
    {
      title: '名称',
      dataIndex: 'name',
      render: (name: string, record: TagTreeNode) => (
        record.id === '__back__' ? (
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
              background: record.kind === 'dir' ? '#e6f4ff' : '#f6ffed',
              color: record.kind === 'dir' ? '#1677ff' : '#389e0d',
              fontSize: 12,
              lineHeight: '22px',
            }}
          >
            {record.kind === 'dir' ? '目录' : '标签'}
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

  const displayItems: TagTreeNode[] = scopeId
    ? [
        {
          id: '__back__',
          name: '返回上级目录',
          kind: 'tag',
          dir_type: null,
          description: '',
          parent_id: null,
          status: null,
          has_children: false,
          aliases: [],
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
              ...(scopeId && scopeLabel && !directoryItems.some((item) => item.id === scopeId)
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
        <Table<TagTreeNode>
          rowKey="id"
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
              if (record.id === '__back__') {
                goToParentDirectory()
              } else if (record.has_children === true) {
                void expandSelectedDirectory(record)
              } else {
                openTag(record.id)
              }
            },
            style: { cursor: 'pointer' },
          })}
          pagination={false}
        />
      </div>
      <Drawer
        title="节点详情"
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
          <>
          <Descriptions
            column={1}
            items={[
              {
                key: 'kind',
                label: '种类',
                children: (
                  <Tag color={detail.kind === 'dir' ? 'blue' : 'green'}>
                    {detail.kind === 'dir' ? '目录' : '标签'}
                  </Tag>
                ),
              },
              { key: 'id', label: 'ID', children: detail.id },
              {
                key: 'name',
                label: '名称',
                children: (
                  <Space size={4} wrap>
                    {(detail.path ?? [detail.name]).map((name, index) => {
                      const isSelf = index === (detail.path?.length ?? 1) - 1
                      const directoryId = detail.path_ids?.[index]
                      return isSelf || directoryId === undefined ? (
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
          {/* 生命周期操作仅对标签渲染：目录节点 status 恒 null 且后端对目录
              的生命周期操作一律 404，前端直接不显示入口。 */}
          {detail.kind === 'tag' && (
            <>
            <Button
              danger={detail.status === 'active'}
              loading={statusUpdating}
              onClick={async () => {
                setStatusUpdating(true)
                try {
                  const next = detail.status === 'deprecated'
                    ? await activateTag(detail.id)
                    : await deactivateTag(detail.id)
                  setDetail(next)
                } catch (err) {
                  setDetailError(err instanceof Error ? err.message : '状态更新失败')
                } finally {
                  setStatusUpdating(false)
                }
              }}
            >
              {detail.status === 'deprecated' ? '启用' : '停用'}
            </Button>
            <Button
              danger
              loading={deleting}
              onClick={() => confirmDeleteTag(detail)}
            >
              删除
            </Button>
            </>
          )}
          </>
        )}
      </Drawer>
    </Card>
  )
}
