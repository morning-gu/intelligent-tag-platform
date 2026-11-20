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
// 注意：antd Table 展开图标按钮无可访问名，查询一律走
// .ant-table-row-expand-icon（与 GenerationTasks 测试同口径）。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ClosedEvalMovieTable from './ClosedEvalMovieTable'
import {
  getRunMovieDetail,
  getRunMovies,
  type MovieDetail,
  type MovieSummaryPayload,
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

/** 摘要夹具：k_p=8 / k_r=20 档位（表头字面量 P@8 / R@20）。三行即服务端序：
 * 失败片置顶 → p=0.2 坏例片 → p=null 无交集片垫底。 */
const SUMMARY: MovieSummaryPayload = {
  theta_used: 0.4,
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
    },
  ],
}

/** 明细夹具（坏例片）：produced 含命中（●）/ 未命中（—）/ tag_name 缺失
 * （回退 tag_id）/ GT 分缺失（暂无）四类；out_of_space 与 misses（服务端已
 * 按 gt 分降序：0.9 → 0.6）各含一条。 */
const DETAIL: MovieDetail = {
  tt: 'tt0000001',
  title: '坏例片',
  ok: true,
  error: null,
  elapsed_ms: 8000,
  theta_used: 0.4,
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
    { tag_id: 't-301', tag_name: '悲剧', gt_score: 0.9 },
    { tag_id: 't-302', tag_name: '悬疑', gt_score: 0.6 },
  ],
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
    expect(screen.getByText('P@8')).toBeInTheDocument()
    expect(screen.getByText('R@20')).toBeInTheDocument()

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

    // 失败片与无交集片：Spearman/P@8/R@20 三格 null → 各 3 个暂无；有值行不出暂无
    expect(within(rowOf('tt0000003')).getAllByText('暂无')).toHaveLength(3)
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
})
