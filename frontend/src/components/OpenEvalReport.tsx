// 开放评测报告视图（open-eval Task 8）：report.json 的十块只读渲染，镜像
// ClosedEvalReport 的 None 处理与结构——① banner 警示（list[str] 逐条
// Alert.warning：部分运行/同模评审/小样本，开放侧把闭环单条扩成多条聚合）
// + warnings 口径警示（非空才出）② 汇总卡（样本核算五数 Descriptions +
// no_gt 次行 + 头条六值 Statistic）③ θ 网格表（n = 交集内命中非空的片数）
// ④ 准确率 vs θ 三系 BarChart（系统/随机/先验逐 θ 组装，None 点跳过不造 0
// ——BarItem.value 是 number，0 是真值不是缺失）⑤ 通过率与标定（宏/微/n +
// 缺评审条目「缺评审 ≠ 模型漏判」+ 假阳/假阴口径说明：评审 pass 的语义是
// 质量三条件、gt≥θ 的语义是 genome 相关性，一致率衡量口径对齐不是评审员
// 「错误率」）⑥ 伴生指标（产出规模 min/中位/max + 每片计数 Collapse +
// judge 审计段四行，Record<string,number> 的键原样渲染——dropped 原因可能
// 是中文键）⑦ 每片明细折叠表（失败/评审失败置顶，排序在组件内做：ok=false
// 最前、review=failed 次之、其余原序）⑧ failed_movies/review_failed_movies
// 清单（两块分开，错误原文，空 → 整块不渲染）⑨ 产物下载五链接（openFileUrl
// 拼路径 + <a download>）⑩ 银标准 note 恒渲染（Alert.warning）+ calibre
// 小字（比闭环多 review_model/review_same_model/review_prompt_version/
// modalities 四字段，同模评审括注如后端 report.md）。
// null 一律「暂无」（None≠0 纪律）。头条取格 = theta_rows 的 θ0.4 行**精确
// 匹配**（无 0.4 档 → 暂无，不回退最近有值档——与闭环汇总的回退取格不同，
// 开放侧历史摘要的回退在后端 _read_open_summary，报告视图如实呈现该格）。
import { Alert, Collapse, Descriptions, Statistic, Table, Typography } from 'antd'
import type { TableProps } from 'antd'
import {
  openFileUrl,
  type OpenEvalReport as OpenEvalReportData,
  type OpenFileName,
  type OpenMovieRow,
  type OpenThetaRow,
} from '../api/evalOpen'
import BarChart from './BarChart'
import type { BarItem } from './BarChart'

const NA = '暂无'

/** 开放评测头条取格 θ：镜像后端 open_ranking.DEFAULT_THETA_HEADLINE 与
 * 页面 OpenEval.tsx 的同名常量（值变更须三处同步）；头条从 theta_rows
 * 精确匹配该档（无档 → 暂无，不回退）。 */
const OPEN_HEADLINE_THETA = 0.4

/** 产物下载五件套（后端 files 端点白名单全量；report.json 有专用 /report
 * 端点、不在白名单）。 */
const DOWNLOADS: OpenFileName[] = [
  'report.md',
  'metrics.csv',
  'raw_results.json',
  'meta.json',
  'progress.jsonl',
]

/** 假阳/假阴与一致率的口径说明（spec §9 固定文案：评审 pass 语义 ≠ gt
 * 相关语义，一致率是口径对齐不是评审员「错误率」）。 */
const CALIBRE_NOTE =
  '评审 pass 的语义是质量三条件、gt≥θ 的语义是 genome 相关性——一致率衡量两个' +
  '口径的对齐程度，不是评审员「错误率」；假阳率（FP）：gt 相关性未达 θ 却产出；' +
  '假阴率（FN）：gt 相关性达 θ 却未产出——刻画的是产出与银标准口径的差异。'

/** null → 「暂无」（None≠0）；数值原样字符串化（与闭环报告同口径）。 */
function na(v: number | null): string {
  return v === null ? NA : String(v)
}

/** 字符串版的 null 口径（calibre 可空字段用；空串同 None → 暂无，对齐后端
 * _fmt_cal 的「None / 空串 → 暂无」）。 */
function naText(v: string | null): string {
  return v === null || v === '' ? NA : v
}

