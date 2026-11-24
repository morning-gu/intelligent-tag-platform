// 漏斗图契约：六环节标签/保留÷上游计数/相邻比率、零数量占位、
// null 比率「暂无」（None≠0）；图下补非相邻分母两率。
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import FunnelChart from './FunnelChart'
import type { JudgeFunnel } from '../api/evalOpen'

const FUNNEL: JudgeFunnel = {
  raw: 555, merged: 327, verdicts: 321, judge_passed: 280, rank_kept: 56, final: 0,
  merge_keep_rate: 327 / 555, judge_coverage: 321 / 327, judge_pass_rate: 280 / 327,
  judge_pass_rate_scored: 280 / 321, rank_keep_rate: 56 / 280, dir_mount_rate: 0,
  final_keep_rate: 0,
}

describe('FunnelChart 生产漏斗图', () => {
  it('六环节标签 + 保留/上游计数 + 相邻比率 + 非相邻分母脚注', () => {
    render(<FunnelChart funnel={FUNNEL} />)
    expect(screen.getByRole('img', { name: /生产数量漏斗：原始生成 555/ })).toBeInTheDocument()
    for (const label of ['原始生成', '合并保留', 'Judge 返回', 'Judge 通过', '排序配额保留', '最终生产保留']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByText('327 / 555')).toBeInTheDocument()
    expect(screen.getByText('0 / 56')).toBeInTheDocument()
    expect(screen.getByText('58.9% · 合并保留率')).toBeInTheDocument()
    expect(screen.getByText('98.2% · 返回覆盖率')).toBeInTheDocument()
    expect(screen.getByText('87.2% · 按已评分候选')).toBeInTheDocument()
    expect(screen.getByText('20.0% · 配额保留率')).toBeInTheDocument()
    expect(screen.getByText('0.0% · 挂载成功率')).toBeInTheDocument()
    expect(screen.getByText(/Judge 通过率（按合并候选）85\.6% · 最终生产保留率 0\.0%（0\/327）/)).toBeInTheDocument()
  })

  it('全零漏斗：比率「暂无」，不出现 NaN', () => {
    const zero: JudgeFunnel = {
      raw: 0, merged: 0, verdicts: 0, judge_passed: 0, rank_kept: 0, final: 0,
      merge_keep_rate: null, judge_coverage: null, judge_pass_rate: null,
      judge_pass_rate_scored: null, rank_keep_rate: null, dir_mount_rate: null,
      final_keep_rate: null,
    }
    render(<FunnelChart funnel={zero} />)
    expect(screen.getAllByText(/^暂无 ·/)).toHaveLength(5)
    expect(screen.getByText(/最终生产保留率 暂无（0\/0）/)).toBeInTheDocument()
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
  })
})
