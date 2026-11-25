// 清空确认弹窗（clear-reset Task 3）：内容/标签两页共用的危险操作确认层。
// 行为契约（spec §4）：打开即拉 preview 计数 → 只渲染非零键清单（键中文名
// 映射 CLEAR_COUNT_LABELS）；Input 输入恰为"清空"才点亮 danger 确认按钮；
// 确认 → execute → message.success（非零键中文摘要）→ onDone → onClose；
// ApiError（含 409 守卫文案）→ 弹窗内 Alert 透出且不关窗；preview 失败 →
// 独立 Alert、描述文案不依赖 preview 仍可确认；onExport 提供时渲染
// "先导出备份"链接（③ 清空标签库用，复用既有标签导出）。
import { Alert, Input, Modal, Space, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import {
  fetchClearPreview,
  type ClearCounts,
  type ClearPreviewScope,
  type ClearResult,
} from '../api/clear'

/** counts 键 → 弹窗计数清单中文名（键序即 ClearCounts 字面序）。 */
export const CLEAR_COUNT_LABELS: Record<keyof ClearCounts, string> = {
  pre_asset_tag: '待审核标签',
  asset_tag: '正式挂载',
  generation_tasks: '生成任务',
  quality_evaluations: '质量评估',
  asset_build_task: '构建任务',
  media_assets: '内容资产',
  scenes: '场景',
  tag_define: '标签定义',
  tag_catalog: '标签目录',
}

/** 确认词：输入恰好等于它才点亮确认按钮（用户拍板：输入"清空"确认够）。 */
const CONFIRM_WORD = '清空'

/** 单个危险操作的装配描述：两页按钮点击时构造、传给本弹窗。 */
export interface ClearOpConfig {
  title: string
  description: string
  previewScope: ClearPreviewScope
  assetId?: string
  execute: () => Promise<ClearResult>
  onDone?: () => Promise<void> | void
  onExport?: () => void
}

interface ClearConfirmModalProps {
  open: boolean
  onClose: () => void
  config: ClearOpConfig | null
}

/** 成功 toast 的非零键中文摘要（空数据 → 无可删除提示）。 */
function summary(counts: ClearCounts): string {
  const rows = Object.entries(counts).filter(([, v]) => v > 0) as Array<
    [keyof ClearCounts, number]
  >
  if (rows.length === 0) return '没有数据需要清空'
  return `已删除：${rows.map(([k, v]) => `${CLEAR_COUNT_LABELS[k]} ${v}`).join('、')}`
}

export default function ClearConfirmModal({
  open,
  onClose,
  config,
}: ClearConfirmModalProps) {
  const [counts, setCounts] = useState<ClearCounts | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [confirmWord, setConfirmWord] = useState('')
  const [executing, setExecuting] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || config === null) return
    setCounts(null)
    setPreviewError(null)
    setConfirmWord('')
    setActionError(null)
    let cancelled = false
    fetchClearPreview(config.previewScope, config.assetId).then(
      (preview) => {
        if (!cancelled) setCounts(preview.counts)
      },
      (err) => {
        if (!cancelled) {
          setPreviewError(
            err instanceof Error ? err.message : '计数预览加载失败')
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [open, config])

  if (config === null || !open) return null

  async function handleOk() {
    if (executing || config === null || confirmWord !== CONFIRM_WORD) return
    setExecuting(true)
    setActionError(null)
    try {
      const result = await config.execute()
      message.success(summary(result.counts))
      await config.onDone?.()
      onClose()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setExecuting(false)
    }
  }

  const rows = counts
    ? (Object.entries(counts).filter(([, v]) => v > 0) as Array<
        [keyof ClearCounts, number]
      >)
    : []

  return (
    <Modal
      open={open}
      title={config.title}
      okText="确认清空"
      okButtonProps={{ danger: true, disabled: confirmWord !== CONFIRM_WORD }}
      cancelText="取消"
      confirmLoading={executing}
      onOk={() => void handleOk()}
      onCancel={onClose}
      destroyOnClose
    >
      <Typography.Paragraph>{config.description}</Typography.Paragraph>
      {previewError !== null && (
        <Alert
          type="warning"
          showIcon
          message="计数预览加载失败"
          description={previewError}
          style={{ marginBottom: 12 }}
        />
      )}
      {counts !== null && rows.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <Typography.Text type="secondary">将删除：</Typography.Text>
          <Space direction="vertical" size={2} style={{ display: 'flex' }}>
            {rows.map(([key, value]) => (
              <Typography.Text key={key}>
                {`${CLEAR_COUNT_LABELS[key]}：${value}`}
              </Typography.Text>
            ))}
          </Space>
        </div>
      )}
      {counts !== null && rows.length === 0 && (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
          当前没有可删除的数据。
        </Typography.Paragraph>
      )}
      {config.onExport !== undefined && (
        <Typography.Link
          onClick={config.onExport}
          style={{ display: 'block', marginBottom: 12 }}
        >
          先导出备份
        </Typography.Link>
      )}
      {actionError !== null && (
        <Alert
          type="error"
          showIcon
          message={actionError}
          style={{ marginBottom: 12 }}
        />
      )}
      <Input
        aria-label="清空确认输入"
        placeholder={`输入“${CONFIRM_WORD}”二字确认`}
        value={confirmWord}
        onChange={(e) => setConfirmWord(e.target.value)}
      />
    </Modal>
  )
}
