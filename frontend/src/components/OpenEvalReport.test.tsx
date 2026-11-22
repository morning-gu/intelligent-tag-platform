// 开放评测报告视图测试（open-eval Task 8）：以与后端 open_report.
// assemble_open_report 键逐一镜像的夹具驱动十块视图——① banner 三态警示
// （list[str] 逐条 Alert）+ warnings 口径警示（非空才出，alert 数量口径）
// ② 汇总卡：样本核算五数 Descriptions + no_gt 次行 + 头条六值 Statistic
// （θ0.4 行精确匹配取格，无 0.4 档 → 暂无不回退）③ θ 网格表（数据行数 =
// theta_rows 长；±CI 统一格式；CI 缺失只出值；全 null 行七个「暂无」）
// ④ 准确率 vs θ 三系 BarChart（系统/随机/先验逐 θ 组装，None 点跳过不造 0）
// ⑤ 通过率与标定（宏/微/n + 缺评审条目「缺评审 ≠ 模型漏判」字样 + 一致率
// 口径说明「不是评审员『错误率』」）⑥ 伴生指标（min/中位/max + 每片产出
// 计数 Collapse + judge 段四行，Record 键原样渲染——dropped 中文键「低置信」
// 直接出）⑦ 每片明细折叠表（默认折叠；展开后失败片置顶、评审失败次之、
// 其余原序；评审失败行通过率暂无）⑧ failed/review_failed 两块分开清单
// （错误原文；空 → 整块不渲染）⑨ 下载五链接 href（openFileUrl 纯路径拼接，
// 断言字面量期望防恒真）⑩ 银标准 note 恒渲染 + calibre 小字（review/
// same_model/modalities 增量字段；null → 暂无）。null 一律「暂无」（None≠0）。
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import OpenEvalReport from './OpenEvalReport'
import type { OpenEvalReport as OpenEvalReportData } from '../api/evalOpen'

const RUN_ID = 'run-20261120-000000-aa00'

/** 夹具一：口径齐全的 run——banner 三态齐发（部分运行/同模/小样本）+
 * warnings 一条；θ 网格三行（0.4 全值 / 0.2 部分缺格 / 0.6 全 null 行）；
 * judge dropped 含中文键；movies 三行覆盖 正常/评审失败/produce 失败三态
 * （原序 故意乱序——验证置顶排序）；失败/评审失败清单各一部。 */
