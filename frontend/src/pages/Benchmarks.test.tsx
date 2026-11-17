import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { apiFetch } from '../api/client'
import { fetchAssets } from '../api/assets'
import Benchmarks from './Benchmarks'

vi.mock('../api/client', () => ({ apiFetch: vi.fn() }))
vi.mock('../api/assets', () => ({ fetchAssets: vi.fn() }))

const report = {
  id: 'bench_a', algorithm: 'x-mlclass-online-v1', status: 'succeeded', created_at: '2026-11-16',
  evaluation_model: 'glm-5.3-flash', evaluation_version: 'viewer-quality-v3', max_tags: 15,
  items: [], summary: { completed: 0, failed: 0, evaluated: 0,
    generation_tokens: 0, generation_elapsed_s: 0,
    quality: { keep: 0, rewrite: 0, omit: 0, distinct_keep: 0, display_rate: null, mean_distinct_keep: null } },
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(fetchAssets).mockResolvedValue([{ asset_id: 'film1', title: '测试影片', asset_type: 'movie',
    parent_id: null, duration_ms: null, video_uri: null, has_content: true, created_at: null, updated_at: null }])
  vi.mocked(apiFetch).mockImplementation(async (path, init) => path.endsWith('/evaluation-prompt') ? { prompt: '默认评测', max_length: 12000 } : init?.method === 'POST' ? {} : [report])
})
afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

it('shows the X-MLClass benchmark page', async () => {
  render(<Benchmarks />)
  await screen.findByRole('button', { name: '删除记录' })
  expect(screen.getByText('算法基准 · 在线 X-MLClass')).toBeInTheDocument()
  expect(screen.queryByRole('combobox', { name: '基准算法' })).not.toBeInTheDocument()
})

it.each([
  ['x-mlclass-online-v1', '在线 X-MLClass'],
  ['mtg-v3', 'mtg-v3'],
])('submits %s with ordered assets and the edited prompt', async (algorithm, name) => {
  vi.stubEnv('VITE_BENCHMARK_ALGORITHM', algorithm)
  render(<Benchmarks />)
  await screen.findByRole('button', { name: '删除记录' })
  expect(screen.getByText(`算法基准 · ${name}`)).toBeVisible()
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '基准影片' }))
  fireEvent.click(await screen.findByTitle('测试影片'))
  fireEvent.change(screen.getByLabelText('本次评估提示词'), { target: { value: '自定义展示评测' } })
  fireEvent.click(screen.getByRole('button', { name: '生成并自动评估' }))
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/v1/benchmarks', {
    method: 'POST', body: JSON.stringify({ asset_ids: ['film1'], max_tags: 15, algorithm, evaluation_prompt: '自定义展示评测' }),
  }))
})

it.each(['x-mlclass-online-v1', 'mtg-v3'])('shows the shared results table for %s', async algorithm => {
  vi.stubEnv('VITE_BENCHMARK_ALGORITHM', algorithm)
  const longDescription = '长场景原文'.repeat(30)
  const quality = { keep: 0, rewrite: 1, omit: 0, distinct_keep: 0, display_rate: 0 }
  vi.mocked(apiFetch).mockImplementation(async (path) => path.endsWith('/evaluation-prompt') ? { prompt: '默认评测', max_length: 12000 } : [{ ...report,
    summary: { ...report.summary, completed: 1, evaluated: 1, quality: { ...quality, mean_distinct_keep: 0 } },
    algorithm,
    items: [{ asset_id: 'film1', title: '测试影片', status: 'succeeded', generation_tokens: null,
      generation_timings_s: algorithm === 'x-mlclass-online-v1' ? {
        nli_model_load_s: 3, nli_initial_s: 10, llm_keywords_s: 25, nli_rescore_s: 12,
      } : undefined,
      tags: [{ tag_name: '鸟类观察', evidence: [
        { scene_id: 's1', start_ms: 0, end_ms: 99, description: longDescription },
        { scene_id: 's2', start_ms: 100, end_ms: 199, description: '短场景摘要' },
      ] }],
      discovery: { vocabulary_before: 8, vocabulary_after: 9, candidates_after: 2, added_labels: ['new'],
        low_coverage_scenes: ['s1'], rejected_keyword_count: 1, rescored: true, nli_pairs: 32 },
      evaluation: { usage: { total_tokens: 20 }, elapsed_s: 2, quality, judgments: [
        { id: 0, verdict: 'rewrite', reason: '标签可以更明确', suggestion: '候鸟迁徙观察' },
      ] },
    }],
  }])
  render(<Benchmarks />)
  await screen.findByText('测试影片')
  expect(screen.getByRole('columnheader', { name: '评估 token / 秒' })).toBeVisible()
  expect(screen.getByText('20 / 2')).toBeInTheDocument()
  expect(screen.getByText('未知')).toBeInTheDocument()
  const expand = document.querySelector<HTMLButtonElement>('.ant-table-row-expand-icon')!
  fireEvent.click(expand)
  expect(await screen.findByText('标签可以更明确')).toBeInTheDocument()
  expect(screen.getByRole('columnheader', { name: '场景证据' })).toBeVisible()
  expect(screen.getByText('候鸟迁徙观察')).toBeInTheDocument()
  expect(screen.getByText(/已重打分/)).toBeInTheDocument()
  expect(screen.getByText(/低覆盖场景 1 个；待积累候选 2 个；因引用无效跳过 1 个关键词/)).toBeInTheDocument()
  expect(screen.getByText('s2 [100–199ms] 短场景摘要')).toBeVisible()
  expect(screen.getByText(`s1 [0–99ms] ${longDescription}`)).toBeVisible()
  if (algorithm === 'x-mlclass-online-v1') {
    expect(screen.getByText('LLM 标签生成 25.00 秒')).toBeVisible()
    expect(screen.getByText('NLI 首轮打分 10.00 秒')).toBeVisible()
    expect(screen.getByText('NLI 新词重评分 12.00 秒')).toBeVisible()
    expect(screen.getByText('NLI 合计（含加载） 25.00 秒')).toBeVisible()
  } else {
    expect(screen.queryByText('生成耗时明细')).not.toBeInTheDocument()
  }
})


it('deletes a finished report after confirmation', async () => {
  render(<Benchmarks />)
  fireEvent.click(await screen.findByRole('button', { name: '删除记录' }))
  fireEvent.click(await screen.findByRole('button', { name: /^删\s*除$/ }))
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/v1/benchmarks/bench_a', { method: 'DELETE' }))
})
