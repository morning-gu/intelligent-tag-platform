import { useEffect, useState } from 'react'
import { Alert, Button, Card, InputNumber, Select, Space, Table, Typography, Statistic } from 'antd'
import { apiFetch } from '../api/client'
import { fetchAssets, type AssetSummary } from '../api/assets'

type Judgment = { id: number; supported: boolean; concept: string; reason: string; evidence?: { scene_id: string; quote: string }[] }
type Item = { asset_id: string; title: string; status: string; stage?: string; error?: string; generation_elapsed_s?: number; generation_tokens?: number; tags?: { tag_name: string; evidence: { scene_id: string; description: string; start_ms: number; end_ms: number }[] }[]; evaluation_cost?: { usage: Record<string, number>; elapsed_s: number }; evaluation?: { independent_count: number; supported_count: number; effective_rate: number | null; redundant_count: number; unsupported_rate: number | null; judgments: Judgment[]; usage: Record<string, number>; elapsed_s: number } }
type Report = { id: string; algorithm: string; status: string; error?: string; created_at: string; evaluation_model: string; evaluation_version: string; max_tags: number; items: Item[]; summary: { completed: number; failed: number; evaluated: number; independent_count: number; effective_rate: number | null; mean_independent_count: number | null; unsupported_rate: number | null; generation_tokens: number; generation_elapsed_s: number } }
const endpoint = '/api/v1/benchmarks'
const percent = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`
const statuses: Record<string, string> = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', generating: '生成中', evaluating: '独立评估中', completed: '已完成' }

export default function Benchmarks() {
  const [reports, setReports] = useState<Report[]>([])
  const [assets, setAssets] = useState<AssetSummary[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [limit, setLimit] = useState(15)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function refresh() {
    try { setReports(await apiFetch<Report[]>(endpoint)); setError('') }
    catch (e) { setError(String(e)) }
  }
  useEffect(() => {
    let active = true
    const load = () => apiFetch<Report[]>(endpoint).then(r => { if (active) setReports(r) }).catch(e => { if (active) setError(String(e)) })
    void load()
    fetchAssets().then(r => { if (active) setAssets(r) }).catch(e => { if (active) setError(String(e)) })
    const timer = setInterval(() => void load(), 5000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  async function create() {
    setBusy(true)
    try {
      await apiFetch(endpoint, { method: 'POST', body: JSON.stringify({ asset_ids: selected, max_tags: limit }) })
      await refresh()
    } catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  async function download(report: Report) {
    try {
      const snapshot = await apiFetch<unknown>(`${endpoint}/${report.id}`)
      const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a'); link.href = url; link.download = report.id + '.json'; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) { setError(String(e)) }
  }
  return <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <Card title="算法基准 · mtg-v3">
      <Typography.Paragraph>选片后一次运行：mtg-v3 生成 → 独立模型评估 → 查看有效性。固定使用 glm-5.3-flash，通过现有 MaaS 网关和 key 调用。建议首轮 5–10 部。</Typography.Paragraph>
      <Space wrap>
        <Select mode="multiple" maxCount={50} optionFilterProp="label" aria-label="基准影片" placeholder="选择影片（按选择顺序运行）" style={{ minWidth: 320 }} value={selected} onChange={setSelected} options={assets.filter(a => a.has_content).map(a => ({ value: a.asset_id, label: a.title }))} />
        <InputNumber aria-label="统一标签上限" min={1} max={100} value={limit} onChange={v => setLimit(v ?? 15)} />
        <Button type="primary" loading={busy} disabled={!selected.length} onClick={() => void create()}>生成并自动评估</Button>
        <Button onClick={() => void refresh()}>刷新</Button>
      </Space>
      <Typography.Paragraph type="secondary">评估模型须区别于生成模型和内部 judge，并可通过当前模型服务访问。统一上限在原算法输出后截取；生成耗时/token 与评估成本分别展示。失败后可重新创建批次。</Typography.Paragraph>
      <Typography.Paragraph type="secondary">主要看每片有证据的独立概念数是否更多、不支持比例是否更低。有效标签占比 = 有证据独立概念数 ÷ 最终标签数；冗余数 = 有支持标签数 − 独立概念数。评估是模型判定，不是真实召回率；跨算法比较请固定影片、评估模型、口径版本和标签上限。</Typography.Paragraph>
    </Card>
    {error && <Alert type="error" message={error} />}
    {reports.map(report => <Card key={report.id} title={`${report.algorithm} · ${report.created_at} · ${statuses[report.status] ?? report.status}`} extra={<Button onClick={() => void download(report)}>导出完整快照</Button>}>
      <Typography.Paragraph>批次：{report.id}；评估模型：{report.evaluation_model}；口径：{report.evaluation_version}；标签上限：{report.max_tags}</Typography.Paragraph>
      <Space wrap size="large" style={{ marginBottom: 16 }}>
        <Statistic title="每片有效独立概念数" value={report.summary.mean_independent_count ?? '—'} precision={2} />
        <Statistic title="有效标签占比" value={percent(report.summary.effective_rate)} />
        <Statistic title="证据不支持比例" value={percent(report.summary.unsupported_rate)} />
        <Statistic title="已评估影片" value={`${report.summary.evaluated}/${report.items.length}`} />
      </Space>
      {report.error && <Alert type="error" message={report.error} />}
      <Typography.Paragraph>完成 {report.summary.completed}/{report.items.length} 部，失败 {report.summary.failed} 部；已评估 {report.summary.evaluated} 部的独立概念合计 {report.summary.independent_count}；不支持比例（按标签加权）：{report.summary.unsupported_rate == null ? '—' : `${(report.summary.unsupported_rate * 100).toFixed(1)}%`}；已记录生成成本 {report.summary.generation_tokens} token / {report.summary.generation_elapsed_s} 秒。</Typography.Paragraph>
      <Table<Item> rowKey="asset_id" dataSource={report.items} pagination={false} columns={[
        { title: '影片', dataIndex: 'title' }, { title: '状态', render: (_, i) => i.status === 'failed' ? `失败（${statuses[i.stage ?? ''] ?? '执行'}）` : statuses[i.stage ?? i.status] ?? i.status },
        { title: '生成秒数', dataIndex: 'generation_elapsed_s' }, { title: '生成 token', dataIndex: 'generation_tokens' },
        { title: '最终标签数', render: (_, i) => i.tags?.length ?? '—' },
        { title: '有证据独立概念数', render: (_, i) => i.evaluation?.independent_count ?? '—' },
        { title: '有效标签占比', render: (_, i) => percent(i.evaluation?.effective_rate ?? null) },
        { title: '有支持 / 冗余', render: (_, i) => i.evaluation ? `${i.evaluation.supported_count} / ${i.evaluation.redundant_count}` : '—' },
        { title: '不支持比例', render: (_, i) => i.evaluation?.unsupported_rate == null ? '—' : `${(i.evaluation.unsupported_rate * 100).toFixed(1)}%` },
        { title: '评估 token / 秒', render: (_, i) => {
          const cost = i.evaluation ?? i.evaluation_cost
          if (!cost) return '—'
          const tokens = cost.usage.total_tokens ?? (cost.usage.prompt_tokens != null && cost.usage.completion_tokens != null ? cost.usage.prompt_tokens + cost.usage.completion_tokens : null)
          return `${tokens ?? '未知'} / ${cost.elapsed_s}`
        } },
        { title: '错误', dataIndex: 'error' },
      ]} expandable={{ expandedRowRender: item => <Table rowKey="id" pagination={false} dataSource={(item.tags ?? []).map((t, id) => ({ ...t, id, judgment: item.evaluation?.judgments.find(j => j.id === id) }))} columns={[
        { title: '标签', dataIndex: 'tag_name' },
        { title: '场景证据', render: (_, t) => t.evidence.map(e => `${e.scene_id} [${e.start_ms}–${e.end_ms}ms] ${e.description}`).join('；') },
        { title: '独立评估', render: (_, t) => t.judgment ? `${t.judgment.supported ? '支持' : '不支持'} · ${t.judgment.concept} · ${t.judgment.reason}` : '尚无有效评估' },
        { title: '评估原文引用', render: (_, t) => t.judgment?.evidence?.map(e => `${e.scene_id}：${e.quote}`).join('；') || '—' },
      ]} /> }} />
    </Card>)}
  </Space>
}
