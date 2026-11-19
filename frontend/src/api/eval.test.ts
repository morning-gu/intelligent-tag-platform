// 闭环评测 API 契约测试：mock apiFetch 模块，断言数据集列表/影片清单/
// 起跑/取消/历史/详情/报告端点的路径与方法形状、起跑载荷 JSON 序列化，
// 以及 fileUrl 纯路径拼接（不走 apiFetch）。类型形状与后端
// tagplatform/api/routers/eval.py 的 Pydantic 模型、closed_eval 的
// report.json 结构对齐（None → null 口径）。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import {
  cancelRun,
  fileUrl,
  getReport,
  getRun,
  listDatasets,
  listMovies,
  listRuns,
  startRun,
  type ClosedEvalReport,
  type StartRunRequest,
} from './eval'

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>()
  return { ...actual, apiFetch: vi.fn() }
})

const mockedApiFetch = vi.mocked(apiFetch)

beforeEach(() => {
  mockedApiFetch.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('取数端点契约', () => {
  it('listDatasets：GET /api/v1/eval/closed/datasets', async () => {
    mockedApiFetch.mockResolvedValue([
      {
        id: 'movie-tags-silver',
        gt_path: 'datasets/movie-tags/gt.csv',
        n_movies: 1100,
        n_tags: 82,
        library_overlap: 83,
        usable_movies: 297,
      },
    ] as never)

    const datasets = await listDatasets()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/datasets')
    expect(datasets).toHaveLength(1)
    expect(datasets[0].usable_movies).toBe(297)
  })

  it('listMovies：GET /api/v1/eval/closed/datasets/{dataset_id}/movies', async () => {
    mockedApiFetch.mockResolvedValue([
      { tt: 'tt0111161', title: '肖申克的救赎' },
    ] as never)

    const movies = await listMovies('movie-tags-silver')

    expect(mockedApiFetch).toHaveBeenCalledWith(
      '/api/v1/eval/closed/datasets/movie-tags-silver/movies',
    )
    expect(movies).toEqual([{ tt: 'tt0111161', title: '肖申克的救赎' }])
  })

  it('listRuns：GET /api/v1/eval/closed/runs（历史摘要行原样透出）', async () => {
    const row = {
      run_id: 'run-1',
      dataset_id: 'movie-tags-silver',
      status: 'succeeded',
      done: 30,
      total: 30,
      failed: 0,
      started_at: '2026-11-20T10:00:00',
      error: null,
      ndcg10: 0.61,
      p10: 0.51,
      r20: 0.33,
      coverage_median: 0.11,
    }
    mockedApiFetch.mockResolvedValue([row] as never)

    const runs = await listRuns()

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/runs')
    expect(runs).toEqual([row])
  })

  it('getRun：GET /api/v1/eval/closed/runs/{run_id}', async () => {
    mockedApiFetch.mockResolvedValue({
      run_id: 'run-1',
      status: 'running',
      done: 12,
      total: 30,
      failed: 0,
      started_at: '2026-11-20T10:00:00',
      error: null,
    } as never)

    const state = await getRun('run-1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/runs/run-1')
    expect(state.status).toBe('running')
  })
})

describe('起跑/取消端点契约', () => {
  it('startRun：POST /api/v1/eval/closed/runs，载荷 JSON 序列化（全参数）', async () => {
    mockedApiFetch.mockResolvedValue({ run_id: 'run-1' } as never)
    // 注解钉死 mode 词表（字面量宽化为 string 会编译报错——契约即文档）
    const req: StartRunRequest = {
      dataset: 'movie-tags-silver',
      movies: { mode: 'custom', ids: ['tt0111161', 'tt1375666'] },
      thetas: [0.3, 0.4, 0.5],
      ks: [10, 20],
      concurrency: 4,
    }

    const created = await startRun(req)

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/runs', {
      method: 'POST',
      body: JSON.stringify(req),
    })
    expect(created).toEqual({ run_id: 'run-1' })
  })

  it('startRun：可选项缺省时载荷不含该键（服务端回退默认网格/并发）', async () => {
    mockedApiFetch.mockResolvedValue({ run_id: 'run-1' } as never)

    await startRun({ dataset: 'movie-tags-silver', movies: { mode: 'preset30' } })

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/runs', {
      method: 'POST',
      body: JSON.stringify({
        dataset: 'movie-tags-silver',
        movies: { mode: 'preset30' },
      }),
    })
  })

  it('cancelRun：POST /api/v1/eval/closed/runs/{run_id}/cancel', async () => {
    mockedApiFetch.mockResolvedValue({ ok: true } as never)

    const result = await cancelRun('run-1')

    expect(mockedApiFetch).toHaveBeenCalledWith(
      '/api/v1/eval/closed/runs/run-1/cancel',
      { method: 'POST' },
    )
    expect(result).toEqual({ ok: true })
  })
})

