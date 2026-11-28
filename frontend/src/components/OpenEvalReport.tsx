// 开放评测报告视图（三维评审口径，分支式内容组织）：report.json 的只读
// 渲染——① banner 警示（list[str] 逐条 Alert.warning：部分运行/同模评审/
// 旧协议/小样本）+ warnings 口径警示 ② 汇总卡（样本核算 + 通过率族 + 三维
// 均分/满分率 + 新词占比）+ 一致性对照段（judge↔评审分歧率/三桶/逐维均差，
// schema v4 新增，缺键不渲染）③ 评分分布表（0-4 五档 + 无法判断 + 联合
// 满分率）④ judge 审计段（生产漏斗图 + 三维分聚合 + 淘汰原因 + 库内分档）
// ⑤ 逐片折叠明细（每标签三维分 + 理由，展开看评审理由（口径随 pass_mode）/
// 评审摘录/judge 参考；内层「生产审计」再展开时懒加载候选去向，0 条产出
// 的影片也有现场可看）⑥ 异常表（produce 失败 / 评审失败分开，产出不作废）
// ⑦ 产物下载五链接 ⑧ calibre 口径小字。null 一律「暂无」（None≠0），
// 一致性对照段内为「—」。
import { Alert, Collapse, Descriptions, Space, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import FunnelChart from './FunnelChart'
import OpenEvalMovieAudit from './OpenEvalMovieAudit'
import {
  openFileUrl,
  type DimensionMetrics,
  type DimensionScore,
  type JudgeReference,
  type OpenEvalReport as OpenEvalReportData,
  type OpenFileName,
  type OpenMovieRow,
  type QualityMetrics,
  type TagReview,
} from '../api/evalOpen'

const NA = '暂无'

const DIMENSIONS = [
  { key: 'relevance', title: '内容相关性' },
  { key: 'semantics', title: '语义准确性' },
  { key: 'value', title: '标签价值' },
] as const

/** 产物下载五件套（后端 files 端点白名单全量；report.json 有专用 /report
 * 端点、不在白名单）。 */
const DOWNLOADS: OpenFileName[] = [
  'report.md',
  'metrics.csv',
  'raw_results.json',
  'meta.json',
  'progress.jsonl',
]

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? NA : `${(value * 100).toFixed(1)}%`
}

function average(value: number | null | undefined): string {
  return value === null || value === undefined ? NA : `${value.toFixed(2)} / 4`
}

/** 一致性对照段专用 null 口径：「—」而非「暂无」（段内三桶/逐维数字的
 * 缺席语义与汇总卡的 None≠0 呈现分开）。 */
function deltaPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

function deltaMean(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2)
}

/** judge↔评审一致性对照段（summary.consistency，schema v4 新增；缺键
 * 整段不渲染——旧 run 无此块）：分歧率+三桶一行、逐维均差/一致率行。 */
function ConsistencySection({ consistency }: {
  consistency: NonNullable<OpenEvalReportData['summary']['consistency']>
}) {
  return (
    <section aria-label="一致性对照">
      <Typography.Title level={5}>judge↔评审一致性对照</Typography.Title>
      <Descriptions size="small" column={2} items={[
        {
          key: 'rate',
          label: '与 judge 分歧率',
          children: `${deltaPercent(consistency.disagreement_rate)}（可判 ${consistency.denominator} 行）`,
        },
        {
          key: 'buckets',
          label: '评审否决 / 无法判定 / 缺 judge 参考',
          children: `${consistency.review_rejected} / ${consistency.review_undetermined} / ${consistency.no_judge_reference}`,
        },
        ...DIMENSIONS.map(({ key, title }) => {
          const delta = consistency.axis_delta?.[key]
          return {
            key,
            label: `${title} judge 均差`,
            children: `均差 ${deltaMean(delta?.mean_abs)} / 一致率 ${deltaPercent(delta?.exact_rate)}（${delta?.n ?? 0} 行）`,
          }
        }),
      ]} />
    </section>
  )
}

