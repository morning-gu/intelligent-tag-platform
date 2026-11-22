import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { OpenEvalReport as Report } from '../api/eval'
import type { QualityMetrics } from '../api/tasks'
import OpenEvalReport from './OpenEvalReport'

it('用总览、五档分布和有效影片覆盖率展示开放评估报告', () => {
  const dimension = {
    evaluated_tags: 1, average: 3, high_rate: 0,
    distribution: [0, 0, 0, 1, 0], undetermined: 0, undetermined_rate: 0,
  }
  const metrics: QualityMetrics = {
    total_tags: 2, evaluated_tags: 1, coverage: 0.5, max_score: 4,
    joint_high_rate: 0, three_dimension_high_rate: 0,
    undetermined_tags: 0, undetermined_rate: 0,
    relevance: dimension, semantics: dimension, value: dimension,
  }
  const report: Report = {
    meta: {
      dataset: { id: 'MovieTagsV2', root: '/cache/MovieTagsV2' }, prompt: { version: 'mtg-v5' },
      llm_model: 'generator', quality_model: 'judge', quality_prompt_version: 'tag-quality-v4',
      mock_llm: false, mock_embedder: false, done: 2, total: 2,
    },
    incomplete: true,
    film_counts: { partial: 1, generation_failed: 1 },
    metrics,
    films: [
      { tt: 'tt1', title: '影片甲', status: 'partial', error: '服务超时', note: null, frame_count: 2, metrics, produced: [] },
      { tt: 'tt2', title: '影片乙', status: 'generation_failed', error: '生成失败原因', note: null,
        frame_count: 0, metrics: null, produced: [] },
    ],
  }
  render(<OpenEvalReport report={report} runId="run-sample" />)
  expect(within(screen.getByTestId('summary-scored')).getByText('1 / 2')).toBeInTheDocument()
  const distribution = within(screen.getByRole('region', { name: '评分分布' }))
  expect(distribution.getByRole('columnheader', { name: '4 分' })).toBeInTheDocument()
  expect(distribution.getByRole('row', { name: /语义准确性.*3.00/ })).toBeInTheDocument()
  expect(screen.getByTestId('bar-row-[0.4,0.6)')).toHaveTextContent('1')
  expect(screen.getByTestId('bar-row-[0,0.2)')).toHaveTextContent('0')
  const issues = within(screen.getByRole('region', { name: '处理异常' }))
  expect(issues.getByText('生成失败原因')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /影片甲/ }))
  expect(within(screen.getByRole('region', { name: '逐片结果' })).getByText('服务超时')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'report.json' })).toHaveAttribute(
    'href', '/api/v1/eval/open/runs/run-sample/files/report.json',
  )
})
