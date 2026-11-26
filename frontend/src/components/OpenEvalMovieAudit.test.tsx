// 单片生产审计面板测试：mock ../api/evalOpen 的 getOpenMovieDiagnostics
// （mock 到模块边界即隔离网络，模式同 ClosedEvalMovieTable.test.tsx）。
// 覆盖：整片 0 条产出时仍列出全部候选与淘汰原因（value=null 整批归零的
// 现场）、保留行标出最终名、目录契合 false 如实呈现、judge 协议异常提示、
// 评审失败批次原因、取数失败透出 message、无审计时的兜底文案。
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

  it('零产出的 judge 失败保留旧候选并可展开每次响应', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      audit: {
        error: 'JudgeError: 缺少有效判决',
        candidates: ['粗粝写实', '冷峻压抑'],
        judge_trace: {
          errors: [{ attempt: 0, error: 'value 漏字段' }, { attempt: 1, error: '始终漏判' }],
          raw: [{ attempt: 0, content: '{"verdicts":[]}' }, { attempt: 1, content: '[]' }],
        },
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    expect(await screen.findByText('粗粝写实')).toBeInTheDocument()
    expect(screen.getByText('合并候选 2 条 · judge 判决 0 条 · 最终保留 0 条')).toBeInTheDocument()
    expect(screen.queryByText(/生成阶段就没有产出/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('judge 原始响应（2 次）'))
    fireEvent.click(screen.getByText('judge 第 2 次 · 始终漏判'))
    expect(screen.getByText('[]')).toBeVisible()
  })

  it('评审原始响应和预算耗尽的条目均可追查', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      review: {
        error: 'ReviewRequestError: 评审响应未包含有效判决',
        batches: [{ batch: 't0-t10', attempt: 1, raw: '{}', error: '评审响应未包含有效判决',
                    targets: ['t0', 't10'], missing: ['t10'] }],
        errors: { t10: '评审调用预算耗尽，未发出恢复请求' },
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    expect(await screen.findByText(/评审失败：ReviewRequestError/)).toBeInTheDocument()
    expect(screen.getByText(/评审未完成：t10 评审调用预算耗尽/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('独立评审原始响应（1 次）'))
    fireEvent.click(screen.getByText(/t0-t10#1.*缺少 t10/))
    expect(screen.getByText('{}')).toBeVisible()
  })

  it('展开逐候选目录判决，区分不契合与漏判', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      audit: {
        merged: [{ tag: '冷峻压抑' }],
        directory_targets: { 冷峻压抑: ['dir_mood', 'dir_event', 'dir_missing'] },
        verdicts: [{ tag: '冷峻压抑', relevance: 3, semantics: 3, value: 3,
                     dir_fits: { dir_mood: true, dir_event: false } }],
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    await screen.findByText('冷峻压抑')
    fireEvent.click(document.querySelectorAll('.ant-table-row-expand-icon')[0])
    expect(await screen.findByText('dir_mood 属于 · dir_event 不属于 · dir_missing 未判')).toBeInTheDocument()
  })

  it('同名候选按编号显示各自的阶段分数和聚类去向', async () => {
    const tag = '重复意象'
    const representative = '反复出现的镜像'
    mockedFetch.mockResolvedValue(diagnostics({
      audit: {
        merged: [
          { tag, stage: 1, candidate_id: 'scene-1' },
          { tag, stage: 2, candidate_id: 'film-1' },
          { tag: representative, stage: 1, candidate_id: 'scene-2' },
        ],
        clustering: { input: 2, target: 1, clusters: [
          { representative: 'scene-2', members: ['scene-1', 'scene-2'] },
        ] },
        verdicts: [
          { tag, candidate_id: 'scene-1', relevance: 4, semantics: 4, value: 2 },
          { tag, candidate_id: 'film-1', relevance: 4, semantics: 4, value: 3 },
        ],
        accepted: [
          { tag, candidate_id: 'film-1' },
          { tag: '镜像母题', candidate: representative, candidate_id: 'scene-2', origin: 'catalog' },
        ],
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    const first = await screen.findByRole('row', { name: /重复意象 一阶段/ })
    const second = screen.getByRole('row', { name: /重复意象 二阶段/ })
    expect(within(first).getByText('2')).toBeInTheDocument()
    expect(within(first).getByText(`聚类未选为代表（代表标签：${representative}）`)).toBeInTheDocument()
    expect(within(second).getByText('3')).toBeInTheDocument()
    expect(within(second).getByText('保留')).toBeInTheDocument()
    expect(screen.getByText('保留（库内标准名：镜像母题）')).toBeInTheDocument()
  })

  it('聚类记录缺少代表候选详情时仍显示原去向', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      audit: {
        merged: [{ tag: '重复意象', candidate_id: 'scene-1' }],
        clustering: { input: 2, target: 1, clusters: [
          { representative: 'scene-2', members: ['scene-1', 'scene-2'] },
        ] },
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    expect(await screen.findByText('聚类未选为代表')).toBeInTheDocument()
  })

  it('新版无 dir_fits 审计隐藏旧目录契合列并在展开行展示价值三方向明细', async () => {
    mockedFetch.mockResolvedValue(diagnostics({
      audit: {
        version: 'mtg-v13-aligned-criteria',
        merged: [{ tag: '雨夜追击', stage: 1, candidate_id: 'scene-1' }],
        verdicts: [{
          tag: '雨夜追击', candidate_id: 'scene-1',
          relevance: 4, semantics: 4, value: 2.8, tag_kind: 'scene',
          value_scores: { presentation: 4, search: 2, recommendation: 0 },
          reason: '场景直接可核', quote: '雨夜中两人追逐',
        }],
        accepted: [{ tag: '雨夜追击', candidate_id: 'scene-1' }],
      },
    }))
    render(<OpenEvalMovieAudit runId={RUN_ID} tt="tt0067116" />)
    expect(await screen.findByText('2.80')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: '目录契合' })).not.toBeInTheDocument()
    fireEvent.click(document.querySelectorAll('.ant-table-row-expand-icon')[0])
    expect(await screen.findByText('场景级：内容呈现 4 · 内容搜索 2 · 内容推荐 0')).toBeInTheDocument()
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
