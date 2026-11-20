// 闭环评测报告视图测试（Task 9）：以与后端 report.json 组装结构同形的夹具驱动
// 九块视图——① banner/warning 非空才出警示（alert 数量口径）② 汇总五卡（同 K
// 有值格中 θ 最接近 0.4；θ=0.4 格无有效影片 → 回退最近有值格）③ θ×K 网格表
// （数据行数、±CI 统一格式、CI 缺失只出值、全 null 行五个「暂无」）④ 基线对照
// （系统/先验/随机三行，随机档无 nDCG → 暂无）⑤ 覆盖分布 5 桶 bar-row-* 计数
// （右开右闭边界）+ min/中位/max 一行文本 ⑥ Spearman 宏值 + 每片 Collapse
// （None → 暂无）⑦ 失败影片列表（空 → 整块不渲染）⑧ 下载三条 href（fileUrl
// 纯路径拼接，断言字面量期望防恒真）⑨ 银标准 note 原文 + calibre 小字（null
// 字段 → 暂无）。null 一律「暂无」（None≠0 纪律）。另测单片明细节：
// ClosedEvalMovieTable 以 vi.mock 顶替（其挂载即拉取摘要，jsdom 下会发真实
// fetch），只断言「单片明细」节的锚点顺序（spearman-section 之后、
// downloads-section 之前）。
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ClosedEvalReport from './ClosedEvalReport'
import type { ClosedEvalReport as ClosedEvalReportData } from '../api/eval'

vi.mock('./ClosedEvalMovieTable', () => ({ default: () => <div data-testid="movie-table" /> }))

/** 夹具一：口径齐全的 run——k=10 两档 θ（0.4 更近 0.4）；k=20 的 θ=0.4 档无
 * 有效影片（全 null → 汇总回退 θ=0.6 有值格）；k=5 全 null 行；先验两行；
 * 覆盖值命中全部五桶（含 0.2/0.8/1.0 边界）；失败影片一部；每片 Spearman
 * 含一个 None。 */
const REPORT: ClosedEvalReportData = {
  banner: '部分运行口径：取消收尾，仅含已完成影片',
  warning: '样本量偏小，指标波动较大',
  sample_accounting: { total: 30, ok: 26, failed: 2, invalid: 1, no_gt: 1 },
  grid: [
    {
      theta: 0.4,
      k: 10,
      n_valid: 26,
      precision: 0.52,
      recall: 0.2,
      ndcg: 0.48,
      precision_ci: [0.4, 0.64],
      recall_ci: [0.1, 0.3],
      ndcg_ci: [0.35, 0.61],
      precision_random: 0.02,
      recall_random: 0.08,
    },
    {
      theta: 0.6,
      k: 10,
      n_valid: 26,
      precision: 0.46,
      recall: 0.18,
      ndcg: 0.42,
      precision_ci: null,
      recall_ci: null,
      ndcg_ci: null,
      precision_random: 0.03,
      recall_random: 0.07,
    },
    {
      theta: 0.4,
      k: 20,
      n_valid: 0,
      precision: null,
      recall: null,
      ndcg: null,
      precision_ci: null,
      recall_ci: null,
      ndcg_ci: null,
      precision_random: null,
      recall_random: null,
    },
    {
      theta: 0.6,
      k: 20,
      n_valid: 26,
      precision: 0.3,
      recall: 0.35,
      ndcg: 0.41,
      precision_ci: [0.2, 0.4],
      recall_ci: [0.25, 0.45],
      ndcg_ci: [0.31, 0.51],
      precision_random: 0.01,
      recall_random: 0.1,
    },
    {
      theta: 0.4,
      k: 5,
      n_valid: 0,
      precision: null,
      recall: null,
      ndcg: null,
      precision_ci: null,
      recall_ci: null,
      ndcg_ci: null,
      precision_random: null,
      recall_random: null,
    },
  ],
  baselines: {
    prior: [
      {
        theta: 0.4,
        k: 10,
        n_valid: 26,
        precision: 0.11,
        recall: 0.05,
        ndcg: 0.09,
        precision_ci: null,
        recall_ci: null,
        ndcg_ci: null,
        precision_random: null,
        recall_random: null,
      },
      {
        theta: 0.4,
        k: 20,
        n_valid: 26,
        precision: 0.08,
        recall: 0.12,
        ndcg: 0.07,
        precision_ci: null,
        recall_ci: null,
        ndcg_ci: null,
        precision_random: null,
        recall_random: null,
      },
    ],
  },
  coverage: {
    min: 0.05,
    median: 0.5,
    max: 1.0,
    values: [0, 0.19, 0.2, 0.39, 0.4, 0.5, 0.79, 0.8, 1],
  },
  spearman: {
    macro: 0.42,
    per_movie: [
      { tt: 'tt0000001', spearman: 0.5 },
      { tt: 'tt0000002', spearman: null },
    ],
  },
  failed_movies: [{ tt: 'tt0000009', error: 'LLM 超时' }],
  calibre: {
    llm_model: 'qwen3.8-27b',
    embedding_model: 'bge-m3',
    prompt_version: 'closed-v1',
    library_version: 'v3',
    dataset_id: 'genome-scores',
    gt_sha8: 'abcd1234',
  },
  note: 'genome relevance 是模型输出非人工标注（银标准警示）',
}

