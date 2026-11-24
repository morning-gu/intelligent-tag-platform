// 清空确认弹窗测试（clear-reset Task 3）：非零键计数清单、输入"清空"
// 点亮确认按钮、确认流（execute → onDone → onClose）、409 透出弹窗不关、
// 预览失败独立错误态不阻塞确认、导出备份链接。fetchClearPreview 经
// vi.mock 桩掉（组件只依赖 clear 模块，不打 global fetch）。
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { fetchClearPreview, type ClearPreview } from '../api/clear'
import ClearConfirmModal, { type ClearOpConfig } from './ClearConfirmModal'

vi.mock('../api/clear', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/clear')>()
  return { ...actual, fetchClearPreview: vi.fn() }
})

const mockedPreview = vi.mocked(fetchClearPreview)

const PREVIEW: ClearPreview = {
  scope: 'all-tags',
  counts: {
    pre_asset_tag: 2,
    asset_tag: 1,
    generation_tasks: 3,
    quality_evaluations: 0,
    asset_build_task: 0,
    media_assets: 0,
    scenes: 0,
    tag_define: 0,
    tag_catalog: 0,
  },
}

const BASE_CONFIG: ClearOpConfig = {
  title: '清空全部内容的标签',
  description: '将删除全部内容的待审核标签、正式挂载与生成任务历史。标签库保留。',
  previewScope: 'all-tags',
  execute: vi.fn().mockResolvedValue({ ok: true, counts: PREVIEW.counts }),
  onDone: vi.fn().mockResolvedValue(undefined),
}

function renderModal(config: ClearOpConfig = BASE_CONFIG) {
  const onClose = vi.fn()
  const utils = render(
    <ClearConfirmModal open onClose={onClose} config={config} />,
  )
  return { onClose, ...utils }
}

/** 打开弹窗并等待计数就绪，返回确认输入框。 */
async function openAndReady(config: ClearOpConfig = BASE_CONFIG) {
  const utils = renderModal(config)
  await screen.findByText('待审核标签：2')
  return utils
}

/** 输入确认词点亮按钮。 */
function typeConfirmWord(word = '清空') {
  fireEvent.change(screen.getByLabelText('清空确认输入'), {
    target: { value: word },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedPreview.mockResolvedValue(PREVIEW)
})

describe('ClearConfirmModal 清空确认弹窗', () => {
  it('只渲染非零计数键的中文名（零键不出现）', async () => {
    await openAndReady()
    expect(screen.getByText('正式挂载：1')).toBeInTheDocument()
    expect(screen.getByText('生成任务：3')).toBeInTheDocument()
    expect(screen.queryByText(/质量评估/)).toBeNull()
    expect(screen.queryByText(/标签定义/)).toBeNull()
  })

  it('输入错误字确认按钮禁用，输入"清空"点亮', async () => {
    await openAndReady()
    const confirm = screen.getByRole('button', { name: '确认清空' })
    expect(confirm).toBeDisabled()
    typeConfirmWord('清空一')
    expect(confirm).toBeDisabled()
    typeConfirmWord('清空')
    expect(confirm).not.toBeDisabled()
  })

  it('确认流：execute 调用一次 → onDone → onClose（父级置 open=false 后卸载）', async () => {
    // jsdom 不触发 antd 关闭动画的 transitionend，DOM 自卸载断言改为按
    // 父级契约：onClose 回调后父级置 open=false → 组件守卫直接返回 null
    const onClose = vi.fn()
    const config: ClearOpConfig = { ...BASE_CONFIG }
    const { rerender } = render(
      <ClearConfirmModal open onClose={onClose} config={config} />,
    )
    await screen.findByText('待审核标签：2')
    typeConfirmWord()
    fireEvent.click(screen.getByRole('button', { name: '确认清空' }))
    await waitFor(() => expect(config.execute).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(config.onDone).toHaveBeenCalled())
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    rerender(
      <ClearConfirmModal open={false} onClose={onClose} config={config} />,
    )
    expect(screen.queryByText(config.title)).toBeNull()
  })

  it('409 错误透出 detail 且弹窗不关', async () => {
    const config: ClearOpConfig = {
      ...BASE_CONFIG,
      execute: vi.fn().mockRejectedValue(
        new ApiError(409, '有 2 个生成/构建任务正在执行，请先等待其完成')),
    }
    const { onClose } = await openAndReady(config)
    typeConfirmWord()
    fireEvent.click(screen.getByRole('button', { name: '确认清空' }))
    await screen.findByText('有 2 个生成/构建任务正在执行，请先等待其完成')
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByText(BASE_CONFIG.title)).toBeInTheDocument()
  })

  it('预览失败独立错误态：描述仍在、确认仍可继续', async () => {
    mockedPreview.mockRejectedValue(new ApiError(500, '预览加载失败'))
    renderModal()
    await screen.findByText('预览加载失败')
    expect(screen.getByText(BASE_CONFIG.description)).toBeInTheDocument()
    typeConfirmWord()
    expect(screen.getByRole('button', { name: '确认清空' }))
      .not.toBeDisabled()
  })

  it('onExport 提供时渲染"先导出备份"链接且 click 调用', async () => {
    const onExport = vi.fn()
    await openAndReady({ ...BASE_CONFIG, onExport })
    fireEvent.click(screen.getByText('先导出备份'))
    expect(onExport).toHaveBeenCalledTimes(1)
  })
})
