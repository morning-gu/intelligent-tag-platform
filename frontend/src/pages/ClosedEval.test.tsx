// 闭环评测页测试：mock ../api/eval 的取数函数（页面只依赖该模块，mock 到
// 模块边界即隔离网络），覆盖：挂载双拉与历史行渲染（状态 Tag 文案、指标列、
// null 一律「暂无」——None≠0 纪律）、起跑载荷组装（θ/K 逗号串→number[]、
// 留空不携带走服务端默认网格、并发默认 4、custom 选片 ids）、活跃 run 的
// 起跑禁用+提示（挂载从历史发现 running 行 → getRun 接管轮询——Review
// Focus 6 的前端半边）、取消按钮调 cancelRun、2s 轮询推进 running→succeeded
// 与 running→failed（终态清定时器+刷新历史+起跑恢复；failed 是后端第五终态
// worker 兜底）、卸载清定时器、行点击取报告（404 毫秒
// 级窗口优雅提示不崩页面/成功渲染 Task 9 报告视图）、数据集加载失败提示。
// 轮询用例以 vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
// 只伪造 interval（setTimeout/Date/rAF 保持真实——antd 渲染与 RTL waitFor
// 不受影响），用 advanceTimersByTimeAsync 推进 tick 并以 act 包裹刷新。
// 注意：antd 对两个汉字的按钮自动插入空隙（"起跑"渲染为"起 跑"），按钮
// 可访问名查询一律用宽容正则（与 ModelConfig 测试同理）。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ClosedEval from './ClosedEval'
import { ApiError } from '../api/client'
import {
  cancelRun,
  deleteRun,
  getDatasetRoot,
  getReport,
  getRun,
  listDatasets,
  listMovies,
  listRuns,
  setDatasetRoot,
  startRun,
  type ClosedEvalReport,
  type DatasetInfo,
  type MovieRef,
  type RunStatus,
  type RunSummary,
} from '../api/eval'

vi.mock('../api/eval', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/eval')>()
  return {
    ...actual,
    listDatasets: vi.fn(),
    listMovies: vi.fn(),
    startRun: vi.fn(),
    listRuns: vi.fn(),
    getRun: vi.fn(),
    cancelRun: vi.fn(),
    deleteRun: vi.fn(),
    getReport: vi.fn(),
    getDatasetRoot: vi.fn(),
    setDatasetRoot: vi.fn(),
  }
})

const mockedListDatasets = vi.mocked(listDatasets)
const mockedListMovies = vi.mocked(listMovies)
const mockedStartRun = vi.mocked(startRun)
const mockedListRuns = vi.mocked(listRuns)
const mockedGetRun = vi.mocked(getRun)
const mockedCancelRun = vi.mocked(cancelRun)
const mockedDeleteRun = vi.mocked(deleteRun)
const mockedGetReport = vi.mocked(getReport)
const mockedGetDatasetRoot = vi.mocked(getDatasetRoot)
const mockedSetDatasetRoot = vi.mocked(setDatasetRoot)

// ---------- 夹具（与后端 DatasetInfoOut / MovieRefOut / RunSummaryOut / RunStateOut 契约同形） ----------

const DATASETS: DatasetInfo[] = [
  {
    id: 'genome-scores',
    gt_path: '/cache/MovieTags/gt/genome-scores.csv',
    n_movies: 131,
    n_tags: 147,
    library_overlap: 83,
    usable_movies: 131,
  },
  {
    id: 'genome-tags-83',
    gt_path: '/cache/MovieTags/gt/genome-tags-83.csv',
    n_movies: 131,
    n_tags: 83,
    library_overlap: 61,
    usable_movies: 129,
  },
]

/** custom 选片清单候选。 */
const MOVIES: MovieRef[] = [
  { tt: 'tt0000001', title: '铁道游击队' },
  { tt: 'tt0000002', title: '高山下的花环' },
  { tt: 'tt0000003', title: '芙蓉镇' },
]

/** 历史三行：succeeded（指标齐全）/ cancelled（四指标 null→暂无）/
 * interrupted（进度与指标全 null→暂无）。finished_at：succeeded/cancelled
 * 有终态时刻，interrupted（老 run）无 → 暂无。 */