const REPORT: OpenEvalReportData = {
  banner: [
    '部分运行：指标仅覆盖完成影片，与全量口径不可直接横比',
    '评审与生成同模型，通过率绝对值系统性偏高，趋势对比可用、绝对值需打折解读',
    '小样本警示：produce 失败 5/30；评审失败 2/30，占比超 10%——成功样本少，指标波动大，勿单独引用',
  ],
  sample_accounting: { total: 30, ok: 25, failed: 5, review_failed: 2, invalid: 1 },
  no_gt: 7,
  theta_rows: [
    {
      theta: 0.4,
      n_valid: 22,
      accuracy: 0.66,
      accuracy_ci: [0.5, 0.8],
      accuracy_random: 0.21,
      accuracy_prior: 0.3,
      coverage: 0.75,
      n_calibrated: 20,
      agreement: 0.64,
      agreement_ci: [0.5, 0.75],
      fp_rate: 0.12,
      fn_rate: 0.31,
    },
    {
      theta: 0.2,
      n_valid: 22,
      accuracy: null,
      accuracy_ci: null,
      accuracy_random: 0.15,
      accuracy_prior: null,
      coverage: 0.7,
      n_calibrated: 18,
      agreement: 0.61,
      agreement_ci: null,
      fp_rate: 0.14,
      fn_rate: 0.24,
    },
    {
      theta: 0.6,
      n_valid: 0,
      accuracy: null,
      accuracy_ci: null,
      accuracy_random: null,
      accuracy_prior: null,
      coverage: null,
      n_calibrated: 0,
      agreement: null,
      agreement_ci: null,
      fp_rate: null,
      fn_rate: null,
    },
  ],
  pass_rates: {
    new_macro: 0.8,
    new_micro: 0.82,
    new_n: 23,
    hit_macro: 0.72,
    hit_micro: 0.74,
    hit_n: 23,
    review_missing: 3,
  },
  ndcg: { macro: 0.58, ci: [0.45, 0.7] },
  new_ratio: { macro: 0.21 },
  produced_stats: {
    counts: [
      { tt: 'tt0000001', hits_in: 12, hits_out: 2, new_words: 5 },
      { tt: 'tt0000002', hits_in: 9, hits_out: 1, new_words: 3 },
    ],
    min: 8,
    median: 15.5,
    max: 30,
  },
  judge: {
    self_filter: 0.15,
    verdicts: { supported: 40, insufficient: 5, contradicted: 2 },
    dropped: { 低置信: 3, 低相关: 1 },
    bands: { duplicate: 30, suspect: 5, new: 4 },
  },
  calibre: {
    llm_model: 'qwen3.8-27b',
    embedding_model: 'bge-m3',
    prompt_version: 'open-v1',
    review_model: 'qwen3.8-27b',
    review_same_model: true,
    review_prompt_version: 'open-review-v1',
    library_version: 'v3',
    dataset_id: 'genome-scores',
    gt_sha8: 'abcd1234',
    modalities: ['script', 'dialogue'],
  },
  movies: [
    {
      tt: 'tt0000001',
      title: '铁道游击队',
      ok: true,
      review: 'ok',
      hits_n: 14,
      new_n: 5,
      accuracy: 0.83,
      new_pass: 0.85,
      hit_pass: 0.91,
      error: null,
      review_error: null,
      elapsed_ms: 4210,
    },
    {
      tt: 'tt0000003',
      title: '芙蓉镇',
      ok: true,
      review: 'failed',
      hits_n: 10,
      new_n: 2,
      accuracy: 0.55,
      new_pass: null,
      hit_pass: null,
      error: null,
      review_error: 'ReviewRequestError: 评审端点超时',
      elapsed_ms: 3800,
    },
    {
      tt: 'tt0000002',
      title: '高山下的花环',
      ok: false,
      review: 'skipped',
      hits_n: 0,
      new_n: 0,
      accuracy: null,
      new_pass: null,
      hit_pass: null,
      error: 'RuntimeError: produce 失败',
      review_error: null,
      elapsed_ms: 1200,
    },
  ],
  failed_movies: [{ tt: 'tt0000002', error: 'RuntimeError: produce 失败' }],
  review_failed_movies: [
    { tt: 'tt0000003', review_error: 'ReviewRequestError: 评审端点超时' },
  ],
  warnings: ['评审官审计（audit/）缺失或为空，judge 段无数据'],
  note: '银标准警示：genome relevance 是相关性模型输出，近似而非人工标注（银标准原文占位）',
}

/** 夹具二：最薄口径——banner/warnings 空数组（全量干净 run）、theta_rows
 * 空（θ0.4 无档 → 头条暂无不回退）、通过率/nDCG/新词占比/产出统计全 null、
 * judge 无审计（self_filter null、dropped 空）、calibre 全可空字段 null、
 * 空 movies/空失败清单。 */
const MINIMAL_REPORT: OpenEvalReportData = {
  banner: [],
  sample_accounting: { total: 10, ok: 10, failed: 0, review_failed: 0, invalid: 0 },
  no_gt: 0,
  theta_rows: [],
  pass_rates: {
    new_macro: null,
    new_micro: null,
    new_n: 0,
    hit_macro: null,
    hit_micro: null,
    hit_n: 0,
    review_missing: 2,
  },
  ndcg: { macro: null, ci: null },
  new_ratio: { macro: null },
  produced_stats: { counts: [], min: null, median: null, max: null },
  judge: {
    self_filter: null,
    verdicts: { supported: 0, insufficient: 0, contradicted: 0 },
    dropped: {},
    bands: { duplicate: 0, suspect: 0, new: 0 },
  },
  calibre: {
    llm_model: 'llm-x',
    embedding_model: 'emb-x',
    prompt_version: null,
    review_model: null,
    review_same_model: null,
    review_prompt_version: null,
    library_version: null,
    dataset_id: null,
    gt_sha8: null,
    modalities: null,
  },
  movies: [],
  failed_movies: [],
  review_failed_movies: [],
  warnings: [],
  note: 'note 原文占位',
}

