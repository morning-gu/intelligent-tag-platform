import { useEffect, useState } from 'react'
import { Alert, Button, Card, Input, InputNumber, Popconfirm, Select, Space, Table, Typography, Statistic } from 'antd'
import { apiFetch } from '../api/client'
import { fetchAssets, type AssetSummary } from '../api/assets'

const qualityLabels = { keep: '可直接展示', rewrite: '建议改写', omit: '不建议展示' }
type Quality = { keep: number; rewrite: number; omit: number; distinct_keep: number; display_rate: number | null }
type Judgment = {
  id: number
  verdict: keyof typeof qualityLabels
  reason: string
  suggestion?: string | null
}
type Cost = { usage: Record<string, number>; elapsed_s: number }
type SceneEvidence = { scene_id: string; description: string; start_ms: number; end_ms: number }
type Item = {
  discovery?: {
    vocabulary_before: number
    vocabulary_after: number
    candidates_after: number
    added_labels: string[]
    low_coverage_scenes?: string[]
    rejected_keyword_count?: number
    rescored: boolean
    nli_pairs: number
  }
  asset_id: string
  title: string
  status: string
  stage?: string
  error?: string
  generation_elapsed_s?: number
  generation_timings_s?: Record<string, number>
  generation_tokens?: number | null
  tags?: { tag_name: string; evidence: SceneEvidence[] }[]
  evaluation_cost?: Cost
  evaluation?: Cost & {
    quality: Quality
    judgments: Judgment[]
  }
}
type Report = {
  id: string
  algorithm: string
  status: string
  error?: string
  created_at: string
  evaluation_model: string
  evaluation_version: string
  custom_evaluation_prompt?: boolean
  max_tags: number
  items: Item[]
  summary: {
    completed: number
    failed: number
    evaluated: number
    quality: Quality & { mean_distinct_keep: number | null }
    generation_tokens: number
    generation_elapsed_s: number
  }
}
const endpoint = '/api/v1/benchmarks'
const percent = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`
const statuses: Record<string, string> = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', generating: '生成中', evaluating: '独立评估中', completed: '已完成' }
const costText = (cost?: Cost) => {
  if (!cost) return '—'
  const tokens = cost.usage.total_tokens ?? (cost.usage.prompt_tokens != null && cost.usage.completion_tokens != null ? cost.usage.prompt_tokens + cost.usage.completion_tokens : null)
  return `${tokens ?? '未知'} / ${cost.elapsed_s}`
}

const generationPhases = [
  ['recall_s', '向量召回'],
  ['nli_model_load_s', 'NLI 模型加载'],
  ['nli_initial_s', 'NLI 首轮打分'],
  ['materials_s', '提词素材准备'],
  ['llm_keywords_s', 'LLM 标签生成'],
  ['llm_synonym_s', 'LLM 同义判断'],
  ['nli_rescore_s', 'NLI 新词重评分'],
  ['dedup_s', '最终标签去重（含向量和可能的 LLM 调用）'],
  ['other_s', '其他生成开销'],
]

function GenerationTimings({ timings }: { timings: Record<string, number> }) {
  const nliStages = ['nli_model_load_s', 'nli_initial_s', 'nli_rescore_s'].filter(stage => timings[stage] != null)
  const nliTotal = nliStages.reduce((sum, stage) => sum + timings[stage], 0)
  return <section aria-label="生成耗时明细">
    <Typography.Text strong>生成耗时明细</Typography.Text>
    {nliStages.length > 0 && <Typography.Text style={{ marginInlineStart: 12 }}>NLI 合计（含加载） {nliTotal.toFixed(2)} 秒</Typography.Text>}
    <div><Space wrap size={[20, 4]}>
      {generationPhases.filter(([stage]) => timings[stage] != null).map(([stage, label]) =>
        <span key={stage}>{label} {timings[stage].toFixed(2)} 秒</span>)}
    </Space></div>
  </section>
}

function SceneEvidenceList({ evidence }: { evidence: SceneEvidence[] }) {
  if (!evidence.length) return '—'
  return <div style={{ display: 'grid', gap: 8 }}>
    {evidence.map(e => <div key={e.scene_id} style={{ whiteSpace: 'pre-wrap' }}>
      {`${e.scene_id} [${e.start_ms}–${e.end_ms}ms] ${e.description}`}
    </div>)}
  </div>
}

export default function Benchmarks() {
  const isMtg = import.meta.env.VITE_BENCHMARK_ALGORITHM === 'mtg-v3'
  const algorithm = isMtg ? 'mtg-v3' : 'x-mlclass-online-v1'
  const name = isMtg ? 'mtg-v3' : '在线 X-MLClass'
  const [reports, setReports] = useState<Report[]>([])
  const [assets, setAssets] = useState<AssetSummary[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [limit, setLimit] = useState(15)
  const [promptSettings, setPromptSettings] = useState<{ prompt: string; max_length: number }>()
  const [prompt, setPrompt] = useState('')
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
    apiFetch<{ prompt: string; max_length: number }>(`${endpoint}/evaluation-prompt`).then(settings => {
      if (active) {
        setPromptSettings(settings)
        setPrompt(settings.prompt)
      }
    }).catch(e => { if (active) setError(String(e)) })
    const timer = setInterval(() => void load(), 5000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  async function create() {
    setBusy(true)
    try {
      await apiFetch(endpoint, { method: 'POST', body: JSON.stringify({ asset_ids: selected, max_tags: limit, algorithm, evaluation_prompt: prompt }) })
      setPrompt(promptSettings?.prompt ?? '')
      await refresh()
    } catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  async function remove(report: Report) {
    try {
      await apiFetch(`${endpoint}/${report.id}`, { method: 'DELETE' })
      await refresh()
    } catch (e) { setError(String(e)) }
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
    <Card title={`算法基准 · ${name}`}>
      <Typography.Paragraph>{name} 生成后，glm-5.3-flash 会判断每条标签能否独立读懂，以及能否帮助观众了解或选择这部影片。</Typography.Paragraph>
      <Space wrap>
        <Select mode="multiple" maxCount={50} optionFilterProp="label" aria-label="基准影片" placeholder="选择影片（按选择顺序运行）" style={{ minWidth: 320 }} value={selected} onChange={setSelected} options={assets.filter(a => a.has_content).map(a => ({ value: a.asset_id, label: a.title }))} />
        <InputNumber aria-label="统一标签上限" min={1} max={100} value={limit} onChange={v => setLimit(v ?? 15)} />
        <Button type="primary" loading={busy} disabled={!selected.length || !promptSettings || !prompt.trim()} onClick={() => void create()}>生成并自动评估</Button>
        <Button onClick={() => void refresh()}>刷新</Button>
      </Space>
      <div style={{ marginBlock: 16 }}>
        <Space style={{ marginBottom: 8 }}>
          <Typography.Text strong><label htmlFor="evaluation-prompt">本次评估提示词</label></Typography.Text>
          <Button size="small" disabled={busy || !promptSettings || prompt === promptSettings.prompt}
            onClick={() => setPrompt(promptSettings?.prompt ?? '')}>恢复默认</Button>
        </Space>
        <Input.TextArea id="evaluation-prompt" value={prompt} rows={8}
          disabled={!promptSettings || busy} maxLength={promptSettings?.max_length}
          onChange={event => setPrompt(event.target.value)} />
        <Typography.Text type="secondary">修改只用于本次批次并随报告保存。系统会保留可直接展示、建议改写、不建议展示三类输出格式。</Typography.Text>
      </div>
      <Typography.Paragraph type="secondary">可展示占比按每部片的可直接展示概念去重后计算。改写建议只用于评估查看。</Typography.Paragraph>
    </Card>
    {error && <Alert type="error" message={error} />}
    {reports.map(report => <Card key={report.id} title={`${report.algorithm} · ${report.created_at} · ${statuses[report.status] ?? report.status}`} extra={
      <Space>
        <Button onClick={() => void download(report)}>导出完整快照</Button>
        {(report.status === 'succeeded' || report.status === 'failed') && (
          <Popconfirm title="删除这条评估记录？" description="删除后无法从算法基准页找回。"
            okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => remove(report)}>
            <Button danger>删除记录</Button>
          </Popconfirm>
        )}
      </Space>
    }>
      <Typography.Paragraph>批次：{report.id}；评估模型：{report.evaluation_model}；口径：{report.evaluation_version}{report.custom_evaluation_prompt && '（自定义提示词）'}；标签上限：{report.max_tags}</Typography.Paragraph>
      <Space wrap size="large" style={{ marginBottom: 16 }}>
        <Statistic title="每片可展示概念数（去重）" value={report.summary.quality.mean_distinct_keep ?? '—'} precision={2} />
        <Statistic title="可展示占比（去重）" value={percent(report.summary.quality.display_rate)} />
        <Statistic title="建议改写 / 不建议展示" value={`${report.summary.quality.rewrite} / ${report.summary.quality.omit}`} />
        <Statistic title="已评估影片" value={`${report.summary.evaluated}/${report.items.length}`} />
      </Space>
      {report.error && <Alert type="error" message={report.error} />}
      <Typography.Paragraph>完成 {report.summary.completed}/{report.items.length} 部，失败 {report.summary.failed} 部。已记录生成成本 {report.summary.generation_tokens} token / {report.summary.generation_elapsed_s} 秒。</Typography.Paragraph>
      <Table<Item> rowKey="asset_id" dataSource={report.items} pagination={false} tableLayout="fixed" style={{ overflowWrap: 'anywhere' }} columns={[
        { title: '影片', dataIndex: 'title', width: '14%' }, { title: '状态', render: (_, i) => i.status === 'failed' ? `失败（${statuses[i.stage ?? ''] ?? '执行'}）` : statuses[i.stage ?? i.status] ?? i.status },
        { title: '生成秒数', dataIndex: 'generation_elapsed_s' }, { title: '生成 token', render: (_, i) => i.generation_tokens ?? '未知' },
        { title: '最终标签数', render: (_, i) => i.tags?.length ?? '—' },
        { title: '可展示 / 改写 / 不展示', render: (_, i) => {
          const quality = i.evaluation?.quality
          return quality ? `${quality.keep} / ${quality.rewrite} / ${quality.omit}` : '—'
        } },
        { title: '可展示概念数（去重）', render: (_, i) => i.evaluation?.quality.distinct_keep ?? '—' },
        { title: '可展示占比（去重）', render: (_, i) => percent(i.evaluation?.quality.display_rate ?? null) },
        { title: '评估 token / 秒', render: (_, i) => costText(i.evaluation ?? i.evaluation_cost) },
        { title: '实验词表 / 待积累', render: (_, i) => i.discovery ? `${i.discovery.vocabulary_after} / ${i.discovery.candidates_after}` : '—' },
        { title: '错误', dataIndex: 'error', width: '16%' },
      ]} expandable={{ expandedRowRender: item => <div style={{ display: 'grid', gap: 8 }}>
      {item.generation_timings_s ? <GenerationTimings timings={item.generation_timings_s} /> :
        report.algorithm === 'x-mlclass-online-v1' && item.generation_elapsed_s != null &&
        <Typography.Text type="secondary">该报告未记录分段耗时。</Typography.Text>}
      {item.discovery && <div>
        实验词表 {item.discovery.vocabulary_before} → {item.discovery.vocabulary_after}；本片入表 {item.discovery.added_labels.length} 个标签；
        {item.discovery.low_coverage_scenes && `低覆盖场景 ${item.discovery.low_coverage_scenes.length} 个；`}
        待积累候选 {item.discovery.candidates_after} 个；
        {!!item.discovery.rejected_keyword_count && `因引用无效跳过 ${item.discovery.rejected_keyword_count} 个关键词；`}
        {item.discovery.rescored ? '已重打分' : '无需重打分'}；NLI 判断 {item.discovery.nli_pairs} 对。
      </div>}
      <Table rowKey="id" pagination={false} tableLayout="fixed" dataSource={(item.tags ?? []).map((t, id) => ({ ...t, id, judgment: item.evaluation?.judgments.find(j => j.id === id) }))} columns={[
        { title: '标签', dataIndex: 'tag_name', width: '15%' },
        { title: '场景证据', render: (_, t) => <SceneEvidenceList evidence={t.evidence} /> },
        { title: '独立评估', width: '27%', render: (_, t) => {
          const judgment = t.judgment
          if (!judgment) return '尚无有效评估'
          return <><strong>{qualityLabels[judgment.verdict]}</strong><div>{judgment.reason}</div></>
        } },
        { title: '改写建议', width: '23%', render: (_, t) => t.judgment?.suggestion || '—' },
      ]} />
      </div> }} />
    </Card>)}
  </Space>
}
