// 内容资产详情（v2）：上半视频播放器 + 场景时间轴，下半正式挂载标签表。
// - 数据三路并取（Task 12 契约）：资产详情（含 recognized 识别状态）、正式
//   挂载标签（仅审核通过的复制行）、完整内容 JSON（/assets/{id}/content，
//   场景时间轴数据源）。content 404 = 未完成内容识别（上传占位资产/场景子
//   资产），非致命降级——警示提示，播放器与挂载表照常；其他加载失败同样
//   非致命（警示透出错误文案）。context 概念退役，contexts 行删除。
// - 挂载表只保留挂载关系与展示值：标签名/来源（closed 闭环 / open 开放，
//   历史挂载无对应 pre 行时 origin 为 null → "—"）/创建时间。评分/证据/
//   审核状态留在 pre 侧不出现——证据查看走审核中心与任务详情页。
// - 播放器不依赖详情加载（src 只需 assetId），挂载即可被 URL query 定位：
//   ?scene={scene_id}&t={start_ms}（审核中心证据卡跳入）解析并 seek + 选中
//   场景；场景点击 → 播放器 seek 到场景起始毫秒并选中高亮。
import { Alert, Card, Spin, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import {
  fetchAssetContent,
  fetchAssetDetail,
  fetchAssetTags,
  type AssetDetailData,
  type AssetMount,
  type AssetScene,
  type ContentContext,
} from '../api/assets'
import { ApiError } from '../api/client'
import { formatTimestamp } from '../api/review'
import SceneTimeline from '../components/SceneTimeline'
import VideoPlayer, { type VideoPlayerHandle } from '../components/VideoPlayer'

/** 来源中文展示（origin 词表 closed|open，与审核中心同规）。 */
const ORIGIN_LABELS: Record<string, string> = { closed: '闭环', open: '开放' }

/** 来源 Tag 颜色（沿审核中心配色：闭环蓝 / 开放紫）。 */
const ORIGIN_COLORS: Record<string, string> = { closed: 'blue', open: 'purple' }

/** 内容加载的非致命失败标记：404 = 未完成内容识别（空态降级），
 * 其他 = 加载失败（警示透出错误文案）。 */
type ContentIssue = { kind: 'unrecognized' } | { kind: 'error'; message: string }

export default function AssetDetail() {
  const { assetId = '' } = useParams()
  const [searchParams] = useSearchParams()
  const playerRef = useRef<VideoPlayerHandle>(null)

  // —— 页面级数据：详情 + 挂载标签（任一失败整页报错）——
  const [detail, setDetail] = useState<AssetDetailData | null>(null)
  const [tags, setTags] = useState<AssetMount[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  // —— 时间轴数据：完整内容 JSON（非致命：404 未识别走空态降级，其他失败
  //    仅时间轴不可用，标签列表与播放器 seek 定位仍可用）——
  const [content, setContent] = useState<ContentContext | null>(null)
  const [contentIssue, setContentIssue] = useState<ContentIssue | null>(null)
  // —— 交互态：当前选中场景（场景点击/URL 定位写入）。记录写入时的
  //    assetId 与 searchParams 引用，渲染期派生有效选中：资产切换或 URL
  //    参数变更（审核中心再次跳入）后旧点击态自动失效，回退到 query 场景
  //    ——免重置 effect（set-state-in-effect 级联渲染反模式）。
  const [selected, setSelected] = useState<{
    assetId: string
    sceneId: string
    params: URLSearchParams
  } | null>(null)
  const querySceneId = searchParams.get('scene')
  const hasQueryLocate = querySceneId !== null && searchParams.get('t') !== null
  const selectedSceneId =
    selected !== null && selected.assetId === assetId && selected.params === searchParams
      ? selected.sceneId
      : hasQueryLocate
        ? querySceneId
        : null

  const load = useCallback(async () => {
    setLoading(true)
    try {
      // 三路并取：content 失败（404 未识别/其他错误）就地转降级标记，
      // 不让 Promise.all 整页炸掉
      const contentResult = fetchAssetContent(assetId).then(
        (value) => ({ ok: true as const, value }),
        (error: unknown) => ({ ok: false as const, error }),
      )
      const [detailData, tagRows, contentRes] = await Promise.all([
        fetchAssetDetail(assetId),
        fetchAssetTags(assetId),
        contentResult,
      ])
      setDetail(detailData)
      setTags(tagRows)
      setLoadError(null)
      if (contentRes.ok) {
        setContent(contentRes.value)
        setContentIssue(null)
      } else {
        setContent(null)
        const err = contentRes.error
        setContentIssue(
          err instanceof ApiError && err.status === 404
            ? { kind: 'unrecognized' }
            : {
                kind: 'error',
                message: err instanceof Error ? err.message : '场景数据加载失败',
              },
        )
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '资产详情加载失败')
    } finally {
      setLoading(false)
    }
  }, [assetId])

  useEffect(() => {
    // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置
    // state）；cancelled 丢弃卸载/依赖变更后的过期触发
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void load()
    })
    return () => {
      cancelled = true
    }
  }, [load])

  // URL query ?t=（审核中心证据卡跳入定位）：播放器 seek 到 t 毫秒。仅与
  // 外部系统（video 元素）同步，不写 state——场景选中由渲染期派生生效，
  // 时间轴数据后到也不影响高亮。
  useEffect(() => {
    const t = searchParams.get('t')
    if (t === null) {
      return
    }
    const ms = Number(t)
    if (Number.isFinite(ms)) {
      playerRef.current?.seek(ms)
    }
  }, [searchParams])

  /** 场景分段点击：seek 到场景起始毫秒 + 选中高亮。 */
  function handleSceneClick(scene: AssetScene) {
    setSelected({ assetId, sceneId: scene.scene_id, params: searchParams })
    playerRef.current?.seek(scene.start_ms)
  }

  // 时间轴宽度基准：优先资产 duration_ms；缺失时回落到场景最大结束毫秒
  const maxSceneEnd =
    content === null
      ? 0
      : content.content.scenes.reduce((m, s) => Math.max(m, s.end_ms ?? 0), 0)
  const durationMs = detail?.duration_ms ?? maxSceneEnd

  // 挂载表（v2）：仅挂载关系与展示值，评分/证据/审核状态留在 pre 侧
  const mountColumns: TableProps<AssetMount>['columns'] = [
    { title: '标签名', dataIndex: 'tag_name' },
    {
      title: '来源',
      dataIndex: 'origin',
      render: (v: string | null) =>
        v === null ? (
          '—'
        ) : (
          <Tag color={ORIGIN_COLORS[v] ?? 'default'}>{ORIGIN_LABELS[v] ?? v}</Tag>
        ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      render: (v: string | null) => (v === null ? '—' : v.replace('T', ' ').slice(0, 19)),
    },
  ]

  if (loadError !== null) {
    return <Alert type="error" showIcon message="资产详情加载失败" description={loadError} />
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title={detail === null ? '视频与场景' : detail.title}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <VideoPlayer ref={playerRef} assetId={assetId} />
          {detail !== null && (
            <>
              <div>
                <Tag color="blue">{detail.asset_type}</Tag>
                <Typography.Text type="secondary">
                  {`时长 ${detail.duration_ms === null ? '未知' : formatTimestamp(detail.duration_ms)}`}
                </Typography.Text>
                <Tag color={detail.recognized ? 'green' : 'default'} style={{ marginInlineStart: 12 }}>
                  {detail.recognized ? '识别完成' : '未识别'}
                </Tag>
              </div>
              {content !== null && (
                <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                  {content.content.description ?? '（无剧情描述）'}
                </Typography.Paragraph>
              )}
            </>
          )}
          {content !== null ? (
            <SceneTimeline
              scenes={content.content.scenes}
              durationMs={durationMs}
              selectedSceneId={selectedSceneId}
              onSceneClick={handleSceneClick}
            />
          ) : loading ? (
            <Spin />
          ) : contentIssue !== null ? (
            contentIssue.kind === 'unrecognized' ? (
              <Alert
                type="warning"
                showIcon
                message="未完成内容识别"
                description="该资产尚未导入识别 JSON 或未完成识别，场景时间轴暂不可用。"
              />
            ) : (
              <Alert
                type="warning"
                showIcon
                message="场景数据加载失败"
                description={contentIssue.message}
              />
            )
          ) : null}
        </div>
      </Card>
      <Card title="标签列表">
        <Table<AssetMount>
          rowKey="id"
          columns={mountColumns}
          dataSource={tags}
          loading={loading}
          pagination={false}
        />
      </Card>
    </div>
  )
}
