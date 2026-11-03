// 标签导入页（Task 13）：Excel/XML 标签库上传导入 + 批次历史。
// - 上传（AntD Upload 手动模式）：beforeUpload 拦截改由 apiFetch 走
//   POST /tags/import（multipart FormData，client 对 FormData 不强设
//   Content-Type）；成功展示结果摘要（total/inserted/skipped 统计 +
//   issues/parse_errors 折叠明细表），失败（422 坏文件/坏表头）透出后端
//   detail。
// - 批次历史 Table（GET /import/batches，created_at 降序由后端保证）：
//   行"详情"按钮 → 批次详情 Drawer（GET /import/batches/{id}），摘要展示
//   复用同一 SummaryView（result_summary 解析失败时后端为 null，降级提示）。
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
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useState, type ReactElement } from 'react'
import {
  fetchImportBatch,
  fetchImportBatches,
  importTags,
  type ImportBatchDetail,
  type ImportBatchSummary,
  type ImportResponse,
  type ImportSummary,
} from '../api/imports'

/** 批次状态中文展示与 Tag 颜色（键集与后端 ImportBatch.status 一致）。 */
const BATCH_STATUS_LABELS: Record<string, string> = {
  processing: '处理中',
  completed: '已完成',
  failed: '已失败',
}
const BATCH_STATUS_COLORS: Record<string, string> = {
  processing: 'orange',
  completed: 'green',
  failed: 'red',
}

/** ISO 时间 → "YYYY-MM-DD HH:mm:ss"；null → "—"。 */
function formatTime(v: string | null): string {
  return v === null ? '—' : v.replace('T', ' ').slice(0, 19)
}

/** 结果摘要视图：total/inserted/skipped 统计 + 治理问题/解析失败折叠明细表。
 * 上传结果与批次详情两处复用；空明细的折叠面板不渲染（避免空表噪音）。 */
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
            {
              title: '相似标签',
              dataIndex: 'similar_to',
              render: (v: string | null) => v ?? '—',
            },
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
          <Statistic title="传入治理行数" value={summary.total} />
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
  // —— 批次历史 ——
  const [batches, setBatches] = useState<ImportBatchSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  // —— 上传状态与最近一次结果摘要 ——
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState<ImportResponse | null>(null)

  // —— 批次详情 Drawer ——
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ImportBatchDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  const loadBatches = useCallback(async () => {
    setLoading(true)
    try {
      const rows = await fetchImportBatches()
      setBatches(rows)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '批次历史加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void loadBatches()
    })
    return () => {
      cancelled = true
    }
  }, [loadBatches])

  /** 打开批次详情 Drawer：在事件内重置详情态（含上次残留）并记录批次号，
   * 拉取交给下方 effect（effect 内不做同步 setState）。 */
  function openBatch(batchId: string) {
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setSelectedBatchId(batchId)
  }

  // selectedBatchId 变化即拉取批次详情（仅异步置 state）
  useEffect(() => {
    if (selectedBatchId === null) return
    let cancelled = false
    fetchImportBatch(selectedBatchId).then(
      (d) => {
        if (!cancelled) {
          setDetail(d)
          setDetailLoading(false)
        }
      },
      (err) => {
        if (!cancelled) {
          setDetailError(err instanceof Error ? err.message : '批次详情加载失败')
          setDetailLoading(false)
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [selectedBatchId])

  /** 手动上传：beforeUpload 返回 false 阻断 antd 自带 xhr，改经 apiFetch
   * （统一会话 cookie 与 401 拦截）。成功展示摘要并刷新批次历史。 */
  async function handleUpload(file: File) {
    setUploading(true)
    try {
      const res = await importTags(file)
      setUploadResult(res)
      message.success(`导入完成：新增 ${res.summary.inserted} 个标签`)
      await loadBatches()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '导入失败')
    } finally {
      setUploading(false)
    }
  }

  const columns: TableProps<ImportBatchSummary>['columns'] = [
    { title: '批次号', dataIndex: 'batch_id' },
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
      render: (_, batch) => (
        <Button type="link" size="small" onClick={() => openBatch(batch.batch_id)}>
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
          {uploadResult !== null && (
            <div>
              <Typography.Title level={5} style={{ marginTop: 0 }}>
                导入结果（批次 {uploadResult.batch_id}）
              </Typography.Title>
              <SummaryView summary={uploadResult.summary} />
            </div>
          )}
        </div>
      </Card>
      <Card title="批次历史">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {loadError !== null && (
            <Alert type="error" showIcon message="批次历史加载失败" description={loadError} />
          )}
          <Table<ImportBatchSummary>
            rowKey="batch_id"
            columns={columns}
            dataSource={batches}
            loading={loading}
            pagination={{ showTotal: (t) => `共 ${t} 条` }}
          />
        </div>
      </Card>
      <Drawer
        title={`批次详情：${selectedBatchId ?? ''}`}
        open={selectedBatchId !== null}
        width={640}
        destroyOnHidden
        onClose={() => setSelectedBatchId(null)}
      >
        {detailError !== null && (
          <Alert type="error" showIcon message="批次详情加载失败" description={detailError} />
        )}
        {detailLoading && <Typography.Text type="secondary">详情加载中…</Typography.Text>}
        {detail !== null && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <Descriptions
              size="small"
              column={2}
              items={[
                { key: 'batch', label: '批次号', children: detail.batch_id },
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
            {detail.summary === null ? (
              <Typography.Text type="secondary">无结果摘要</Typography.Text>
            ) : (
              <SummaryView summary={detail.summary} />
            )}
          </div>
        )}
      </Drawer>
    </div>
  )
}