const RUNS_HISTORY: RunSummary[] = [
  {
    run_id: 'run-20261119-100000-ab12',
    dataset_id: 'genome-scores',
    status: 'succeeded',
    done: 30,
    total: 30,
    failed: 2,
    started_at: '2026-11-19T10:00:00',
    finished_at: '2026-11-19T11:30:00',
    error: null,
    ndcg10: 0.48,
    p10: 0.52,
    r20: 0.31,
    coverage_median: 0.55,
  },
  {
    run_id: 'run-20261118-090000-cd34',
    dataset_id: 'genome-scores',
    status: 'cancelled',
    done: 12,
    total: 30,
    failed: 0,
    started_at: '2026-11-18T09:00:00',
    finished_at: '2026-11-18T10:05:00',
    error: null,
    ndcg10: null,
    p10: null,
    r20: null,
    coverage_median: null,
  },
  {
    run_id: 'run-20261117-080000-ef56',
    dataset_id: null,
    status: 'interrupted',
    done: null,
    total: null,
    failed: null,
    started_at: null,
    finished_at: null,
    error: null,
    ndcg10: null,
    p10: null,
    r20: null,
    coverage_median: null,
  },
]

/** 挂载历史里发现的活跃 run 行（运行中无报告，四指标 null；终态时刻未到）。 */
const RUNNING_SUMMARY: RunSummary = {
  run_id: 'run-20261120-120000-xy90',
  dataset_id: 'genome-scores',
  status: 'running',
  done: 8,
  total: 30,
  failed: 1,
  started_at: '2026-11-20T12:00:00',
  finished_at: null,
  error: null,
  ndcg10: null,
  p10: null,
  r20: null,
  coverage_median: null,
}

/** getRun 轮询中的进度快照（与 RUNNING_SUMMARY 同 run）。 */
const RUNNING_STATUS: RunStatus = {
  run_id: 'run-20261120-120000-xy90',
  status: 'running',
  done: 8,
  total: 30,
  failed: 1,
  started_at: '2026-11-20T12:00:00',
  finished_at: null,
  error: null,
}

/** getRun 的终态快照（轮询推进的落点）。 */
const SUCCEEDED_STATUS: RunStatus = {
  ...RUNNING_STATUS,
  status: 'succeeded',
  done: 30,
  failed: 2,
}

/** getRun 的 failed 终态快照（worker 收敛异常兜底态，closed_eval.py:692——
 * 后端第五终态，轮询必须同样收束）。 */
const FAILED_STATUS: RunStatus = {
  ...RUNNING_STATUS,
  status: 'failed',
  done: 12,
  error: 'RuntimeError: 收敛循环异常',
}

/** failed 终态的历史摘要行（终态刷新后进入历史表，done=14 与 cancelled 行
 * 的 12/30 区分定位）。 */
const FAILED_SUMMARY: RunSummary = {
  ...RUNNING_SUMMARY,
  status: 'failed',
  done: 14,
  error: 'RuntimeError: 收敛循环异常',
  ndcg10: null,
  p10: null,
  r20: null,
  coverage_median: null,
}

/** getReport 成功夹具（与后端 report.json 组装结构同形，banner/warning 无）。 */
const REPORT: ClosedEvalReport = {
  banner: null,
  sample_accounting: { total: 30, ok: 28, failed: 2, invalid: 0, no_gt: 0 },
  grid: [
    {
      theta: 0.4,
      k: 10,
      n_valid: 28,
      precision: 0.52,
      recall: 0.2,
      ndcg: 0.48,
      precision_ci: [0.4, 0.6],
      recall_ci: [0.1, 0.3],
      ndcg_ci: [0.35, 0.55],
      precision_random: 0.02,
      recall_random: 0.08,
    },
  ],
  baselines: { prior: [] },
  coverage: { min: 0.2, median: 0.55, max: 0.9, values: [0.2, 0.55, 0.9] },
  spearman: { macro: 0.42, per_movie: [] },
  failed_movies: [],
  warning: null,
  calibre: {
    llm_model: 'qwen3.8-27b',
    embedding_model: 'bge-m3',
    prompt_version: 'closed-v1',
    library_version: null,
    dataset_id: 'genome-scores',
    gt_sha8: 'abcd1234',
  },
  note: 'genome relevance 是模型输出非人工标注',
}

// ---------- mock 装配与查询辅助 ----------

