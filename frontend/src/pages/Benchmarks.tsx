import { useEffect, useState } from 'react'
import { Alert, Button, Card, InputNumber, Select, Space, Table, Typography, Statistic } from 'antd'
import { apiFetch } from '../api/client'
import { fetchAssets, type AssetSummary } from '../api/assets'

type Judgment = { id: number; supported: boolean; concept: string; reason: string; evidence?: { scene_id: string; quote: string }[] }
type Cost = { usage: Record<string, number>; elapsed_s: number }
type ComparisonRow = Judgment & { side: 'a' | 'b'; tag_name: string; exclusive: boolean }
type Item = { asset_id: string; title: string; status: string; stage?: string; error?: string; generation_elapsed_s?: number; generation_tokens?: number | null; tags?: { tag_name: string; evidence: { scene_id: string; description: string; start_ms: number; end_ms: number }[] }[]; evaluation_cost?: Cost; comparison_cost?: Cost; comparison?: Cost & { rows: ComparisonRow[] }; discovery?: { vocabulary_before: number; vocabulary_after: number; candidates_after: number; added_labels: string[]; rescored: boolean; nli_pairs: number }; evaluation?: Cost & { independent_count: number; supported_count: number; effective_rate: number | null; redundant_count: number; unsupported_rate: number | null; judgments: Judgment[] } }
type Report = { id: string; algorithm: string; baseline_id?: string; status: string; error?: string; created_at: string; evaluation_model: string; evaluation_version: string; max_tags: number; items: Item[]; summary: { completed: number; failed: number; evaluated: number; independent_count: number; effective_rate: number | null; mean_independent_count: number | null; unsupported_rate: number | null; generation_tokens: number; generation_elapsed_s: number } }
const endpoint = '/api/v1/benchmarks'
const percent = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`
const statuses: Record<string, string> = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', generating: '生成中', evaluating: '独立评估中', comparing: '跨算法盲评中', completed: '已完成' }
const costText = (cost?: Cost) => {
  if (!cost) return '—'
  const tokens = cost.usage.total_tokens ?? (cost.usage.prompt_tokens != null && cost.usage.completion_tokens != null ? cost.usage.prompt_tokens + cost.usage.completion_tokens : null)
  return `${tokens ?? '未知'} / ${cost.elapsed_s}`
}

export default function Benchmarks() {
  const [reports, setReports] = useState<Report[]>([])
  const [assets, setAssets] = useState<AssetSummary[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [limit, setLimit] = useState(15)
  const [algorithm, setAlgorithm] = useState('mtg-v3')
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
      await apiFetch(endpoint, { method: 'POST', body: JSON.stringify({ asset_ids: selected, max_tags: limit, algorithm }) })
      await refresh()
    } catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  async function replay(report: Report) {
    setBusy(true)
    try {
      await apiFetch(`${endpoint}/${report.id}/replay-xmlclass`, { method: 'POST' })
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
    <Card title="算法基准 · mtg-v3 / 在线 X-MLClass">
      <Typography.Paragraph>选片后生成并使用固定的 glm-5.3-flash 独立评估，建议首轮 5–10 部。对照实验请先完成 mtg-v3，再点击该批次的「用 X-MLClass 回放对比」，复用相同影片、片序、初始词表和标签上限。</Typography.Paragraph>
      <Space wrap>
        <Select aria-label="基准算法" value={algorithm} onChange={setAlgorithm} style={{ width: 220 }} options={[{ value: 'mtg-v3', label: 'mtg-v3' }, { value: 'x-mlclass-online-v1', label: '在线 X-MLClass' }]} />
        <Select mode="multiple" maxCount={50} optionFilterProp="label" aria-label="基准影片" placeholder="选择影片（按选择顺序运行）" style={{ minWidth: 320 }} value={selected} onChange={setSelected} options={assets.filter(a => a.has_content).map(a => ({ value: a.asset_id, label: a.title }))} />
        <InputNumber aria-label="统一标签上限" min={1} max={100} value={limit} onChange={v => setLimit(v ?? 15)} />
        <Button type="primary" loading={busy} disabled={!selected.length} onClick={() => void create()}>生成并自动评估</Button>
        <Button onClick={() => void refresh()}>刷新</Button>
      </Space>
      <Typography.Paragraph type="secondary">评估模型须区别于生成模型和内部 judge，并可通过当前模型服务访问。统一上限在原算法输出后截取；生成耗时/token 与评估成本分别展示。失败后可重新创建批次。</Typography.Paragraph>
      <Typography.Paragraph type="secondary">主要看每片有证据的独立概念数是否更多、不支持比例是否更低。有效标签占比 = 有证据独立概念数 ÷ 最终标签数；冗余数 = 有支持标签数 − 独立概念数。评估是模型判定，不是真实召回率；跨算法比较请固定影片、评估模型、口径版本和标签上限。</Typography.Paragraph>
      <Typography.Paragraph type="secondary">X-MLClass 按片序积累候选，默认至少两部影片支持才加入实验词表。每个新批次从初始词表开始；实验词表和正式标签库分开保存。NLI 为本地模型计算，生成 token 仅统计提词与同义判断。</Typography.Paragraph>
    </Card>
    {error && <Alert type="error" message={error} />}
    {reports.map(report => <Card key={report.id} title={`${report.algorithm} · ${report.created_at} · ${statuses[report.status] ?? report.status}`} extra={<Space>
      {report.algorithm === 'mtg-v3' && report.status === 'succeeded' && <Button loading={busy} onClick={() => void replay(report)}>用 X-MLClass 回放对比</Button>}
      <Button onClick={() => void download(report)}>导出完整快照</Button>
    </Space>}>
      <Typography.Paragraph>批次：{report.id}；评估模型：{report.evaluation_model}；口径：{report.evaluation_version}；标签上限：{report.max_tags}</Typography.Paragraph>
      {report.baseline_id && <Typography.Paragraph>对照基线：{report.baseline_id}。展开影片可查看联合盲评判定的独有概念及理由。</Typography.Paragraph>}
      <Space wrap size="large" style={{ marginBottom: 16 }}>
        <Statistic title="每片有效独立概念数" value={report.summary.mean_independent_count ?? '—'} precision={2} />
        <Statistic title="有效标签占比" value={percent(report.summary.effective_rate)} />
        <Statistic title="证据不支持比例" value={percent(report.summary.unsupported_rate)} />
        <Statistic title="已评估影片" value={`${report.summary.evaluated}/${report.items.length}`} />
      </Space>
      {report.error && <Alert type="error" message={report.error} />}
      <Typography.Paragraph>完成 {report.summary.completed}/{report.items.length} 部，失败 {report.summary.failed} 部；已评估 {report.summary.evaluated} 部的独立概念合计 {report.summary.independent_count}；不支持比例（按标签加权）：{report.summary.unsupported_rate == null ? '—' : `${(report.summary.unsupported_rate * 100).toFixed(1)}%`}；已记录生成成本 {report.summary.generation_tokens} token / {report.summary.generation_elapsed_s} 秒。</Typography.Paragraph>
      <Table<Item> rowKey="asset_id" dataSource={report.items} pagination={false} scroll={{ x: 'max-content' }} columns={[
        { title: '影片', dataIndex: 'title' }, { title: '状态', render: (_, i) => i.status === 'failed' ? `失败（${statuses[i.stage ?? ''] ?? '执行'}）` : statuses[i.stage ?? i.status] ?? i.status },
        { title: '生成秒数', dataIndex: 'generation_elapsed_s' }, { title: '生成 token', render: (_, i) => i.generation_tokens ?? '未知' },
        { title: '最终标签数', render: (_, i) => i.tags?.length ?? '—' },
        { title: '有证据独立概念数', render: (_, i) => i.evaluation?.independent_count ?? '—' },
        { title: '有效标签占比', render: (_, i) => percent(i.evaluation?.effective_rate ?? null) },
        { title: '有支持 / 冗余', render: (_, i) => i.evaluation ? `${i.evaluation.supported_count} / ${i.evaluation.redundant_count}` : '—' },
        { title: '不支持比例', render: (_, i) => i.evaluation?.unsupported_rate == null ? '—' : `${(i.evaluation.unsupported_rate * 100).toFixed(1)}%` },
        { title: '评估 token / 秒', render: (_, i) => costText(i.evaluation ?? i.evaluation_cost) },
        { title: '对比 token / 秒', render: (_, i) => costText(i.comparison ?? i.comparison_cost) },
        { title: '实验词表 / 待积累', render: (_, i) => i.discovery ? `${i.discovery.vocabulary_after} / ${i.discovery.candidates_after}` : '—' },
        { title: '错误', dataIndex: 'error' },
      ]} expandable={{ expandedRowRender: item => <Space direction="vertical" style={{ width: '100%' }}>
      {item.discovery && <Typography.Paragraph>实验词表 {item.discovery.vocabulary_before} → {item.discovery.vocabulary_after}；本片新增 {item.discovery.added_labels.length} 个标签；{item.discovery.rescored ? '已重打分' : '无需重打分'}；NLI 判断 {item.discovery.nli_pairs} 对。</Typography.Paragraph>}
      <Table rowKey="id" pagination={false} dataSource={(item.tags ?? []).map((t, id) => ({ ...t, id, judgment: item.evaluation?.judgments.find(j => j.id === id) }))} columns={[
        { title: '标签', dataIndex: 'tag_name' },
        { title: '场景证据', render: (_, t) => t.evidence.map(e => `${e.scene_id} [${e.start_ms}–${e.end_ms}ms] ${e.description}`).join('；') },
        { title: '独立评估', render: (_, t) => t.judgment ? `${t.judgment.supported ? '支持' : '不支持'} · ${t.judgment.concept} · ${t.judgment.reason}` : '尚无有效评估' },
        { title: '评估原文引用', render: (_, t) => t.judgment?.evidence?.map(e => `${e.scene_id}：${e.quote}`).join('；') || '—' },
      ]} />
      {item.comparison && <Table<ComparisonRow> rowKey="id" pagination={false} dataSource={item.comparison.rows.filter(r => r.exclusive)} locale={{ emptyText: '联合盲评未发现独有概念' }} columns={[
        { title: '独有概念来源', render: (_, r) => r.side === 'a' ? 'mtg-v3' : 'X-MLClass' },
        { title: '标签', dataIndex: 'tag_name' }, { title: '概念', dataIndex: 'concept' },
        { title: '证据判定', render: (_, r) => r.supported ? '支持' : '不支持' },
        { title: '理由', dataIndex: 'reason' },
        { title: '原文引用', render: (_, r) => r.evidence?.map(e => `${e.scene_id}：${e.quote}`).join('；') || '—' },
      ]} />}
      </Space> }} />
    </Card>)}
  </Space>
}
