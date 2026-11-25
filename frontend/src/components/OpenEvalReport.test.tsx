// 开放评测报告视图测试（三维评审口径，分支式内容组织）：以与后端
// open_report.assemble_open_report 键逐一镜像的夹具驱动各块视图——
// ① banner/warnings 逐条 Alert ② 汇总卡（通过率族
// + 新词占比 + 三维满分率，null → 暂无）③ 评分分布表（0-4 五档 + 无法
// 判断 + 联合满分率）④ judge 审计段（数量漏斗/三维聚合/dropped 中文键/
// bands）⑤ 逐片折叠明细（默认折叠；展开后失败片置顶、评审失败次之；
// 标签三维分 + 理由，展开行看评审理由/摘录/judge 参考）⑥ 失败清单两块
// 分开 ⑦ 下载五链接 href 字面量 ⑧ calibre 小字（同模标注）。
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import OpenEvalReport from './OpenEvalReport'
import type {
  DimensionMetrics,
  OpenEvalReport as OpenEvalReportData,
  OpenMovieRow,
  QualityMetrics,
  TagReview,
} from '../api/evalOpen'

const RUN_ID = 'run-20261120-000000-aa00'

function dim(over: Partial<DimensionMetrics> = {}): DimensionMetrics {
  return {
    evaluated_tags: 10, average: 3.2, high_rate: 0.5,
    distribution: [1, 1, 2, 3, 3], undetermined: 1, undetermined_rate: 0.1,
    ...over,
  }
}

function metrics(over: Partial<QualityMetrics> = {}): QualityMetrics {
  return {
    total_tags: 10, evaluated_tags: 10, max_score: 4, coverage: 1,
    joint_high_rate: 0.4, three_dimension_high_rate: 0.25,
    undetermined_tags: 1, undetermined_rate: 0.1,
    relevance: dim(), semantics: dim({ average: 3.0 }), value: dim({ average: 2.6 }),
    ...over,
  }
}

function review(pass: boolean | null, r = 4, s = 3, v = 3): TagReview {
  return {
    pass,
    relevance: { score: r, reason: '相关理由' },
    semantics: { score: s, reason: '语义理由' },
    value: { score: v, reason: '价值理由' },
    reason: '有独立检索价值，建议保留',
    evaluated: pass !== null, evidence_scene_ids: ['scene_1'],
  }
}

function movie(over: Partial<OpenMovieRow> = {}): OpenMovieRow {
  return {
    tt: 'tt0000001', title: '铁道游击队', ok: true, error: null,
    review_error: null, elapsed_ms: 1000, n_hits: 1, n_new_words: 1,
    hits: [{
      tag_id: 't1', tag_name: '悬疑', desc: null, scene_ids: ['scene_025'], level: 'film',
      dir_id: 'dir_mood', dir_name: '情绪氛围', target_type: 'content',
      judge_reference: { relevance: 4, semantics: 3, value: 3, reason: 'judge 依据', quote: '原文摘录' },
      review: review(true),
    }],
    new_words: [{
      name: '新词甲', desc: '通用释义', level: 'scene',
      dir_id: 'dir_story', target_type: 'content',
      judge_reference: null, review: review(false, 2),
    }],
    pass_rate: 0.5, new_pass: 0, hit_pass: 1, review_missing: 0, review_pending: 0,
    metrics: metrics(),
    ...over,
  }
}