/** 表格数据行数（排除 antd 可能插入的 aria-hidden 测量行）。 */
function dataRows(section: HTMLElement): number {
  return Array.from(section.querySelectorAll('tbody tr')).filter(
    (tr) => tr.getAttribute('aria-hidden') !== 'true',
  ).length
}

/** 区块内第 i 条 tbody 数据行（行级断言用，规避跨区块同值文本碰撞）。 */
function dataRowAt(sectionTestId: string, i: number): HTMLElement {
  const section = screen.getByTestId(sectionTestId)
  const rows = Array.from(section.querySelectorAll<HTMLElement>('tbody tr')).filter(
    (tr) => tr.getAttribute('aria-hidden') !== 'true',
  )
  expect(rows.length).toBeGreaterThan(i)
  return rows[i]!
}

describe('OpenEvalReport 报告视图', () => {
  it('banner 三态警示逐条渲染 + warnings 口径警示；最薄口径只剩 note 一条 alert', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    // banner 三态（部分运行/同模/小样本）逐条 Alert + warnings 一条
    expect(
      screen.getByText('部分运行：指标仅覆盖完成影片，与全量口径不可直接横比'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        '评审与生成同模型，通过率绝对值系统性偏高，趋势对比可用、绝对值需打折解读',
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        '小样本警示：produce 失败 5/30；评审失败 2/30，占比超 10%——成功样本少，指标波动大，勿单独引用',
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText('评审官审计（audit/）缺失或为空，judge 段无数据'),
    ).toBeInTheDocument()
    // 3 banner + 1 warning + 1 note = 5 条 alert
    expect(screen.getAllByRole('alert')).toHaveLength(5)
  })

  it('汇总卡：样本核算五数 + no_gt 次行 + 头条六值取 θ0.4 行精确匹配', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    // 样本核算五数（Descriptions 单元格逐值断言，作用域内数字互不碰撞）
    const accounting = screen.getByTestId('accounting')
    expect(within(accounting).getByText('30')).toBeInTheDocument()
    expect(within(accounting).getByText('25')).toBeInTheDocument()
    expect(within(accounting).getByText('5')).toBeInTheDocument()
    expect(within(accounting).getByText('2')).toBeInTheDocument()
    expect(within(accounting).getByText('1')).toBeInTheDocument()
    // no_gt 次行（不计分只计数）
    expect(
      screen.getByText('产出行落在 库∩gt 交集外 7 条（不计分只计数）'),
    ).toBeInTheDocument()

    // 头条六值：准确率/标定一致率取 theta_rows θ0.4 行（exact match），
    // 通过率取 pass_rates 宏平均、nDCG 取 ndcg.macro
    expect(screen.getByTestId('summary-accuracy40')).toHaveTextContent('0.66')
    expect(screen.getByTestId('summary-new-pass')).toHaveTextContent('0.8')
    expect(screen.getByTestId('summary-hit-pass')).toHaveTextContent('0.72')
    expect(screen.getByTestId('summary-agreement40')).toHaveTextContent('0.64')
    expect(screen.getByTestId('summary-new-ratio')).toHaveTextContent('0.21')
    expect(screen.getByTestId('summary-ndcg')).toHaveTextContent('0.58')
    // 卡题锚定头条 θ（与历史表列头同字面量）
    expect(
      within(screen.getByTestId('summary-accuracy40')).getByText('准确率(θ0.4)'),
    ).toBeInTheDocument()
    expect(
      within(screen.getByTestId('summary-agreement40')).getByText('标定一致率(θ0.4)'),
    ).toBeInTheDocument()
  })

  it('θ 网格表：行数 = theta_rows 长，±CI 统一格式、CI 缺失只出值、全 null 行七个「暂无」', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    expect(dataRows(screen.getByTestId('theta-section'))).toBe(3)
    // θ0.4 行：accuracy/agreement 带 CI 的统一 `值 ± [lo, hi]` 格式
    const row04 = dataRowAt('theta-section', 0)
    expect(within(row04).getByText('0.66 ± [0.5, 0.8]')).toBeInTheDocument()
    expect(within(row04).getByText('0.64 ± [0.5, 0.75]')).toBeInTheDocument()
    // θ0.2 行：accuracy/prior 无值 → 暂无；agreement 有值但 CI 缺失 → 只出值
    const row02 = dataRowAt('theta-section', 1)
    expect(within(row02).getAllByText('暂无')).toHaveLength(2)
    expect(within(row02).getByText('0.61')).toBeInTheDocument()
    expect(within(row02).getByText('0.15')).toBeInTheDocument()
    // θ0.6 全 null 行：准确率/随机/先验/覆盖/一致率/FP/FN 七个「暂无」（None≠0）
    const row06 = dataRowAt('theta-section', 2)
    expect(within(row06).getAllByText('暂无')).toHaveLength(7)
  })

  it('准确率 vs θ 三系 BarChart：系统/随机/先验逐 θ 组装，None 点跳过不造 0', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    // θ0.4 三系齐备
    expect(screen.getByTestId('bar-row-θ0.4 系统')).toHaveTextContent('0.66')
    expect(screen.getByTestId('bar-row-θ0.4 随机')).toHaveTextContent('0.21')
    expect(screen.getByTestId('bar-row-θ0.4 先验')).toHaveTextContent('0.3')
    // θ0.2 只剩随机有值（accuracy/prior null → 不渲染该条，不出 0 假值）
    expect(screen.getByTestId('bar-row-θ0.2 随机')).toHaveTextContent('0.15')
    expect(screen.queryByTestId('bar-row-θ0.2 系统')).not.toBeInTheDocument()
    expect(screen.queryByTestId('bar-row-θ0.2 先验')).not.toBeInTheDocument()
    // θ0.6 全 null → 一条不出
    expect(screen.queryByTestId('bar-row-θ0.6 系统')).not.toBeInTheDocument()
    expect(screen.queryByTestId('bar-row-θ0.6 随机')).not.toBeInTheDocument()
    expect(screen.queryByTestId('bar-row-θ0.6 先验')).not.toBeInTheDocument()
  })

  it('通过率与标定：宏/微/n 三行 + 缺评审条目字样 + 一致率口径说明', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    expect(screen.getByText('命中通过率：宏 0.72 / 微 0.74（23 片）')).toBeInTheDocument()
    expect(screen.getByText('新词通过率：宏 0.8 / 微 0.82（23 片）')).toBeInTheDocument()
    // 「缺评审条目」字样（勿用「模型漏判」）
    expect(
      screen.getByText(
        '缺评审条目 3 条（缺评审 ≠ 模型漏判：含评审失败片的整体缺位与评审成功片的单条缺席）',
      ),
    ).toBeInTheDocument()
    // 假阳/假阴说明：评审 pass 语义 ≠ gt 相关语义，一致率是口径对齐不是错误率
    expect(
      screen.getByText(/一致率衡量两个口径的对齐程度，不是评审员「错误率」/),
    ).toBeInTheDocument()
    expect(screen.getByText(/假阳率（FP）/)).toBeInTheDocument()
    expect(screen.getByText(/假阴率（FN）/)).toBeInTheDocument()
  })

  it('伴生指标：min/中位/max 一行 + 每片产出计数 Collapse 默认折叠、展开后计数行出现', async () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    expect(screen.getByText('min 8 · 中位 15.5 · max 30')).toBeInTheDocument()
    // 默认折叠：计数行不渲染
    expect(screen.queryByText('tt0000001')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('每片产出计数'))
    expect(await screen.findByText('tt0000001')).toBeInTheDocument()
    const countRow = screen.getByText('tt0000001').closest('tr')!
    expect(countRow).toHaveTextContent('12') // 交集内命中
    expect(countRow).toHaveTextContent('2') // 交集外命中
    expect(countRow).toHaveTextContent('5') // 新词
  })

  it('judge 段：自滤率/verdicts/dropped/bands 四行，Record 键原样渲染（中文键直出）', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    const judge = screen.getByTestId('judge-section')
    expect(within(judge).getByText('自滤率（accepted / merged）：0.15')).toBeInTheDocument()
    expect(
      within(judge).getByText('verdicts 分布：supported 40 / insufficient 5 / contradicted 2'),
    ).toBeInTheDocument()
    // dropped 中文键原样渲染（不翻译不映射）
    expect(within(judge).getByText('dropped 原因：低置信 ×3、低相关 ×1')).toBeInTheDocument()
    expect(within(judge).getByText('对齐分档：duplicate 30 / suspect 5 / new 4')).toBeInTheDocument()
  })

  it('每片明细折叠表：默认折叠，展开后 失败片置顶、评审失败次之、其余原序', async () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    expect(screen.queryByText('tt0000001')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('每片明细（3）'))

    const movieSection = screen.getByTestId('movie-section')
    const tt1 = await within(movieSection).findByText('tt0000001')
    const tt2 = within(movieSection).getByText('tt0000002')
    const tt3 = within(movieSection).getByText('tt0000003')
    // 排序：tt0000002（produce 失败）最前 → tt0000003（评审失败）次之 →
    // tt0000001（正常）保持原序兜底
    expect(
      tt2.compareDocumentPosition(tt3) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(
      tt3.compareDocumentPosition(tt1) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()

    // produce 失败行：产出 失败 / 评审 跳过 / 准确率与通过率三个「暂无」
    const rowSkipped = tt2.closest('tr')!
    expect(within(rowSkipped).getByText('失败')).toBeInTheDocument()
    expect(within(rowSkipped).getByText('跳过')).toBeInTheDocument()
    expect(within(rowSkipped).getAllByText('暂无')).toHaveLength(3)
    // 评审失败行：产出 成功 / 评审 失败 / 片级通过率整体暂无（准确率照常可算）
    const rowReviewFailed = tt3.closest('tr')!
    expect(within(rowReviewFailed).getByText('成功')).toBeInTheDocument()
    expect(within(rowReviewFailed).getAllByText('暂无')).toHaveLength(2)
    expect(rowReviewFailed).toHaveTextContent('0.55')
    // 正常行：评审 正常，无暂无
    const rowOk = tt1.closest('tr')!
    expect(within(rowOk).getByText('正常')).toBeInTheDocument()
    expect(within(rowOk).queryByText('暂无')).not.toBeInTheDocument()
    expect(rowOk).toHaveTextContent('4210')
  })

  it('失败清单两块分开：错误原文；下载五链接 href 为 openFileUrl 拼路径的字面量口径', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    // 两块分开渲染（failed 在前、review_failed 在后），错误原文直出
    expect(screen.getByTestId('failed-section')).toBeInTheDocument()
    expect(screen.getByText('失败影片（1）')).toBeInTheDocument()
    expect(screen.getByText('RuntimeError: produce 失败')).toBeInTheDocument()
    expect(screen.getByTestId('review-failed-section')).toBeInTheDocument()
    expect(screen.getByText('评审失败影片（1）')).toBeInTheDocument()
    expect(screen.getByText('ReviewRequestError: 评审端点超时')).toBeInTheDocument()
    // 块序：failed-section 在 review-failed-section 之前
    expect(
      screen.getByTestId('failed-section').compareDocumentPosition(
        screen.getByTestId('review-failed-section'),
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()

    // 下载五件套（白名单全量；href 为 openFileUrl 纯路径拼接的字面量期望）
    const files = ['report.md', 'metrics.csv', 'raw_results.json', 'meta.json', 'progress.jsonl']
    for (const name of files) {
      expect(screen.getByRole('link', { name })).toHaveAttribute(
        'href',
        `/api/v1/eval/open/runs/${RUN_ID}/files/${name}`,
      )
    }
  })

  it('银标准 note 恒渲染 + calibre 小字（同模评审标注与模态增量字段）', () => {
    render(<OpenEvalReport report={REPORT} runId={RUN_ID} />)

    expect(
      screen.getByText(
        '银标准警示：genome relevance 是相关性模型输出，近似而非人工标注（银标准原文占位）',
      ),
    ).toBeInTheDocument()
    const calibre = screen.getByTestId('calibre')
    expect(calibre).toHaveTextContent(
      '模型 qwen3.8-27b · 向量 bge-m3 · prompt open-v1 · 词库 v3 · 数据集 genome-scores · GT abcd1234',
    )
    // review/same_model/modalities 增量字段：同模评审 true → 括注
    expect(calibre).toHaveTextContent(
      '评审模型 qwen3.8-27b（同模评审） · 评审 prompt open-review-v1 · 模态 script、dialogue',
    )
  })

  it('最薄口径：头条六值暂无（θ0.4 无档不回退）、空块省略、judge 空段暂无、calibre null → 暂无', () => {
    render(<OpenEvalReport report={MINIMAL_REPORT} runId="run-y" />)

    // banner/warnings 空数组 → 只剩 note 一条 alert（恒渲染）
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByText('note 原文占位')).toBeInTheDocument()

    // θ 网格无 0.4 档 → 准确率/标定一致率暂无（头条精确匹配，不回退最近有值档）；
    // 通过率/nDCG/新词占比全 null → 暂无
    for (const key of ['accuracy40', 'new-pass', 'hit-pass', 'agreement40', 'new-ratio', 'ndcg']) {
      expect(within(screen.getByTestId(`summary-${key}`)).getByText('暂无')).toBeInTheDocument()
    }

    // 样本核算与 no_gt 次行照常渲染
    expect(
      screen.getByText('产出行落在 库∩gt 交集外 0 条（不计分只计数）'),
    ).toBeInTheDocument()

    // θ 表零数据行（剩 antd 空表占位行）；BarChart 无条目 → 「暂无」兜底
    expect(dataRows(screen.getByTestId('theta-section'))).toBe(1)
    expect(
      within(screen.getByTestId('accuracy-chart-section')).getByText('暂无'),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('bar-row-θ0.4 系统')).not.toBeInTheDocument()

    // 通过率与标定全 null → 「暂无」口径；缺评审条目计数照常
    expect(screen.getByText('命中通过率：宏 暂无 / 微 暂无（0 片）')).toBeInTheDocument()
    expect(screen.getByText('新词通过率：宏 暂无 / 微 暂无（0 片）')).toBeInTheDocument()
    expect(
      screen.getByText(
        '缺评审条目 2 条（缺评审 ≠ 模型漏判：含评审失败片的整体缺位与评审成功片的单条缺席）',
      ),
    ).toBeInTheDocument()

    // 产出统计全 null → 一行三个「暂无」；judge 空段：自滤率/dropped 暂无，
    // verdicts/bands 已知档位前置 0 照常原样渲染
    expect(screen.getByText('min 暂无 · 中位 暂无 · max 暂无')).toBeInTheDocument()
    const judge = screen.getByTestId('judge-section')
    expect(within(judge).getByText('自滤率（accepted / merged）：暂无')).toBeInTheDocument()
    expect(
      within(judge).getByText('verdicts 分布：supported 0 / insufficient 0 / contradicted 0'),
    ).toBeInTheDocument()
    expect(within(judge).getByText('dropped 原因：暂无')).toBeInTheDocument()
    expect(within(judge).getByText('对齐分档：duplicate 0 / suspect 0 / new 0')).toBeInTheDocument()

    // 空失败清单 → 两块整块不渲染
    expect(screen.queryByTestId('failed-section')).not.toBeInTheDocument()
    expect(screen.queryByTestId('review-failed-section')).not.toBeInTheDocument()

    // calibre 全 null 字段 → 暂无（两行整行字面量断言）
    const calibre = screen.getByTestId('calibre')
    expect(calibre).toHaveTextContent(
      '模型 llm-x · 向量 emb-x · prompt 暂无 · 词库 暂无 · 数据集 暂无 · GT 暂无',
    )
    expect(calibre).toHaveTextContent('评审模型 暂无 · 评审 prompt 暂无 · 模态 暂无')
  })
})
