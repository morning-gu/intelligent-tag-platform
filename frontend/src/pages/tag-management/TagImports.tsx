import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Drawer,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  Upload,
  message,
  Modal,
} from 'antd'
import type { TableProps } from 'antd'
import { useEffect, useState, useSyncExternalStore, type ReactElement } from 'react'
import {
  fetchImportTask,
  fetchImportTasks,
  downloadImportTask,
  importTags,
  revokeImportTask,
  type ImportSummary,
  type ImportTaskDetail,
  type ImportTaskSummary,
} from '../../api/imports'
import { formatTime } from '../../api/time'

// 上传请求跨页面切换继续，重新进入页面时仍能读取上传状态。
let uploadState = { uploading: false, finishedUploads: 0 }
const uploadListeners = new Set<() => void>()
function subscribeUpload(listener: () => void) {
  uploadListeners.add(listener)
  return () => { uploadListeners.delete(listener) }
}
function setUploadingFile(uploading: boolean) {
  uploadState = {
    uploading,
    finishedUploads: uploading ? uploadState.finishedUploads : uploadState.finishedUploads + 1,
  }
  uploadListeners.forEach((listener) => listener())
}
function getUploadState() {
  return uploadState
}

/** 任务状态中文展示与 Tag 颜色（键集与后端 ImportTask.status 一致）。 */
const BATCH_STATUS_LABELS: Record<string, string> = {
  processing: '处理中',
  completed: '已完成',
  revoked: '已撤销',
  failed: '已失败',
}
const BATCH_STATUS_COLORS: Record<string, string> = {
  processing: 'orange',
  completed: 'green',
  failed: 'red',
}

/** 结果摘要视图：total/inserted/skipped 统计 + 治理问题/解析失败折叠明细表。
 * 口径注记（v2）：total 仅计标签定义行（目录行落 tag_catalog 不进计数），
 * inserted 为其中新入库数，skipped = total - inserted。
 * 空明细的折叠面板不渲染。 */
function SummaryView({ summary }: { summary: ImportSummary }) {
  // 折叠面板按需构建（空明细不渲染面板，children 惰性渲染进表格）
  const panels: { key: string; label: string; children: ReactElement }[] = []
  if (summary.issues.length > 0) {
    panels.push({
      key: 'issues',
      label: `治理问题（${summary.issues.length}）`,
      children: (
        <Table<ImportSummary['issues'][number]>
          rowKey={(r) => `${r.tag_name}-${r.code}-${r.detail}`}
          size="small"
          columns={[
            { title: '标签名', dataIndex: 'tag_name' },
            { title: '问题码', dataIndex: 'code' },
            { title: '明细', dataIndex: 'detail' },
          ]}
          dataSource={summary.issues}
          pagination={false}
        />
      ),
    })
  }
  if (summary.parse_errors.length > 0) {
    panels.push({
      key: 'parse_errors',
      label: `解析失败（${summary.parse_errors.length}）`,
      children: (
        <Table<ImportSummary['parse_errors'][number]>
          rowKey={(r) => `${r.row_number}-${r.code}-${r.detail}`}
          size="small"
          columns={[
            { title: '行号', dataIndex: 'row_number', width: 80 },
            {
              title: '标签名',
              dataIndex: 'tag_name',
              render: (v: string | null) => v ?? '—',
            },
            { title: '问题码', dataIndex: 'code' },
            { title: '明细', dataIndex: 'detail' },
          ]}
          dataSource={summary.parse_errors}
          pagination={false}
        />
      ),
    })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card size="small">
        <Space size="large">
          <Statistic title="传入标签行数" value={summary.total} />
          <Statistic title="新增入库" value={summary.inserted} />
          <Statistic title="跳过" value={summary.skipped} />
        </Space>
      </Card>
      {panels.length === 0 ? (
        <Typography.Text type="success">本批无治理问题与解析失败</Typography.Text>
      ) : (
        // 有明细的面板默认展开（导入后即可见问题），仍可手工折叠
        <Collapse defaultActiveKey={panels.map((p) => p.key)} items={panels} />
      )}
    </div>
  )
}

