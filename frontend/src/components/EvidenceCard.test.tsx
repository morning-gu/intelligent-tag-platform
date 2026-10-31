// 证据卡测试（Task 10）：渲染证据明细（场景号/mm:ss 时间段/描述），点击
// 一项生成内容详情定位导航参数（mock useNavigate，断言 scene 与 t 入参）。
import { fireEvent, render, screen } from '@testing-library/react'
import { useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import EvidenceCard from './EvidenceCard'
import type { ReviewEvidence } from '../api/review'

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: vi.fn() }
})

const mockedUseNavigate = vi.mocked(useNavigate)

const EVIDENCE: ReviewEvidence[] = [
  { scene_id: 's2', start_ms: 65_000, end_ms: 71_000, description: '主角争执推搡' },
  { scene_id: 's5', start_ms: 0, end_ms: 3_000, description: '片头冲突镜头' },
]

beforeEach(() => {
  mockedUseNavigate.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('EvidenceCard 证据卡', () => {
  it('渲染场景号、mm:ss 时间段与描述', () => {
    render(<EvidenceCard assetId="a1" evidence={EVIDENCE} />)

    expect(screen.getByText('s2')).toBeInTheDocument()
    expect(screen.getByText('01:05 - 01:11')).toBeInTheDocument()
    expect(screen.getByText('主角争执推搡')).toBeInTheDocument()
    expect(screen.getByText('s5')).toBeInTheDocument()
    expect(screen.getByText('00:00 - 00:03')).toBeInTheDocument()
  })

  it('点击证据项：跳内容详情并携带 scene 与 t 定位参数', () => {
    const navigate = vi.fn()
    mockedUseNavigate.mockReturnValue(navigate)

    render(<EvidenceCard assetId="a1" evidence={EVIDENCE} />)
    fireEvent.click(screen.getByRole('button', { name: /s2/ }))

    expect(navigate).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith('/assets/a1?scene=s2&t=65000')
  })

  it('evidence 为空时展示空态提示且不渲染可点击项', () => {
    render(<EvidenceCard assetId="a1" evidence={[]} />)

    expect(screen.getByText('暂无证据')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('提供 onEvidenceClick 时点击不再跳转，原位回调证据对象（详情页 seek 用）', () => {
    const navigate = vi.fn()
    mockedUseNavigate.mockReturnValue(navigate)
    const onEvidenceClick = vi.fn()

    render(<EvidenceCard assetId="a1" evidence={EVIDENCE} onEvidenceClick={onEvidenceClick} />)
    fireEvent.click(screen.getByRole('button', { name: /s2/ }))

    expect(onEvidenceClick).toHaveBeenCalledTimes(1)
    expect(onEvidenceClick).toHaveBeenCalledWith(EVIDENCE[0])
    expect(navigate).not.toHaveBeenCalled()
  })
})
