// 纯 SVG 生产漏斗图。每个环节的
//  band 宽度 ∝ 数量/原始生成（零数量给 4px 占位短条，左侧仍标真实数字，
//  形状不撒谎）；右侧标上一环节→本环节的保留率，图下补两个非相邻分母
//  口径（judge 通过率按合并候选、最终生产保留率）。null 比率「暂无」。
import type { JudgeFunnel } from '../api/evalOpen'

const COLORS = ['#1677ff', '#3d8bff', '#64a9ff', '#8bc7ff', '#b2e5ff', '#d6f0ff', '#e7f6ff']
const ZERO_COLOR = '#d9d9d9'

function percent(value: number | null): string {
  return value === null ? '暂无' : `${(value * 100).toFixed(1)}%`
}

interface Stage {
  label: string
  value: number
  /** 左侧数量行：保留/上游；首环节只有绝对数 */
  fraction: string
  /** 上一环节→本环节保留率（首环节无） */
  rate: number | null
  rateLabel: string
}

function stages(f: JudgeFunnel): Stage[] {
  const rankInput = f.cluster_kept ?? f.judge_passed
  return [
    { label: '原始生成', value: f.raw, fraction: `${f.raw}`, rate: null, rateLabel: '' },
    { label: f.cluster_kept === undefined ? '合并保留' : '送审候选', value: f.merged, fraction: `${f.merged} / ${f.raw}`, rate: f.merge_keep_rate, rateLabel: f.cluster_kept === undefined ? '合并保留率' : '送审保留率' },
    { label: 'Judge 返回', value: f.verdicts, fraction: `${f.verdicts} / ${f.merged}`, rate: f.judge_coverage, rateLabel: '返回覆盖率' },
    { label: 'Judge 通过', value: f.judge_passed, fraction: `${f.judge_passed} / ${f.merged}`, rate: f.judge_pass_rate_scored, rateLabel: '按已评分候选' },
    ...(f.cluster_kept === undefined ? [] : [{
      label: '聚类后保留', value: f.cluster_kept,
      fraction: `${f.cluster_kept} / ${f.judge_passed}`,
      rate: f.cluster_keep_rate ?? null, rateLabel: '聚类保留率',
    }]),
    { label: '排序配额保留', value: f.rank_kept, fraction: `${f.rank_kept} / ${rankInput}`, rate: f.rank_keep_rate, rateLabel: '配额保留率' },
    { label: '最终生产保留', value: f.final, fraction: `${f.final} / ${f.rank_kept}`, rate: f.dir_mount_rate, rateLabel: '挂载成功率' },
  ]
}

// 几何：左标签列 0–150，漏斗中心 340、最大半宽 150，右侧比率列 510 起。
const ROW_H = 46
const BAND_H = 36
const PAD_TOP = 6
const CX = 340
const MAX_HALF = 150
const MIN_HALF = 4
const RIGHT_X = 510
const WIDTH = 760

export default function FunnelChart({ funnel }: { funnel: JudgeFunnel }) {
  const rows = stages(funnel)
  const height = PAD_TOP + rows.length * ROW_H
  const base = Math.max(funnel.raw, 1)
  const half = (value: number) => Math.max(MIN_HALF, MAX_HALF * value / base)
  const summary = `生产数量漏斗：` + rows.map((s) => `${s.label} ${s.value}`).join(' → ')
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      <svg
        viewBox={`0 0 ${WIDTH} ${height}`}
        role="img"
        aria-label={summary}
        style={{ width: '100%', maxWidth: WIDTH, height: 'auto', fontFamily: 'inherit' }}
      >
        <title>{summary}</title>
        {rows.map((stage, i) => {
          const y0 = PAD_TOP + i * ROW_H
          const y1 = y0 + BAND_H
          const topHw = half(stage.value)
          const botHw = half(rows[i + 1]?.value ?? stage.value)
          const color = stage.value === 0 ? ZERO_COLOR : COLORS[i]
          const points = `${CX - topHw},${y0} ${CX + topHw},${y0} ${CX + botHw},${y1} ${CX - botHw},${y1}`
          return (
            <g key={stage.label}>
              <polygon points={points} fill={color} />
              <text x={144} y={y0 + 15} textAnchor="end" fontSize={12} fontWeight={600} fill="#1f2733">
                {stage.label}
              </text>
              <text x={144} y={y0 + 30} textAnchor="end" fontSize={11} fill="#566479">
                {stage.fraction}
              </text>
              {i > 0 && (
                <text x={RIGHT_X} y={y0 + 22} fontSize={12}
                  fill={stage.rate === 0 ? '#cf1322' : '#566479'}>
                  {`${percent(stage.rate)} · ${stage.rateLabel}`}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      <small style={{ color: '#566479' }}>
        {`Judge 通过率（按合并候选）${percent(funnel.judge_pass_rate)} · 最终生产保留率 ${percent(funnel.final_keep_rate)}（${funnel.final}/${funnel.merged}）`}
      </small>
    </div>
  )
}