/** 无活跃 run 的常规桩：数据集与历史齐备。 */
function mockIdleApis() {
  mockedListDatasets.mockResolvedValue(DATASETS)
  mockedListRuns.mockResolvedValue(RUNS_HISTORY)
  mockedGetDatasetRoot.mockResolvedValue({ root: '/cache/MovieTags', is_default: true })
}

/** 挂载历史含活跃 run 的桩（配合 getRun 返回 running 进度）。 */
function mockActiveRunApis() {
  mockedListDatasets.mockResolvedValue(DATASETS)
  mockedListRuns.mockResolvedValue([RUNNING_SUMMARY])
  mockedGetRun.mockResolvedValue(RUNNING_STATUS)
}

/** 选中数据集下拉第一项（打开下拉 → 按 title 定位点击，与 GenerationTasks
 * 测试的 antd v6 下拉交互方式一致）。 */
async function selectDataset(title: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '数据集' }))
  fireEvent.click(await screen.findByTitle(title))
}

beforeEach(() => {
  mockedListDatasets.mockReset()
  mockedListMovies.mockReset()
  mockedStartRun.mockReset()
  mockedListRuns.mockReset()
  mockedGetRun.mockReset()
  mockedCancelRun.mockReset()
  mockedDeleteRun.mockReset()
  mockedGetReport.mockReset()
  mockedGetDatasetRoot.mockReset()
  mockedSetDatasetRoot.mockReset()
  localStorage.clear()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('ClosedEval 闭环评测页', () => {
  it('挂载拉取数据集与历史：行渲染状态 Tag/完成/指标列，null 一律「暂无」', async () => {
    mockIdleApis()
    render(<ClosedEval />)

    // 列头标约定取格（后端 _read_summary 统一约定：θ0.2/θ0.8 最近有值格）
    expect(screen.getByText('nDCG@10(θ0.2)')).toBeInTheDocument()
    expect(screen.getByText('P@10(θ0.2)')).toBeInTheDocument()
    expect(screen.getByText('R@20(θ0.8)')).toBeInTheDocument()

    // succeeded 行出现即证明挂载历史拉取完成（数据集拉取由下拉选片用例证明）
    const okRow = (await screen.findByText('2026-11-19 10:00:00')).closest('tr')!
    expect(mockedListRuns).toHaveBeenCalledTimes(1)
    expect(okRow).toHaveTextContent('genome-scores')
    expect(okRow).toHaveTextContent('已成功')
    expect(okRow).toHaveTextContent('30/30')
    expect(within(okRow).getByText('2')).toBeInTheDocument()
    expect(okRow).toHaveTextContent('0.48')
    expect(okRow).toHaveTextContent('0.52')
    expect(okRow).toHaveTextContent('0.31')
    expect(okRow).toHaveTextContent('0.55')

    // cancelled 行：12/30、失败 0，四指标 null → 暂无
    const cancelledRow = screen.getByText('2026-11-18 09:00:00').closest('tr')!
    expect(cancelledRow).toHaveTextContent('已取消')
    expect(cancelledRow).toHaveTextContent('12/30')
    expect(within(cancelledRow).getByText('0')).toBeInTheDocument()
    expect(within(cancelledRow).getAllByText('暂无')).toHaveLength(4)

    // interrupted 行（历史表无 run_id 列，全 null 行以状态 Tag 文案定位）：
    // 进度/指标/开始/结束时间/数据集全 null → 9 个「暂无」
    const interruptedRow = screen.getByText('已中断').closest('tr')!
    expect(interruptedRow).toHaveTextContent('已中断')
    expect(within(interruptedRow).getAllByText('暂无')).toHaveLength(9)
  })

  it('历史表渲染结束时间列：有值显示格式化时间，null → 暂无', async () => {
    mockIdleApis()
    render(<ClosedEval />)
    // succeeded 行的 finished_at → 格式化时间
    const okRow = (await screen.findByText('2026-11-19 11:30:00')).closest('tr')!
    expect(okRow).toHaveTextContent('2026-11-19 11:30:00')
    // cancelled 行的 finished_at 也渲染
    expect(screen.getByText('2026-11-18 10:05:00')).toBeInTheDocument()
    // interrupted 行 finished_at null → 第 9 个「暂无」（首个用例已计数，
    // 这里只确认该行不出现任何格式化时间）
    const interruptedRow = screen.getByText('已中断').closest('tr')!
    expect(interruptedRow.textContent).not.toMatch(/2026-/)
  })

  it('删除终态 run：Popconfirm 确认 → deleteRun + 刷新历史 + 报告卡片收起', async () => {
    mockIdleApis()
    mockedGetReport.mockResolvedValue(REPORT)
    mockedDeleteRun.mockResolvedValue({ ok: true })
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')
    // 先打开该 run 的报告（行点击），再删它——报告卡片应一并收起
    fireEvent.click(
      within(screen.getByText('2026-11-19 10:00:00').closest('tr')!).getByText('已成功'))
    expect(
      await screen.findByText(/评测报告：run-20261119-100000-ab12/)).toBeInTheDocument()
    fireEvent.click(
      within(screen.getByText('2026-11-19 10:00:00').closest('tr')!)
        .getByRole('button', { name: /删\s*除/ }))
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))
    await waitFor(() =>
      expect(mockedDeleteRun).toHaveBeenCalledWith('run-20261119-100000-ab12'))
    // 等 UI 生效而非只看 mock 调用数：报告收起是异步续体里的状态更新
    await waitFor(() => {
      expect(mockedListRuns).toHaveBeenCalledTimes(2)
      expect(
        screen.queryByText(/评测报告：run-20261119-100000-ab12/)).not.toBeInTheDocument()
    })
    // 删除点击不得连带触发行点击重开报告（外层 span 拦截冒泡）
    expect(mockedGetReport).toHaveBeenCalledTimes(1)
  })

  it('删除失败（409 运行中）：透出后端 detail，不崩页面', async () => {
    mockIdleApis()
    mockedDeleteRun.mockRejectedValue(
      new ApiError(409, '运行中的 run 不可删除（先取消或等收尾）：run-x'))
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')
    fireEvent.click(
      within(screen.getByText('2026-11-19 10:00:00').closest('tr')!)
        .getByRole('button', { name: /删\s*除/ }))
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))
    expect(await screen.findByText(/不可删除（先取消或等收尾）/)).toBeInTheDocument()
    // 失败不刷新历史（挂载一次之外无第二次拉取）
    expect(mockedListRuns).toHaveBeenCalledTimes(1)
  })

  it('运行中行的删除按钮禁用', async () => {
    mockActiveRunApis()
    render(<ClosedEval />)
    expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()
    // 历史表无 run_id 列，按状态 Tag 定位运行行
    const row = screen.getByText('进行中').closest('tr')!
    expect(within(row).getByRole('button', { name: /删\s*除/ })).toBeDisabled()
  })

  it('起跑：θ/K 逗号串解析为 number[]（默认档），并发度默认 4', async () => {
    mockIdleApis()
    mockedStartRun.mockResolvedValue({ run_id: 'run-20261120-120000-xy90' })
    // 本用例只断言载荷：受理后的立即首拉给终态，轮询即止（不留活定时器跨卸载）
    mockedGetRun.mockResolvedValue(SUCCEEDED_STATUS)
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    // θ/K 填默认档（placeholder 即默认档位提示）
    fireEvent.change(screen.getByPlaceholderText('默认档 0.2,0.4,0.6,0.8'), {
      target: { value: '0.2,0.4,0.6,0.8' },
    })
    fireEvent.change(screen.getByPlaceholderText('默认档 5,10,20,40'), {
      target: { value: '5,10,20,40' },
    })
    // 并发度保持初始值 4（input value 为字符串）
    expect(screen.getByRole('spinbutton', { name: '并发度' })).toHaveValue('4')

    fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

    await waitFor(() => expect(mockedStartRun).toHaveBeenCalledTimes(1))
    expect(mockedStartRun).toHaveBeenCalledWith({
      dataset: 'genome-scores',
      movies: { mode: 'preset30' },
      thetas: [0.2, 0.4, 0.6, 0.8],
      ks: [5, 10, 20, 40],
      concurrency: 4,
    })
  })

  it('起跑：θ/K 留空不携带（服务端回退默认网格），影片集默认试点 30', async () => {
    mockIdleApis()
    mockedStartRun.mockResolvedValue({ run_id: 'run-20261120-120000-xy90' })
    mockedGetRun.mockResolvedValue(SUCCEEDED_STATUS)
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

    await waitFor(() => expect(mockedStartRun).toHaveBeenCalledTimes(1))
    // 严格深比较：载荷不含 thetas/ks 键
    expect(mockedStartRun).toHaveBeenCalledWith({
      dataset: 'genome-scores',
      movies: { mode: 'preset30' },
      concurrency: 4,
    })
  })

  it('自定义影片集：listMovies 拉清单，选中 tt 进载荷 ids', async () => {
    mockIdleApis()
    mockedListMovies.mockResolvedValue(MOVIES)
    mockedStartRun.mockResolvedValue({ run_id: 'run-20261120-120000-xy90' })
    mockedGetRun.mockResolvedValue(SUCCEEDED_STATUS)
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('radio', { name: '自定义' }))

    // custom 选中后按数据集拉影片清单
    await waitFor(() => expect(mockedListMovies).toHaveBeenCalledWith('genome-scores'))
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '影片清单' }))
    fireEvent.click(await screen.findByTitle('铁道游击队'))
    fireEvent.click(await screen.findByTitle('高山下的花环'))

    fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

    await waitFor(() => expect(mockedStartRun).toHaveBeenCalledTimes(1))
    expect(mockedStartRun).toHaveBeenCalledWith({
      dataset: 'genome-scores',
      movies: { mode: 'custom', ids: ['tt0000001', 'tt0000002'] },
      concurrency: 4,
    })
  })

  it('自定义影片集：搜索框输入关键字按标题过滤下拉选项', async () => {
    mockIdleApis()
    mockedListMovies.mockResolvedValue(MOVIES)
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('radio', { name: '自定义' }))
    await waitFor(() => expect(mockedListMovies).toHaveBeenCalledWith('genome-scores'))

    // 打开下拉并输入搜索词：列表应过滤到标题含「花环」的一部
    const search = screen.getByRole('combobox', { name: '影片清单' })
    fireEvent.mouseDown(search)
    fireEvent.change(search, { target: { value: '花环' } })

    await waitFor(() => {
      expect(screen.getByTitle('高山下的花环')).toBeInTheDocument()
    })
    expect(screen.queryByTitle('铁道游击队')).not.toBeInTheDocument()
    expect(screen.queryByTitle('芙蓉镇')).not.toBeInTheDocument()
  })

  it('搜索按 tt 编号命中（大小写不敏感）', async () => {
    mockIdleApis()
    mockedListMovies.mockResolvedValue(MOVIES)
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('radio', { name: '自定义' }))
    await waitFor(() => expect(mockedListMovies).toHaveBeenCalledWith('genome-scores'))

    // 大写 tt 编号也能命中（filterOption 小写化）
    const search = screen.getByRole('combobox', { name: '影片清单' })
    fireEvent.mouseDown(search)
    fireEvent.change(search, { target: { value: 'TT0000002' } })
    await waitFor(() => {
      expect(screen.getByTitle('高山下的花环')).toBeInTheDocument()
    })
    expect(screen.queryByTitle('铁道游击队')).not.toBeInTheDocument()
    expect(screen.queryByTitle('芙蓉镇')).not.toBeInTheDocument()
  })

  it('活跃 run 时起跑禁用+提示（挂载从历史发现 → getRun 接管轮询）', async () => {
    mockActiveRunApis()
    render(<ClosedEval />)

    // 发现 running 行后立即轮询接管
    await waitFor(() => expect(mockedGetRun).toHaveBeenCalledWith('run-20261120-120000-xy90'))
    // 进行中卡片：进度文本与取消按钮
    expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /取\s*消/ })).toBeInTheDocument()
    // 起跑禁用 + 提示文案
    expect(screen.getByRole('button', { name: /起\s*跑/ })).toBeDisabled()
    expect(screen.getByText(/已有进行中的评测 run/)).toBeInTheDocument()
  })

  it('取消：点击后调用 cancelRun(run_id) 并提示受理', async () => {
    mockActiveRunApis()
    mockedCancelRun.mockResolvedValue({ ok: true })
    render(<ClosedEval />)
    expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }))

    await waitFor(() =>
      expect(mockedCancelRun).toHaveBeenCalledWith('run-20261120-120000-xy90'),
    )
    expect(await screen.findByText(/已请求取消/)).toBeInTheDocument()
  })

  it('轮询推进 running→succeeded：终态清定时器、刷新历史、起跑恢复可用', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      mockIdleApis()
      mockedStartRun.mockResolvedValue({ run_id: RUNNING_STATUS.run_id })
      mockedGetRun.mockResolvedValueOnce(RUNNING_STATUS).mockResolvedValue(SUCCEEDED_STATUS)
      render(<ClosedEval />)
      await screen.findByText('2026-11-19 10:00:00')

      await selectDataset('genome-scores（交集 83）')
      fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

      // 受理后立即首拉（running）→ 进行中卡片
      await waitFor(() => expect(mockedGetRun).toHaveBeenCalledTimes(1))
      expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()

      // 2s tick → succeeded 终态：卡片收起、历史刷新（挂载+终态两次）、起跑恢复
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
      expect(mockedGetRun).toHaveBeenCalledTimes(2)
      expect(screen.queryByText(/已完成\s*8\s*\/\s*30/)).not.toBeInTheDocument()
      expect(mockedListRuns).toHaveBeenCalledTimes(2)
      expect(screen.getByRole('button', { name: /起\s*跑/ })).not.toBeDisabled()

      // 终态后定时器已清：继续推进不产生新轮询
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000)
      })
      expect(mockedGetRun).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('轮询收到 failed 终态（worker 兜底第五终态）：停止轮询、渲染「已失败」、起跑恢复', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      mockIdleApis()
      mockedStartRun.mockResolvedValue({ run_id: RUNNING_STATUS.run_id })
      mockedGetRun.mockResolvedValueOnce(RUNNING_STATUS).mockResolvedValue(FAILED_STATUS)
      render(<ClosedEval />)
      await screen.findByText('2026-11-19 10:00:00')

      await selectDataset('genome-scores（交集 83）')
      fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

      // 受理后立即首拉（running）→ 进行中卡片
      await waitFor(() => expect(mockedGetRun).toHaveBeenCalledTimes(1))
      expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()

      // 终态刷新的历史补一行 failed 摘要（挂载那次已消费默认桩，Once 供终态刷新）
      mockedListRuns.mockResolvedValueOnce([...RUNS_HISTORY, FAILED_SUMMARY])

      // 2s tick → failed 终态：卡片收起、起跑恢复
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
      expect(mockedGetRun).toHaveBeenCalledTimes(2)
      expect(screen.queryByText(/已完成\s*8\s*\/\s*30/)).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: /起\s*跑/ })).not.toBeDisabled()

      // 历史表渲染「已失败」Tag（中文文案而非英文原文）
      const failedRow = screen.getByText('已失败').closest('tr')!
      expect(failedRow).toHaveTextContent('14/30')

      // 终态后定时器已清：继续推进不产生新轮询
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000)
      })
      expect(mockedGetRun).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('卸载清除轮询定时器：不再产生 getRun 请求', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      mockActiveRunApis()
      const { unmount } = render(<ClosedEval />)
      await waitFor(() => expect(mockedGetRun).toHaveBeenCalledTimes(1)) // 立即首拉
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
      expect(mockedGetRun).toHaveBeenCalledTimes(2) // 轮询在跑

      unmount()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(mockedGetRun).toHaveBeenCalledTimes(2) // 卸载后不再轮询
    } finally {
      vi.useRealTimers()
    }
  })

  it('行点击取报告：终态落盘非原子的 404 窗口优雅提示，不崩页面', async () => {
    mockIdleApis()
    mockedGetReport.mockRejectedValue(new ApiError(404, '报告不存在或尚未生成'))
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    fireEvent.click(within(screen.getByText('2026-11-19 10:00:00').closest('tr')!).getByText('已成功'))

    expect(await screen.findByText('报告生成中，稍后重试')).toBeInTheDocument()
    // 页面未被卸载：其余历史行仍在
    expect(screen.getByText('2026-11-18 09:00:00')).toBeInTheDocument()
    expect(mockedGetReport).toHaveBeenCalledWith('run-20261119-100000-ab12')
  })

  it('行点击取报告成功：渲染报告视图（Task 9 组件）——note 与下载链接就位', async () => {
    mockIdleApis()
    mockedGetReport.mockResolvedValue(REPORT)
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    fireEvent.click(within(screen.getByText('2026-11-19 10:00:00').closest('tr')!).getByText('已成功'))

    expect(await screen.findByText(/评测报告：run-20261119-100000-ab12/)).toBeInTheDocument()
    // 报告视图内容（Task 9 组件接管占位区）：银标准 note 原文与产物下载链接
    expect(
      await screen.findByText('genome relevance 是模型输出非人工标注'),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'metrics.csv' })).toHaveAttribute(
      'href',
      '/api/v1/eval/closed/runs/run-20261119-100000-ab12/files/metrics.csv',
    )
    expect(mockedGetReport).toHaveBeenCalledWith('run-20261119-100000-ab12')
  })

  it('起跑失败（409 双发竞态）：透出后端 detail，不进入进行中卡片', async () => {
    mockIdleApis()
    mockedStartRun.mockRejectedValue(new ApiError(409, '已有活跃 run：run-other'))
    render(<ClosedEval />)
    await screen.findByText('2026-11-19 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

    expect(await screen.findByText('已有活跃 run：run-other')).toBeInTheDocument()
    expect(screen.queryByText(/已完成\s*\d+\s*\/\s*\d+/)).not.toBeInTheDocument()
    // 起跑按钮仍可用（可重试）
    expect(screen.getByRole('button', { name: /起\s*跑/ })).not.toBeDisabled()
  })

  it('数据集加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedListDatasets.mockRejectedValue(new Error('后端不可用'))
    mockedListRuns.mockResolvedValue([])
    render(<ClosedEval />)

    expect(await screen.findByText('数据集列表加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })

  it('数据集根：挂载显示当前生效根；localStorage 存值（is_default 时）预填输入', async () => {
    localStorage.setItem('closed-eval-dataset-root', '/cache/MovieTagsV2')
    mockIdleApis()
    render(<ClosedEval />)

    // 当前生效 = GET 返回的服务端真相
    expect(await screen.findByText(/当前生效：\/cache\/MovieTags/)).toBeInTheDocument()
    // is_default=true（重启后 override 已消失）→ 输入框预填上次选择，仅提示
    expect(screen.getByRole('textbox', { name: '数据集根路径' })).toHaveValue('/cache/MovieTagsV2')
  })

  it('应用数据集根：调 setDatasetRoot → 刷新数据集列表与生效根、存 localStorage', async () => {
    mockIdleApis()
    mockedSetDatasetRoot.mockResolvedValue({ root: '/cache/MovieTagsV2', is_default: false })
    render(<ClosedEval />)
    await screen.findByText(/当前生效：\/cache\/MovieTags/)

    fireEvent.change(screen.getByRole('textbox', { name: '数据集根路径' }), {
      target: { value: '/cache/MovieTagsV2' },
    })
    fireEvent.click(screen.getByRole('button', { name: /应\s*用/ }))

    await waitFor(() => expect(mockedSetDatasetRoot).toHaveBeenCalledWith('/cache/MovieTagsV2'))
    // 成功后：数据集列表重拉（新根的数据集）+ 生效展示更新（POST 返回值）+ localStorage 记上次输入
    await waitFor(() => expect(mockedListDatasets).toHaveBeenCalledTimes(2))
    expect(await screen.findByText(/当前生效：\/cache\/MovieTagsV2/)).toBeInTheDocument()
    expect(localStorage.getItem('closed-eval-dataset-root')).toBe('/cache/MovieTagsV2')
  })

  it('应用数据集根失败：400 detail 经错误提示透出，不崩页面', async () => {
    mockIdleApis()
    mockedSetDatasetRoot.mockRejectedValue(
      new ApiError(400, '数据集根目录不存在或不是目录：/nope'))
    render(<ClosedEval />)
    await screen.findByText(/当前生效：\/cache\/MovieTags/)

    fireEvent.change(screen.getByRole('textbox', { name: '数据集根路径' }), {
      target: { value: '/nope' },
    })
    fireEvent.click(screen.getByRole('button', { name: /应\s*用/ }))

    expect(await screen.findByText(/不存在或不是目录：\/nope/)).toBeInTheDocument()
    // 失败不落 localStorage
    expect(localStorage.getItem('closed-eval-dataset-root')).toBeNull()
  })
})