/** 夹具二：最薄口径——k=10 不在档位、覆盖统计全 null、空先验/空失败/空每片、
 * calibre 四个 null 字段（「暂无」与整块省略的口径验证）。 */
const MINIMAL_REPORT: ClosedEvalReportData = {
  banner: null,
  sample_accounting: { total: 10, ok: 10, failed: 0, invalid: 0, no_gt: 0 },
  grid: [
    {
      theta: 0.4,
      k: 20,
      n_valid: 10,
      precision: 0.3,
      recall: 0.35,
      ndcg: 0.4,
      precision_ci: null,
      recall_ci: null,
      ndcg_ci: null,
      precision_random: 0.01,
      recall_random: 0.09,
    },
  ],
  baselines: { prior: [] },
  coverage: { min: null, median: null, max: null, values: [] },
  spearman: { macro: null, per_movie: [] },
  failed_movies: [],
  warning: null,
  calibre: {
    llm_model: 'llm-x',
    embedding_model: 'emb-x',
    prompt_version: null,
    library_version: null,
    dataset_id: null,
    gt_sha8: null,
  },
  note: 'note 原文占位',
}

const RUN_ID = 'run-20261120-000000-aa00'

/** 表格数据行数（排除 antd 可能插入的 aria-hidden 测量行）。 */
function dataRows(section: HTMLElement): number {
  return Array.from(section.querySelectorAll('tbody tr')).filter(
    (tr) => tr.getAttribute('aria-hidden') !== 'true',
  ).length
}