/** 评分分布表（汇总与逐片共用）：逐维均分/满分率/0-4 分布/无法判断。 */
function ScoreDistribution({ metrics }: { metrics: QualityMetrics }) {
  type Row = DimensionMetrics & { key: string; title: string }
  const rows: Row[] = DIMENSIONS.map(({ key, title }) => ({ key, title, ...metrics[key] }))
  const columns: TableProps<Row>['columns'] = [
    { title: '评分维度', dataIndex: 'title' },
    { title: '均分', render: (_, row) => average(row.average) },
    { title: '满分率', render: (_, row) => percent(row.high_rate) },
    ...Array.from({ length: 5 }, (_, score) => ({
      key: `score-${score}`,
      title: `${score} 分`,
      render: (_: unknown, row: Row) => row.distribution[score] ?? '—',
    })),
    { title: '无法判断', dataIndex: 'undetermined' },
  ]
  return (
    <Space direction="vertical" style={{ display: 'flex' }}>
      <Table<Row> rowKey="key" columns={columns} dataSource={rows} size="small"
        pagination={false} scroll={{ x: 680 }} />
      <Space wrap size="large">
        <Typography.Text type="secondary">
          内容与语义同时满分 {percent(metrics.joint_high_rate)}
        </Typography.Text>
        <Typography.Text type="secondary">
          三维同时满分 {percent(metrics.three_dimension_high_rate)}
        </Typography.Text>
      </Space>
    </Space>
  )
}