export default function TagImports() {
  // —— 任务历史 ——
  const [tasks, setTasks] = useState<ImportTaskSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [refresh, setRefresh] = useState(0)

  const { uploading, finishedUploads } = useSyncExternalStore(subscribeUpload, getUploadState)

  // —— 任务详情 Drawer ——
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ImportTaskDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [revoking, setRevoking] = useState(false)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function loadTasks() {
      let poll = true
      try {
        const rows = await fetchImportTasks()
        if (cancelled) return
        setTasks(rows)
        setLoadError(null)
        poll = rows.some((task) => task.status === 'processing')
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : '任务历史加载失败')
      } finally {
        if (!cancelled) {
          setLoading(false)
          if (poll) timer = setTimeout(() => void loadTasks(), 2000)
        }
      }
    }
    void loadTasks()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [refresh, finishedUploads])

  /** 打开任务详情 Drawer：在事件内重置详情态（含上次残留）并记录任务号，
   * 拉取交给下方 effect（effect 内不做同步 setState）。 */
  function openTask(taskId: string) {
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setSelectedTaskId(taskId)
  }

  const selectedTaskStatus = tasks.find((task) => task.task_id === selectedTaskId)?.status
  useEffect(() => {
    if (selectedTaskId === null) return
    let cancelled = false
    fetchImportTask(selectedTaskId).then(
      (d) => {
        if (!cancelled) {
          setDetail(d)
          setDetailError(null)
          setDetailLoading(false)
        }
      },
      (err) => {
        if (!cancelled) {
          setDetailError(err instanceof Error ? err.message : '任务详情加载失败')
          setDetailLoading(false)
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [selectedTaskId, selectedTaskStatus])

  /** 手动上传：beforeUpload 返回 false 阻断 antd 自带 xhr，改经 apiFetch
   * （统一会话 cookie 与 401 拦截）。服务端接收后刷新任务历史。 */
  async function handleUpload(file: File) {
    if (uploadState.uploading) return
    setUploadingFile(true)
    try {
      const task = await importTags(file)
      if (task.status === 'failed') {
        message.error('导入启动失败，请在任务详情中查看原因')
      } else {
        message.success('已创建导入任务')
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '导入失败')
    } finally {
      setUploadingFile(false)
    }
  }

  function saveBlob(blob: Blob, fileName: string) {
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = fileName
      link.click()
      URL.revokeObjectURL(url)
    }

  function confirmRevoke(task: ImportTaskDetail) {
      Modal.confirm({
        title: '撤销导入任务',
        content: '撤销后将删除本任务导入的标签，是否先导出 Excel 和 XML 文件？',
        okText: '导出并撤销',
        cancelText: '取消',
        onOk: async () => {
          setRevoking(true)
          try {
            const blob = await downloadImportTask(task.task_id)
            saveBlob(blob, `${task.task_id}-export.zip`)
            await revokeImportTask(task.task_id)
            message.success('任务已撤销')
            setDetail((current) => current ? { ...current, status: 'revoked' } : current)
            setRefresh((current) => current + 1)
          } catch (err) {
            message.error(err instanceof Error ? err.message : '撤销任务失败')
          } finally {
            setRevoking(false)
          }
        },
        footer: (_, { OkBtn, CancelBtn }) => (
          <Space>
            <CancelBtn />
            <Button
              onClick={() => {
                Modal.destroyAll()
                Modal.confirm({
                  title: '确认直接撤销',
                  content: '确定不导出文件并撤销该任务吗？',
                  okText: '直接撤销',
                  cancelText: '取消',
                  onOk: async () => {
                    setRevoking(true)
                    try {
                      await revokeImportTask(task.task_id)
                      message.success('任务已撤销')
                      setDetail((current) => current ? { ...current, status: 'revoked' } : current)
                      setRefresh((current) => current + 1)
                    } catch (err) {
                      message.error(err instanceof Error ? err.message : '撤销任务失败')
                    } finally {
                      setRevoking(false)
                    }
                  },
                })
              }}
            >
              直接撤销
            </Button>
            <OkBtn />
          </Space>
        ),
      })
  }

  const columns: TableProps<ImportTaskSummary>['columns'] = [
    { title: '任务号', dataIndex: 'task_id' },
    { title: '文件名', dataIndex: 'file_name' },
    {
      title: '状态',
      dataIndex: 'status',
      render: (v: string) => (
        <Tag color={BATCH_STATUS_COLORS[v] ?? 'default'}>{BATCH_STATUS_LABELS[v] ?? v}</Tag>
      ),
    },
    { title: '创建时间', dataIndex: 'created_at', render: formatTime },
    {
      title: '操作',
      key: 'actions',
      render: (_, task) => (
        <Button type="link" size="small" onClick={() => openTask(task.task_id)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="上传标签文件">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <Upload
            accept=".xlsx,.xml"
            maxCount={1}
            showUploadList={false}
            disabled={uploading}
            beforeUpload={(file) => {
              void handleUpload(file)
              return false // 阻断 antd 自带上传，改由 handleUpload 走 apiFetch
            }}
          >
            <Button type="primary" loading={uploading}>
              选择 Excel/XML 文件上传
            </Button>
          </Upload>
        </div>
      </Card>
      <Card title="任务历史">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {loadError !== null && (
            <Alert type="error" showIcon message="任务历史加载失败" description={loadError} />
          )}
          <Table<ImportTaskSummary>
            rowKey="task_id"
            columns={columns}
            dataSource={tasks}
            loading={loading}
            pagination={{ showTotal: (t) => `共 ${t} 条` }}
          />
        </div>
      </Card>
      <Drawer
        title={`任务详情：${selectedTaskId ?? ''}`}
        open={selectedTaskId !== null}
        width={640}
        destroyOnHidden
        onClose={() => setSelectedTaskId(null)}
      >
        {detailError !== null && (
          <Alert type="error" showIcon message="任务详情加载失败" description={detailError} />
        )}
        {detailLoading && <Typography.Text type="secondary">详情加载中…</Typography.Text>}
        {detail !== null && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <Descriptions
              size="small"
              column={2}
              items={[
                { key: 'task', label: '任务号', children: detail.task_id },
                { key: 'file', label: '文件名', children: detail.file_name },
                {
                  key: 'status',
                  label: '状态',
                  children: (
                    <Tag color={BATCH_STATUS_COLORS[detail.status] ?? 'default'}>
                      {BATCH_STATUS_LABELS[detail.status] ?? detail.status}
                    </Tag>
                  ),
                },
                { key: 'created', label: '创建时间', children: formatTime(detail.created_at) },
              ]}
            />
            {detail.status === 'failed' && (
              <Alert type="error" showIcon message={detail.error ?? '导入处理失败'} />
            )}
            {detail.summary && <SummaryView summary={detail.summary} />}
            {detail.summary === null && detail.status !== 'failed' && (
              <Typography.Text type="secondary">
                {detail.status === 'processing' ? '导入任务正在后台处理' : '无结果摘要'}
              </Typography.Text>
            )}
            <Button
              danger
              loading={revoking}
              disabled={detail.status !== 'completed'}
              onClick={() => confirmRevoke(detail)}
            >
              撤销
            </Button>
          </div>
        )}
      </Drawer>
    </div>
  )
}
