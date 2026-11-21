// 内容资产列表页面骨架（Task 15 拆分后）：按资产类型分组展示（电影/剧集/
// 场景），分组可折叠且默认折叠；标题搜索（回车/按钮触发），行点击进入详情
// 页 /assets/{asset_id}。数据来自 GET /assets（q 标题子串匹配）。
// 场景行标题就是场景描述（很长），列表里截断预览，点进详情再看全文
// （AssetDetail 展示完整描述）。
// 三个 Modal 职责已拆到同目录子组件，本页只持有各自的开关 state：
// - AssetUploadWizard：上传视频向导（/media/match 预检 + XHR 传输进度 +
//   生成进度轮询视图 AssetProgressPolling）；
// - AssetDirImportModal：从目录导入（浏览服务器目录、勾选注册为资产）。
import {
  Alert,
  Button,
  Card,
  Collapse,
  Empty,
  Space,
  Table,
  Tag,
  Input,
  Typography,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAssets, type AssetSummary } from '../../api/assets'
import AssetDirImportModal from './AssetDirImportModal'
import AssetUploadWizard from './AssetUploadWizard'
import { formatTimestamp } from '../../api/review'
import { formatTime } from '../../api/time'

/** 资产类型中文展示（取值域与内容 JSON 契约的 Literal 一致）。 */
const ASSET_TYPE_LABELS: Record<string, string> = {
  movie: '电影',
  series: '剧集',
  scene: '场景',
}

/** 分组展示顺序（电影 → 剧集 → 场景；未知类型排最后）。 */
const ASSET_TYPE_ORDER = ['movie', 'series', 'scene']

/** 场景描述在列表中的预览长度：超出截断，点进详情看全文。 */
const SCENE_TITLE_PREVIEW = 60

/** 超长文本截断（场景描述预览用）。 */
function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

export default function AssetList() {
  const navigate = useNavigate()
  const [assets, setAssets] = useState<AssetSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  // —— 搜索状态：提交（回车/按钮）时才生效 ——
  const [searchQ, setSearchQ] = useState('')
  const [inputQ, setInputQ] = useState('')

  // —— 两个 Modal 的开关（会话状态都在子组件内部） ——
  const [uploadOpen, setUploadOpen] = useState(false)
  const [dirOpen, setDirOpen] = useState(false)

  const loadAssets = useCallback(async () => {
    setLoading(true)
    try {
      const rows = await fetchAssets({ q: searchQ })
      setAssets(rows)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '资产列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [searchQ])

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

  /** 按类型分组（固定顺序，未知类型殿后）。 */
  const groups: { type: string; rows: AssetSummary[] }[] = []
  for (const type of ASSET_TYPE_ORDER) {
    const rows = assets.filter((a) => a.asset_type === type)
    if (rows.length > 0) groups.push({ type, rows })
  }
  const extraRows = assets.filter((a) => !ASSET_TYPE_ORDER.includes(a.asset_type))
  if (extraRows.length > 0) groups.push({ type: 'other', rows: extraRows })

  const columns: TableProps<AssetSummary>['columns'] = [
    {
      title: '标题',
      dataIndex: 'title',
      render: (v: string, record) =>
        record.asset_type === 'scene' ? (
          <Typography.Text>{truncate(v, SCENE_TITLE_PREVIEW)}</Typography.Text>
        ) : (
          v
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
      title: '内容',
      dataIndex: 'has_content',
      render: (v: boolean) =>
        v ? <Tag color="green">已生成</Tag> : <Tag>未生成</Tag>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      render: formatTime,
    },
  ]

  return (
    <Card
      title="内容资产"
      extra={
        <Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            文件名需含 IMDb ID（如 tt0068646）或英文片名（如 The.Godfather.1972）
          </Typography.Text>
          <Button type="primary" onClick={() => setUploadOpen(true)}>
            上传视频
          </Button>
          <Button onClick={() => setDirOpen(true)}>从目录导入</Button>
        </Space>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <Space wrap>
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
        {assets.length === 0 && !loading ? (
          <Empty description="暂无资产" />
        ) : (
          <Collapse
            defaultActiveKey={[]}
            items={groups.map((group) => ({
              key: group.type,
              label: (
                <Space>
                  <Typography.Text strong>
                    {ASSET_TYPE_LABELS[group.type] ?? group.type}
                  </Typography.Text>
                  <Tag>{`${group.rows.length} 条`}</Tag>
                </Space>
              ),
              children: (
                <Table<AssetSummary>
                  rowKey="asset_id"
                  columns={columns}
                  dataSource={group.rows}
                  loading={loading}
                  onRow={(record) => ({
                    onClick: () => navigate(`/assets/${record.asset_id}`),
                    style: { cursor: 'pointer' },
                  })}
                  pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
                  size="small"
                />
              ),
            }))}
          />
        )}
      </div>
      <AssetUploadWizard
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        refreshAssets={loadAssets}
        onOpenAsset={(assetId) => navigate(`/assets/${assetId}`)}
      />
      <AssetDirImportModal
        open={dirOpen}
        onClose={() => setDirOpen(false)}
        refreshAssets={loadAssets}
      />
    </Card>
  )
}
