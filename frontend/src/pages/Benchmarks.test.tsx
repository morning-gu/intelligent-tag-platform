import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { apiFetch } from '../api/client'
import { fetchAssets } from '../api/assets'
import Benchmarks from './Benchmarks'

vi.mock('../api/client', () => ({ apiFetch: vi.fn() }))
vi.mock('../api/assets', () => ({ fetchAssets: vi.fn() }))

const baseline = {
  id: 'bench_a', algorithm: 'mtg-v3', status: 'succeeded', created_at: '2026-11-16',
  evaluation_model: 'glm-5.3-flash', evaluation_version: 'evidence-concept-v2', max_tags: 15,
  items: [], summary: { completed: 0, failed: 0, evaluated: 0, independent_count: 0,
    effective_rate: null, mean_independent_count: null, unsupported_rate: null,
    generation_tokens: 0, generation_elapsed_s: 0 },
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(fetchAssets).mockResolvedValue([{ asset_id: 'film1', title: '测试影片', asset_type: 'movie',
    parent_id: null, duration_ms: null, video_uri: null, has_content: true, created_at: null, updated_at: null }])
  vi.mocked(apiFetch).mockImplementation(async (path, init) => path.endsWith('/evaluation-prompt') ? { prompt: '默认评测', max_length: 12000 } : init?.method === 'POST' ? {} : [baseline])
})
afterEach(cleanup)

it('replays a completed baseline without sending current selection or a changed cap', async () => {
  render(<Benchmarks />)
  fireEvent.click(await screen.findByRole('button', { name: '用 X-MLClass 回放对比' }))
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/v1/benchmarks/bench_a/replay-xmlclass', { method: 'POST' }))
})

it('submits the selected independent algorithm and ordered assets', async () => {
  render(<Benchmarks />)
  await screen.findByRole('button', { name: '用 X-MLClass 回放对比' })
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '基准算法' }))
  fireEvent.click(await screen.findByTitle('在线 X-MLClass'))
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '基准影片' }))
  fireEvent.click(await screen.findByTitle('测试影片'))
  fireEvent.change(screen.getByLabelText('本次评估提示词'), { target: { value: '自定义展示评测' } })
  fireEvent.click(screen.getByRole('button', { name: '生成并自动评估' }))
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/v1/benchmarks', {
    method: 'POST', body: JSON.stringify({ asset_ids: ['film1'], max_tags: 15, algorithm: 'x-mlclass-online-v1', evaluation_prompt: '自定义展示评测' }),
  }))
})

it('shows discovery growth, unknown tokens and exclusive judgments', async () => {
  vi.mocked(apiFetch).mockImplementation(async (path) => path.endsWith('/evaluation-prompt') ? { prompt: '默认评测', max_length: 12000 } : [{ ...baseline, id: 'bench_b', algorithm: 'x-mlclass-online-v1', baseline_id: 'bench_a', evaluation_version: 'viewer-quality-v3',
    summary: { ...baseline.summary, quality: { keep: 1, rewrite: 0, omit: 0, distinct_keep: 1, display_rate: 1, mean_distinct_keep: 1 } },
    items: [{ asset_id: 'film1', title: '测试影片', status: 'succeeded', generation_tokens: null, tags: [],
      discovery: { vocabulary_before: 8, vocabulary_after: 9, candidates_after: 2, added_labels: ['new'], rescored: true, nli_pairs: 32 },
      comparison: { usage: { total_tokens: 20 }, elapsed_s: 2, rows: [{ id: 0, side: 'b', tag_name: '候鸟迁徙',
        verdict: 'keep', concept: '候鸟迁徙', exclusive: true, reason: '迁徙路线有原文支持', evidence: [] }] },
    }],
  }])
  render(<Benchmarks />)
  await screen.findByText('测试影片')
  expect(screen.queryByRole('button', { name: '用 X-MLClass 回放对比' })).not.toBeInTheDocument()
  expect(screen.getByText('9 / 2')).toBeInTheDocument()
  expect(screen.getByText('未知')).toBeInTheDocument()
  const expand = document.querySelector<HTMLButtonElement>('.ant-table-row-expand-icon')!
  fireEvent.click(expand)
  expect(await screen.findByText('迁徙路线有原文支持')).toBeInTheDocument()
  expect(screen.getByText(/已重打分/)).toBeInTheDocument()
})


it('deletes a finished report after confirmation', async () => {
  render(<Benchmarks />)
  fireEvent.click(await screen.findByRole('button', { name: '删除记录' }))
  fireEvent.click(await screen.findByRole('button', { name: /^删\s*除$/ }))
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/v1/benchmarks/bench_a', { method: 'DELETE' }))
})
