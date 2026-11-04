// 内容资产列表（Task 11）：按资产类型分组展示（电影/剧集/场景），分组
// 可折叠且默认折叠；标题搜索（回车/按钮触发），行点击进入详情页
// /assets/{asset_id}。数据来自 GET /assets（q 标题子串匹配）。
// 场景行标题就是场景描述（很长），列表里截断预览，点进详情再看全文
// （AssetDetail 展示完整描述）。
// 上传入口：顶部"上传视频"按钮 → Modal 单文件选择 → POST /media/upload
// （multipart FormData）。文件名命中数据集影片时后端返回 build_task_id，
// Modal 内切换为生成进度视图（轮询 /media/build-tasks/{id}），完成后刷新
// 列表；未命中则按普通占位资产登记。
import {
  Alert,
  Button,
  Card,
  Collapse,
  Empty,
  Modal,
  Progress,
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
import {
  fetchBuildTask,
  uploadMedia,
  type BuildTask,
  type MediaUploadResult,
} from '../api/media'
import { formatTimestamp } from '../api/review'

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

const BUILD_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  failed: '失败',
}

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

  // —— 上传 / 生成进度：Modal 开关 + 已选文件 + 提交中 + 后台任务 ——
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [buildTaskId, setBuildTaskId] = useState<string | null>(null)
  const [buildTask, setBuildTask] = useState<BuildTask | null>(null)
  const [matchedTitle, setMatchedTitle] = useState<string | null>(null)

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

  // 生成进度轮询：2s 一次，终态（succeeded/failed）停止；成功后刷新列表。
  useEffect(() => {
    if (buildTaskId === null) return
    let cancelled = false
    let timer: number | undefined
    const tick = async () => {
      try {
        const task = await fetchBuildTask(buildTaskId)
        if (cancelled) return
        setBuildTask(task)
        if (task.status === 'succeeded') {
          message.success('资产生成完成')
          void loadAssets()
          return
        }
        if (task.status === 'failed') {
          message.error(`生成失败：${task.error ?? '未知错误'}`)
          return
        }
      } catch (err) {
        if (!cancelled) {
          message.error(err instanceof Error ? err.message : '进度查询失败')
        }
        return
      }
      if (!cancelled) timer = window.setTimeout(() => void tick(), 2000)
    }
    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [buildTaskId, loadAssets])

  /** 打开上传 Modal：清掉上次残留的文件与进度。 */
  function openUpload() {
    setUploadFile(null)
    setBuildTaskId(null)
    setBuildTask(null)
    setMatchedTitle(null)
    setUploadOpen(true)
  }

  /** 关闭并复位上传 Modal。 */
  function closeUpload() {
    setUploadOpen(false)
    setUploadFile(null)
    setBuildTaskId(null)
    setBuildTask(null)
    setMatchedTitle(null)
  }

  /** 提交上传：命中数据集 → Modal 内展示生成进度；未命中 → 占位登记。 */
  async function handleUploadSubmit() {
    if (uploadFile === null) return
    setUploading(true)
    try {
      const res: MediaUploadResult = await uploadMedia(uploadFile)
      if (res.build_task_id !== null) {
        setMatchedTitle(res.matched_title ?? res.movie_id)
        setBuildTaskId(res.build_task_id)
        setUploading(false)
        return
      }
      message.success(`已登记「${res.title}」，未匹配到影片——文件名需含 IMDb ID 或英文片名`)
      closeUpload()
      await loadAssets()
    } catch (err) {
      const msg = err instanceof Error ? err.message : '上传失败'
      message.error(msg.includes('413') ? '文件超过 2GB 上传上限' : msg)
    } finally {
      setUploading(false)
    }
  }

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
      title: '创建时间',
      dataIndex: 'created_at',
      render: (v: string | null) => (v === null ? '—' : v.replace('T', ' ').slice(0, 19)),
    },
  ]

  // 进度视图数据
  const progressPct =
    buildTask !== null && buildTask.total_scenes > 0
      ? Math.round((100 * buildTask.done_scenes) / buildTask.total_scenes)
      : 0
  const progressStatus =
    buildTask?.status === 'failed'
      ? 'exception'
      : buildTask?.status === 'succeeded'
        ? 'success'
        : 'active'

  return (
    <Card
      title="内容资产"
      extra={
        <Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            文件名需含 IMDb ID（如 tt0068646）或英文片名（如 The.Godfather.1972）
          </Typography.Text>
          <Button type="primary" onClick={openUpload}>
            上传视频
          </Button>
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
      <Modal
        title={buildTaskId === null ? '上传视频' : '资产生成进度'}
        open={uploadOpen}
        okText="开始上传"
        cancelText="取消"
        okButtonProps={{ disabled: uploadFile === null }}
        confirmLoading={uploading}
        onOk={buildTaskId === null ? handleUploadSubmit : undefined}
        onCancel={closeUpload}
        footer={
          buildTaskId === null
            ? undefined
            : [
                <Button key="close" type="primary" onClick={closeUpload}>
                  关闭
                </Button>,
              ]
        }
        destroyOnHidden
      >
        {buildTaskId === null ? (
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
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <Typography.Text>
              {matchedTitle === null ? '已匹配影片，后台生成中…' : `已匹配影片《${matchedTitle}》`}
            </Typography.Text>
            <div data-testid="build-progress">
              <Progress percent={progressPct} status={progressStatus} />
            </div>
            <Typography.Text type="secondary">
              {buildTask === null
                ? '读取任务状态…'
                : `${BUILD_STATUS_LABELS[buildTask.status] ?? buildTask.status} · ${buildTask.done_scenes}/${buildTask.total_scenes || '?'} 场景`}
            </Typography.Text>
            {buildTask?.status === 'failed' && buildTask.error !== null && (
              <Alert type="error" showIcon message="生成失败" description={buildTask.error} />
            )}
          </div>
        )}
      </Modal>
    </Card>
  )
}
