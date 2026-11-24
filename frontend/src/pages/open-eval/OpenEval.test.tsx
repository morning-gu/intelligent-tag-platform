// 开放评测页测试：mock ../api/evalOpen 与闭环 ../api/eval（跨服务互斥的
// busy 探测走闭环 listRuns——镜像 ClosedEval.test.tsx 的 mock 结构，mock 到
// 模块边界即隔离网络），覆盖：挂载双拉与历史行渲染（状态 Tag 文案、五指标
// 列、null 一律「暂无」——None≠0 纪律）、datasets 拉取与选项交集数提示、
// 起跑载荷组装（modalities 默认 script+dialogue、并发默认 4、custom 选片
// ids、缺 script 客户端拒绝）、video 勾选禁用+tooltip（v1 文本模态）、
// closed 活跃 → 起跑禁用并注明占用 run（跨服务互斥的前端半边）、open 活跃
// 挂载发现接管轮询、2s 轮询推进 running→succeeded 与 running→failed（终态
// 停表收卡片+起跑恢复；failed 是 worker 收敛异常兜底终态，closed 第 11 轮
// 教训——TERMINAL_STATUSES 必含）、取消按钮调 cancelOpenRun、删除
// Popconfirm（span 拦冒泡——antd v6 Trigger 教训，删除点击不得连带触发行
// 点击重开报告）、行点击取报告（404 毫秒级窗口优雅提示不崩页面/渲染
// Task 8 报告视图——note 原文 + banner 警示 + openFileUrl 下载链接）、
// 数据集根挂载展示与应用、数据集加载失败提示。
// 轮询用例以 vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
// 只伪造 interval（setTimeout/Date/rAF 保持真实——antd 渲染与 RTL waitFor
// 不受影响），用 advanceTimersByTimeAsync 推进 tick 并以 act 包裹刷新。
// 注意：antd 对两个汉字的按钮自动插入空隙（"起跑"渲染为"起 跑"），按钮
// 可访问名查询一律用宽容正则（与 ClosedEval 测试同理）。
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OpenEval from './OpenEval'
import { ApiError } from '../../api/client'
import { listRuns, type DatasetInfo, type MovieRef, type RunSummary } from '../../api/eval'
import {
  cancelOpenRun,
  deleteOpenRun,
  getOpenDatasetRoot,
  getOpenReport,
  listOpenDatasets,
  listOpenMovies,
  listOpenRuns,
  setOpenDatasetRoot,
  startOpenRun,
  type OpenEvalReport,
  type OpenRunSummary,
} from '../../api/evalOpen'

vi.mock('../../api/evalOpen', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/evalOpen')>()
  return {
    ...actual,
    listOpenDatasets: vi.fn(),
    listOpenMovies: vi.fn(),
    getOpenDatasetRoot: vi.fn(),
    setOpenDatasetRoot: vi.fn(),
    startOpenRun: vi.fn(),
    listOpenRuns: vi.fn(),
    getOpenRun: vi.fn(),
    cancelOpenRun: vi.fn(),
    deleteOpenRun: vi.fn(),
    getOpenReport: vi.fn(),
  }
})

// 跨服务互斥探测消费闭环 listRuns（与 open 页共享互斥状态的服务端契约）
vi.mock('../../api/eval', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/eval')>()
  return {
    ...actual,
    listRuns: vi.fn(),
  }
})

const mockedListOpenDatasets = vi.mocked(listOpenDatasets)
const mockedListOpenMovies = vi.mocked(listOpenMovies)
const mockedGetOpenDatasetRoot = vi.mocked(getOpenDatasetRoot)
const mockedSetOpenDatasetRoot = vi.mocked(setOpenDatasetRoot)
const mockedStartOpenRun = vi.mocked(startOpenRun)
const mockedListOpenRuns = vi.mocked(listOpenRuns)
const mockedCancelOpenRun = vi.mocked(cancelOpenRun)
const mockedDeleteOpenRun = vi.mocked(deleteOpenRun)
const mockedGetOpenReport = vi.mocked(getOpenReport)
const mockedListRuns = vi.mocked(listRuns)

