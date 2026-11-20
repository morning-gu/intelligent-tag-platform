// 闭环评测报告视图（Task 9）：report.json 的九块只读渲染——① 部分口径警示
// （banner/warning 非空才出 Alert.warning）② 汇总 Statistic 卡 ③ θ×K 网格表
// ④ 基线对照表（系统/先验/随机）⑤ 覆盖分布 BarChart（5 桶计数）+ min/中位/max
// ⑥ Spearman 宏值 + 每片 Collapse ⑦ 失败影片列表（空 → 整块不渲染）⑧ 产物
// 下载（fileUrl 拼路径 + <a download> 原生下载）⑨ Alert.info 银标准 note 原文
// + calibre 小字。另在 Spearman 节后接「单片明细」表（ClosedEvalMovieTable，
// runId 直传，摘要/明细拉取由子组件自理）。
// null 一律「暂无」（None≠0 纪律）：网格格值缺失、CI 缺失（只出值不出 ±）、
// 档位缺格（k 不在本次档位）、基线缺行、calibre 可空字段同口径。
// 汇总/基线的取格规则：同 K 的「有值」格中 θ 最接近 0.4（并列取小 θ）——
// θ=0.4 格无有效影片（指标 null）时回退最近有值格，宁出近似不空屏；k 整档
// 缺格才是「暂无」。随机基线只出 P/R（GridRow 契约无 ndcg_random）。
import { Alert, Collapse, Statistic, Table, Typography } from 'antd'
import type { TableProps } from 'antd'
import {
  fileUrl,
  type ClosedEvalReport as ClosedEvalReportData,
  type EvalFileName,
  type GridRow,
} from '../api/eval'
import BarChart from './BarChart'
import type { BarItem } from './BarChart'
import ClosedEvalMovieTable from './ClosedEvalMovieTable'

const NA = '暂无'

/** 汇总与基线的目标 K 档（P/nDCG 取 K=10、R 取 K=20）。 */
const K_P = 10
const K_R = 20

/** 产物下载三条（后端 files 端点白名单的子集）。 */
const DOWNLOADS: EvalFileName[] = ['report.md', 'metrics.csv', 'raw_results.json']

/** 覆盖率分桶（末桶右闭、其余右开）：label 即桶名，直接做 BarChart 行标签。 */
const COVERAGE_BUCKETS = [
  { label: '[0,0.2)', lo: 0, hi: 0.2 },
  { label: '[0.2,0.4)', lo: 0.2, hi: 0.4 },
  { label: '[0.4,0.6)', lo: 0.4, hi: 0.6 },
  { label: '[0.6,0.8)', lo: 0.6, hi: 0.8 },
  { label: '[0.8,1.0]', lo: 0.8, hi: 1 },
]

/** null → 「暂无」（None≠0）；数值原样字符串化（与历史表同口径）。 */
function na(v: number | null): string {
  return v === null ? NA : String(v)
}

/** 字符串版的 null 口径（calibre 可空字段用）。 */
function naText(v: string | null): string {
  return v ?? NA
}

/** 网格取格：指定 K 的有值格中 θ 最接近 0.4（并列取小 θ）；全无值 → null。
 * 「有值」逐字段判定——θ=0.4 格无有效影片（指标 null）时回退最近有值格。 */
function pickField(
  rows: GridRow[],
  k: number,
  field: 'precision' | 'recall' | 'ndcg' | 'precision_random' | 'recall_random',
): number | null {
  const valued = rows
    .filter((r) => r.k === k && r[field] !== null)
    .sort((a, b) => Math.abs(a.theta - 0.4) - Math.abs(b.theta - 0.4) || a.theta - b.theta)
  return valued.length > 0 ? valued[0][field] : null
}

/** 指标 ±CI 渲染：值 null → 暂无；CI 缺失只出值；统一 `0.42 ± [0.31, 0.53]`。 */
function ciText(v: number | null, ci: [number, number] | null): string {
  if (v === null) return NA
  return ci === null ? String(v) : `${v} ± [${ci[0]}, ${ci[1]}]`
}

/** 覆盖率值 → 5 桶计数（末桶右闭）。 */
function coverageBars(values: number[]): BarItem[] {
  return COVERAGE_BUCKETS.map((bucket, i) => {
    const last = i === COVERAGE_BUCKETS.length - 1
    return {
      label: bucket.label,
      value: values.filter((v) => v >= bucket.lo && (last ? v <= bucket.hi : v < bucket.hi))
        .length,
    }
  })
}

/** θ×K 网格列（null 一律「暂无」）。 */
const gridColumns: TableProps<GridRow>['columns'] = [
  { title: 'θ', dataIndex: 'theta' },
  { title: 'K', dataIndex: 'k' },
  { title: 'P（±CI）', key: 'precision', render: (_, r) => ciText(r.precision, r.precision_ci) },
  { title: 'R（±CI）', key: 'recall', render: (_, r) => ciText(r.recall, r.recall_ci) },
  { title: 'nDCG（±CI）', key: 'ndcg', render: (_, r) => ciText(r.ndcg, r.ndcg_ci) },
  { title: '随机 P', key: 'precision_random', render: (_, r) => na(r.precision_random) },
  { title: '随机 R', key: 'recall_random', render: (_, r) => na(r.recall_random) },
]

/** 基线对照行（三行同列形）。 */
interface BaselineRow {
  name: string
  p10: number | null
  r20: number | null
  ndcg10: number | null
}