/** 指标 ±CI 渲染：值 null → 暂无；CI 缺失只出值；统一 `0.66 ± [0.5, 0.8]`。 */
function ciText(v: number | null, ci: [number, number] | null): string {
  if (v === null) return NA
  return ci === null ? String(v) : `${v} ± [${ci[0]}, ${ci[1]}]`
}

/** Record<string,number> → 原样键值串（键不翻译不映射——dropped 原因可能
 * 是中文键如「低置信」）；空 Record → 暂无。countPrefix 供 dropped 的 ×。 */
function formatCounts(record: Record<string, number>, joiner: string, countPrefix = ''): string {
  const entries = Object.entries(record)
  if (entries.length === 0) return NA
  return entries.map(([key, count]) => `${key} ${countPrefix}${count}`).join(joiner)
}

/** movies 行置顶排序秩：produce 失败（ok=false）最前、评审失败次之、其余。 */
function movieRank(row: OpenMovieRow): 0 | 1 | 2 {
  if (!row.ok) return 0
  if (row.review === 'failed') return 1
  return 2
}

/** 每片计数行（produced_stats.counts 元素，具名化供 Table 泛型）。 */
interface ProducedCount {
  tt: string
  hits_in: number
  hits_out: number
  new_words: number
}

/** θ 网格列（null 一律「暂无」；accuracy/agreement 带 CI 同格）。 */
const thetaColumns: TableProps<OpenThetaRow>['columns'] = [
  { title: 'θ', dataIndex: 'theta' },
  { title: 'n', dataIndex: 'n_valid' },
  { title: '准确率（±CI）', key: 'accuracy', render: (_, r) => ciText(r.accuracy, r.accuracy_ci) },
  { title: '随机基线', dataIndex: 'accuracy_random', render: na },
  { title: '先验基线', dataIndex: 'accuracy_prior', render: na },
  { title: '覆盖率', dataIndex: 'coverage', render: na },
  { title: '一致率（±CI）', key: 'agreement', render: (_, r) => ciText(r.agreement, r.agreement_ci) },
  { title: 'FP率', dataIndex: 'fp_rate', render: na },
  { title: 'FN率', dataIndex: 'fn_rate', render: na },
  { title: '标定片数', dataIndex: 'n_calibrated' },
]

/** 每片明细列（review 三态中文标签；片级准确率列头锚定头条 θ）。 */
const REVIEW_LABELS: Record<string, string> = { ok: '正常', failed: '失败', skipped: '跳过' }

const movieColumns: TableProps<OpenMovieRow>['columns'] = [
  { title: 'tt', dataIndex: 'tt' },
  { title: '标题', dataIndex: 'title' },
  { title: '产出', dataIndex: 'ok', render: (v: boolean) => (v ? '成功' : '失败') },
  { title: '评审', dataIndex: 'review', render: (v: string) => REVIEW_LABELS[v] ?? v },
  { title: '命中', dataIndex: 'hits_n' },
  { title: '新词', dataIndex: 'new_n' },
  { title: `准确率(θ${OPEN_HEADLINE_THETA})`, dataIndex: 'accuracy', render: na },
  { title: '新词通过率', dataIndex: 'new_pass', render: na },
  { title: '命中通过率', dataIndex: 'hit_pass', render: na },
  { title: '耗时(ms)', dataIndex: 'elapsed_ms' },
]

const countColumns: TableProps<ProducedCount>['columns'] = [
  { title: 'tt', dataIndex: 'tt' },
  { title: '交集内命中', dataIndex: 'hits_in' },
  { title: '交集外命中', dataIndex: 'hits_out' },
  { title: '新词', dataIndex: 'new_words' },
]

const failedColumns: TableProps<{ tt: string; error: string }>['columns'] = [
  { title: 'tt', dataIndex: 'tt' },
  { title: '错误', dataIndex: 'error' },
]

const reviewFailedColumns: TableProps<{ tt: string; review_error: string }>['columns'] = [
  { title: 'tt', dataIndex: 'tt' },
  { title: '评审错误', dataIndex: 'review_error' },
]

/** 准确率三系（BarChart 行名 × theta_rows 字段）：None 点不入 items（不出 0 假值）。 */
const ACCURACY_SERIES = [
  { name: '系统', field: 'accuracy' },
  { name: '随机', field: 'accuracy_random' },
  { name: '先验', field: 'accuracy_prior' },
] as const

