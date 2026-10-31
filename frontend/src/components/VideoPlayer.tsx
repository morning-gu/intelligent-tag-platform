// 视频播放器（Task 11）：原生 <video controls>，src 直连后端视频流端点
// （Range 206 支持浏览器拖动，原生标签即够用）。seek(ms) 经 ref 暴露给
// 父组件——场景时间轴点击与证据卡定位共用同一播放器实例。
import { forwardRef, useImperativeHandle, useRef } from 'react'

/** 播放器对外能力：父组件经 ref 调用 seek 定位到指定毫秒。 */
export interface VideoPlayerHandle {
  /** 定位播放头：currentTime = ms/1000（毫秒 → 秒）。 */
  seek: (ms: number) => void
}

interface VideoPlayerProps {
  /** 所属资产 id（决定视频流 src）。 */
  assetId: string
}

const VideoPlayer = forwardRef<VideoPlayerHandle, VideoPlayerProps>(
  function VideoPlayer({ assetId }, ref) {
    const videoRef = useRef<HTMLVideoElement>(null)

    useImperativeHandle(ref, () => ({
      seek: (ms: number) => {
        if (videoRef.current !== null) {
          videoRef.current.currentTime = ms / 1000
        }
      },
    }))

    return (
      <video
        ref={videoRef}
        data-testid="asset-video"
        controls
        src={`/api/v1/assets/${assetId}/video`}
        style={{ width: '100%', background: '#000' }}
      />
    )
  },
)

export default VideoPlayer
