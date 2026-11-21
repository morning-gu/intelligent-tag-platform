// 资产生成进度视图（Task 15 从 AssetList 拆出）：2s 一次拉
// /media/build-tasks/{id}，终态（succeeded/failed）停止；成功后回调父级刷新
// 列表。轮询定时器的启动/清理随本组件生命周期走：buildTaskId 变化或组件
// 卸载时清理 timer，不泄漏。
import { Alert, Progress, Typography, message } from 'antd'
import { useEffect, useState } from 'react'
import { fetchBuildTask, type BuildTask } from '../../api/media'

const BUILD_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '生成中',
  succeeded: '已完成',
  failed: '失败',
}

/** 生成进度视图 props：buildTaskId 非空才轮询；成功后触发 onSucceeded。 */
interface AssetProgressPollingProps {
  /** 后台生成任务 id（父级只在非空时挂载本组件；null 不轮询） */
  buildTaskId: string | null
  /** 上传响应命中的影片标题（null 显示通用文案） */
  matchedTitle: string | null
  /** 任务 succeeded 后触发（父级刷新资产列表） */
  onSucceeded: () => void
}

export default function AssetProgressPolling({
  buildTaskId,
  matchedTitle,
  onSucceeded,
}: AssetProgressPollingProps) {
  const [buildTask, setBuildTask] = useState<BuildTask | null>(null)

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
          void onSucceeded()
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
  }, [buildTaskId, onSucceeded])

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
  )
}