// ---------- 夹具（与后端 DatasetInfoOut / MovieRefOut / OpenRunSummaryOut 契约同形） ----------

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

/** 历史三行：succeeded（五指标齐全）/ cancelled（五指标 null→暂无）/
 * interrupted（进度与指标全 null→暂无）。 */
const RUNS_HISTORY: OpenRunSummary[] = [
  {
    run_id: 'run-20261120-100000-ab12',
    dataset_id: 'genome-scores',
    status: 'succeeded',
    done: 30,
    total: 30,
    failed: 2,
    started_at: '2026-11-20T10:00:00',
    finished_at: '2026-11-20T11:30:00',
    error: null,
    pass_rate: 0.66,
    new_pass: 0.8,
    hit_pass: 0.72,
    three_high: 0.25,
    new_ratio: 0.21,
  },
  {
    run_id: 'run-20261119-090000-cd34',
    dataset_id: 'genome-scores',
    status: 'cancelled',
    done: 12,
    total: 30,
    failed: 0,
    started_at: '2026-11-19T09:00:00',
    finished_at: '2026-11-19T10:05:00',
    error: null,
    pass_rate: null,
    new_pass: null,
    hit_pass: null,
    three_high: null,
    new_ratio: null,
  },
  {
    run_id: 'run-20261118-080000-ef56',
    dataset_id: null,
    status: 'interrupted',
    done: null,
    total: null,
    failed: null,
    started_at: null,
    finished_at: null,
    error: null,
    pass_rate: null,
    new_pass: null,
    hit_pass: null,
    three_high: null,
    new_ratio: null,
  },
]

/** 挂载历史里发现的活跃 run 行（运行中无报告，五指标 null；终态时刻未到）。 */
const RUNNING_SUMMARY: OpenRunSummary = {
  run_id: 'run-20261121-120000-xy90',
  dataset_id: 'genome-scores',
  status: 'running',
  done: 8,
  total: 30,
  failed: 1,
  started_at: '2026-11-21T12:00:00',
  finished_at: null,
  error: null,
  pass_rate: null,
  new_pass: null,
  hit_pass: null,
  three_high: null,
  new_ratio: null,
}

/** 轮询推进的 succeeded 终态行（done=30 与 cancelled 行的 12/30 区分定位）。 */
const SUCCEEDED_SUMMARY: OpenRunSummary = {
  ...RUNNING_SUMMARY,
  status: 'succeeded',
  done: 30,
  failed: 2,
  finished_at: '2026-11-21T13:30:00',
  pass_rate: 0.66,
  new_pass: 0.8,
  hit_pass: 0.72,
  three_high: 0.25,
  new_ratio: 0.21,
}

/** 轮询推进的 failed 终态行（worker 收敛异常兜底终态——TERMINAL_STATUSES
 * 必含，closed 第 11 轮教训）。 */
const FAILED_SUMMARY: OpenRunSummary = {
  ...RUNNING_SUMMARY,
  status: 'failed',
  done: 14,
  error: 'RuntimeError: 收敛循环异常',
}

/** 闭环侧活跃 run 行（跨服务互斥探测的发现源——open 起跑禁用的另一路）。 */
const CLOSED_RUNNING_SUMMARY: RunSummary = {
  run_id: 'run-closed-20261121-090000-zz77',
  dataset_id: 'genome-scores',
  status: 'running',
  done: 5,
  total: 30,
  failed: 0,
  started_at: '2026-11-21T09:00:00',
  finished_at: null,
  error: null,
  ndcg10: null,
  p10: null,
  r20: null,
  coverage_median: null,
}