export default function OpenEvalReport({ report, runId }: { report: OpenEvalReportData; runId: string }) {
  const acc = report.sample_accounting
  const pr = report.pass_rates
  const stats = report.produced_stats
  const judge = report.judge
  const calibre = report.calibre

  // 头条取格：theta_rows 的 θ0.4 行精确匹配（无档 → 暂无，不回退）
  const theta04 = report.theta_rows.find((row) => row.theta === OPEN_HEADLINE_THETA)
  const summary = [
    {
      key: 'accuracy40',
      title: `准确率(θ${OPEN_HEADLINE_THETA})`,
      value: theta04?.accuracy ?? null,
    },
    { key: 'new-pass', title: '新词通过率', value: pr.new_macro },
    { key: 'hit-pass', title: '命中通过率', value: pr.hit_macro },
    {
      key: 'agreement40',
      title: `标定一致率(θ${OPEN_HEADLINE_THETA})`,
      value: theta04?.agreement ?? null,
    },
    { key: 'new-ratio', title: '新词占比', value: report.new_ratio.macro },
    { key: 'ndcg', title: 'nDCG', value: report.ndcg.macro },
  ]

  // BarChart 三系组装：逐 θ 行（后端 theta_rows 序）逐系取值，None 点跳过
  const accuracyBars: BarItem[] = []
  for (const row of report.theta_rows) {
    for (const series of ACCURACY_SERIES) {
      const value = row[series.field]
      if (value !== null) {
        accuracyBars.push({ label: `θ${row.theta} ${series.name}`, value })
      }
    }
  }

  // 每片明细置顶排序（稳定：秩同值保持原序，idx 决胜）
  const sortedMovies = report.movies
    .map((row, idx) => ({ row, idx }))
    .sort((a, b) => movieRank(a.row) - movieRank(b.row) || a.idx - b.idx)
    .map((entry) => entry.row)

  const modalitiesText =
    calibre.modalities !== null && calibre.modalities.length > 0
      ? calibre.modalities.join('、')
      : NA

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* ① banner 三态警示逐条 + warnings 口径警示（非空才出） */}
      {report.banner.map((item) => (
        <Alert key={item} type="warning" showIcon message={item} />
      ))}
      {report.warnings.map((item) => (
        <Alert key={item} type="warning" showIcon message={item} />
      ))}

      {/* ② 汇总卡：样本核算五数 + 头条六值 */}
      <div data-testid="summary-section">
        <Typography.Title level={5}>汇总</Typography.Title>
        <div data-testid="accounting">
          <Descriptions
            size="small"
            column={5}
            items={[
              { key: 'total', label: '影片总数', children: acc.total },
              { key: 'ok', label: 'produce 成功', children: acc.ok },
              { key: 'failed', label: 'produce 失败', children: acc.failed },
              { key: 'review-failed', label: '评审失败', children: acc.review_failed },
              { key: 'invalid', label: '交集内命中为空', children: acc.invalid },
            ]}
          />
        </div>
        <Typography.Text type="secondary">
          {`产出行落在 库∩gt 交集外 ${report.no_gt} 条（不计分只计数）`}
        </Typography.Text>
        <div data-testid="summary-row" style={{ display: 'flex', gap: 48, flexWrap: 'wrap' }}>
          {summary.map((s) => (
            <div key={s.key} data-testid={`summary-${s.key}`}>
              <Statistic title={s.title} value={s.value === null ? NA : s.value} />
            </div>
          ))}
        </div>
      </div>

      {/* ③ θ 网格表 */}
      <div data-testid="theta-section">
        <Typography.Title level={5}>θ 网格（n = 交集内命中非空的片数）</Typography.Title>
        <Table<OpenThetaRow>
          rowKey={(r) => String(r.theta)}
          columns={thetaColumns}
          dataSource={report.theta_rows}
          pagination={false}
          size="small"
        />
      </div>

      {/* ④ 准确率 vs θ 三系 BarChart（全空 → 暂无兜底） */}
      <div data-testid="accuracy-chart-section">
        <Typography.Title level={5}>准确率 vs θ（系统 / 随机 / 先验）</Typography.Title>
        {accuracyBars.length > 0 ? (
          <BarChart items={accuracyBars} />
        ) : (
          <Typography.Text type="secondary">{NA}</Typography.Text>
        )}
      </div>

      {/* ⑤ 通过率与标定（宏/微/n + review_missing + 口径说明） */}
      <div data-testid="pass-rates-section">
        <Typography.Title level={5}>通过率与标定</Typography.Title>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span>{`命中通过率：宏 ${na(pr.hit_macro)} / 微 ${na(pr.hit_micro)}（${pr.hit_n} 片）`}</span>
          <span>{`新词通过率：宏 ${na(pr.new_macro)} / 微 ${na(pr.new_micro)}（${pr.new_n} 片）`}</span>
          <span>
            {`缺评审条目 ${pr.review_missing} 条（缺评审 ≠ 模型漏判：含评审失败片的整体缺位与评审成功片的单条缺席）`}
          </span>
        </div>
        <Typography.Text type="secondary">{CALIBRE_NOTE}</Typography.Text>
      </div>

      {/* ⑥ 伴生指标：产出规模 + 每片计数折叠 + judge 审计段 */}
      <div data-testid="produced-section">
        <Typography.Title level={5}>伴生指标</Typography.Title>
        <Typography.Text>{`min ${na(stats.min)} · 中位 ${na(stats.median)} · max ${na(stats.max)}`}</Typography.Text>
        <Typography.Text type="secondary">
          {`（${stats.counts.length} 部成功片逐片产出行数：交集内 + 交集外命中 + 新词）`}
        </Typography.Text>
        <Collapse
          items={[
            {
              key: 'produced-counts',
              label: '每片产出计数',
              children: (
                <Table<ProducedCount>
                  rowKey="tt"
                  columns={countColumns}
                  dataSource={stats.counts}
                  pagination={false}
                  size="small"
                />
              ),
            },
          ]}
        />
        <div data-testid="judge-section">
          <Typography.Title level={5}>评审官审计（judge 自省）</Typography.Title>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span>{`自滤率（accepted / merged）：${na(judge.self_filter)}`}</span>
            <span>{`verdicts 分布：${formatCounts(judge.verdicts, ' / ')}`}</span>
            <span>{`dropped 原因：${formatCounts(judge.dropped, '、', '×')}`}</span>
            <span>{`对齐分档：${formatCounts(judge.bands, ' / ')}`}</span>
          </div>
        </div>
      </div>

      {/* ⑦ 每片明细折叠表（失败/评审失败置顶） */}
      <div data-testid="movie-section">
        <Typography.Title level={5}>每片明细</Typography.Title>
        <Collapse
          items={[
            {
              key: 'movies',
              label: `每片明细（${report.movies.length}）`,
              children: (
                <Table<OpenMovieRow>
                  rowKey="tt"
                  columns={movieColumns}
                  dataSource={sortedMovies}
                  pagination={false}
                  size="small"
                />
              ),
            },
          ]}
        />
      </div>

      {/* ⑧ 失败清单两块分开（空 → 整块不渲染） */}
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
      {report.review_failed_movies.length > 0 && (
        <div data-testid="review-failed-section">
          <Typography.Title level={5}>
            {`评审失败影片（${report.review_failed_movies.length}）`}
          </Typography.Title>
          <Table
            rowKey="tt"
            columns={reviewFailedColumns}
            dataSource={report.review_failed_movies}
            pagination={false}
            size="small"
          />
        </div>
      )}

      {/* ⑨ 产物下载五链接 */}
      <div data-testid="downloads-section">
        <Typography.Title level={5}>产物下载</Typography.Title>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
          {DOWNLOADS.map((name) => (
            <a key={name} href={openFileUrl(runId, name)} download>
              {name}
            </a>
          ))}
        </div>
      </div>

      {/* ⑩ 银标准警示恒渲染 + calibre 小字（review/same_model/modalities 增量） */}
      <Alert
        type="warning"
        showIcon
        message="银标准警示"
        description={
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span>{report.note}</span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="calibre">
              <span>
                {`模型 ${naText(calibre.llm_model)} · 向量 ${naText(calibre.embedding_model)} · prompt ${naText(calibre.prompt_version)} · 词库 ${naText(calibre.library_version)} · 数据集 ${naText(calibre.dataset_id)} · GT ${naText(calibre.gt_sha8)}`}
              </span>
              <span>
                {`评审模型 ${naText(calibre.review_model)}${calibre.review_same_model === true ? '（同模评审）' : ''} · 评审 prompt ${naText(calibre.review_prompt_version)} · 模态 ${modalitiesText}`}
              </span>
            </Typography.Text>
          </div>
        }
      />
    </div>
  )
}