describe('getReport 报告契约（report.json 形状）', () => {
  it('GET /api/v1/eval/closed/runs/{run_id}/report，响应原样透出', async () => {
    // 全字段夹具：None → null（该 θ 无有效影片/算不出的片/无警示），
    // CI 为自举百分位区间 [lo, hi] 元组——编译期钉死契约形状。
    const report: ClosedEvalReport = {
      banner: null,
      sample_accounting: { total: 30, ok: 28, failed: 2, invalid: 1, no_gt: 5 },
      grid: [
        {
          theta: 0.4,
          k: 10,
          n_valid: 27,
          precision: 0.512,
          recall: 0.331,
          ndcg: 0.602,
          precision_ci: [0.42, 0.6],
          recall_ci: [0.25, 0.41],
          ndcg_ci: [0.51, 0.68],
          precision_random: 0.12,
          recall_random: 0.3,
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
      ],
      baselines: {
        prior: [
          {
            theta: 0.4,
            k: 10,
            n_valid: 27,
            precision: 0.2,
            recall: 0.15,
            ndcg: 0.3,
            precision_ci: [0.15, 0.26],
            recall_ci: [0.1, 0.2],
            ndcg_ci: [0.24, 0.37],
            precision_random: 0.12,
            recall_random: 0.3,
          },
        ],
      },
      coverage: { min: 0.02, median: 0.11, max: 0.4, values: [0.02, 0.11, 0.4] },
      spearman: {
        macro: 0.21,
        per_movie: [
          { tt: 'tt0111161', spearman: 0.4 },
          { tt: 'tt1375666', spearman: null },
        ],
      },
      failed_movies: [{ tt: 'tt0962736', error: 'LLM 调用超时（重试 3 次）' }],
      warning: null,
      calibre: {
        llm_model: 'qwen3.8-27b',
        embedding_model: 'bge-m3',
        prompt_version: 'v3',
        library_version: '2026-11-20',
        dataset_id: 'movie-tags-silver',
        gt_sha8: '1a2b3c4d',
      },
      note: '银标准警示：genome relevance 是相关性模型输出，近似而非人工标注',
    }
    mockedApiFetch.mockResolvedValue(report as never)

    const got = await getReport('run-1')

    expect(mockedApiFetch).toHaveBeenCalledWith('/api/v1/eval/closed/runs/run-1/report')
    expect(got).toEqual(report)
  })
})

describe('fileUrl 纯路径拼接', () => {
  it('五件套白名单名拼出下载路径（不经 apiFetch）', () => {
    expect(fileUrl('run-1', 'report.md')).toBe(
      '/api/v1/eval/closed/runs/run-1/files/report.md',
    )
    expect(fileUrl('run-1', 'metrics.csv')).toBe(
      '/api/v1/eval/closed/runs/run-1/files/metrics.csv',
    )
    expect(fileUrl('run-1', 'raw_results.json')).toBe(
      '/api/v1/eval/closed/runs/run-1/files/raw_results.json',
    )
    expect(fileUrl('run-1', 'meta.json')).toBe(
      '/api/v1/eval/closed/runs/run-1/files/meta.json',
    )
    expect(fileUrl('run-1', 'progress.jsonl')).toBe(
      '/api/v1/eval/closed/runs/run-1/files/progress.jsonl',
    )
    expect(mockedApiFetch).not.toHaveBeenCalled()
  })
})
