// 视频播放器测试（Task 11）：jsdom 无真实视频解码，只断言 video 元素的
// src/controls 属性与 seek 经 ref 暴露后的 currentTime 赋值（毫秒 → 秒）。
import { render } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it } from 'vitest'
import VideoPlayer, { type VideoPlayerHandle } from './VideoPlayer'

describe('VideoPlayer 视频播放器', () => {
  it('渲染原生 video 元素：src 指向资产视频流端点且带控件', () => {
    const { getByTestId } = render(<VideoPlayer assetId="a1" />)

    const video = getByTestId('asset-video') as HTMLVideoElement
    expect(video.tagName).toBe('VIDEO')
    expect(video.src).toContain('/api/v1/assets/a1/video')
    expect(video).toHaveAttribute('controls')
  })

  it('seek(ms)：经 ref 暴露，currentTime 赋值为秒（6500ms → 6.5）', () => {
    const ref = createRef<VideoPlayerHandle>()
    const { getByTestId } = render(<VideoPlayer assetId="a1" ref={ref} />)

    ref.current!.seek(6500)

    const video = getByTestId('asset-video') as HTMLVideoElement
    expect(video.currentTime).toBe(6.5)
  })

  it('seek(0)：回片头（0ms → 0）', () => {
    const ref = createRef<VideoPlayerHandle>()
    const { getByTestId } = render(<VideoPlayer assetId="a1" ref={ref} />)

    ref.current!.seek(65_000)
    ref.current!.seek(0)

    const video = getByTestId('asset-video') as HTMLVideoElement
    expect(video.currentTime).toBe(0)
  })
})
