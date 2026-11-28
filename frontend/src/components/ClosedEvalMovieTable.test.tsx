// 闭环评测单片明细表测试（badcase 视图）：mock ../api/eval 的 getRunMovies/
// getRunMovieDetail（mock 到模块边界即隔离网络，模式同 ClosedEval.test.tsx）。
// 覆盖：服务端序渲染不重排（失败置顶 → P@K 升序 → null 垫底由服务层保证，
// 前端按数组序出）与动态表头（P@{k_p}/R@{k_r} 由 payload 档位拼字面量）、
// 耗时列 Math.round(elapsed_ms/1000) 秒、展开懒加载明细并按 tt 缓存（收起
// 再展开不重复请求）、计分清单三组对照（● 命中 / — 未命中、tag_name null
// 回退 tag_id、misses 服务端 gt 分降序）、失败片展开只渲染 error 原文不发
// 请求、null 指标一律「暂无」（None≠0 纪律）、明细 fetch 失败透出
// error.message（ApiError.message 即后端 detail 文案）；回归两条：runId 切换
// 后 A run 的在途明细不得串写成 B run 可命中的缓存（B run 展开必须重新请求）、
// 首次明细在途窗口内收起再展开不得重复请求（pending 去重）。
// 管线追踪（增补 2026-11-26）：主表漏斗列（召回→提及→产出，null 暂无，title
// 全链计数）、明细漏斗条五阶段 mini BarChart（复用 FunnelBars，门控过/挂载成
// 由分流键差出；召回 0 全 0 行兜底无 NaN）、候选踪迹表
// （结局 Select 筛选为本地受控状态、服务端召回序不重排、结局中文映射、
// null 数值暂无）、漏检表丢失环节列（中文映射、null 暂无）、trace null 的
// 老 run 说明文案（无踪迹表/漏斗条/下载）、llm-raw 下载 404 → message.error
// 透出不崩视图（fetch mock，不用原生 <a> 直链——404 会开浏览器错误页）。
// 整体漏斗块（增补 2026-11-26）：主表上方全 run 聚合（payload.rows 纯派生，
// 十键逐键加总 funnel 非 null 行 + 差式派生门控过/挂载成 + 存活率 + 覆盖
// 标注；老 run 全 null → 说明文案；失败片混入只计非 null 行；召回 0 片不进
// 存活率；切 θ 重拉聚合值不漂移）。
// 轮询驱动 refreshTick（增补 2026-11-27）：tick 变化即带当前 θ 静默重拉——
// 不清 θ/展开/明细缓存（行只增不改）、在途窗口旧表不闪、失败静默下一 tick
// 重试；undefined（终态/不轮询直开）时不重拉，行为与旧版相同。
// 注意：antd Table 展开图标按钮无可访问名，查询一律走
// .ant-table-row-expand-icon（与 GenerationTasks 测试同口径）。
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ClosedEvalMovieTable from './ClosedEvalMovieTable'
import {
  getRunMovieDetail,
  getRunMovies,
  movieRawUrl,
  type FunnelCounts,
  type MovieDetail,
  type MovieSummaryPayload,
  type MovieTrace,
} from '../api/eval'

vi.mock('../api/eval', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/eval')>()
  return {
    ...actual,
    getRunMovies: vi.fn(),
    getRunMovieDetail: vi.fn(),
  }
})

const mockedGetRunMovies = vi.mocked(getRunMovies)
const mockedGetRunMovieDetail = vi.mocked(getRunMovieDetail)

const RUN_ID = 'run-20261120-000000-aa00'

// ---------- 夹具（与后端 MovieSummaryOut / MovieDetailOut 契约同形） ----------

/** 漏斗十键夹具（与 DETAIL.trace.counts 同一对象——摘要行 funnel 与明细
 * trace 的 counts 在真实后端同源派生，测试保持同数）：5 候选五结局各一、
 * 编造 1、批次 2（重试 1）、上限 6。 */
const FUNNEL: FunnelCounts = {
  n_recall: 5,
  n_mentioned: 4,
  n_kept: 1,
  n_gate: 1,
  n_unmountable: 1,
  n_m_cap: 1,
  n_fabricated: 1,
  n_batches: 2,
  parse_retries: 1,
  max_tags: 6,
}

/** 摘要夹具：k_p=8 / k_r=20 档位（表头字面量 P@8 / R@20）。三行即服务端序：
 * 失败片置顶 → p=0.2 坏例片 → p=null 无交集片垫底。funnel：失败片无 trace
 * （trace 不落）→ null；两成功片有 trace → 计数对象（无交集片单独一份，
 * 防行间串数）。 */
const SUMMARY: MovieSummaryPayload = {
  theta_p: 0.2,
  theta_r: 0.8,
  thetas: [0.2, 0.4, 0.6, 0.8],
  k_p: 8,
  k_r: 20,
  rows: [
    {
      tt: 'tt0000003',
      title: '落榜片',
      ok: false,
      error: 'LLM 超时',
      spearman: null,
      p_at_k: null,
      r_at_k: null,
      elapsed_ms: 1200,
      n_produced: 0,
      n_relevant: 5,
      funnel: null,
    },
    {
      tt: 'tt0000001',
      title: '坏例片',
      ok: true,
      error: null,
      spearman: 0.3,
      p_at_k: 0.2,
      r_at_k: 0.1,
      elapsed_ms: 8000,
      n_produced: 10,
      n_relevant: 10,
      funnel: FUNNEL,
    },
    {
      tt: 'tt0000002',
      title: '无交集片',
      ok: true,
      error: null,
      spearman: null,
      p_at_k: null,
      r_at_k: null,
      elapsed_ms: 3000,
      n_produced: 4,
      n_relevant: 0,
      funnel: {
        n_recall: 4,
        n_mentioned: 4,
        n_kept: 4,
        n_gate: 0,
        n_unmountable: 0,
        n_m_cap: 0,
        n_fabricated: 0,
        n_batches: 1,
        parse_retries: 0,
        max_tags: 6,
      },
    },
  ],
}