/** getOpenReport 成功夹具（与后端 assemble_open_report 键逐一镜像）。 */
const REPORT: OpenEvalReport = {
  schema_version: 3, status: 'succeeded', source_run_id: null,
  banner: ['部分运行：cancelled/aborted/failed 或 done<total 的 run 不可与全量横比'],
  sample_accounting: { total: 30, ok: 28, failed: 2, review_failed: 1 },
  summary: {
    total_tags: 120,
    reviewed_tags: 110, passed_tags: 96, judged_tags: 110, review_pending: 0,
    hits: 80,
    new_words: 40,
    new_ratio: 0.33,
    pass_rate: 0.75,
    new_pass: 0.8,
    hit_pass: 0.72,
    review_missing: 3,
    metrics: {
      total_tags: 110,
      evaluated_tags: 110,
      max_score: 4,
      coverage: 1,
      joint_high_rate: 0.4,
      three_dimension_high_rate: 0.25,
      undetermined_tags: 4,
      undetermined_rate: 0.036,
      relevance: { evaluated_tags: 110, average: 3.2, high_rate: 0.5,
        distribution: [2, 5, 18, 40, 45], undetermined: 4, undetermined_rate: 0.036 },
      semantics: { evaluated_tags: 110, average: 3.0, high_rate: 0.4,
        distribution: [1, 4, 25, 45, 35], undetermined: 2, undetermined_rate: 0.018 },
      value: { evaluated_tags: 110, average: 2.6, high_rate: 0.3,
        distribution: [3, 10, 35, 37, 25], undetermined: 1, undetermined_rate: 0.009 },
    },
  },
  produced_stats: {
    counts: [{ tt: 'tt0000001', hits: 14, new_words: 5 }],
    min: 8,
    median: 15.5,
    max: 30,
  },
  judge: {
    funnel: {
      raw: 120, merged: 47, verdicts: 45, judge_passed: 40, rank_kept: 19, final: 7,
      merge_keep_rate: 47 / 120, judge_coverage: 45 / 47, judge_pass_rate: 40 / 47,
      judge_pass_rate_scored: 40 / 45, rank_keep_rate: 19 / 40, dir_mount_rate: 7 / 19,
      final_keep_rate: 7 / 47,
    },
    scores: {
      relevance: { n: 47, average: 3.4, distribution: [1, 3, 8, 15, 20], null: 2 },
      semantics: { n: 49, average: 3.1, distribution: [0, 2, 10, 20, 17], null: 0 },
      value: { n: 49, average: 2.8, distribution: [2, 6, 15, 16, 10], null: 0 },
    },
    dropped: { 'judge 评分未过线（value=1 < 2）': 3 },
    bands: { duplicate: 30, suspect: 5, new: 4 },
  },
  calibre: {
    llm_model: 'qwen3.8-27b',
    embedding_model: 'bge-m3',
    prompt_version: 'mtg-v6-graded-judge',
    review_model: 'qwen3.8-27b',
    review_same_model: true,
    review_prompt_version: 'open-review-v2',
    library_version: 'lib-v3',
    dataset_id: 'genome-scores',
    modalities: ['script', 'dialogue'],
  },
  movies: [
    {
      tt: 'tt0000001',
      title: '铁道游击队',
      ok: true,
      error: null,
      review_error: null,
      elapsed_ms: 4210,
      n_hits: 1,
      n_new_words: 1,
      hits: [
        {
          tag_id: 't1',
          tag_name: '悬疑',
          desc: '悬念驱动的叙事',
          level: 'film',
          judge_reference: { relevance: 4, semantics: 3, value: 3, reason: 'judge 依据' },
          review: {
            pass: true,
            relevance: { score: 4, reason: '场景有据' },
            semantics: { score: 3, reason: '名称清晰' },
            value: { score: 3, reason: '有区分度' },
            reason: '场景有据；名称清晰；有区分度',
            evaluated: true, evidence_scene_ids: ['scene_1'],
          },
        },
      ],
      new_words: [
        {
          name: '新词甲',
          desc: '通用释义',
          level: 'film',
          judge_reference: null,
          review: {
            pass: false,
            relevance: { score: 2, reason: '限定缺失' },
            semantics: { score: 3, reason: '名称清晰' },
            value: { score: 2, reason: '普通类目' },
            reason: '限定缺失；名称清晰；普通类目',
            evaluated: true, evidence_scene_ids: [],
          },
        },
      ],
      pass_rate: 0.5,
      new_pass: 0,
      hit_pass: 1,
      review_missing: 0, review_pending: 0,
      metrics: null,
    },
  ],
  failed_movies: [{ tt: 'tt0000003', error: 'RuntimeError: produce 失败' }],
  review_failed_movies: [{ tt: 'tt0000002', review_error: 'ReviewRequestError: 评审端点超时' }],
  warnings: ['评审失败 1 部，已整体排除出通过率'],
  note: 'genome relevance 是模型输出非人工标注',
}

