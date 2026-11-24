// 单片生产审计面板测试：mock ../api/evalOpen 的 getOpenMovieDiagnostics
// （mock 到模块边界即隔离网络，模式同 ClosedEvalMovieTable.test.tsx）。
// 覆盖：整片 0 条产出时仍列出全部候选与淘汰原因（value=null 整批归零的
// 现场）、保留行标出最终名、目录契合 false 如实呈现、judge 协议异常提示、
// 评审失败批次原因、取数失败透出 message、无审计时的兜底文案。
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import OpenEvalMovieAudit from './OpenEvalMovieAudit'
import { getOpenMovieDiagnostics, type OpenMovieDiagnostics } from '../api/evalOpen'

vi.mock('../api/evalOpen', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/evalOpen')>()
  return { ...actual, getOpenMovieDiagnostics: vi.fn() }
})

const mockedFetch = vi.mocked(getOpenMovieDiagnostics)

const RUN_ID = 'run-20261124-060432-84ee'

function diagnostics(over: Partial<OpenMovieDiagnostics> = {}): OpenMovieDiagnostics {
  return {
    tt: 'tt0067116',
    audit: {
      version: 'mtg-v9-catalog-fit',
      merged: [
        { tag: '粗粝写实', scope: 'film' },
        { tag: '屋顶枪战追逐', scope: 'scene' },
        { tag: '冷峻压抑', scope: 'film' },
      ],
      verdicts: [
        { tag: '粗粝写实', relevance: 3, semantics: 3, value: null, in_dir: true,
          reason: '风格需一步推断', quote: 'Let me bust him.' },
        { tag: '屋顶枪战追逐', relevance: 4, semantics: 4, value: 4, in_dir: false,
          reason: '动作直接可核', quote: 'Nicoli from the roof' },
        { tag: '冷峻压抑', relevance: 3, semantics: 3, value: 2, in_dir: true,
          reason: '跨场景情绪基调', quote: 'either skated' },
      ],
      dropped: [
        { tag: '粗粝写实', why: 'judge 评分未过线（value=null < 2）' },
        { tag: '屋顶枪战追逐', why: '标签含义不属于目标目录' },
      ],
      accepted: [{ tag: '冷峻压抑', candidate: '冷峻压抑' }],
    },
    review: null,
    ...over,
  }
}

describe('OpenEvalMovieAudit 生产审计面板', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('0 条产出也列出候选去向：null 分与淘汰原因逐条可见', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      audit: { ...diagnostics().audit!, accepted: [] },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    expect(await screen.findByText('粗粝写实')).toBeInTheDocument()
    expect(screen.getByText('judge 评分未过线（value=null < 2）')).toBeInTheDocument()
    expect(screen.getByText('标签含义不属于目标目录')).toBeInTheDocument()
    expect(screen.getByText('合并候选 3 条 · judge 判决 3 条 · 最终保留 0 条')).toBeInTheDocument()
    expect(screen.getAllByText('null').length).toBeGreaterThan(0)
    expect(screen.getByText('不属于')).toBeInTheDocument()
    expect(mockedFetch).toHaveBeenCalledWith(RUN_ID, 'tt0067116')
  })

  it('保留行标出最终名；展开行给 judge 理由与原文摘录', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      audit: {
        ...diagnostics().audit!,
        accepted: [{ tag: '冷峻', candidate: '冷峻压抑' }],
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    await screen.findByText('粗粝写实')
    expect(screen.getByText('保留（库内标准名：冷峻）')).toBeInTheDocument()
    fireEvent.click(document.querySelectorAll('.ant-table-row-expand-icon')[0])
    expect(await screen.findByText('风格需一步推断')).toBeInTheDocument()
    expect(screen.getByText('Let me bust him.')).toBeInTheDocument()
  })

  it('judge 协议异常与评审失败批次各自透出', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      audit: {
        ...diagnostics().audit!,
        judge_trace: { errors: [{ attempt: 0, error: '判决不合协议（粗粝写实：value 不接受 null）' }] },
      },
      review: {
        error: 'ReviewRequestError: 评审响应未包含有效判决',
        batches: [{ batch: 't0-t10', attempt: 1, raw: '{}', error: '评审响应未包含有效判决' }],
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    expect(await screen.findByText(/judge 响应曾不合协议/)).toBeInTheDocument()
    expect(screen.getByText(/评审失败：ReviewRequestError/)).toBeInTheDocument()
    expect(screen.getByText(/t0-t10#1 评审响应未包含有效判决/)).toBeInTheDocument()
  })

  it('取数失败透出后端文案；无审计时给兜底说明', async () => {
    mockedFetch.mockRejectedValue(new Error('该影片无诊断产物'))
    const { unmount } = render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    expect(await screen.findByText('无法读取生产审计：该影片无诊断产物')).toBeInTheDocument()
    unmount()
    mockedFetch.mockResolvedValue({ tt: 'tt0067116', audit: null, review: null })
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    await waitFor(() => expect(screen.getByText(/该片没有生产审计/)).toBeInTheDocument())
  })
})
