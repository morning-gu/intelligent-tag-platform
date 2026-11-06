// 内容资产列表（Task 11）：按资产类型分组展示（电影/剧集/场景），分组
// 可折叠且默认折叠；标题搜索（回车/按钮触发），行点击进入详情页
// /assets/{asset_id}。数据来自 GET /assets（q 标题子串匹配）。
// 场景行标题就是场景描述（很长），列表里截断预览，点进详情再看全文
// （AssetDetail 展示完整描述）。
// 上传入口：顶部"上传视频"按钮 → Modal 单文件选择 → 先 GET /media/match
// 预检（命中影片且已有资产时问用户"是否重新生成"，不确认就不放行）→
// POST /media/upload（multipart FormData）。文件名命中数据集影片时后端返回
// build_task_id，Modal 内切换为生成进度视图（轮询 /media/build-tasks/{id}），
// 完成后刷新列表；未命中则按普通占位资产登记。
import {
  Alert,
  Button,
  Card,
  Checkbox,
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
  matchMedia,
  uploadMedia,
  type BuildTask,
  type MediaMatchResult,
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

/** 文件大小展示（GB/MB，一位小数）：几百 MB 的片子传多久心里有数。 */
function formatSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`
}

/** 后端 ISO 时间 → `YYYY-MM-DD HH:mm`（预检里"生成于…"用；解析失败原样回显）。 */
function formatBuiltAt(iso: string): string {
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${pad(when.getHours())}:${pad(when.getMinutes())}`
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
  // 选文件预检：命中影片且已有内容时，先问"是否重新生成"
  const [preflight, setPreflight] = useState<MediaMatchResult | null>(null)
  const [checkingFile, setCheckingFile] = useState(false)
  // 用户在提示上确认要重新生成（覆盖旧内容）
  const [confirmRegenerate, setConfirmRegenerate] = useState(false)
  // 浏览器 → 服务器的传输进度（0-100）；100 之后还有服务端落盘，见文案
  const [uploadPct, setUploadPct] = useState(0)
  // 是否让大模型补充资产字段（描述/氛围/事件/说话人），默认打开。
  // 关掉时只落数据集已有的事实：不调模型、快，但氛围/事件/说话人为空。
  const [useLlm, setUseLlm] = useState(true)

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
    setPreflight(null)
    setConfirmRegenerate(false)
    setUploadPct(0)
    setUseLlm(true)
    setUploadOpen(true)
  }

  /** 关闭并复位上传 Modal。 */
  function closeUpload() {
    setUploadOpen(false)
    setUploadFile(null)
    setBuildTaskId(null)
    setBuildTask(null)
    setMatchedTitle(null)
    setPreflight(null)
    setConfirmRegenerate(false)
    setUploadPct(0)
    setUseLlm(true)
  }

  /** 选完文件先预检：命中影片且已有资产 → 提示"是否重新生成"。
   *
   * 预检失败不挡上传（比如老后端没有这个接口），直接按原流程走。 */
  async function runPreflight(file: File) {
    setCheckingFile(true)
    try {
      setPreflight(await matchMedia(file.name))
    } catch {
      setPreflight(null)
    } finally {
      setCheckingFile(false)
    }
  }

  async function checkMediaFile(file: File) {
    setUploadFile(file)
    setPreflight(null)
    setConfirmRegenerate(false)
    setUploadPct(0)
    await runPreflight(file)
  }

  /** 提交上传：命中数据集 → Modal 内展示生成进度；未命中 → 占位登记。 */
  async function handleUploadSubmit() {
    if (uploadFile === null) return
    setUploading(true)
    setUploadPct(0)
    try {
      const res: MediaUploadResult = await uploadMedia(uploadFile, {
        infer: useLlm,
        onProgress: setUploadPct,
      })
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
      title: '内容',
      dataIndex: 'has_content',
      render: (v: boolean) =>
        v ? <Tag color="green">已生成</Tag> : <Tag>未生成</Tag>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      render: (v: string | null) =>
        v === null
          ? '—'
          : v.replace('T', ' ').slice(0, 19),
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

  // 已有资产、且用户还没确认重新生成：挡在上传之前（避免白传一遍视频）
  const blockedByExisting =
    preflight !== null && preflight.matched && preflight.has_content && !confirmRegenerate

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
        okButtonProps={{ disabled: uploadFile === null || checkingFile || blockedByExisting }}
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
            {/* 手动上传：beforeUpload 返回 false 阻断 antd 自带上传（文件也不进
                内建列表），选完先调 /media/match 预检，再由确认按钮经 XHR 提交
                （XHR 只为拿 upload.onprogress 的真实百分比）。 */}
            <Upload
              accept=".mp4,.webm,.mkv"
              maxCount={1}
              showUploadList={false}
              disabled={uploading}
              beforeUpload={(file) => {
                void checkMediaFile(file)
                return false
              }}
            >
              <Button disabled={uploading}>选择视频文件</Button>
            </Upload>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <Checkbox
                checked={useLlm}
                disabled={uploading}
                onChange={(event) => {
                  setUseLlm(event.target.checked)
                }}
              >
                使用大模型补充资产字段
              </Checkbox>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {useLlm
                  ? '逐场景看关键帧生成描述/氛围/事件/说话人（较慢，会调用模型）'
                  : '只填数据集已有的事实（场景描述、字幕、时间），不调用模型'}
              </Typography.Text>
            </div>
            {uploadFile !== null && (
              <Typography.Text>
                已选择：{uploadFile.name}（{formatSize(uploadFile.size)}）
              </Typography.Text>
            )}
            {checkingFile && (
              <Typography.Text type="secondary">正在识别文件名…</Typography.Text>
            )}
            {blockedByExisting && preflight !== null && (
              <Alert
                type="warning"
                showIcon
                message="该视频已有对应资产，是否重新生成？"
                description={
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <span>
                      《{preflight.matched_title ?? preflight.movie_id}》已有资产
                      {preflight.scene_count > 0 ? `，${preflight.scene_count} 个场景` : ''}
                      {preflight.built_at !== null
                        ? `，更新于 ${formatBuiltAt(preflight.built_at)}`
                        : ''}
                      。重新生成会覆盖现有内容；不重新生成就直接打开资产详情查看。
                    </span>
                    <Button size="small" danger onClick={() => setConfirmRegenerate(true)}>
                      重新生成（覆盖现有内容）
                    </Button>
                  </div>
                }
              />
            )}
            {confirmRegenerate && (
              <Alert
                type="info"
                showIcon
                message="将清空该片现有的资产内容，重新跑一遍生成"
              />
            )}
            {uploading && (
              <div
                data-testid="upload-progress"
                style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
              >
                <Progress percent={uploadPct} size="small" status="active" />
                <Typography.Text type="secondary">
                  {uploadPct >= 100
                    ? '上传完成，服务器正在存盘登记…'
                    : `正在上传 ${uploadPct}%`}
                </Typography.Text>
              </div>
            )}
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
