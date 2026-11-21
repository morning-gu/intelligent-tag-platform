// 上传视频向导（Task 15 从 AssetList 拆出）：Modal 单文件选择 → 先 GET
// /media/match 预检（命中影片且已有资产时问用户"是否重新生成"，不确认就不
// 放行）→ POST /media/upload（multipart FormData，XHR 拿真实传输百分比）。
// 文件名命中数据集影片时后端返回 build_task_id，Modal 内切换为生成进度视图
// （轮询见 AssetProgressPolling），完成后由父组件刷新列表；未命中则按普通
// 占位资产登记。
import {
  Alert,
  Button,
  Checkbox,
  Modal,
  Progress,
  Typography,
  Upload,
  message,
} from 'antd'
import { useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import {
  matchMedia,
  uploadMedia,
  type MediaMatchResult,
  type MediaUploadResult,
} from '../../api/media'
import AssetProgressPolling from './AssetProgressPolling'

/** 上传向导 props：open 由父级持有（工具栏按钮触发）；数据变更后由父级刷新列表。 */
interface AssetUploadWizardProps {
  /** Modal 显隐（父级持有开关，本组件只负责上传会话状态的复位） */
  open: boolean
  /** 请求关闭 Modal（父级把 open 置 false） */
  onClose: () => void
  /** 资产数据变更后刷新列表（占位登记成功 / 生成完成） */
  refreshAssets: () => void
  /** 在详情路由打开指定资产（预检提示"打开已有资产"用） */
  onOpenAsset: (assetId: string) => void
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

export default function AssetUploadWizard({
  open,
  onClose,
  refreshAssets,
  onOpenAsset,
}: AssetUploadWizardProps) {
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [buildTaskId, setBuildTaskId] = useState<string | null>(null)
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

  // 上传会话状态复位（根治"任一上传状态未复位即跨会话残留"）：原来
  // openUpload/closeUpload 各写一份复位序列、靠人工保持同步，漏一项就残留。
  // 现在收敛为这份唯一序列，在 open 的每次变化（关闭路径 + 打开路径）都
  // 完整执行一遍——打开路径再兜一次底，防住"关闭后才落地的异步回写"
  // （如预检响应晚到）在下次打开时露出旧内容。
  useEffect(() => {
    setUploadFile(null)
    setUploading(false)
    setBuildTaskId(null)
    setMatchedTitle(null)
    setPreflight(null)
    setCheckingFile(false)
    setPreflightError(null)
    setConfirmRegenerate(false)
    setUploadPct(0)
    setUseLlm(true)
  }, [open])

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
      onClose()
      await refreshAssets()
    } catch (err) {
      // 413 按状态码识别（ApiError.status；media.ts 上传走 XHR 同样抛
      // ApiError）——不再对 message 做 "413" 子串匹配（后端 detail 或
      // 网关 HTML 里的无关数字会误触发），统一给出可行动文案
      if (err instanceof ApiError && err.status === 413) {
        message.error('文件超过 2GB 上传上限')
      } else {
        message.error(err instanceof Error ? err.message : '上传失败')
      }
    } finally {
      setUploading(false)
    }
  }

  // 已有资产、且用户还没确认重新生成：挡在上传之前（避免白传一遍视频）
  const duplicateHit =
    preflight !== null && preflight.matched && preflight.has_content
  // 文件名认不出影片（按资产标题兜底命中）时没法重建：只能提示改名
  const canRebuild = preflight === null || preflight.can_rebuild !== false
  const blockedByExisting = duplicateHit && !confirmRegenerate

  return (
    <Modal
      title={buildTaskId === null ? '上传视频' : '资产生成进度'}
      open={open}
      okText="开始上传"
      cancelText="取消"
      okButtonProps={{ disabled: uploadFile === null || checkingFile || blockedByExisting }}
      confirmLoading={uploading}
      onOk={buildTaskId === null ? handleUploadSubmit : undefined}
      onCancel={onClose}
      footer={
        buildTaskId === null
          ? undefined
          : [
              <Button key="close" type="primary" onClick={onClose}>
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
                          onOpenAsset(preflight.asset_id)
                        }
                        onClose()
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
        <AssetProgressPolling
          buildTaskId={buildTaskId}
          matchedTitle={matchedTitle}
          onSucceeded={refreshAssets}
        />
      )}
    </Modal>
  )
}