function formatScoreValue(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'null'
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

function formatValueBreakdown(
  dimensions: DimensionScore['dimensions'],
): string | null {
  if (!dimensions) return null
  const p = dimensions.presentation?.score
  const s = dimensions.search?.score
  const r = dimensions.recommendation?.score
  return `呈现 ${formatScoreValue(p)} · 搜索 ${formatScoreValue(s)} · 推荐 ${formatScoreValue(r)}`
}

function formatJudgeValueBreakdown(scores: Record<string, number> | undefined): string {
  if (!scores) return ''
  const p = scores.presentation
  const s = scores.search
  const r = scores.recommendation
  if (p === undefined && s === undefined && r === undefined) return ''
  return `（呈现 ${formatScoreValue(p)} / 搜索 ${formatScoreValue(s)} / 推荐 ${formatScoreValue(r)}）`
}

/** 单维评分 cell：分值 + 三方向明细分（value 有明细时）+ 理由（null = 无法判断）。 */
function ScoreCell({ rating }: { rating: DimensionScore | null | undefined }) {
  if (!rating) return <Typography.Text type="secondary">—</Typography.Text>
  const breakdown = formatValueBreakdown(rating.dimensions)
  return (
    <Space direction="vertical" size={0}>
      <Typography.Text strong>
        {rating.score === null ? '无法判断' : `${formatScoreValue(rating.score)} / 4`}
      </Typography.Text>
      {breakdown && <Typography.Text type="secondary">{breakdown}</Typography.Text>}
      <Typography.Text type="secondary">{rating.reason}</Typography.Text>
    </Space>
  )
}

/** 逐片标签表行：命中/新词统一形状（组别 + 名称 + 评审 + judge 参考）。 */
interface LabelRow {
  key: string
  group: '命中' | '新词'
  name: string
  sceneIds: string[]
  desc: string | null
  level: string | null
  directory: string | null
  targetType: string | null
  judge_reference: JudgeReference | null
  review: TagReview | null
  reviewFailed: boolean
}

function movieLabelRows(movie: OpenMovieRow): LabelRow[] {
  return [
    ...movie.hits.map((hit, index) => ({
      key: `${movie.tt}-hit-${index}`,
      group: '命中' as const,
      name: hit.tag_name,
      sceneIds: hit.scene_ids ?? [],
      desc: hit.desc,
      level: hit.level,
      directory: hit.dir_name || hit.dir_id || null,
      targetType: hit.target_type ?? null,
      judge_reference: hit.judge_reference,
      review: hit.review, reviewFailed: !!movie.review_error,
    })),
    ...movie.new_words.map((word, index) => ({
      key: `${movie.tt}-new-${index}`,
      group: '新词' as const,
      name: word.name,
      sceneIds: word.scene_ids ?? [],
      desc: word.desc,
      level: word.level,
      directory: word.dir_name || word.dir_id || null,
      targetType: word.target_type ?? null,
      judge_reference: word.judge_reference,
      review: word.review, reviewFailed: !!movie.review_error,
    })),
  ]
}

function passTag({ review, reviewFailed }: LabelRow) {
  if (review === null) return <Tag>{reviewFailed ? '评审失败' : '待评'}</Tag>
  if (review.pass === null) return <Tag>{review.evaluated ? '无法判断' : '漏判'}</Tag>
  return review.pass ? <Tag color="green">通过</Tag> : <Tag color="red">未通过</Tag>
}

const MOUNT_LEVELS = new Map([
  ['content', '全片级'],
  ['scene', '场景级'],
  ['character', '角色级'],
])

const labelColumns: TableProps<LabelRow>['columns'] = [
  {
    title: '标签',
    dataIndex: 'name',
    render: (name: string, row) => (
      <Space direction="vertical" size={0}>
        <Space size={4}>
          <Tag>{row.group}</Tag>
          <Typography.Text strong>{name}</Typography.Text>
        </Space>
        {row.desc && <Typography.Text type="secondary">{row.desc}</Typography.Text>}
      </Space>
    ),
    width: '24%',
  },
  {
    title: '生成阶段',
    dataIndex: 'level',
    render: (level: string | null) => {
      if (level === 'scene') return <Tag color="cyan">一阶段</Tag>
      if (level === 'film') return <Tag>二阶段</Tag>
      return '—'
    },
    width: 90,
  },
  { title: '挂载目录', dataIndex: 'directory', render: (directory) => directory ?? '—', width: 128 },
  {
    title: '挂载层级',
    render: (_, row) => MOUNT_LEVELS.get(row.targetType ?? '') ?? '—',
    width: 90,
  },
  { title: '内容相关性', render: (_, row) => <ScoreCell rating={row.review?.relevance} /> },
  { title: '语义准确性', render: (_, row) => <ScoreCell rating={row.review?.semantics} /> },
  { title: '标签价值', render: (_, row) => <ScoreCell rating={row.review?.value} /> },
  { title: '评审', render: (_, row) => passTag(row) },
]

/** 展开行：评审理由（口径随 pass_mode）/评审摘录 / 摘录核验 / judge 参考
 * （仅供参考，评审独立判断）。reason 标签三分支：model = 旧协议模型直出
 * 保留意见；threshold = 阈值复算口径（模型只出分，reason 为评分口径文案）；
 * 缺 pass_mode = 更旧的 run 照旧「评分理由汇总」。 */
function reasonLabel(passMode: 'model' | 'threshold' | undefined): string {
  if (passMode === 'model') return '评审理由'
  if (passMode === 'threshold') return '评分口径（通过按阈值复算）'
  return '评分理由汇总'
}

function labelDetail(row: LabelRow, passMode: 'model' | 'threshold' | undefined) {
  const reference = row.judge_reference
  return (
    <Descriptions size="small" column={1} items={[
      { key: 'reason', label: reasonLabel(passMode), children: row.review?.reason || '—' },
      { key: 'source', label: '生成来源场景', children: row.sceneIds.join('、') || '—' },
      { key: 'evidence', label: '评审引用场景', children: row.review?.evidence_scene_ids?.join('、') || '—' },
      ...(row.review?.quote ? [{ key: 'review-quote', label: '评审摘录', children: row.review.quote }] : []),
      { key: 'quote', label: '生产 judge 摘录', children: reference?.quote || '—' },
      {
        key: 'judge',
        label: 'judge 参考',
        children: reference ? (
          <Space direction="vertical" size={0}>
            <Typography.Text>
              相关 {formatScoreValue(reference.relevance)} · 语义 {formatScoreValue(reference.semantics)}
              {' '}· 价值 {formatScoreValue(reference.value)}
              {formatJudgeValueBreakdown(reference.value_scores)}（生产 judge，仅供参考）
            </Typography.Text>
            {reference.reason && (
              <Typography.Text type="secondary">{reference.reason}</Typography.Text>
            )}
          </Space>
        ) : '—（审计无该标签判决）',
      },
    ]} />
  )
}

function movieStatusTag(movie: OpenMovieRow) {
  if (!movie.ok) return <Tag color="volcano">produce 失败</Tag>
  if (movie.review_error) return <Tag color="orange">评审失败（产出保留）</Tag>
  return <Tag color="green">完成</Tag>
}

export default function OpenEvalReport({ report, runId }: { report: OpenEvalReportData; runId: string }) {
  const { summary, sample_accounting: acc, judge, calibre } = report
  const metrics = summary.metrics
  // 失败/评审失败片置顶，其余原序
  const movies = [...report.movies].sort((a, b) => {
    const rank = (m: OpenMovieRow) => (!m.ok ? 0 : m.review_error ? 1 : 2)
    return rank(a) - rank(b)
  })
  const judgeScoreRows = DIMENSIONS.map(({ key, title }) => ({
    key,
    title,
    ...(judge.scores[key] ?? { n: 0, average: null, distribution: [], null: 0 }),
  }))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {report.banner.map((text) => (
        <Alert key={text} type="warning" showIcon message={text} />
      ))}
      {report.warnings.map((text) => (
        <Alert key={text} type="info" showIcon message={text} />
      ))}

      <section aria-label="汇总">
        <Typography.Title level={5}>汇总</Typography.Title>
        <Descriptions size="small" column={3} items={[
          { key: 'movies', label: '影片', children: `${acc.ok} 产出成功（其中 ${acc.review_failed} 部评审失败） / ${acc.failed} 产出失败（共 ${acc.total}）` },
          { key: 'tags', label: '产出标签', children: `${summary.total_tags}（新词 ${summary.new_words} / 命中 ${summary.hits}）` },
          { key: 'reviewed', label: '已评审', children: `${summary.reviewed_tags} / ${summary.total_tags}（漏判或失败 ${summary.review_missing}，待评 ${summary.review_pending}）` },
          { key: 'pass', label: '评审通过率', children: `${percent(summary.pass_rate)}（${summary.passed_tags}/${summary.judged_tags}）` },
          { key: 'new_pass', label: '新词通过率', children: percent(summary.new_pass) },
          { key: 'hit_pass', label: '命中通过率', children: percent(summary.hit_pass) },
          { key: 'new_ratio', label: '新词占比', children: percent(summary.new_ratio) },
          { key: 'three_high', label: '三维同时满分', children: percent(metrics?.three_dimension_high_rate) },
          { key: 'undetermined', label: '无法判断', children: metrics ? `${metrics.undetermined_tags} 条` : NA },
        ]} />
      </section>

      {summary.consistency && <ConsistencySection consistency={summary.consistency} />}

      {metrics && (
        <section aria-label="评分分布">
          <Typography.Title level={5}>评分分布（独立评审，0-4）</Typography.Title>
          <ScoreDistribution metrics={metrics} />
        </section>
      )}

      <section aria-label="judge 审计">
        <Typography.Title level={5}>judge 审计与生产漏斗</Typography.Title>
        <Space direction="vertical" style={{ display: 'flex' }}>
          {judge.funnel ? (
            <FunnelChart funnel={judge.funnel} />
          ) : (
            <Typography.Text>数量漏斗 {NA}</Typography.Text>
          )}
          <Typography.Text>
            库内分档：
            {Object.entries(judge.bands).map(([band, count]) => `${band} ${count}`).join(' / ') || NA}
          </Typography.Text>
          <Table
            rowKey="key"
            size="small"
            pagination={false}
            dataSource={judgeScoreRows}
            columns={[
              { title: 'judge 维度', dataIndex: 'title' },
              { title: '均分', render: (_, row) => average(row.average) },
              { title: '评分条数', dataIndex: 'n' },
              { title: 'null', dataIndex: 'null' },
              ...Array.from({ length: 5 }, (_, score) => ({
                key: `judge-${score}`,
                title: `${score} 分`,
                render: (_: unknown, row: (typeof judgeScoreRows)[number]) =>
                  row.distribution[score] ?? '—',
              })),
            ]}
            scroll={{ x: 680 }}
          />
          {Object.keys(judge.dropped).length > 0 && (
            <Typography.Text type="secondary">
              淘汰原因：{Object.entries(judge.dropped).map(([why, count]) => `${why} ×${count}`).join('；')}
            </Typography.Text>
          )}
        </Space>
      </section>

      {movies.length > 0 && (
        <section aria-label="逐片明细">
          <Typography.Title level={5}>逐片明细</Typography.Title>
          <Collapse items={movies.map((movie) => ({
            key: movie.tt,
            label: (
              <Space wrap>
                <Typography.Text strong>{movie.title || movie.tt}</Typography.Text>
                <Typography.Text type="secondary">{movie.tt}</Typography.Text>
                {movieStatusTag(movie)}
                <Typography.Text type="secondary">
                  {movie.n_hits + movie.n_new_words} 条标签 · 通过率 {percent(movie.pass_rate)}
                </Typography.Text>
              </Space>
            ),
            children: (
              <Space direction="vertical" style={{ display: 'flex' }}>
                {movie.error && <Alert type="error" showIcon message={movie.error} />}
                {movie.review_error && (
                  <Alert type="warning" showIcon
                    message={`评审失败（产出保留，不计入通过率）：${movie.review_error}`} />
                )}
                {movie.metrics && <ScoreDistribution metrics={movie.metrics} />}
                {movieLabelRows(movie).length > 0 && (
                  <Table<LabelRow>
                    size="small"
                    rowKey="key"
                    columns={labelColumns}
                    dataSource={movieLabelRows(movie)}
                    pagination={false}
                    scroll={{ x: 1040 }}
                    expandable={{ expandedRowRender: (row) => labelDetail(row, calibre.review_config?.pass_mode) }}
                  />
                )}
                {/* 产出为 0 的影片上面一张表都没有：候选去向只能从这里看 */}
                <Collapse
                  size="small"
                  items={[{
                    key: 'audit',
                    label: '生产审计（候选去向与淘汰原因）',
                    children: <OpenEvalMovieAudit runId={runId} tt={movie.tt} />,
                  }]}
                />
              </Space>
            ),
          }))} />
        </section>
      )}

      {report.failed_movies.length > 0 && (
        <section aria-label="produce 失败影片">
          <Typography.Title level={5}>produce 失败（{report.failed_movies.length} 部）</Typography.Title>
          <Table
            rowKey="tt"
            size="small"
            pagination={false}
            dataSource={report.failed_movies}
            columns={[
              { title: '影片', dataIndex: 'tt' },
              { title: '原因', dataIndex: 'error' },
            ]}
          />
        </section>
      )}
      {report.review_failed_movies.length > 0 && (
        <section aria-label="评审失败影片">
          <Typography.Title level={5}>评审失败（{report.review_failed_movies.length} 部，产出保留）</Typography.Title>
          <Table
            rowKey="tt"
            size="small"
            pagination={false}
            dataSource={report.review_failed_movies}
            columns={[
              { title: '影片', dataIndex: 'tt' },
              { title: '原因', dataIndex: 'review_error' },
            ]}
          />
        </section>
      )}

      <section aria-label="产物下载">
        <Typography.Title level={5}>产物下载</Typography.Title>
        <Space direction="vertical" size={4}>
          {DOWNLOADS.map((name) => (
            <a key={name} href={openFileUrl(runId, name)} download>{name}</a>
          ))}
        </Space>
      </section>

      <Typography.Text type="secondary">
        口径：数据集 {calibre.dataset_id ?? NA} · 生成 {calibre.llm_model || NA}
        （{calibre.prompt_version ?? NA}） · 评审 {calibre.review_model ?? NA}
        （{calibre.review_prompt_version ?? NA}
        {calibre.review_same_model ? '，与生成同模——通过率绝对值需打折解读' : ''}）
        {calibre.review_config && (
          <> · 推理强度 {calibre.review_config.reasoning_effort ?? '模型默认'}
            {' '}· 输出上限 {calibre.review_config.max_tokens ?? NA}
            {' '}· 同时评审上限 {calibre.review_config.max_concurrent_requests ?? NA}</>
        )}
        {report.source_run_id && <> · 原运行 {report.source_run_id}</>}
        · 嵌入 {calibre.embedding_model || NA} · 库版本 {calibre.library_version ?? NA}
        · 模态 {(calibre.modalities ?? []).join(' + ') || NA}
      </Typography.Text>
    </div>
  )
}
