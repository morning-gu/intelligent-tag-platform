// 内容资产列表（Task 11）：轻量资产表格——类型筛选（movie/series/scene）
// 与标题搜索（回车/按钮触发），行点击进入详情页 /assets/{asset_id}。
// 数据来自 GET /assets（asset_type 精确过滤、q 标题子串匹配），一次性
// 返回全量（无分页契约），表格用 antd 默认前端分页。
import { Alert, Card, Select, Space, Table, Tag, Input } from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAssets, type AssetSummary } from '../api/assets'
import { formatTimestamp } from '../api/review'

/** 资产类型中文展示（取值域与内容 JSON 契约的 Literal 一致）。 */
const ASSET_TYPE_LABELS: Record<string, string> = {
  movie: '电影',
  series: '剧集',
  scene: '场景',
}

/** 类型筛选固定选项（数据模型固定枚举，无动态端点）。 */
const ASSET_TYPE_OPTIONS = Object.entries(ASSET_TYPE_LABELS).map(([value, label]) => ({
  value,
  label,
}))

export default function AssetList() {
  const navigate = useNavigate()
  const [assets, setAssets] = useState<AssetSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  // —— 筛选状态：类型变更即拉取；标题搜索在提交（回车/按钮）时生效 ——
  const [assetType, setAssetType] = useState<string | undefined>()
  const [searchQ, setSearchQ] = useState('')
  const [inputQ, setInputQ] = useState('')

  const loadAssets = useCallback(async () => {
    setLoading(true)
    try {
      const rows = await fetchAssets({ asset_type: assetType, q: searchQ })
      setAssets(rows)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '资产列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [assetType, searchQ])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void loadAssets()
    })
    return () => {
      cancelled = true
    }
  }, [loadAssets])

  const columns: TableProps<AssetSummary>['columns'] = [
    { title: '标题', dataIndex: 'title' },
    {
      title: '类型',
      dataIndex: 'asset_type',
      render: (v: string) => (
        <Tag color={v === 'movie' ? 'blue' : v === 'series' ? 'purple' : 'green'}>
          {ASSET_TYPE_LABELS[v] ?? v}
        </Tag>
      ),
    },
    {
      title: '时长',
      dataIndex: 'duration_ms',
      render: (v: number | null) => (v === null ? '—' : formatTimestamp(v)),
    },
    {
      title: '视频',
      dataIndex: 'video_uri',
      render: (v: string | null) =>
        v === null || v === '' ? <Tag>无</Tag> : <Tag color="cyan">可播放</Tag>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      render: (v: string | null) => (v === null ? '—' : v.replace('T', ' ').slice(0, 19)),
    },
  ]

  return (
    <Card title="内容资产">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Space wrap>
          <Select
            aria-label="类型筛选"
            value={assetType}
            onChange={(v) => setAssetType(v)}
            options={ASSET_TYPE_OPTIONS}
            allowClear
            placeholder="类型"
            style={{ width: 140 }}
          />
          <Input.Search
            aria-label="标题搜索"
            placeholder="标题搜索"
            enterButton="搜索"
            value={inputQ}
            onChange={(e) => setInputQ(e.target.value)}
            onSearch={(v) => setSearchQ(v)}
            style={{ width: 280 }}
          />
        </Space>
        {loadError !== null && (
          <Alert type="error" showIcon message="资产列表加载失败" description={loadError} />
        )}
        <Table<AssetSummary>
          rowKey="asset_id"
          columns={columns}
          dataSource={assets}
          loading={loading}
          onRow={(record) => ({
            onClick: () => navigate(`/assets/${record.asset_id}`),
            style: { cursor: 'pointer' },
          })}
          pagination={{ showTotal: (t) => `共 ${t} 条` }}
        />
      </div>
    </Card>
  )
}