/** 管线追踪夹具（与后端 MovieTraceOut 同形）：candidates 五结局各一（服务端
 * 序=召回序）、unmentioned 候选 applicability/sf 双 null 且 tag_name null
 * （回退 tag_id）、gate 候选 sf null（门控丢弃点不上报 sf）；counts 与
 * FUNNEL 同数（mentioned = kept+gate+unmountable+m_cap，候选逐一可对账）。 */
const TRACE: MovieTrace = {
  counts: FUNNEL,
  candidates: [
    { tag_id: 'c-1', tag_name: '保留候选', dir_id: 'd-1', recall: 0.9, batch: 0, outcome: 'kept', applicability: 5, sf: 0.88 },
    { tag_id: 'c-2', tag_name: null, dir_id: 'd-1', recall: 0.8, batch: 0, outcome: 'unmentioned', applicability: null, sf: null },
    { tag_id: 'c-3', tag_name: '门控候选', dir_id: 'd-2', recall: 0.7, batch: 1, outcome: 'gate', applicability: 3, sf: null },
    { tag_id: 'c-4', tag_name: '挂载候选', dir_id: 'd-2', recall: 0.6, batch: 1, outcome: 'unmountable', applicability: 4, sf: 0.55 },
    { tag_id: 'c-5', tag_name: '截断候选', dir_id: 'd-3', recall: 0.5, batch: 2, outcome: 'm_cap', applicability: 5, sf: 0.44 },
  ],
  batches: [
    { dir_id: 'd-1', batch: 0, n: 2, parse_retried: false },
    { dir_id: 'd-2', batch: 1, n: 2, parse_retried: true },
  ],
  fabricated: ['fab-9'],
  max_tags: 6,
}

/** 明细夹具（坏例片）：produced 含命中（●）/ 未命中（—）/ tag_name 缺失
 * （回退 tag_id）/ GT 分缺失（暂无）四类；out_of_space 与 misses（服务端已
 * 按 gt 分降序：0.9 → 0.6 → 0.5）各含条目；misses.lost_at 覆盖未召回/
 * 门控丢弃/null（kept-却-漏检防御）三态；trace 挂管线追踪夹具。 */
const DETAIL: MovieDetail = {
  tt: 'tt0000001',
  title: '坏例片',
  ok: true,
  error: null,
  elapsed_ms: 8000,
  theta_p: 0.2,
  theta_r: 0.8,
  k_p: 8,
  k_r: 20,
  produced: [
    {
      tag_id: 't-101',
      tag_name: '动作',
      sf: 0.9,
      confidence: 0.8,
      evidence_count: 3,
      gt_score: 0.85,
      hit: true,
    },
    {
      tag_id: 't-102',
      tag_name: null,
      sf: 0.7,
      confidence: 0.6,
      evidence_count: 2,
      gt_score: 0.3,
      hit: false,
    },
    {
      tag_id: 't-103',
      tag_name: '爱情',
      sf: 0.5,
      confidence: 0.5,
      evidence_count: 1,
      gt_score: null,
      hit: false,
    },
  ],
  out_of_space: [{ tag_id: 't-201', tag_name: '外空间甲', sf: 0.55 }],
  misses: [
    { tag_id: 't-301', tag_name: '悲剧', gt_score: 0.9, lost_at: 'unrecalled' },
    { tag_id: 't-302', tag_name: '悬疑', gt_score: 0.6, lost_at: 'gate' },
    { tag_id: 't-303', tag_name: '喜剧', gt_score: 0.5, lost_at: null },
  ],
  trace: TRACE,
}

/** 老 run 明细夹具：raw 行无 trace 键 → trace/lost_at 全 null（键恒在），
 * funnel 同理（摘要侧已覆盖）。 */
const DETAIL_OLD_RUN: MovieDetail = {
  ...DETAIL,
  trace: null,
  misses: DETAIL.misses.map((m) => ({ ...m, lost_at: null })),
}

// ---------- 查询辅助 ----------

/** 主表里 tt 单元格所在行。 */
function rowOf(tt: string): HTMLElement {
  return screen.getByText(tt).closest('tr')!
}

/** 行的展开图标（antd 图标按钮无可访问名，走 class 查询）。 */
function expandIconOf(row: HTMLElement): Element {
  const icon = row.querySelector('.ant-table-row-expand-icon')
  expect(icon).not.toBeNull()
  return icon as Element
}

beforeEach(() => {
  mockedGetRunMovies.mockReset()
  mockedGetRunMovieDetail.mockReset()
  mockedGetRunMovies.mockResolvedValue(SUMMARY)
})

afterEach(() => {
  // 下载用例 stub 的全局 fetch / URL.* 不渗入其他用例
  vi.unstubAllGlobals()
})