const baselineColumns: TableProps<BaselineRow>['columns'] = [
  { title: '口径', dataIndex: 'name' },
  { title: 'P@10', dataIndex: 'p10', render: na },
  { title: 'R@20', dataIndex: 'r20', render: na },
  { title: 'nDCG@10', dataIndex: 'ndcg10', render: na },
]

const perMovieColumns: TableProps<{ tt: string; spearman: number | null }>['columns'] = [
  { title: 'tt', dataIndex: 'tt' },
  { title: 'Spearman', dataIndex: 'spearman', render: na },
]

const failedColumns: TableProps<{ tt: string; error: string }>['columns'] = [
  { title: 'tt', dataIndex: 'tt' },
  { title: '错误', dataIndex: 'error' },
]

export default function ClosedEvalReport({
  report,
  runId,
}: {
  report: ClosedEvalReportData
  runId: string
}) {
  const grid = report.grid
  const prior = report.baselines.prior
  // 汇总五键：P@10/nDCG@10 取 K=10 格、R@20 取 K=20 格（θ 最接近 0.4 的有值格）
  const ndcg10 = pickField(grid, K_P, 'ndcg')
  const p10 = pickField(grid, K_P, 'precision')
  const r20 = pickField(grid, K_R, 'recall')

  const summary = [
    { key: 'ndcg10', title: 'nDCG@10', value: ndcg10 },
    { key: 'p10', title: 'P@10', value: p10 },
    { key: 'r20', title: 'R@20', value: r20 },
    { key: 'coverage-median', title: '覆盖中位', value: report.coverage.median },
    { key: 'spearman-macro', title: 'Spearman 宏', value: report.spearman.macro },
  ]

  const baselineRows: BaselineRow[] = [
    { name: '系统', p10, r20, ndcg10 },
    {
      name: '先验',
      p10: pickField(prior, K_P, 'precision'),
      r20: pickField(prior, K_R, 'recall'),
      ndcg10: pickField(prior, K_P, 'ndcg'),
    },
    {
      name: '随机',
      p10: pickField(grid, K_P, 'precision_random'),
      r20: pickField(grid, K_R, 'recall_random'),
      ndcg10: null, // 契约无 ndcg_random，随机档只出 P/R
    },
  ]

  const coverageStats = `min ${na(report.coverage.min)} · 中位 ${na(report.coverage.median)} · max ${na(report.coverage.max)}`
  const calibre = report.calibre

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {report.banner !== null && <Alert type="warning" showIcon message={report.banner} />}
      {report.warning !== null && <Alert type="warning" showIcon message={report.warning} />}

      <div data-testid="summary-row" style={{ display: 'flex', gap: 48, flexWrap: 'wrap' }}>
        {summary.map((s) => (
          <div key={s.key} data-testid={`summary-${s.key}`}>
            <Statistic title={s.title} value={s.value === null ? NA : s.value} />
          </div>
        ))}
      </div>

      <div data-testid="grid-section">
        <Typography.Title level={5}>θ×K 网格</Typography.Title>
        <Table<GridRow>
          rowKey={(r) => `${r.theta}-${r.k}`}
          columns={gridColumns}
          dataSource={grid}
          pagination={false}
          size="small"
        />
      </div>

      <div data-testid="baseline-section">
        <Typography.Title level={5}>基线对照</Typography.Title>
        <Table<BaselineRow>
          rowKey="name"
          columns={baselineColumns}
          dataSource={baselineRows}
          pagination={false}
          size="small"
        />
      </div>

      <div data-testid="coverage-section">
        <Typography.Title level={5}>覆盖分布</Typography.Title>
        <BarChart items={coverageBars(report.coverage.values)} />
        <Typography.Text type="secondary">{coverageStats}</Typography.Text>
      </div>

      <div data-testid="spearman-section">
        <Typography.Title level={5}>Spearman 相关性</Typography.Title>
        <Typography.Text>{`宏 ${na(report.spearman.macro)}`}</Typography.Text>
        <Collapse
          items={[
            {
              key: 'per-movie',
              label: '每片 Spearman',
              children: (
                <Table
                  rowKey="tt"
                  columns={perMovieColumns}
                  dataSource={report.spearman.per_movie}
                  pagination={false}
                  size="small"
                />
              ),
            },
          ]}
        />
      </div>

      <div data-testid="movie-section">
        <Typography.Title level={5}>单片明细</Typography.Title>
        <ClosedEvalMovieTable runId={runId} />
      </div>

      {report.failed_movies.length > 0 && (
        <div data-testid="failed-section">
          <Typography.Title level={5}>
            {`失败影片（${report.failed_movies.length}）`}
          </Typography.Title>
          <Table
            rowKey="tt"
            columns={failedColumns}
            dataSource={report.failed_movies}
            pagination={false}
            size="small"
          />
        </div>
      )}

      <div data-testid="downloads-section">
        <Typography.Title level={5}>产物下载</Typography.Title>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
          {DOWNLOADS.map((name) => (
            <a key={name} href={fileUrl(runId, name)} download>
              {name}
            </a>
          ))}
        </div>
      </div>

      <Alert
        type="info"
        showIcon
        message="银标准口径"
        description={
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span>{report.note}</span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="calibre">
              {`模型 ${calibre.llm_model} · 向量 ${calibre.embedding_model} · prompt ${naText(calibre.prompt_version)} · 词库 ${naText(calibre.library_version)} · 数据集 ${naText(calibre.dataset_id)} · GT ${naText(calibre.gt_sha8)}`}
            </Typography.Text>
          </div>
        }
      />
    </div>
  )
}
