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
import {
  browseDirs,
  bulkImportMovieDirs,
  type DirEntry,
  type MovieImportResult,
} from '../api/movieImport'

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
  // 预检本身失败时的提示（不再静默，否则"没跳提示"分不清是没重复还是没查）
  const [preflightError, setPreflightError] = useState<string | null>(null)
  // 用户在提示上确认要重新生成（覆盖旧内容）
  const [confirmRegenerate, setConfirmRegenerate] = useState(false)
  // 浏览器 → 服务器的传输进度（0-100）；100 之后还有服务端落盘，见文案
  const [uploadPct, setUploadPct] = useState(0)
  // 是否让大模型补充资产字段（描述/氛围/事件/说话人），默认打开。
  // 关掉时只落数据集已有的事实：不调模型、快，但氛围/事件/说话人为空。
  const [useLlm, setUseLlm] = useState(true)

  // —— 目录导入：先浏览/选择服务器上的一个目录，再把里面的 content.json 注册成资产 ——
  // 与上传那条路并行：上传是"视频 → 现场生成内容"，这条是"内容已就绪 → 只登记"。
  const [dirOpen, setDirOpen] = useState(false)
  const [dirLoading, setDirLoading] = useState(false)
  // 当前浏览到的目录（绝对路径）+ 它的上级（根目录时为 null）
  const [dirPath, setDirPath] = useState('')
  const [dirParent, setDirParent] = useState<string | null>(null)
  // 路径输入框（可以和当前目录不同，点"打开"才生效）
  const [dirInput, setDirInput] = useState('')
  const [dirEntries, setDirEntries] = useState<DirEntry[]>([])
  const [dirError, setDirError] = useState<string | null>(null)
  // 勾选的影片目录名（默认全选可导入且库里还没有的）
  const [dirPicked, setDirPicked] = useState<string[]>([])
  // 覆盖已存在资产的内容（默认不覆盖，避免冲掉上传那条路产出的内容）
  const [dirOverwrite, setDirOverwrite] = useState(false)
  const [dirImporting, setDirImporting] = useState(false)
  const [dirResult, setDirResult] = useState<MovieImportResult | null>(null)

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
    setPreflightError(null)
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
    setPreflightError(null)
    setConfirmRegenerate(false)
    setUploadPct(0)
    setUseLlm(true)
  }

  /** 选完文件先预检：命中影片且已有资产 → 提示"是否重新生成"。
   *
   * 预检失败不挡上传（比如老后端没有这个接口），但要把失败显式说出来：
   * 静默吞掉会让"重复上传没提示"看起来像功能不存在。 */
  async function runPreflight(file: File) {
    setCheckingFile(true)
    setPreflightError(null)
    try {
      setPreflight(await matchMedia(file.name))
    } catch {
      setPreflight(null)
      setPreflightError('重复检查失败：识别文件名时出错了，仍可继续上传')
    } finally {
      setCheckingFile(false)
    }
  }

  async function checkMediaFile(file: File) {
    setUploadFile(file)
    setPreflight(null)
    setPreflightError(null)
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

  /** 浏览一个目录（缺省落在后端配置的根）：列出子目录 + 里面套着的影片数。 */
  async function loadDir(path?: string | null) {
    setDirLoading(true)
    setDirError(null)
    setDirResult(null)
    try {
      const result = await browseDirs(path ?? null)
      setDirPath(result.path)
      setDirInput(result.path)
      setDirParent(result.parent)
      setDirEntries(result.entries)
      // 默认勾选：这一层里能导入、且库里还没有的影片目录
      setDirPicked(
        result.entries
          .filter((item) => item.is_movie && item.error === null && !item.exists)
          .map((item) => item.name),
      )
    } catch (err) {
      setDirError(err instanceof Error ? err.message : '目录读取失败')
    } finally {
      setDirLoading(false)
    }
  }

  /** 打开目录导入 Modal：先浏览默认根目录，由用户逐层点进目标目录。 */
  async function openDirImport() {
    setDirOpen(true)
    setDirPicked([])
    setDirEntries([])
    setDirOverwrite(false)
    setDirResult(null)
    await loadDir(null)
  }

  /** 执行导入：只提交当前目录下勾选的影片；结果按 imported/skipped/failed 汇总。 */
  async function handleDirImport() {
    if (dirPicked.length === 0) return
    setDirImporting(true)
    setDirError(null)
    try {
      const result = await bulkImportMovieDirs({
        dirNames: dirPicked,
        overwrite: dirOverwrite,
        root: dirPath,
      })
      setDirResult(result)
      if (result.imported > 0) {
        message.success(`已注册 ${result.imported} 部影片资产`)
        await loadAssets()
      }
      // 结果里被导入的会变成"已导入"，重扫这一层让状态与勾选同步
      await loadDir(dirPath)
      setDirResult(result)
    } catch (err) {
      setDirError(err instanceof Error ? err.message : '目录导入失败')
    } finally {
      setDirImporting(false)
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
  const duplicateHit =
    preflight !== null && preflight.matched && preflight.has_content
  // 文件名认不出影片（按资产标题兜底命中）时没法重建：只能提示改名
  const canRebuild = preflight === null || preflight.can_rebuild !== false
  const blockedByExisting = duplicateHit && !confirmRegenerate

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
          <Button onClick={() => void openDirImport()}>从目录导入</Button>
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
                message={
                  canRebuild
                    ? '该视频已有对应资产，是否重新生成？'
                    : '该视频已有对应资产（重复上传会被拒绝）'
                }
                description={
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <span>
                      《{preflight.matched_title ?? preflight.movie_id}》已有资产
                      {preflight.scene_count > 0 ? `，${preflight.scene_count} 个场景` : ''}
                      {preflight.built_at !== null
                        ? `，更新于 ${formatBuiltAt(preflight.built_at)}`
                        : ''}
                      {canRebuild
                        ? '。重新生成会覆盖现有内容；不重新生成就直接打开资产详情查看。'
                        : '。文件名没能识别出是哪部片（需要含 IMDb ID 或英文片名），所以没法在这里重新生成——改名后再选一次，或直接打开资产详情查看。'}
                    </span>
                    {canRebuild ? (
                      <Button size="small" danger onClick={() => setConfirmRegenerate(true)}>
                        重新生成（覆盖现有内容）
                      </Button>
                    ) : (
                      <Button
                        size="small"
                        onClick={() => {
                          if (preflight.asset_id !== null) {
                            navigate(`/assets/${preflight.asset_id}`)
                          }
                          closeUpload()
                        }}
                      >
                        打开已有资产
                      </Button>
                    )}
                  </div>
                }
              />
            )}
            {preflightError !== null && (
              <Alert type="info" showIcon message={preflightError} />
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
      <Modal
        title="从目录导入资产"
        open={dirOpen}
        width={880}
        okText={`导入所选（${dirPicked.length}）`}
        cancelText="关闭"
        okButtonProps={{ disabled: dirPicked.length === 0 || dirImporting }}
        confirmLoading={dirImporting}
        onOk={() => void handleDirImport()}
        onCancel={() => setDirOpen(false)}
        destroyOnHidden
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Typography.Text type="secondary">
            选择服务器上的目录：本层带 content.json 的即影片目录，可勾选直接注册为资产
            （不传视频、不跑生成）；普通文件夹点名称进去继续找。关键帧留在原目录，
            生成阶段按场景直接取用。
          </Typography.Text>

          {/* 路径栏：粘贴绝对路径直接打开，或上一级/进入子目录逐层走 */}
          <Space.Compact style={{ width: '100%' }}>
            <Input
              aria-label="目录路径"
              value={dirInput}
              onChange={(e) => setDirInput(e.target.value)}
              onPressEnter={() => void loadDir(dirInput)}
              placeholder="绝对路径，如 /cache/h50065879/movie_asset/movie_asset"
              disabled={dirLoading || dirImporting}
            />
            <Button onClick={() => void loadDir(dirInput)} disabled={dirLoading || dirImporting}>
              打开
            </Button>
            <Button
              onClick={() => void loadDir(dirParent)}
              disabled={dirParent === null || dirLoading || dirImporting}
            >
              上一级
            </Button>
          </Space.Compact>

          {dirError !== null && <Alert type="error" showIcon message="目录读取失败" description={dirError} />}
          {dirLoading ? (
            <div data-testid="dir-scan-loading">
              <Progress percent={100} status="active" showInfo={false} />
              <Typography.Text type="secondary">正在读取目录…</Typography.Text>
            </div>
          ) : (
            <>
              <Typography.Text>
                当前目录：<Typography.Text code>{dirPath}</Typography.Text>
              </Typography.Text>
              <Table<DirEntry>
                size="small"
                // 行键必须是"目录名"：勾选列表与导入请求里的 dir_names 都是这个名字。
                // 用 path 当行键时，用户点行勾选拿回的是路径，后端按名字找不到 →
                // 静默 0 导入（页面还显示"导入完成"）。
                rowKey="name"
                pagination={{ pageSize: 10, showSizeChanger: false }}
                dataSource={dirEntries}
                rowSelection={{
                  selectedRowKeys: dirPicked,
                  onChange: (keys) => setDirPicked(keys as string[]),
                  getCheckboxProps: (row) => ({
                    disabled:
                      !row.is_movie || row.error !== null || (row.exists && !dirOverwrite),
                  }),
                }}
                columns={[
                  {
                    title: '名称',
                    dataIndex: 'name',
                    render: (_, row) =>
                      row.is_movie ? (
                        <span>{row.name}</span>
                      ) : (
                        <Button
                          type="link"
                          size="small"
                          style={{ padding: 0 }}
                          onClick={() => void loadDir(row.path)}
                        >
                          📁 {row.name}
                        </Button>
                      ),
                  },
                  {
                    title: '类型',
                    width: 150,
                    render: (_, row) =>
                      row.is_movie ? (
                        <Tag color="geekblue">影片目录</Tag>
                      ) : (
                        <Tag>文件夹{row.movies > 0 ? ` · 含 ${row.movies} 部` : ''}</Tag>
                      ),
                  },
                  { title: '场景', dataIndex: 'scenes', width: 70, render: (v, row) => (row.is_movie ? v : '-') },
                  { title: '关键帧', dataIndex: 'frames', width: 80, render: (v, row) => (row.is_movie ? v : '-') },
                  {
                    title: '状态',
                    width: 190,
                    render: (_, row) =>
                      !row.is_movie ? (
                        <Typography.Text type="secondary">—</Typography.Text>
                      ) : row.error !== null ? (
                        <Tag color="red">无法导入</Tag>
                      ) : row.exists ? (
                        <span>
                          <Tag color="blue">已导入</Tag>
                          {row.matched_by !== null && row.matched_by !== 'asset_id' && (
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              {row.matched_by === 'title' ? '按片名匹配' : '按 IMDb 匹配'}
                              {row.existing_asset_id !== null ? ` → ${row.existing_asset_id}` : ''}
                            </Typography.Text>
                          )}
                        </span>
                      ) : (
                        <Tag color="green">可导入</Tag>
                      ),
                  },
                ]}
                expandable={{
                  expandedRowRender: (row) =>
                    row.error !== null ? (
                      <Typography.Text type="danger">{row.error}</Typography.Text>
                    ) : row.is_movie ? (
                      <Typography.Text type="secondary">
                        {row.asset_id ?? '无 asset_id'} · {row.scenes} 个场景 ·{' '}
                        {row.frames} 张关键帧
                        {row.existing_asset_id !== null && row.existing_asset_id !== row.asset_id
                          ? ` · 库里已有同片：${row.existing_asset_id}（${row.matched_by === 'title' ? '按片名' : '按 IMDb'}）`
                          : ''}
                      </Typography.Text>
                    ) : (
                      <Typography.Text type="secondary">
                        {row.path}
                      </Typography.Text>
                    ),
                }}
              />
              <Space wrap>
                <Checkbox
                  checked={dirOverwrite}
                  onChange={(e) => {
                    const next = e.target.checked
                    setDirOverwrite(next)
                    if (!next) {
                      // 取消覆盖时，把"已存在"的行从勾选里剔除
                      setDirPicked((picked) =>
                        picked.filter((name) => {
                          const item = dirEntries.find((i) => i.name === name)
                          return item !== undefined && !item.exists
                        }),
                      )
                    }
                  }}
                >
                  覆盖已导入资产的内容
                </Checkbox>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  已导入的行默认不可选；勾选此项可重导（会覆盖内容，但不动上传的视频地址）
                </Typography.Text>
              </Space>
              {dirResult !== null && (
                <Alert
                  type={
                    dirResult.failed > 0
                      ? 'warning'
                      : dirResult.imported === 0 && dirResult.skipped === 0
                        ? 'warning'
                        : 'success'
                  }
                  showIcon
                  message={
                    dirResult.imported === 0 && dirResult.skipped === 0 && dirResult.failed === 0
                      ? '没有执行任何导入：请确认勾选的行属于"当前目录"'
                      : `导入完成：成功 ${dirResult.imported} · 跳过 ${dirResult.skipped} · 失败 ${dirResult.failed}`
                  }
                  description={
                    dirResult.failed > 0 ? (
                      <div>
                        {dirResult.items
                          .filter((item) => item.status === 'failed')
                          .slice(0, 5)
                          .map((item) => (
                            <div key={item.dir_name}>
                              {item.dir_name}：{item.reason ?? '未知原因'}
                            </div>
                          ))}
                      </div>
                    ) : dirResult.imported === 0 && dirResult.skipped > 0 ? (
                      <div>
                        {dirResult.items
                          .filter((item) => item.status === 'skipped')
                          .slice(0, 5)
                          .map((item) => (
                            <div key={item.dir_name}>
                              {item.dir_name}：{item.reason ?? '已在库内'}
                            </div>
                          ))}
                      </div>
                    ) : undefined
                  }
                />
              )}
              {dirResult === null && dirEntries.length === 0 && (
                <Empty description="这个目录是空的" />
              )}
            </>
          )}
        </div>
      </Modal>
    </Card>
  )
}
