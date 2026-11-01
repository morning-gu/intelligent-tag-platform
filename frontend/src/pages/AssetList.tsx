// 内容资产列表（Task 11）：轻量资产表格——类型筛选（movie/series/scene）
// 与标题搜索（回车/按钮触发），行点击进入详情页 /assets/{asset_id}。
// 数据来自 GET /assets（asset_type 精确过滤、q 标题子串匹配），一次性
// 返回全量（无分页契约），表格用 antd 默认前端分页。
// 上传入口（阶段 4 任务 5，入口 A 占位）：顶部"上传视频"按钮 → Modal 单文件
// 选择 → POST /media/upload（multipart FormData）登记资产，成功后刷新列表。
// 占位语义：仅存文件 + 建资产行，不做识别。列表行形状（AssetSummary）无
// 识别状态字段（context 仅存在于详情接口），故不做"未识别"假 Tag——
// 以成功提示与 Modal 内说明承载占位语义（比弱信号推导更诚实）。
import {
  Alert,
  Button,
  Card,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Input,
  Typography,
  Upload,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAssets, type AssetSummary } from '../api/assets'
import { uploadMedia } from '../api/media'
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

  // —— 上传占位（入口 A）：Modal 开关 + 已选文件 + 提交中 ——
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)

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

  /** 打开上传 Modal：清掉上次残留的已选文件。 */
  function openUpload() {
    setUploadFile(null)
    setUploadOpen(true)
  }

  /** 提交上传：FormData（file 字段）经 api client POST。成功提示占位语义
   * （仅登记未识别，指引入口 B）并刷新列表使新资产行出现；失败透出后端
   * detail（422 非白名单后缀 / 413 超 2GB），保留 Modal 与已选文件供直接
   * 重试。nginx 真实环境的 413 返回 HTML（apiFetch 拿不到 detail，回落
   * "请求失败（HTTP 413）"），按 413 字样特判为 2GB 上限提示；后端 JSON
   * 413 的 detail 不含 413 字样，原样透出。 */
  async function handleUploadSubmit() {
    if (uploadFile === null) return
    setUploading(true)
    try {
      const res = await uploadMedia(uploadFile)
      message.success(`已登记「${res.title}」，未导入识别内容——请经入口 B 导入识别 JSON 后打标`)
      setUploadOpen(false)
      setUploadFile(null)
      await loadAssets() // 刷新列表使新登记的资产行出现
    } catch (err) {
      const msg = err instanceof Error ? err.message : '上传失败'
      message.error(msg.includes('413') ? '文件超过 2GB 上传上限' : msg)
    } finally {
      setUploading(false)
    }
  }

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
    <Card
      title="内容资产"
      extra={
        <Button type="primary" onClick={openUpload}>
          上传视频
        </Button>
      }
    >
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
      <Modal
        title="上传视频"
        open={uploadOpen}
        okText="开始上传"
        cancelText="取消"
        okButtonProps={{ disabled: uploadFile === null }}
        confirmLoading={uploading}
        onOk={handleUploadSubmit}
        onCancel={() => setUploadOpen(false)}
        destroyOnHidden
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* 手动上传：beforeUpload 返回 false 阻断 antd 自带 xhr（此时文件
              也不进内建列表），改由确认按钮经 apiFetch 提交；已选文件以
              "已选择：文件名" 自绘展示，重复选择即替换。 */}
          <Upload
            accept=".mp4,.webm,.mkv"
            maxCount={1}
            showUploadList={false}
            disabled={uploading}
            beforeUpload={(file) => {
              setUploadFile(file)
              return false
            }}
          >
            <Button>选择视频文件</Button>
          </Upload>
          {uploadFile !== null && <Typography.Text>已选择：{uploadFile.name}</Typography.Text>}
          <Typography.Text type="secondary">
            占位上传：仅保存视频文件并登记资产，不做识别。识别内容请经入口 B 导入识别 JSON
            后再行打标。
          </Typography.Text>
        </div>
      </Modal>
    </Card>
  )
}