// ---------- mock 装配与查询辅助 ----------

/** 无活跃 run 的常规桩：open 数据集/历史与 closed 活跃探测（空闲）齐备。 */
function mockIdleApis() {
  mockedListOpenDatasets.mockResolvedValue(DATASETS)
  mockedListOpenRuns.mockResolvedValue(RUNS_HISTORY)
  mockedGetOpenDatasetRoot.mockResolvedValue({ root: '/cache/MovieTags', is_default: true })
  mockedListRuns.mockResolvedValue([])
}

/** 挂载历史含 open 活跃 run 的桩（轮询由页面自行接管——立即首拉）。 */
function mockActiveRunApis() {
  mockedListOpenDatasets.mockResolvedValue(DATASETS)
  mockedListOpenRuns.mockResolvedValue([RUNNING_SUMMARY])
  mockedGetOpenDatasetRoot.mockResolvedValue({ root: '/cache/MovieTags', is_default: true })
  mockedListRuns.mockResolvedValue([])
}

/** 选中数据集下拉第一项（打开下拉 → 按 title 定位点击，与 ClosedEval
 * 测试的 antd v6 下拉交互方式一致）。 */
async function selectDataset(title: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '数据集' }))
  fireEvent.click(await screen.findByTitle(title))
}

beforeEach(() => {
  mockedListOpenDatasets.mockReset()
  mockedListOpenMovies.mockReset()
  mockedGetOpenDatasetRoot.mockReset()
  mockedSetOpenDatasetRoot.mockReset()
  mockedStartOpenRun.mockReset()
  mockedListOpenRuns.mockReset()
  mockedCancelOpenRun.mockReset()
  mockedDeleteOpenRun.mockReset()
  mockedGetOpenReport.mockReset()
  mockedListRuns.mockReset()
  localStorage.clear()
})

afterEach(() => {
  vi.clearAllMocks()
})

// ---------- 用例 ----------