describe('ClosedEvalReport 报告视图', () => {
  it('banner/warning 警示非空才出；汇总五卡取 θ 最接近 0.4 的有值格', () => {
    render(<ClosedEvalReport report={REPORT} runId={RUN_ID} />)

    // ① banner + warning + note(info) 共三条 alert
    expect(screen.getByText('部分运行口径：取消收尾，仅含已完成影片')).toBeInTheDocument()
    expect(screen.getByText('样本量偏小，指标波动较大')).toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(3)

    // ② 汇总五卡：P@10/nDCG@10 取 θ=0.4 的 k=10 格（θ=0.6 档更远不取）；
    // R@20 的 θ=0.4 档无有效影片 → 回退 θ=0.6 有值格。数值断言用
    // toHaveTextContent（antd Statistic 把小数拆 int/decimal 两个 span，
    // getByText 的直接文本节点匹配不命中——与页面历史表断言同口径）
    expect(screen.getByTestId('summary-ndcg10')).toHaveTextContent('0.48')
    expect(screen.getByTestId('summary-p10')).toHaveTextContent('0.52')
    expect(screen.getByTestId('summary-r20')).toHaveTextContent('0.35')
    expect(screen.getByTestId('summary-coverage-median')).toHaveTextContent('0.5')
    expect(screen.getByTestId('summary-spearman-macro')).toHaveTextContent('0.42')
  })

  it('θ×K 网格表：数据行数齐全、±CI 统一格式、CI 缺失只出值、全 null 行五个「暂无」', () => {
    render(<ClosedEvalReport report={REPORT} runId={RUN_ID} />)

    expect(dataRows(screen.getByTestId('grid-section'))).toBe(5)
    // ③ ±CI 统一 `值 ± [lo, hi]` 格式
    expect(screen.getByText('0.52 ± [0.4, 0.64]')).toBeInTheDocument()
    expect(screen.getByText('0.2 ± [0.1, 0.3]')).toBeInTheDocument()
    expect(screen.getByText('0.48 ± [0.35, 0.61]')).toBeInTheDocument()
    expect(screen.getByText('0.35 ± [0.25, 0.45]')).toBeInTheDocument()
    // θ=0.6 的 k=10 行：指标有值但 CI 缺失 → 只出值
    expect(screen.getByText('0.46')).toBeInTheDocument()
    // k=5 全 null 行：P/R/nDCG/随机 P/随机 R 五个「暂无」（None≠0）
    const emptyRow = screen.getByText('5').closest('tr')!
    expect(within(emptyRow).getAllByText('暂无')).toHaveLength(5)
  })

  it('基线对照三行：系统/先验/随机——随机档无 nDCG → 暂无', () => {
    render(<ClosedEvalReport report={REPORT} runId={RUN_ID} />)

    const sysRow = screen.getByText('系统').closest('tr')!
    expect(sysRow).toHaveTextContent('0.52') // P@10（θ=0.4 格）
    expect(sysRow).toHaveTextContent('0.35') // R@20（回退 θ=0.6 有值格）
    expect(sysRow).toHaveTextContent('0.48') // nDCG@10

    const priorRow = screen.getByText('先验').closest('tr')!
    expect(priorRow).toHaveTextContent('0.11') // P@10
    expect(priorRow).toHaveTextContent('0.12') // R@20
    expect(priorRow).toHaveTextContent('0.09') // nDCG@10

    const randomRow = screen.getByText('随机').closest('tr')!
    expect(randomRow).toHaveTextContent('0.02') // P@10 = θ=0.4 的 precision_random
    expect(randomRow).toHaveTextContent('0.1') // R@20 = 回退 θ=0.6 的 recall_random
    expect(within(randomRow).getByText('暂无')).toBeInTheDocument() // 契约无 ndcg_random
  })

  it('覆盖分布：5 桶右开右闭边界计数正确，min/中位/max 一行文本', () => {
    render(<ClosedEvalReport report={REPORT} runId={RUN_ID} />)

    // [0,0.19]→桶1、[0.2,0.39]→桶2、[0.4,0.5]→桶3、0.79→桶4、[0.8,1]→桶5
    expect(within(screen.getByTestId('bar-row-[0,0.2)')).getByText('2')).toBeInTheDocument()
    expect(within(screen.getByTestId('bar-row-[0.2,0.4)')).getByText('2')).toBeInTheDocument()
    expect(within(screen.getByTestId('bar-row-[0.4,0.6)')).getByText('2')).toBeInTheDocument()
    expect(within(screen.getByTestId('bar-row-[0.6,0.8)')).getByText('1')).toBeInTheDocument()
    expect(within(screen.getByTestId('bar-row-[0.8,1.0]')).getByText('2')).toBeInTheDocument()
    expect(screen.getByText('min 0.05 · 中位 0.5 · max 1')).toBeInTheDocument()
  })

  it('Spearman：宏值展示，每片 Collapse 默认折叠，展开后 None → 暂无', async () => {
    render(<ClosedEvalReport report={REPORT} runId={RUN_ID} />)

    expect(screen.getByText('宏 0.42')).toBeInTheDocument()
    // 默认折叠：每片行不渲染
    expect(screen.queryByText('tt0000001')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('每片 Spearman'))
    expect(await screen.findByText('tt0000001')).toBeInTheDocument()
    expect(screen.getByText('tt0000002')).toBeInTheDocument()
    expect(screen.getByText('tt0000001').closest('tr')!).toHaveTextContent('0.5')
    expect(within(screen.getByText('tt0000002').closest('tr')!).getByText('暂无')).toBeInTheDocument()
  })

  it('失败影片列表与产物下载三条（href 为 fileUrl 拼路径的字面量口径）', () => {
    render(<ClosedEvalReport report={REPORT} runId={RUN_ID} />)

    expect(screen.getByText('失败影片（1）')).toBeInTheDocument()
    expect(screen.getByText('tt0000009')).toBeInTheDocument()
    expect(screen.getByText('LLM 超时')).toBeInTheDocument()

    expect(screen.getByRole('link', { name: 'report.md' })).toHaveAttribute(
      'href',
      `/api/v1/eval/closed/runs/${RUN_ID}/files/report.md`,
    )
    expect(screen.getByRole('link', { name: 'metrics.csv' })).toHaveAttribute(
      'href',
      `/api/v1/eval/closed/runs/${RUN_ID}/files/metrics.csv`,
    )
    expect(screen.getByRole('link', { name: 'raw_results.json' })).toHaveAttribute(
      'href',
      `/api/v1/eval/closed/runs/${RUN_ID}/files/raw_results.json`,
    )
  })

  it('银标准口径块：note 原文 + calibre 小字', () => {
    render(<ClosedEvalReport report={REPORT} runId={RUN_ID} />)

    expect(
      screen.getByText('genome relevance 是模型输出非人工标注（银标准警示）'),
    ).toBeInTheDocument()
    const calibre = screen.getByTestId('calibre')
    expect(calibre).toHaveTextContent('qwen3.8-27b')
    expect(calibre).toHaveTextContent('bge-m3')
    expect(calibre).toHaveTextContent('closed-v1')
    expect(calibre).toHaveTextContent('v3')
    expect(calibre).toHaveTextContent('genome-scores')
    expect(calibre).toHaveTextContent('abcd1234')
  })

  it('最薄口径：k 不在档位 → 汇总「暂无」，空块省略，null calibre → 暂无', () => {
    render(<ClosedEvalReport report={MINIMAL_REPORT} runId="run-y" />)

    // banner/warning 为 null → 只剩 note 一条 alert
    expect(screen.getAllByRole('alert')).toHaveLength(1)

    // k=10 不在档位 → nDCG@10/P@10 暂无；R@20 取 k=20 格（Statistic 小数
    // 拆 span，数值断言同上走 toHaveTextContent）
    expect(within(screen.getByTestId('summary-ndcg10')).getByText('暂无')).toBeInTheDocument()
    expect(within(screen.getByTestId('summary-p10')).getByText('暂无')).toBeInTheDocument()
    expect(screen.getByTestId('summary-r20')).toHaveTextContent('0.35')
    expect(screen.getByTestId('summary-coverage-median')).toHaveTextContent('暂无')
    expect(screen.getByTestId('summary-spearman-macro')).toHaveTextContent('暂无')

    // 失败影片为空 → 整块不渲染
    expect(screen.queryByText(/失败影片/)).not.toBeInTheDocument()

    // 覆盖统计全 null → 一行三个「暂无」；空 values → 5 桶全 0 仍渲染
    expect(screen.getByText('min 暂无 · 中位 暂无 · max 暂无')).toBeInTheDocument()
    expect(screen.getByTestId('bar-row-[0,0.2)')).toHaveTextContent('0')

    // 网格只有 k=20 一行
    expect(dataRows(screen.getByTestId('grid-section'))).toBe(1)

    // 先验无行 → 三个「暂无」；随机 P@10 无 k=10 格 → 暂无、R@20 取 k=20 格
    const priorRow = screen.getByText('先验').closest('tr')!
    expect(within(priorRow).getAllByText('暂无')).toHaveLength(3)
    const randomRow = screen.getByText('随机').closest('tr')!
    expect(within(randomRow).getAllByText('暂无')).toHaveLength(2)
    expect(randomRow).toHaveTextContent('0.09')

    // 每片为空 → 宏值「暂无」；calibre 四个 null 字段 → 暂无（一行单文本
    // 节点，整行字面量断言）
    expect(screen.getByText('宏 暂无')).toBeInTheDocument()
    expect(screen.getByTestId('calibre')).toHaveTextContent(
      '模型 llm-x · 向量 emb-x · prompt 暂无 · 词库 暂无 · 数据集 暂无 · GT 暂无',
    )
  })

  it('renders movie table section with runId', () => {
    render(<ClosedEvalReport report={REPORT} runId="run-1" />)

    // 锚点顺序：movie-table 在 spearman-section 之后、downloads-section 之前
    //（compareDocumentPosition 含 FOLLOWING 位即前者在后）
    const spearman = screen.getByTestId('spearman-section')
    const movieTable = screen.getByTestId('movie-table')
    const downloads = screen.getByTestId('downloads-section')
    expect(spearman.compareDocumentPosition(movieTable) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(movieTable.compareDocumentPosition(downloads) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