describe('ClosedEvalMovieTable 单片明细表', () => {
  it('服务端序渲染不重排，表头由档位拼出 P@8/R@20，状态与耗时列取整', async () => {
    render(<ClosedEvalMovieTable runId={RUN_ID} />)

    // 三行按 mock 数组序出现（服务端已排序，前端不重排）：失败 → p=0.2 → null
    expect(await screen.findByText('tt0000003')).toBeInTheDocument()
    const failedRow = rowOf('tt0000003')
    const badRow = rowOf('tt0000001')
    const noneRow = rowOf('tt0000002')
    expect(
      failedRow.compareDocumentPosition(badRow) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    expect(
      badRow.compareDocumentPosition(noneRow) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()

    // 动态表头：k_p=8 / k_r=20 档位拼字面量
    expect(screen.getByText('P@8(θ0.2)')).toBeInTheDocument()
    expect(screen.getByText('R@20(θ0.8)')).toBeInTheDocument()

    // 状态列成功/失败；耗时列 Math.round(elapsed_ms/1000)：8000→8 秒、3000→3 秒
    expect(within(failedRow).getByText('失败')).toBeInTheDocument()
    expect(within(badRow).getByText('成功')).toBeInTheDocument()
    expect(within(badRow).getByText('8')).toBeInTheDocument()
    expect(within(noneRow).getByText('3')).toBeInTheDocument()
  })

  it('展开懒加载明细并按 tt 缓存：三组对照渲染，收起再展开不重复请求', async () => {
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))

    // 计分清单：● 命中 / — 未命中 / tag_name null 回退 tag_id / GT 分缺失暂无
    const hitRow = (await screen.findByText('动作')).closest('tr')!
    expect(within(hitRow).getByText('●')).toBeInTheDocument()
    const fallbackRow = screen.getByText('t-102').closest('tr')!
    expect(within(fallbackRow).getByText('—')).toBeInTheDocument()
    expect(screen.getByText('0.85')).toBeInTheDocument()
    const noGtRow = screen.getByText('爱情').closest('tr')!
    expect(within(noGtRow).getByText('暂无')).toBeInTheDocument()
    // 空间外组与漏检组（misses 按服务端 gt 分降序：0.9 → 0.6）
    expect(screen.getByText('外空间甲')).toBeInTheDocument()
    const missNames = within(screen.getByTestId('detail-misses')).getAllByText(
      /^(悲剧|悬疑)$/,
    )
    expect(missNames.map((el) => el.textContent)).toEqual(['悲剧', '悬疑'])
    expect(within(screen.getByTestId('detail-misses')).getByText('0.9')).toBeInTheDocument()
    expect(within(screen.getByTestId('detail-misses')).getByText('0.6')).toBeInTheDocument()

    expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1)
    expect(mockedGetRunMovieDetail).toHaveBeenCalledWith(RUN_ID, 'tt0000001')

    // 收起再展开：缓存命中，仍只 1 次请求。注意 rc-table v6 收起后展开行
    // 保持挂载（display:none 隐藏不卸载），断言可见性变化而非节点移除
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(screen.getByText('动作')).not.toBeVisible())
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(screen.getByText('动作')).toBeVisible())
    expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1)
  })

  it('失败片展开只渲染 error 原文，不发明细请求', async () => {
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000003')

    fireEvent.click(expandIconOf(rowOf('tt0000003')))

    expect(await screen.findByText('LLM 超时')).toBeInTheDocument()
    expect(mockedGetRunMovieDetail).not.toHaveBeenCalled()
    expect(screen.queryByText('计分清单')).not.toBeInTheDocument()
    expect(screen.queryByTestId('detail-misses')).not.toBeInTheDocument()
  })

  it('null 指标一律「暂无」（None≠0）', async () => {
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000003')

    // 失败片：Spearman/P@8/R@20 三格 null + 漏斗列无 trace（trace 不落）→
    // 共 4 个暂无；无交集片漏斗有值仍 3 个；有值行不出暂无
    expect(within(rowOf('tt0000003')).getAllByText('暂无')).toHaveLength(4)
    expect(within(rowOf('tt0000002')).getAllByText('暂无')).toHaveLength(3)
    expect(within(rowOf('tt0000001')).queryByText('暂无')).not.toBeInTheDocument()
  })

  it('明细 fetch 失败：展开区显示 error.message', async () => {
    mockedGetRunMovieDetail.mockRejectedValueOnce(new Error('boom'))
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))

    expect(await screen.findByText('boom')).toBeInTheDocument()
  })

  // ---------- 管线追踪（增补 2026-11-26） ----------

  it('主表漏斗列：召回→提及→产出 三数渲染，无 trace 行「暂无」', async () => {
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000003')

    // 成功行三数渲染；title 带全链十键计数
    const chain = within(rowOf('tt0000001')).getByText('召回 5 → 提及 4 → 产出 1')
    expect(chain).toHaveAttribute(
      'title',
      '召回 5 · 提及 4 · 产出 1 · 门控丢弃 1 · 挂载失败 1 · 上限截断 1 · '
        + '编造 1 · 批次 2（重试 1） · 输出上限 6',
    )
    expect(within(rowOf('tt0000002')).getByText('召回 4 → 提及 4 → 产出 4')).toBeInTheDocument()
    // 失败行无 trace → 漏斗格「暂无」（行尾最后一格，相关数列之后）
    const funnelCell = rowOf('tt0000003').querySelector('td:last-child')!
    expect(funnelCell.textContent).toBe('暂无')
  })

  it('明细漏斗条渲染五计数与次要计数（门控/挂载/截断/编造/批次/上限）', async () => {
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))

    // 五阶段主链 mini BarChart（复用 FunnelBars）：门控过 = 提及 4 − 门控丢弃 1
    // = 3；挂载成 = 3 − 挂载失败 1 = 2；显式 max=召回 5 行间可比
    const bar = await screen.findByTestId('detail-funnel')
    const barValues = [
      ['召回', '5'],
      ['提及', '4'],
      ['门控过', '3'],
      ['挂载成', '2'],
      ['产出', '1'],
    ] as const
    for (const [label, value] of barValues) {
      const barRow = within(bar).getByTestId(`bar-row-${label}`)
      expect(within(barRow).getByText(value)).toBeInTheDocument()
    }
    // 次要计数：分流/编造/批次（含解析重试）/输出上限
    expect(bar).toHaveTextContent('门控丢弃 1')
    expect(bar).toHaveTextContent('挂载失败 1')
    expect(bar).toHaveTextContent('上限截断 1')
    expect(bar).toHaveTextContent('编造 1')
    expect(bar).toHaveTextContent('批次 2（解析重试 1）')
    expect(bar).toHaveTextContent('输出上限 6')
  })

  it('召回 0 的单片明细：五行 bar-row 渲染无 NaN（BarChart 全 0 兜底为 1）', async () => {
    // 候选全灭片：主链五键全 0（真实求和 0，可渲染——与 None≠0「算不出」
    // 区分）；BarChart 显式 max=召回 0 非正 → 逐行兜底 1 防除零，不渲染 NaN
    mockedGetRunMovieDetail.mockResolvedValue({
      ...DETAIL,
      trace: {
        counts: FUNNEL_ZERO_RECALL,
        candidates: [],
        batches: [],
        fabricated: [],
        max_tags: 6,
      },
    })
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))

    const bar = await screen.findByTestId('detail-funnel')
    for (const label of ['召回', '提及', '门控过', '挂载成', '产出'] as const) {
      const barRow = within(bar).getByTestId(`bar-row-${label}`)
      expect(within(barRow).getByText('0')).toBeInTheDocument()
      expect(within(barRow).queryByText('NaN')).not.toBeInTheDocument()
    }
  })

  it('踪迹表按结局筛选：Select 选 gate → 只剩门控行；null 数值「暂无」', async () => {
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))

    const traceArea = await screen.findByTestId('detail-trace')
    // 服务端序=召回序，不重排：五候选按数组序出（tag_name null 回退 tag_id）
    const names = within(traceArea)
      .getAllByText(/^(保留候选|c-2|门控候选|挂载候选|截断候选)$/)
      .map((el) => el.textContent)
    expect(names).toEqual(['保留候选', 'c-2', '门控候选', '挂载候选', '截断候选'])
    // unmentioned 行 applicability/sf 双 null → A/Sf 两格暂无；gate 行 sf null → 一格暂无
    const unmentionedRow = within(traceArea).getByText('c-2').closest('tr')!
    expect(within(unmentionedRow).getAllByText('暂无')).toHaveLength(2)
    const gateRow = within(traceArea).getByText('门控候选').closest('tr')!
    expect(within(gateRow).getByText('暂无')).toBeInTheDocument()
    expect(within(gateRow).getByText('门控丢弃')).toBeInTheDocument()

    // 结局筛选（本地受控状态）：选「门控丢弃」→ 只剩门控行
    fireEvent.mouseDown(screen.getByTestId('trace-outcome-select'))
    fireEvent.click(await screen.findByTitle('门控丢弃'))
    await waitFor(() => {
      expect(within(traceArea).getByText('门控候选')).toBeInTheDocument()
      expect(traceArea.querySelectorAll('.ant-table-tbody tr.ant-table-row')).toHaveLength(1)
    })
    expect(within(traceArea).queryByText('保留候选')).not.toBeInTheDocument()
    expect(within(traceArea).queryByText('c-2')).not.toBeInTheDocument()
    expect(within(traceArea).queryByText('挂载候选')).not.toBeInTheDocument()
    expect(within(traceArea).queryByText('截断候选')).not.toBeInTheDocument()
  })

  it('漏检表丢失环节列：未召回/门控丢弃 标签正确，null → 暂无', async () => {
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))

    const misses = await screen.findByTestId('detail-misses')
    expect(within(within(misses).getByText('悲剧').closest('tr')!).getByText('未召回'))
      .toBeInTheDocument()
    expect(within(within(misses).getByText('悬疑').closest('tr')!).getByText('门控丢弃'))
      .toBeInTheDocument()
    // lost_at null（kept-却-漏检防御/老 run）→ 暂无
    expect(within(within(misses).getByText('喜剧').closest('tr')!).getByText('暂无'))
      .toBeInTheDocument()
  })

  it('trace 为 null 的老 run 明细：渲染说明文案、无踪迹表/下载链接', async () => {
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL_OLD_RUN)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))

    expect(await screen.findByTestId('detail-trace-absent')).toHaveTextContent(
      '本 run 早于管线追踪功能，无解剖数据',
    )
    expect(screen.queryByTestId('detail-funnel')).not.toBeInTheDocument()
    expect(screen.queryByTestId('detail-trace')).not.toBeInTheDocument()
    expect(screen.queryByTestId('llm-raw-download')).not.toBeInTheDocument()
    // 老 run 的 misses.lost_at 全 null → 丢失环节列三个「暂无」
    expect(within(screen.getByTestId('detail-misses')).getAllByText('暂无')).toHaveLength(3)
  })

  it('下载 404：fetch mock 404 → message.error 透出，不崩视图', async () => {
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ detail: '该 run 无此影片的原始输出' }),
      }),
    )
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    fireEvent.click(await screen.findByTestId('llm-raw-download'))

    // 请求打在新端点上；后端 detail 经 message.error 透出
    await waitFor(() =>
      expect(vi.mocked(fetch)).toHaveBeenCalledWith(movieRawUrl(RUN_ID, 'tt0000001')))
    expect(await screen.findByText(/原始输出下载失败/)).toBeInTheDocument()
    expect(screen.getByText(/该 run 无此影片的原始输出/)).toBeInTheDocument()
    // 视图不崩：踪迹表仍在
    expect(screen.getByTestId('detail-trace')).toBeInTheDocument()
  })

  it('runId 切换后：A run 在途明细不落入 B run 缓存，B run 展开重新请求', async () => {
    // run A 的明细用未决 promise 卡住（模拟在途），run B 的明细即时返回
    let resolveA!: (d: MovieDetail) => void
    mockedGetRunMovieDetail.mockImplementation((rid: string) => {
      if (rid === 'run-A') {
        return new Promise<MovieDetail>((res) => {
          resolveA = res
        })
      }
      return Promise.resolve({
        ...DETAIL,
        produced: [{ ...DETAIL.produced[0], tag_name: '运行乙专属' }],
        misses: [],
      })
    })
    const { rerender } = render(<ClosedEvalMovieTable runId="run-A" />)
    await screen.findByText('tt0000001')
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledWith('run-A', 'tt0000001'))

    // 切到 run B（摘要重拉、展开态清空），A 的明细请求仍在途
    rerender(<ClosedEvalMovieTable runId="run-B" />)
    await screen.findByText('tt0000001')

    // A 的在途请求此刻才 resolve：不得写成 B run 可命中的缓存
    await act(async () => {
      resolveA(DETAIL)
    })
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(2)
    expect(mockedGetRunMovieDetail).toHaveBeenNthCalledWith(2, 'run-B', 'tt0000001')
    // 渲染的是 run B 的明细（B 专属标签），而非 A 的缓存串写
    expect(await screen.findByText('运行乙专属')).toBeInTheDocument()
    expect(screen.queryByText('动作')).not.toBeInTheDocument()
  })

  it('首次明细在途时收起再展开不重复请求（pending 去重）', async () => {
    let resolve!: (d: MovieDetail) => void
    mockedGetRunMovieDetail.mockReturnValueOnce(
      new Promise<MovieDetail>((res) => {
        resolve = res
      }),
    )
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1))

    // 在途窗口内收起再展开：pending 标记挡住重复请求
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1)

    await act(async () => {
      resolve(DETAIL)
    })
    await waitFor(() => expect(screen.getByText('动作')).toBeVisible())
  })

  // ---------- θ 可调（增补 2026-11-22） ----------

  /** θ 切换用夹具：k_p=10 档（表头 P@10(θ…)），thetas 四档供选择器。 */
  const SUMMARY_THETA: MovieSummaryPayload = {
    ...SUMMARY,
    k_p: 10,
  }

  it('θ_P 选择器切换：重拉摘要带 theta_p、明细缓存清空收起、再展开带参取数、表头带门', async () => {
    mockedGetRunMovies
      .mockResolvedValueOnce(SUMMARY_THETA)
      .mockResolvedValueOnce({ ...SUMMARY_THETA, theta_p: 0.4 })
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await screen.findByText('动作')
    expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1)

    // 打开 θ_P 选择器 → 四档选项 → 选 0.4（初值 0.2——同值重选不发事件）
    const pSelect = screen.getByTestId('theta-p-select')
    fireEvent.mouseDown(pSelect)
    const options = await screen.findAllByRole('option')
    expect(options.map((o) => o.textContent)).toEqual(['0.2', '0.4', '0.6', '0.8'])
    fireEvent.click(screen.getByTitle('0.4'))

    // 重拉摘要带 theta_p（θ_R 未选不带）；明细不自动重拉但缓存清空、展开行收起
    // （payload 替换后 rc-table 重渲染会把展开行卸载——图标收起场景才是
    // 保持挂载，故断言兼容「不存在或不可见」两态）
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(2))
    expect(mockedGetRunMovies).toHaveBeenNthCalledWith(2, RUN_ID, { p: 0.4 })
    expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      const el = screen.queryByText('动作')
      if (el !== null) expect(el).not.toBeVisible()
    })

    // 再展开同一行：缓存已清 → 第二次明细调用带 theta_p=0.2
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(2))
    expect(mockedGetRunMovieDetail).toHaveBeenNthCalledWith(
      2, RUN_ID, 'tt0000001', { p: 0.4 },
    )
    await waitFor(() => expect(screen.getByText('动作')).toBeVisible())
    // 表头带门字面量（第二份 payload theta_p=0.4、k_p=10）
    expect(screen.getByText('P@10(θ0.4)')).toBeInTheDocument()
  })

  it('θ 选择器渲染：两 Select 初值=服务端回传档（0.2/0.8），θ_R 同样可切', async () => {
    mockedGetRunMovies.mockResolvedValue(SUMMARY_THETA)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    // 两个选择器都存在且显示当前生效门 0.2/0.8
    expect(screen.getByTestId('theta-p-select')).toBeInTheDocument()
    expect(screen.getByTestId('theta-r-select')).toBeInTheDocument()
    expect(screen.getByTestId('theta-p-select').textContent).toContain('0.2')
    expect(screen.getByTestId('theta-r-select').textContent).toContain('0.8')

    // θ_R 切到 0.6（初值 0.8——同值重选不发事件）→ 重拉带 theta_r
    const rSelect = screen.getByTestId('theta-r-select')
    fireEvent.mouseDown(rSelect)
    fireEvent.click(await screen.findByTitle('0.6'))
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(2))
    expect(mockedGetRunMovies).toHaveBeenNthCalledWith(2, RUN_ID, { r: 0.6 })
  })

  it('换 run 后 θ 状态回服务端默认：摘要重拉不带参、明细取数不带参', async () => {
    mockedGetRunMovies.mockResolvedValue(SUMMARY_THETA)
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    const { rerender } = render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    // 先把 θ_P 切到 0.4（初值 0.2，弄脏状态）
    const pSelect = screen.getByTestId('theta-p-select')
    fireEvent.mouseDown(pSelect)
    fireEvent.click(await screen.findByTitle('0.4'))
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(2))

    // 换 run：θ 状态重置 → 摘要不带 θ 参数、选择器显示 0.4
    rerender(<ClosedEvalMovieTable runId="run-B" />)
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(3))
    expect(mockedGetRunMovies).toHaveBeenLastCalledWith('run-B')
    await screen.findByText('tt0000001')
    expect(screen.getByTestId('theta-p-select').textContent).toContain('0.2')  // 缺省取格 θ_p

    // 明细取数不带 θ（默认门）
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledWith(
      'run-B', 'tt0000001',
    ))
  })

  // ---------- 终审修复回归（generation 守卫） ----------

  it('θ_r 切换时在途明细写回作废：不落缓存、重展开带参重新取数（终审 Important 1；θ_r 0.8→0.6）', async () => {
    // 第一次明细（无 θ）用未决 promise 卡住在途；第二次（θ_r=0.8）返回新门明细
    let resolveOld!: (d: MovieDetail) => void
    mockedGetRunMovies.mockResolvedValue(SUMMARY_THETA)
    mockedGetRunMovieDetail.mockImplementation((_rid: string, _tt: string, theta?: { r?: number }) => {
      if (theta?.r === 0.6) {
        return Promise.resolve({
          ...DETAIL,
          theta_r: 0.6,
          produced: [{ ...DETAIL.produced[0], tag_name: '新门明细' }],
        })
      }
      return new Promise<MovieDetail>((res) => {
        resolveOld = res
      })
    })
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1))

    // 切 θ_r 到 0.6（初值 0.8——同值重选不发事件；缓存清空、旧请求仍在途）
    const rSelect = screen.getByTestId('theta-r-select')
    fireEvent.mouseDown(rSelect)
    fireEvent.click(await screen.findByTitle('0.6'))
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(2))

    // 旧请求此刻才 resolve：写回必须作废（θ 代际已过）——不得落入缓存
    await act(async () => {
      resolveOld(DETAIL)
    })

    // 重展开：缓存里没有旧 θ 明细 → 第二次带 theta_r=0.8 取数、渲染新门明细
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(2))
    expect(mockedGetRunMovieDetail).toHaveBeenLastCalledWith(
      RUN_ID, 'tt0000001', { r: 0.6 },
    )
    expect(await screen.findByText('新门明细')).toBeVisible()
    // 旧 θ 的明细内容（动作）未被串写渲染
    expect(screen.queryByText('动作')).not.toBeInTheDocument()
  })

  it('连续切 θ：后到先回不覆盖，最后选择的门生效（终审 Important 2）', async () => {
    // 行标题作覆盖探针：慢响应（θ=0.2）与快响应（θ=0.6）带不同标题——
    // 表头 θ 取自 state 探测不到旧响应覆盖，必须看 rows 内容
    const slowPayload: MovieSummaryPayload = {
      ...SUMMARY_THETA,
      theta_p: 0.4,
      rows: SUMMARY_THETA.rows.map((r) =>
        r.tt === 'tt0000001' ? { ...r, title: '旧门慢片' } : r),
    }
    const fastPayload: MovieSummaryPayload = {
      ...SUMMARY_THETA,
      theta_p: 0.6,
      rows: SUMMARY_THETA.rows.map((r) =>
        r.tt === 'tt0000001' ? { ...r, title: '新门快片' } : r),
    }
    let resolveSlow!: (p: MovieSummaryPayload) => void
    mockedGetRunMovies
      .mockResolvedValueOnce(SUMMARY_THETA) // 初载（无参）
      .mockReturnValueOnce(new Promise<MovieSummaryPayload>((res) => { // θ_P=0.4（慢）
        resolveSlow = res
      }))
      .mockResolvedValueOnce(fastPayload) // θ_P=0.6（快）
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    const pSelect = screen.getByTestId('theta-p-select')
    fireEvent.mouseDown(pSelect)
    fireEvent.click(await screen.findByTitle('0.4')) // 慢请求在途
    fireEvent.mouseDown(pSelect)
    fireEvent.click(await screen.findByTitle('0.6')) // 快请求立即落地
    await waitFor(() => expect(screen.getByText('新门快片')).toBeInTheDocument())

    // 慢请求（旧门数据）此刻才 resolve：作废，不得覆盖新门 rows
    await act(async () => {
      resolveSlow(slowPayload)
    })
    expect(screen.getByText('新门快片')).toBeInTheDocument()
    expect(screen.queryByText('旧门慢片')).not.toBeInTheDocument()
  })

  it('θ 重拉失败不毁视图：表格与选择器仍在、内联警示、再成功即清除（终审 Important 3）', async () => {
    mockedGetRunMovies
      .mockResolvedValueOnce(SUMMARY_THETA)
      .mockRejectedValueOnce(new Error('θ 重拉失败'))
      .mockResolvedValueOnce({ ...SUMMARY_THETA, theta_p: 0.4 })
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    const pSelect = screen.getByTestId('theta-p-select')
    fireEvent.mouseDown(pSelect)
    fireEvent.click(await screen.findByTitle('0.6'))

    // 失败：表格（行仍在）+ 两个选择器仍在 + 内联警示出现（不是整页 Alert 替换）
    expect(await screen.findByTestId('theta-refetch-error')).toBeInTheDocument()
    expect(screen.getByText('tt0000001')).toBeInTheDocument()
    expect(screen.getByTestId('theta-p-select')).toBeInTheDocument()
    expect(screen.getByTestId('theta-r-select')).toBeInTheDocument()

    // 再切一次到 0.4（与失败前选的 0.6 不同值才触发 onChange）：
    // 成功 → 警示清除、表头更新
    fireEvent.mouseDown(pSelect)
    fireEvent.click(screen.getByRole('option', { name: '0.4' }))
    await waitFor(() => expect(screen.getByText('P@10(θ0.4)')).toBeInTheDocument())
    await waitFor(() =>
      expect(screen.queryByTestId('theta-refetch-error')).not.toBeInTheDocument())
  })

  // ---------- 整体漏斗块（增补 2026-11-26） ----------

  /** 老 run 摘要夹具：rows 全 funnel null（raw 行无 trace → 摘要行同 null）。 */
  const SUMMARY_OLD_RUN: MovieSummaryPayload = {
    ...SUMMARY,
    rows: SUMMARY.rows.map((r) => ({ ...r, funnel: null })),
  }

  /** 召回 0 夹具（候选全灭）：funnel 非 null 仍计入聚合，但 n_kept/n_recall
   * 分母为 0 不可算——存活率跳过该行（None≠0 不造 0）。 */
  const FUNNEL_ZERO_RECALL: FunnelCounts = {
    n_recall: 0,
    n_mentioned: 0,
    n_kept: 0,
    n_gate: 0,
    n_unmountable: 0,
    n_m_cap: 0,
    n_fabricated: 0,
    n_batches: 1,
    parse_retries: 0,
    max_tags: 6,
  }

  it('整体漏斗块：Statistic 五值 + BarChart 五行 + 次要计数 + 存活率 + 覆盖标注', async () => {
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    const block = await screen.findByTestId('run-funnel')
    expect(block).toBeInTheDocument()

    // 五 Statistic（聚合值与差式派生精确相等）：召回 5+4=9、提及 4+4=8、
    // 门控过 8−1=7、挂载成 7−1=6、产出 1+4=5
    expect(screen.getByTestId('run-funnel-recall')).toHaveTextContent('召回9')
    expect(screen.getByTestId('run-funnel-mentioned')).toHaveTextContent('提及8')
    expect(screen.getByTestId('run-funnel-passed')).toHaveTextContent('门控过7')
    expect(screen.getByTestId('run-funnel-mounted')).toHaveTextContent('挂载成6')
    expect(screen.getByTestId('run-funnel-kept')).toHaveTextContent('产出5')

    // BarChart 五行存在且数值对（同上五数）
    const barValues = [
      ['召回', '9'],
      ['提及', '8'],
      ['门控过', '7'],
      ['挂载成', '6'],
      ['产出', '5'],
    ] as const
    for (const [label, value] of barValues) {
      const barRow = within(block).getByTestId(`bar-row-${label}`)
      expect(within(barRow).getByText(value)).toBeInTheDocument()
    }

    // 次要计数行：门控丢弃/挂载失败/上限截断/编造/批次（解析重试）——
    // 不含输出上限（聚合的 max_tags 无意义）
    const secondary = screen.getByTestId('run-funnel-secondary')
    expect(secondary).toHaveTextContent('门控丢弃 1')
    expect(secondary).toHaveTextContent('挂载失败 1')
    expect(secondary).toHaveTextContent('上限截断 1')
    expect(secondary).toHaveTextContent('编造 1')
    expect(secondary).toHaveTextContent('批次 3（解析重试 1）')
    expect(secondary).not.toHaveTextContent('输出上限')

    // 每片存活率（n_kept/n_recall：1/5=0.2、4/4=1；两片偶数 → 中位取均值 0.6）
    expect(screen.getByTestId('run-funnel-survival')).toHaveTextContent(
      '每片存活率 min 0.2 · 中位 0.6 · max 1',
    )

    // 覆盖标注：N=有解剖数据的片数（失败片不计），M=rows 总数
    expect(screen.getByTestId('run-funnel-coverage')).toHaveTextContent('合计覆盖 2 部 / 共 3 部')
  })

  it('老 run：整体块渲染说明文案（rows 全 funnel null）', async () => {
    mockedGetRunMovies.mockResolvedValue(SUMMARY_OLD_RUN)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000003')

    expect(await screen.findByTestId('run-funnel-absent')).toHaveTextContent(
      '本 run 早于管线追踪功能，无解剖数据',
    )
    // 不渲染统计内容（同 detail-trace-absent 口径：None≠0 不是全 0）
    expect(screen.queryByTestId('run-funnel')).not.toBeInTheDocument()
    expect(screen.queryByTestId('run-funnel-recall')).not.toBeInTheDocument()
    expect(screen.queryByTestId('bar-row-召回')).not.toBeInTheDocument()
    expect(screen.queryByTestId('run-funnel-coverage')).not.toBeInTheDocument()
  })

  it('失败片混入：聚合只计非 null 行，覆盖 N/M 真实', async () => {
    // 失败片 funnel null 混两片非 null（数值各异）：逐键加总只计两片
    const MIXED: MovieSummaryPayload = {
      ...SUMMARY,
      rows: [
        { ...SUMMARY.rows[0], funnel: null },
        {
          tt: 'tt0000010',
          title: '甲片',
          ok: true,
          error: null,
          spearman: 0.5,
          p_at_k: 0.5,
          r_at_k: 0.5,
          elapsed_ms: 1000,
          n_produced: 3,
          n_relevant: 8,
          funnel: {
            n_recall: 8,
            n_mentioned: 7,
            n_kept: 4,
            n_gate: 1,
            n_unmountable: 1,
            n_m_cap: 1,
            n_fabricated: 2,
            n_batches: 2,
            parse_retries: 1,
            max_tags: 6,
          },
        },
        {
          tt: 'tt0000011',
          title: '乙片',
          ok: true,
          error: null,
          spearman: null,
          p_at_k: null,
          r_at_k: null,
          elapsed_ms: 2000,
          n_produced: 5,
          n_relevant: 5,
          funnel: {
            n_recall: 5,
            n_mentioned: 5,
            n_kept: 5,
            n_gate: 0,
            n_unmountable: 0,
            n_m_cap: 0,
            n_fabricated: 0,
            n_batches: 3,
            parse_retries: 2,
            max_tags: 8,
          },
        },
      ],
    }
    mockedGetRunMovies.mockResolvedValue(MIXED)
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByTestId('run-funnel')

    // 召回 8+5=13、提及 7+5=12、门控过 12−1=11、挂载成 11−1=10、产出 4+5=9
    expect(screen.getByTestId('run-funnel-recall')).toHaveTextContent('召回13')
    expect(screen.getByTestId('run-funnel-mentioned')).toHaveTextContent('提及12')
    expect(screen.getByTestId('run-funnel-passed')).toHaveTextContent('门控过11')
    expect(screen.getByTestId('run-funnel-mounted')).toHaveTextContent('挂载成10')
    expect(screen.getByTestId('run-funnel-kept')).toHaveTextContent('产出9')
    // 次要计数逐键加总：编造 2+0=2、批次 2+3=5（解析重试 1+2=3）
    const secondary = screen.getByTestId('run-funnel-secondary')
    expect(secondary).toHaveTextContent('编造 2')
    expect(secondary).toHaveTextContent('批次 5（解析重试 3）')
    // 存活率：甲片 4/8=0.5、乙片 5/5=1 → min 0.5 · 中位 0.75 · max 1
    expect(screen.getByTestId('run-funnel-survival')).toHaveTextContent(
      '每片存活率 min 0.5 · 中位 0.75 · max 1',
    )
    // 覆盖 N/M：2 部非 null / 共 3 行（失败片计入 M 不计入 N）
    expect(screen.getByTestId('run-funnel-coverage')).toHaveTextContent('合计覆盖 2 部 / 共 3 部')
  })

  it('召回 0 的片不进存活率；全部召回 0 → 存活率「暂无」', async () => {
    // 一片召回 0（候选全灭）+ 一片正常（2/4=0.5）：存活率只算正常片
    mockedGetRunMovies.mockResolvedValue({
      ...SUMMARY,
      rows: [
        { ...SUMMARY.rows[1], tt: 'tt0000020', title: '全灭片', funnel: FUNNEL_ZERO_RECALL },
        {
          ...SUMMARY.rows[1],
          tt: 'tt0000021',
          title: '正常片',
          funnel: { ...FUNNEL, n_recall: 4, n_mentioned: 4, n_kept: 2, n_gate: 1, n_unmountable: 1, n_m_cap: 0, n_fabricated: 0, n_batches: 1, parse_retries: 0, max_tags: 6 },
        },
      ],
    })
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByTestId('run-funnel')
    expect(screen.getByTestId('run-funnel-survival')).toHaveTextContent(
      '每片存活率 min 0.5 · 中位 0.5 · max 0.5',
    )

    // 第二场景换夹具重挂载（同测试内双 render 需手动 cleanup 防重复节点）
    cleanup()
    mockedGetRunMovies.mockResolvedValue({
      ...SUMMARY,
      rows: [
        { ...SUMMARY.rows[1], tt: 'tt0000030', funnel: FUNNEL_ZERO_RECALL },
        { ...SUMMARY.rows[1], tt: 'tt0000031', funnel: FUNNEL_ZERO_RECALL },
      ],
    })
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByTestId('run-funnel')
    // 聚合仍在（召回 0 是真实求和 0，可渲染），存活率无 eligible → 「暂无」
    expect(screen.getByTestId('run-funnel-recall')).toHaveTextContent('召回0')
    expect(screen.getByTestId('run-funnel-survival')).toHaveTextContent('每片存活率 暂无')
  })

  it('切 θ 重拉后整体块数值不变（payload.rows 纯派生，θ 不触碰聚合）', async () => {
    // 第二份 payload：同 funnel、指标行不同（spearman/p_at_k/r_at_k 变）
    mockedGetRunMovies
      .mockResolvedValueOnce(SUMMARY_THETA)
      .mockResolvedValueOnce({
        ...SUMMARY_THETA,
        theta_p: 0.4,
        rows: SUMMARY_THETA.rows.map((r) =>
          r.tt === 'tt0000001' ? { ...r, spearman: 0.9, p_at_k: 0.9, r_at_k: 0.9 } : r),
      })
    render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')
    expect(screen.getByTestId('run-funnel-recall')).toHaveTextContent('召回9')

    fireEvent.mouseDown(screen.getByTestId('theta-p-select'))
    fireEvent.click(await screen.findByTitle('0.4'))
    await waitFor(() => expect(screen.getByText('P@10(θ0.4)')).toBeInTheDocument())

    // 重拉落地：聚合值不漂移（九键/派生/覆盖全同）
    expect(screen.getByTestId('run-funnel-recall')).toHaveTextContent('召回9')
    expect(screen.getByTestId('run-funnel-mentioned')).toHaveTextContent('提及8')
    expect(screen.getByTestId('run-funnel-passed')).toHaveTextContent('门控过7')
    expect(screen.getByTestId('run-funnel-mounted')).toHaveTextContent('挂载成6')
    expect(screen.getByTestId('run-funnel-kept')).toHaveTextContent('产出5')
    expect(screen.getByTestId('run-funnel-coverage')).toHaveTextContent('合计覆盖 2 部 / 共 3 部')
  })

  // ---------- 轮询驱动 refreshTick（增补 2026-11-27） ----------

  it('refreshTick 变化即重拉：带当前 θ 参数、展开与 θ 选择保留、旧表不闪', async () => {
    // 初载 → 切 θ_P 重拉 → refreshTick 重拉（未决 promise 卡住——在途窗口
    // 断言旧表不闪）。重拉落地行不变 → 展开仍开、明细缓存命中不重发。
    mockedGetRunMovies
      .mockResolvedValueOnce(SUMMARY_THETA)
      .mockResolvedValueOnce({ ...SUMMARY_THETA, theta_p: 0.4 })
    let resolveRefresh!: (p: MovieSummaryPayload) => void
    mockedGetRunMovies.mockReturnValueOnce(
      new Promise<MovieSummaryPayload>((res) => {
        resolveRefresh = res
      }),
    )
    mockedGetRunMovieDetail.mockResolvedValue(DETAIL)
    const { rerender } = render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    // 先展开一行：明细取数并入缓存
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(1))
    await screen.findByText('动作')

    // 切 θ_P 到 0.4（初值 0.2——同值重选不发事件）：重拉带参、缓存清空、展开收起
    fireEvent.mouseDown(screen.getByTestId('theta-p-select'))
    fireEvent.click(await screen.findByTitle('0.4'))
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(2))
    expect(mockedGetRunMovies).toHaveBeenNthCalledWith(2, RUN_ID, { p: 0.4 })

    // 重展开：缓存已清 → 明细带 θ 取数并回填缓存
    fireEvent.click(expandIconOf(rowOf('tt0000001')))
    await waitFor(() => expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(2))
    await screen.findByText('动作')

    // refreshTick 变化即重拉：带当前 θ 参数；在途窗口旧表不闪（行与展开
    // 明细仍在——重拉不清 payload/展开态）
    rerender(<ClosedEvalMovieTable runId={RUN_ID} refreshTick={1} />)
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(3))
    expect(mockedGetRunMovies).toHaveBeenNthCalledWith(3, RUN_ID, { p: 0.4 })
    expect(screen.getByText('tt0000001')).toBeInTheDocument()
    expect(screen.getByText('动作')).toBeVisible()

    // 重拉落地（行不变）→ 视图依旧：展开不收起、明细缓存命中不重发、θ 选择保留
    await act(async () => {
      resolveRefresh(SUMMARY_THETA)
    })
    expect(screen.getByText('动作')).toBeVisible()
    expect(mockedGetRunMovieDetail).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('theta-p-select').textContent).toContain('0.4')
  })

  it('refreshTick 重拉失败静默：旧表保留、无 error Alert、无 theta-refetch-error', async () => {
    // 初载 SUMMARY → refreshTick 重拉 reject → 静默
    mockedGetRunMovies
      .mockResolvedValueOnce(SUMMARY)
      .mockRejectedValueOnce(new Error('轮询重拉失败'))
    const { rerender } = render(<ClosedEvalMovieTable runId={RUN_ID} />)
    await screen.findByText('tt0000001')

    rerender(<ClosedEvalMovieTable runId={RUN_ID} refreshTick={1} />)
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(2))

    // 静默：payload 仍是首次值（行与表头照旧）、无整页错误 Alert、无 θ 重拉警示
    expect(screen.getByText('tt0000001')).toBeInTheDocument()
    expect(screen.getByText('P@8(θ0.2)')).toBeInTheDocument()
    expect(document.querySelector('.ant-alert-error')).toBeNull()
    expect(screen.queryByTestId('theta-refetch-error')).not.toBeInTheDocument()

    // 下一 tick 重试成功 → 照常恢复
    mockedGetRunMovies.mockResolvedValueOnce(SUMMARY)
    rerender(<ClosedEvalMovieTable runId={RUN_ID} refreshTick={2} />)
    await waitFor(() => expect(mockedGetRunMovies).toHaveBeenCalledTimes(3))
    expect(screen.getByText('tt0000001')).toBeInTheDocument()
    expect(screen.getByText('P@8(θ0.2)')).toBeInTheDocument()
  })
})
