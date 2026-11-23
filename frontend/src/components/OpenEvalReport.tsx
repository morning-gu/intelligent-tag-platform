// 开放评测报告视图（三维评审口径，分支式内容组织）：report.json 的只读
// 渲染——① banner 警示（list[str] 逐条 Alert.warning：部分运行/同模评审/
// 小样本）+ warnings 口径警示 ② 汇总卡（样本核算 + 通过率族 + 三维均分/
// 满分率 + 新词占比）③ 评分分布表（0-4 五档 + 无法判断 + 联合满分率）
// ④ judge 审计段（生产侧自滤保留率 + 三维分聚合 + 淘汰原因 + 库内分档）
// ⑤ 逐片折叠明细（每标签三维分 + 理由，展开看评审理由/摘录/judge 参考）
// ⑥ 异常表（produce 失败 / 评审失败分开，产出不作废）⑦ 产物下载五链接
// ⑧ 银标准 note 恒渲染 + calibre 口径小字。null 一律「暂无」（None≠0）。
import { Alert, Collapse, Descriptions, Space, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
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

/** 单维评分 cell：分值 + 理由（null = 无法判断）。 */
function ScoreCell({ rating }: { rating: DimensionScore | null | undefined }) {
  if (!rating) return <Typography.Text type="secondary">—</Typography.Text>
  return (
    <Space direction="vertical" size={0}>
      <Typography.Text strong>
        {rating.score === null ? '无法判断' : `${rating.score} / 4`}
      </Typography.Text>
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
    title: '层级',
    dataIndex: 'level',
    render: (level: string | null) => {
      if (!level) return <Typography.Text type="secondary">—</Typography.Text>
      const isScene = level === 'scene'
      return <Tag color={isScene ? 'cyan' : 'default'}>{isScene ? '场景级' : '全片级'}</Tag>
    },
  },
  { title: '内容相关性', render: (_, row) => <ScoreCell rating={row.review?.relevance} /> },
  { title: '语义准确性', render: (_, row) => <ScoreCell rating={row.review?.semantics} /> },
  { title: '标签价值', render: (_, row) => <ScoreCell rating={row.review?.value} /> },
  { title: '评审', render: (_, row) => passTag(row) },
]

/** 展开行：评审理由 / 摘录核验 / judge 参考（仅供参考，评审独立判断）。 */
function labelDetail(row: LabelRow, modelDecision: boolean) {
  const reference = row.judge_reference
  return (
    <Descriptions size="small" column={1} items={[
      { key: 'reason', label: modelDecision ? '评审理由' : '评分理由汇总', children: row.review?.reason || '—' },
      { key: 'source', label: '生成来源场景', children: row.sceneIds.join('、') || '—' },
      { key: 'evidence', label: '评审引用场景', children: row.review?.evidence_scene_ids?.join('、') || '—' },
      { key: 'quote', label: '生产 judge 摘录', children: reference?.quote || '—' },
      {
        key: 'judge',
        label: 'judge 参考',
        children: reference ? (
          <Space direction="vertical" size={0}>
            <Typography.Text>
              相关 {reference.relevance ?? 'null'} · 语义 {reference.semantics ?? 'null'}
              {' '}· 价值 {reference.value ?? 'null'}（生产 judge，仅供参考）
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

      {metrics && (
        <section aria-label="评分分布">
          <Typography.Title level={5}>评分分布（独立评审，0-4）</Typography.Title>
          <ScoreDistribution metrics={metrics} />
        </section>
      )}

      <section aria-label="judge 审计">
        <Typography.Title level={5}>judge 审计（生产侧自滤）</Typography.Title>
        <Space direction="vertical" style={{ display: 'flex' }}>
          <Typography.Text>
            自滤保留率 {percent(judge.self_filter)} · 库内分档：
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
                    scroll={{ x: 760 }}
                    expandable={{ expandedRowRender: (row) => labelDetail(row, calibre.review_config?.pass_mode === 'model') }}
                  />
                )}
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

      <Alert type="warning" showIcon message={report.note} />
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