describe('OpenEval 开放评测页', () => {
  it('挂载拉取数据集与历史：表单骨架 + 五指标列渲染，null 一律「暂无」', async () => {
    mockIdleApis()
    render(<OpenEval />)

    // 摘要五键列头（后端 _read_open_summary 直读 summary）
    expect(screen.getByText('评审通过率')).toBeInTheDocument()
    expect(screen.getByText('新词通过率')).toBeInTheDocument()
    expect(screen.getByText('命中通过率')).toBeInTheDocument()
    expect(screen.getByText('新词占比')).toBeInTheDocument()
    expect(screen.getByText('三维满分率')).toBeInTheDocument()

    // succeeded 行出现即证明挂载历史拉取完成
    const okRow = (await screen.findByText('2026-11-20 10:00:00')).closest('tr')!
    expect(mockedListOpenRuns).toHaveBeenCalledTimes(1)
    expect(mockedListRuns).toHaveBeenCalledTimes(1) // 同周期 closed 活跃探测
    expect(okRow).toHaveTextContent('genome-scores')
    expect(okRow).toHaveTextContent('已成功')
    expect(okRow).toHaveTextContent('30/30')
    expect(okRow).toHaveTextContent('0.66')
    expect(okRow).toHaveTextContent('0.8')
    expect(okRow).toHaveTextContent('0.72')
    expect(okRow).toHaveTextContent('0.21')
    expect(okRow).toHaveTextContent('0.25')

    // cancelled 行：12/30、失败 0，五指标 null → 暂无
    const cancelledRow = screen.getByText('2026-11-19 09:00:00').closest('tr')!
    expect(cancelledRow).toHaveTextContent('已取消')
    expect(cancelledRow).toHaveTextContent('12/30')
    expect(within(cancelledRow).getAllByText('暂无')).toHaveLength(5)

    // interrupted 行（全 null 行以状态 Tag 文案定位）：
    // 时间/数据集/影片数 + 五指标 → 8 个「暂无」
    const interruptedRow = screen.getByText('已中断').closest('tr')!
    expect(interruptedRow).toHaveTextContent('已中断')
    expect(within(interruptedRow).getAllByText('暂无')).toHaveLength(8)

    // 表单骨架与数据集根展示（页面两大常驻块）
    expect(screen.getByText('新建评测')).toBeInTheDocument()
    expect(screen.getByText('评测历史')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '数据集根路径' })).toBeInTheDocument()
    expect(screen.getByText(/当前生效：\/cache\/MovieTags/)).toBeInTheDocument()
    // 并发度初始 4（θ 网格已随闭环遗留口径移除）
    expect(screen.getByRole('spinbutton', { name: '并发度' })).toHaveValue('4')
    expect(screen.queryByRole('textbox', { name: 'θ 网格' })).not.toBeInTheDocument()
  })

  it('datasets 拉取：下拉选项带库交集数提示', async () => {
    mockIdleApis()
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '数据集' }))
    expect(await screen.findByTitle('genome-scores（交集 83）')).toBeInTheDocument()
    expect(screen.getByTitle('genome-tags-83（交集 61）')).toBeInTheDocument()
  })

  it('video checkbox 禁用（v1 文本模态）+ tooltip 说明；script/dialogue 默认勾选', async () => {
    mockIdleApis()
    render(<OpenEval />)

    const video = screen.getByRole('checkbox', { name: 'video' })
    expect(video).toBeDisabled()
    expect(video).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'script' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'script' })).not.toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'dialogue' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'dialogue' })).not.toBeDisabled()

    // 悬停出 tooltip（disabled 控件外层包 span 承接 hover）
    fireEvent.mouseEnter(video.closest('label')!.parentElement!)
    expect(await screen.findByText('关键帧接入为后续扩展（v1 文本模态）')).toBeInTheDocument()
  })

  it('起跑：modalities（script+dialogue）进载荷，并发默认 4，无 thetas 键', async () => {
    mockIdleApis()
    mockedStartOpenRun.mockResolvedValue({ run_id: 'run-20261121-120000-xy90' })
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

    await waitFor(() => expect(mockedStartOpenRun).toHaveBeenCalledTimes(1))
    // 严格深比较：载荷不含 thetas 键（闭环遗留口径已移除）
    expect(mockedStartOpenRun).toHaveBeenCalledWith({
      dataset: 'genome-scores',
      movies: { mode: 'preset30' },
      modalities: ['script', 'dialogue'],
      concurrency: 4,
    })
  })

  it('模态校验：取消勾选 script → 客户端拒绝起跑（缺 script 评审材料为空）', async () => {
    mockIdleApis()
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('checkbox', { name: 'script' }))
    fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

    expect(await screen.findByText(/模态必须包含 script/)).toBeInTheDocument()
    expect(mockedStartOpenRun).not.toHaveBeenCalled()
  })

  it('自定义影片集：listOpenMovies 拉清单，选中 tt 进载荷 ids', async () => {
    mockIdleApis()
    mockedListOpenMovies.mockResolvedValue(MOVIES)
    mockedStartOpenRun.mockResolvedValue({ run_id: 'run-20261121-120000-xy90' })
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')

    await selectDataset('genome-scores（交集 83）')
    fireEvent.click(screen.getByRole('radio', { name: '自定义' }))

    await waitFor(() => expect(mockedListOpenMovies).toHaveBeenCalledWith('genome-scores'))
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '影片清单' }))
    fireEvent.click(await screen.findByTitle('铁道游击队'))
    fireEvent.click(await screen.findByTitle('高山下的花环'))

    fireEvent.click(screen.getByRole('button', { name: /起\s*跑/ }))

    await waitFor(() => expect(mockedStartOpenRun).toHaveBeenCalledTimes(1))
    expect(mockedStartOpenRun).toHaveBeenCalledWith({
      dataset: 'genome-scores',
      movies: { mode: 'custom', ids: ['tt0000001', 'tt0000002'] },
      modalities: ['script', 'dialogue'],
      concurrency: 4,
    })
  })

  it('closed 活跃 → 起跑禁用并提示（跨服务互斥，提示注明占用 run）', async () => {
    mockIdleApis()
    mockedListRuns.mockResolvedValue([CLOSED_RUNNING_SUMMARY])
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')

    expect(screen.getByRole('button', { name: /起\s*跑/ })).toBeDisabled()
    expect(
      screen.getByText(/闭环评测 run（run-closed-20261121-090000-zz77）进行中/),
    ).toBeInTheDocument()
  })

  it('open 活跃（挂载从历史发现）→ 进行中卡片 + 起跑禁用 + 提示 + 取消按钮', async () => {
    mockActiveRunApis()
    render(<OpenEval />)

    // 接管轮询的立即首拉把真实进度补上
    expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /起\s*跑/ })).toBeDisabled()
    expect(
      screen.getByText(/已有进行中的开放评测 run（run-20261121-120000-xy90）/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /取\s*消/ })).toBeInTheDocument()
  })

  it('open 活跃：实时报告按钮取 live 报告并标注评审进行中', async () => {
    mockActiveRunApis()
    mockedGetOpenReport.mockResolvedValue({ ...REPORT, status: 'running' })
    render(<OpenEval />)
    expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '实时报告' }))
    expect(
      await screen.findByText(/评测报告：run-20261121-120000-xy90（评审进行中…）/),
    ).toBeInTheDocument()
    expect(mockedGetOpenReport).toHaveBeenCalledWith('run-20261121-120000-xy90')
  })

  it('取消：点击后调用 cancelOpenRun(run_id) 并提示受理', async () => {
    mockActiveRunApis()
    mockedCancelOpenRun.mockResolvedValue({ ok: true })
    render(<OpenEval />)
    expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /取\s*消/ }))

    await waitFor(() =>
      expect(mockedCancelOpenRun).toHaveBeenCalledWith('run-20261121-120000-xy90'),
    )
    expect(await screen.findByText(/已请求取消/)).toBeInTheDocument()
  })

  it('轮询推进 running→succeeded：终态停表收卡片、起跑恢复', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      mockIdleApis()
      mockedListOpenRuns.mockResolvedValue([RUNNING_SUMMARY])
      render(<OpenEval />)

      // 挂载发现 running 行 + 接管轮询（立即首拉，清单拉了两次）
      await waitFor(() => expect(mockedListOpenRuns).toHaveBeenCalledTimes(2))
      expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()

      // 下一周期清单里该 run 已 succeeded：终态 → 停表、卡片收起、起跑恢复
      mockedListOpenRuns.mockResolvedValue([SUCCEEDED_SUMMARY])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
      expect(mockedListOpenRuns).toHaveBeenCalledTimes(3)
      expect(screen.queryByText(/已完成\s*8\s*\/\s*30/)).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: /起\s*跑/ })).not.toBeDisabled()
      // 终态行已在本周期刷新进历史表
      expect(screen.getByText('已成功')).toBeInTheDocument()

      // 终态后定时器已清：继续推进不产生新轮询
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(mockedListOpenRuns).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('轮询收到 failed 终态（worker 兜底终态）：停止轮询、渲染「已失败」、起跑恢复', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      mockIdleApis()
      mockedListOpenRuns.mockResolvedValue([RUNNING_SUMMARY])
      render(<OpenEval />)

      await waitFor(() => expect(mockedListOpenRuns).toHaveBeenCalledTimes(2))
      expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()

      mockedListOpenRuns.mockResolvedValue([FAILED_SUMMARY])
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
      expect(mockedListOpenRuns).toHaveBeenCalledTimes(3)
      expect(screen.queryByText(/已完成\s*8\s*\/\s*30/)).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: /起\s*跑/ })).not.toBeDisabled()
      // failed 行渲染「已失败」Tag（中文文案而非英文原文）
      const failedRow = screen.getByText('已失败').closest('tr')!
      expect(failedRow).toHaveTextContent('14/30')

      // 终态后定时器已清：继续推进不产生新轮询
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(mockedListOpenRuns).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('运行中行的删除按钮禁用', async () => {
    mockActiveRunApis()
    render(<OpenEval />)
    expect(await screen.findByText(/已完成\s*8\s*\/\s*30/)).toBeInTheDocument()
    const row = screen.getByText('进行中').closest('tr')!
    expect(within(row).getByRole('button', { name: /删\s*除/ })).toBeDisabled()
  })

  it('删除终态 run：Popconfirm 确认 → deleteOpenRun + 刷新历史 + 报告卡片收起', async () => {
    mockIdleApis()
    mockedGetOpenReport.mockResolvedValue(REPORT)
    mockedDeleteOpenRun.mockResolvedValue({ ok: true })
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')
    // 先打开该 run 的报告（行点击），再删它——报告卡片应一并收起
    fireEvent.click(
      within(screen.getByText('2026-11-20 10:00:00').closest('tr')!).getByText('已成功'),
    )
    expect(
      await screen.findByText(/评测报告：run-20261120-100000-ab12/),
    ).toBeInTheDocument()
    fireEvent.click(
      within(screen.getByText('2026-11-20 10:00:00').closest('tr')!)
        .getByRole('button', { name: /删\s*除/ }),
    )
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))
    await waitFor(() =>
      expect(mockedDeleteOpenRun).toHaveBeenCalledWith('run-20261120-100000-ab12'),
    )
    // 等 UI 生效而非只看 mock 调用数：报告收起是异步续体里的状态更新
    await waitFor(() => {
      expect(mockedListOpenRuns).toHaveBeenCalledTimes(2)
      expect(
        screen.queryByText(/评测报告：run-20261120-100000-ab12/),
      ).not.toBeInTheDocument()
    })
    // 删除点击不得连带触发行点击重开报告（外层 span 拦截冒泡）
    expect(mockedGetOpenReport).toHaveBeenCalledTimes(1)
  })

  it('删除失败（409 运行中）：透出后端 detail，不崩页面', async () => {
    mockIdleApis()
    mockedDeleteOpenRun.mockRejectedValue(
      new ApiError(409, '运行中的 run 不可删除（先取消或等收尾）：run-x'),
    )
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')
    fireEvent.click(
      within(screen.getByText('2026-11-20 10:00:00').closest('tr')!)
        .getByRole('button', { name: /删\s*除/ }),
    )
    fireEvent.click(await screen.findByRole('button', { name: /确\s*定/ }))
    expect(await screen.findByText(/不可删除（先取消或等收尾）/)).toBeInTheDocument()
    // 失败不刷新历史（挂载一次之外无第二次拉取）
    expect(mockedListOpenRuns).toHaveBeenCalledTimes(1)
  })

  it('行点击取报告：Task 8 报告视图渲染（note 原文 + banner 警示 + 下载五链接）', async () => {
    mockIdleApis()
    mockedGetOpenReport.mockResolvedValue(REPORT)
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')

    fireEvent.click(
      within(screen.getByText('2026-11-20 10:00:00').closest('tr')!).getByText('已成功'),
    )

    expect(await screen.findByText(/评测报告：run-20261120-100000-ab12/)).toBeInTheDocument()
    // 报告视图由 OpenEvalReport 组件接管：银标准 note 原文恒渲染 + banner 警示
    expect(
      await screen.findByText('genome relevance 是模型输出非人工标注'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('部分运行：cancelled/aborted/failed 或 done<total 的 run 不可与全量横比'),
    ).toBeInTheDocument()
    // 下载链接走 openFileUrl 纯路径拼接（字面量期望防恒真）
    expect(screen.getByRole('link', { name: 'metrics.csv' })).toHaveAttribute(
      'href',
      '/api/v1/eval/open/runs/run-20261120-100000-ab12/files/metrics.csv',
    )
    expect(mockedGetOpenReport).toHaveBeenCalledWith('run-20261120-100000-ab12')
  })

  it('行点击取报告：终态落盘非原子的 404 窗口优雅提示，不崩页面', async () => {
    mockIdleApis()
    mockedGetOpenReport.mockRejectedValue(new ApiError(404, '报告不存在或尚未生成'))
    render(<OpenEval />)
    await screen.findByText('2026-11-20 10:00:00')

    fireEvent.click(
      within(screen.getByText('2026-11-20 10:00:00').closest('tr')!).getByText('已成功'),
    )

    expect(await screen.findByText('报告生成中，稍后重试')).toBeInTheDocument()
    // 页面未被卸载：其余历史行仍在
    expect(screen.getByText('2026-11-19 09:00:00')).toBeInTheDocument()
    expect(mockedGetOpenReport).toHaveBeenCalledWith('run-20261120-100000-ab12')
  })

  it('数据集根：挂载显示当前生效根；localStorage 存值（is_default 时）预填输入', async () => {
    localStorage.setItem('open-eval-dataset-root', '/cache/MovieTagsV2')
    mockIdleApis()
    render(<OpenEval />)

    // 当前生效 = GET 返回的服务端真相
    expect(await screen.findByText(/当前生效：\/cache\/MovieTags/)).toBeInTheDocument()
    // is_default=true（重启后 override 已消失）→ 输入框预填上次选择，仅提示
    expect(screen.getByRole('textbox', { name: '数据集根路径' })).toHaveValue('/cache/MovieTagsV2')
  })

  it('应用数据集根：调 setOpenDatasetRoot → 刷新数据集列表与生效根、存 localStorage', async () => {
    mockIdleApis()
    mockedSetOpenDatasetRoot.mockResolvedValue({ root: '/cache/MovieTagsV2', is_default: false })
    render(<OpenEval />)
    await screen.findByText(/当前生效：\/cache\/MovieTags/)

    fireEvent.change(screen.getByRole('textbox', { name: '数据集根路径' }), {
      target: { value: '/cache/MovieTagsV2' },
    })
    fireEvent.click(screen.getByRole('button', { name: /应\s*用/ }))

    await waitFor(() => expect(mockedSetOpenDatasetRoot).toHaveBeenCalledWith('/cache/MovieTagsV2'))
    // 成功后：数据集列表重拉（新根的数据集）+ 生效展示更新 + localStorage 记上次输入
    await waitFor(() => expect(mockedListOpenDatasets).toHaveBeenCalledTimes(2))
    expect(await screen.findByText(/当前生效：\/cache\/MovieTagsV2/)).toBeInTheDocument()
    expect(localStorage.getItem('open-eval-dataset-root')).toBe('/cache/MovieTagsV2')
  })

  it('应用数据集根失败：400 detail 经错误提示透出，不崩页面', async () => {
    mockIdleApis()
    mockedSetOpenDatasetRoot.mockRejectedValue(
      new ApiError(400, '数据集根目录不存在或不是目录：/nope'),
    )
    render(<OpenEval />)
    await screen.findByText(/当前生效：\/cache\/MovieTags/)

    fireEvent.change(screen.getByRole('textbox', { name: '数据集根路径' }), {
      target: { value: '/nope' },
    })
    fireEvent.click(screen.getByRole('button', { name: /应\s*用/ }))

    expect(await screen.findByText(/不存在或不是目录：\/nope/)).toBeInTheDocument()
    // 失败不落 localStorage
    expect(localStorage.getItem('open-eval-dataset-root')).toBeNull()
  })

  it('数据集加载失败：渲染错误提示（透出错误文案）', async () => {
    mockedListOpenDatasets.mockRejectedValue(new Error('后端不可用'))
    mockedListOpenRuns.mockResolvedValue([])
    mockedListRuns.mockResolvedValue([])
    render(<OpenEval />)

    expect(await screen.findByText('数据集列表加载失败')).toBeInTheDocument()
    expect(screen.getByText('后端不可用')).toBeInTheDocument()
  })
})