function baseReport(over: Partial<OpenEvalReportData> = {}): OpenEvalReportData {
  return {
    schema_version: 3, status: 'cancelled', source_run_id: null,
    banner: ['部分运行：指标仅覆盖完成影片，与全量口径不可直接横比'],
    sample_accounting: { total: 3, ok: 2, failed: 1, review_failed: 0 },
    summary: {
      total_tags: 4, reviewed_tags: 4, passed_tags: 3, judged_tags: 4, hits: 2, new_words: 2,
      new_ratio: 0.5, pass_rate: 0.75, new_pass: 0.5, hit_pass: 1,
      review_missing: 0, review_pending: 0, metrics: metrics(),
    },
    produced_stats: {
      counts: [{ tt: 'tt0000001', hits: 1, new_words: 1 }],
      min: 2, median: 2, max: 2,
    },
    judge: {
      funnel: {
        raw: 8, merged: 4, verdicts: 4, judge_passed: 2, rank_kept: 2, final: 2,
        merge_keep_rate: 0.5, judge_coverage: 1, judge_pass_rate: 0.5,
        judge_pass_rate_scored: 0.5, rank_keep_rate: 1, dir_mount_rate: 1,
        final_keep_rate: 0.5,
      },
      scores: {
        relevance: { n: 4, average: 3.5, distribution: [0, 1, 0, 1, 2], null: 1 },
        semantics: { n: 5, average: 3.0, distribution: [0, 0, 1, 2, 2], null: 0 },
        value: { n: 5, average: 2.8, distribution: [0, 1, 1, 2, 1], null: 0 },
      },
      dropped: { 'judge 评分未过线（value=1 < 2）': 2 },
      bands: { duplicate: 3, suspect: 1, new: 2 },
    },
    calibre: {
      llm_model: 'qwen3.8-27b', embedding_model: 'bge-m3',
      prompt_version: 'mtg-v6-graded-judge', review_model: 'deepseek-v4.1-flash',
      review_same_model: false, review_prompt_version: 'open-review-v7',
      review_config: { pass_mode: 'model' },
      library_version: 'lib-v3', dataset_id: 'genome-scores',
      modalities: ['script', 'dialogue'],
    },
    movies: [movie()],
    failed_movies: [{ tt: 'tt0000003', error: 'RuntimeError: produce 失败' }],
    review_failed_movies: [],
    warnings: ['评审失败 1 部，已整体排除出通过率'],
    ...over,
  }
}

describe('OpenEvalReport 报告视图', () => {
  it('banner 与 warnings 逐条渲染', () => {
    render(<OpenEvalReport report={baseReport()} runId={RUN_ID} />)
    expect(screen.getByText('部分运行：指标仅覆盖完成影片，与全量口径不可直接横比')).toBeInTheDocument()
    expect(screen.getByText('评审失败 1 部，已整体排除出通过率')).toBeInTheDocument()
  })

  it('汇总卡：通过率族 + 新词占比 + 三维满分率，样本核算三类分开', () => {
    render(<OpenEvalReport report={baseReport()} runId={RUN_ID} />)
    const section = screen.getByRole('region', { name: '汇总' })
    expect(within(section).getByText('75.0%（3/4）')).toBeInTheDocument() // 评审通过率
    expect(within(section).getAllByText('50.0%').length).toBeGreaterThanOrEqual(2) // 新词通过率/新词占比同值
    expect(within(section).getByText('100.0%')).toBeInTheDocument() // 命中通过率
    expect(within(section).getByText('25.0%')).toBeInTheDocument() // 三维同时满分
    expect(within(section).getByText(/2 产出成功（其中 0 部评审失败） \/ 1 产出失败（共 3）/)).toBeInTheDocument()
    expect(within(section).getByText(/4（新词 2 \/ 命中 2）/)).toBeInTheDocument()
  })

  it('评分分布表：逐维均分/满分率/0-4 分布/无法判断 + 联合满分率', () => {
    render(<OpenEvalReport report={baseReport()} runId={RUN_ID} />)
    const section = screen.getByRole('region', { name: '评分分布' })
    expect(within(section).getByText('内容相关性')).toBeInTheDocument()
    expect(within(section).getByText('3.20 / 4')).toBeInTheDocument()
    expect(within(section).getByText('内容与语义同时满分 40.0%')).toBeInTheDocument()
    expect(within(section).getByText('三维同时满分 25.0%')).toBeInTheDocument()
  })

  it('judge 审计段：漏斗图 + 三维聚合 + dropped 中文键 + bands', () => {
    render(<OpenEvalReport report={baseReport()} runId={RUN_ID} />)
    const section = screen.getByRole('region', { name: 'judge 审计' })
    expect(within(section).getByRole('img', { name: /生产数量漏斗/ })).toBeInTheDocument()
    expect(within(section).getByText('Judge 通过')).toBeInTheDocument()
    // 保留/上游计数：Judge 通过 2/4；配额保留与最终生产保留同为 2/2
    expect(within(section).getAllByText('2 / 4')).toHaveLength(1)
    expect(within(section).getAllByText('2 / 2')).toHaveLength(2)
    expect(within(section).getByText('50.0% · 按已评分候选')).toBeInTheDocument()
    expect(within(section).getByText(/Judge 通过率（按合并候选）50\.0%/)).toBeInTheDocument()
    expect(within(section).getByText(/duplicate 3/)).toBeInTheDocument()
    expect(within(section).getByText(/judge 评分未过线（value=1 < 2） ×2/)).toBeInTheDocument()
    expect(within(section).getByText('3.50 / 4')).toBeInTheDocument() // judge relevance 均分
  })

  it('逐片明细：默认折叠，展开后标签三维分与理由出现；失败片置顶', async () => {
    const failed = movie({ tt: 'tt0000009', title: '失败片', ok: false,
      error: 'RuntimeError: boom', hits: [], new_words: [], n_hits: 0,
      n_new_words: 0, pass_rate: null, metrics: null })
    const report = baseReport({ movies: [movie(), failed] })
    render(<OpenEvalReport report={report} runId={RUN_ID} />)
    const section = screen.getByRole('region', { name: '逐片明细' })
    // 折叠态：标签名不出现
    expect(within(section).queryByText('悬疑')).not.toBeInTheDocument()
    fireEvent.click(within(section).getByText('铁道游击队'))
    expect(await within(section).findByText('悬疑')).toBeInTheDocument()
    expect(within(section).getByText('新词甲')).toBeInTheDocument()
    expect(within(section).getAllByText('4 / 4').length).toBeGreaterThan(0)
    expect(within(section).getAllByText('相关理由').length).toBeGreaterThan(0)
    expect(within(section).getByText('通过')).toBeInTheDocument()
    expect(within(section).getByText('未通过')).toBeInTheDocument()
    expect(within(section).getByRole('columnheader', { name: '生成阶段' })).toBeInTheDocument()
    expect(within(section).getByRole('columnheader', { name: '挂载目录' })).toBeInTheDocument()
    expect(within(section).getByRole('columnheader', { name: '挂载层级' })).toBeInTheDocument()
    const hit = within(section).getByRole('row', { name: /悬疑/ })
    expect(within(hit).getByText('二阶段')).toBeInTheDocument()
    expect(within(hit).getByText('情绪氛围')).toBeInTheDocument()
    const word = within(section).getByRole('row', { name: /新词甲/ })
    expect(within(word).getByText('一阶段')).toHaveClass('ant-tag-cyan')
    expect(within(word).getByText('dir_story')).toBeInTheDocument()
    expect(within(word).getByText('全片级')).toBeInTheDocument()
    // 失败片置顶：第一个折叠头是失败片
    const headers = within(section).getAllByText(/produce 失败|完成/)
    expect(headers[0]).toHaveTextContent('produce 失败')
  })

  it('标签展开行：评审理由 / 原文摘录 / judge 参考（仅供参考标注）', async () => {
    render(<OpenEvalReport report={baseReport()} runId={RUN_ID} />)
    const section = screen.getByRole('region', { name: '逐片明细' })
    fireEvent.click(within(section).getByText('铁道游击队'))
    await within(section).findByText('悬疑')
    // 展开第一行（antd 展开按钮 aria-label）
    const expandButtons = within(section).getAllByRole('button', { name: /展开行|Expand row/ })
    fireEvent.click(expandButtons[0])
    expect(await within(section).findByText('有独立检索价值，建议保留')).toBeInTheDocument()
    expect(within(section).getByText('评审理由')).toBeInTheDocument()
    expect(within(section).getByText('生成来源场景')).toBeInTheDocument()
    expect(within(section).getByText('scene_025')).toBeInTheDocument()
    expect(within(section).getByText('评审引用场景')).toBeInTheDocument()
    expect(await within(section).findByText('judge 依据')).toBeInTheDocument()
    expect(within(section).getByText(/生产 judge，仅供参考/)).toBeInTheDocument()
    expect(within(section).getAllByText('原文摘录').length).toBeGreaterThan(0)
  })

  it('评审失败片：警示注明产出保留，通过率暂无', async () => {
    const reviewed = movie({
      tt: 'tt0000002', title: '评审失败片',
      review_error: 'ReviewRequestError: 评审端点超时',
      hits: [{ tag_id: 't2', tag_name: '保留标签', desc: null, level: 'film', judge_reference: null, review: null }],
      new_words: [], n_hits: 1, n_new_words: 0,
      pass_rate: null, new_pass: null, hit_pass: null, metrics: null,
    })
    render(<OpenEvalReport report={baseReport({ movies: [reviewed] })} runId={RUN_ID} />)
    const section = screen.getByRole('region', { name: '逐片明细' })
    fireEvent.click(within(section).getByText('评审失败片'))
    expect(await within(section).findByText(/评审失败（产出保留，不计入通过率）/)).toBeInTheDocument()
    expect(within(section).getByText('评审失败', { exact: true })).toBeInTheDocument()
    expect(within(section).getByText(/通过率 暂无/)).toBeInTheDocument()
  })

  it('失败清单两块分开；下载五链接 href 为 openFileUrl 字面量口径', () => {
    const report = baseReport({
      review_failed_movies: [{ tt: 'tt0000002', review_error: 'ReviewRequestError: 超时' }],
    })
    render(<OpenEvalReport report={report} runId={RUN_ID} />)
    expect(screen.getByRole('region', { name: 'produce 失败影片' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '评审失败影片' })).toBeInTheDocument()
    expect(screen.getByText('RuntimeError: produce 失败')).toBeInTheDocument()
    expect(screen.getByText('ReviewRequestError: 超时')).toBeInTheDocument()
    for (const name of ['report.md', 'metrics.csv', 'raw_results.json', 'meta.json', 'progress.jsonl']) {
      expect(screen.getByRole('link', { name })).toHaveAttribute(
        'href', `/api/v1/eval/open/runs/${RUN_ID}/files/${name}`)
    }
  })

  it('calibre 小字：同模评审标注与模态；独立评审不标注', () => {
    const { rerender } = render(<OpenEvalReport report={baseReport()} runId={RUN_ID} />)
    expect(screen.getByText(/deepseek-v4.1-flash/)).toBeInTheDocument()
    expect(screen.queryByText(/与生成同模/)).not.toBeInTheDocument()
    const sameModel = baseReport()
    sameModel.calibre = { ...sameModel.calibre, review_model: 'qwen3.8-27b', review_same_model: true }
    rerender(<OpenEvalReport report={sameModel} runId={RUN_ID} />)
    expect(screen.getByText(/与生成同模——通过率绝对值需打折解读/)).toBeInTheDocument()
  })

  it('最薄口径：metrics null 无分布段、空 movies 无明细段、judge 空段暂无', () => {
    const thin = baseReport({
      banner: [], warnings: [],
      summary: {
        total_tags: 0, reviewed_tags: 0, passed_tags: 0, judged_tags: 0, hits: 0, new_words: 0,
        new_ratio: null, pass_rate: null, new_pass: null, hit_pass: null,
        review_missing: 0, review_pending: 0, metrics: null,
      },
      judge: {
        funnel: null,
        scores: {
          relevance: { n: 0, average: null, distribution: [], null: 0 },
          semantics: { n: 0, average: null, distribution: [], null: 0 },
          value: { n: 0, average: null, distribution: [], null: 0 },
        },
        dropped: {}, bands: {},
      },
      movies: [], failed_movies: [], review_failed_movies: [],
    })
    render(<OpenEvalReport report={thin} runId={RUN_ID} />)
    expect(screen.queryByRole('region', { name: '评分分布' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '逐片明细' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'produce 失败影片' })).not.toBeInTheDocument()
    expect(screen.getByText(/数量漏斗 暂无/)).toBeInTheDocument()
  })
})
