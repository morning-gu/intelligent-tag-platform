// 内容资产详情（Task 11，承诺二核心）：审核员不看代码即可回答"模型为什么
// 打了这个标签"。
// - 上半：视频播放器（原生 video，src 走视频流端点）+ 场景时间轴。场景
//   点击 → 播放器 seek 到场景起始毫秒并选中高亮；播放器不依赖详情加载
//   （src 只需 assetId），挂载即可被 URL query 定位。
// - 下半：该资产全部生成标签（GET /assets/{id}/tags），行展开证据卡；
//   点击证据 → 播放器 seek 到证据 start_ms + 选中对应场景（EvidenceCard
//   的 onEvidenceClick 原位消费，不跳转）。所有标签证据涉及的场景集合
//   以金色反向高亮常驻时间轴（承诺二①）。
// - URL query ?scene={scene_id}&t={start_ms}：审核中心证据卡跳入定位——
//   挂载/参数变更时解析并 seek + 选中场景。
import { Alert, Card, Progress, Spin, Table, Tag, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import {
  fetchAssetContext,
  fetchAssetDetail,
  fetchAssetTags,
  type AssetDetailData,
  type AssetScene,
  type AssetTagRow,
  type ContentContext,
} from '../api/assets'
import { formatTimestamp, type ReviewEvidence } from '../api/review'
import EvidenceCard from '../components/EvidenceCard'
import SceneTimeline from '../components/SceneTimeline'
import VideoPlayer, { type VideoPlayerHandle } from '../components/VideoPlayer'

/** 审核状态中文展示（键集与后端契约固定枚举一致，与审核中心相同）。 */
const STATUS_LABELS: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已拒绝',
  merged: '已合并',
}

/** 审核状态 Tag 颜色。 */
const STATUS_COLORS: Record<string, string> = {
  pending: 'gold',
  approved: 'green',
  rejected: 'red',
  merged: 'purple',
}

/** 来源中文展示（origin 为数据模型固定取值 generated/catalog）。 */
const ORIGIN_LABELS: Record<string, string> = { generated: '生成', catalog: '目录' }

export default function AssetDetail() {
  const { assetId = '' } = useParams()
  const [searchParams] = useSearchParams()
  const playerRef = useRef<VideoPlayerHandle>(null)

  // —— 页面级数据：详情 + 标签（任一失败整页报错）——
  const [detail, setDetail] = useState<AssetDetailData | null>(null)
  const [tags, setTags] = useState<AssetTagRow[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  // —— 时间轴数据：首个上下文的内容 JSON（非致命：失败仅时间轴不可用，
  //    标签列表与播放器 seek 定位仍可用）——
  const [context, setContext] = useState<ContentContext | null>(null)
  const [contextError, setContextError] = useState<string | null>(null)
  // —— 交互态：当前选中场景（场景点击/证据定位写入）。记录写入时的
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
      const [detailData, tagRows] = await Promise.all([
        fetchAssetDetail(assetId),
        fetchAssetTags(assetId),
      ])
      setDetail(detailData)
      setTags(tagRows)
      setLoadError(null)
      // 默认展示最早导入的上下文（后端按 created_at 升序返回）；多上下文
      // 切换选择器留给后续任务
      const firstContextId = detailData.contexts[0]?.context_id
      if (firstContextId !== undefined) {
        try {
          setContext(await fetchAssetContext(assetId, firstContextId))
          setContextError(null)
        } catch (err) {
          setContext(null)
          setContextError(err instanceof Error ? err.message : '场景数据加载失败')
        }
      } else {
        setContext(null)
        setContextError(null)
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
    playerRef.current?.seek(scene.time_range.start_ms)
  }

  /** 证据点击（标签卡原位定位）：seek 到证据 start_ms + 选中对应场景。 */
  function handleEvidenceClick(ev: ReviewEvidence) {
    setSelected({ assetId, sceneId: ev.scene_id, params: searchParams })
    playerRef.current?.seek(ev.start_ms)
  }

  // 全部标签证据涉及的场景集合（时间轴金色反向高亮，常驻不随选中变化）
  const evidenceSceneIds = useMemo(
    () => [...new Set(tags.flatMap((t) => t.evidence.map((e) => e.scene_id)))],
    [tags],
  )

  // 时间轴宽度基准：优先资产 duration_ms；缺失时回落到场景最大结束毫秒
  const maxSceneEnd =
    context === null
      ? 0
      : context.content.scenes.reduce((m, s) => Math.max(m, s.time_range.end_ms), 0)
  const durationMs = detail?.duration_ms ?? maxSceneEnd

  const tagColumns: TableProps<AssetTagRow>['columns'] = [
    { title: '标签名', dataIndex: 'tag_name' },
    {
      title: '来源',
      dataIndex: 'origin',
      render: (v: string) => (
        <Tag color={v === 'generated' ? 'blue' : 'green'}>{ORIGIN_LABELS[v] ?? v}</Tag>
      ),
    },
    {
      title: '置信度',
      dataIndex: 'confidence',
      render: (v: number | null) =>
        v === null ? '暂无' : <Progress percent={Math.round(v * 100)} size="small" />,
    },
    {
      title: '相关度',
      dataIndex: 'relevance_score',
      render: (v: number | null) => (v === null ? '暂无' : v.toFixed(2)),
    },
    {
      title: '状态',
      dataIndex: 'review_status',
      render: (v: string) => (
        <Tag color={STATUS_COLORS[v] ?? 'default'}>{STATUS_LABELS[v] ?? v}</Tag>
      ),
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
                {detail.contexts.length > 0 && (
                  <Typography.Text type="secondary" style={{ marginInlineStart: 12 }}>
                    {`识别上下文 ${detail.contexts[0].context_id} · ${detail.contexts[0].model_version ?? '未知版本'} · ${detail.contexts[0].recognition_status ?? '未知状态'}`}
                  </Typography.Text>
                )}
              </div>
              {context !== null && (
                <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                  {context.content.summary}
                </Typography.Paragraph>
              )}
            </>
          )}
          {context !== null ? (
            <SceneTimeline
              scenes={context.content.scenes}
              durationMs={durationMs}
              selectedSceneId={selectedSceneId}
              highlightSceneIds={evidenceSceneIds}
              onSceneClick={handleSceneClick}
            />
          ) : loading ? (
            <Spin />
          ) : contextError !== null ? (
            <Alert type="warning" showIcon message="场景数据加载失败" description={contextError} />
          ) : (
            <Typography.Text type="secondary">暂无识别上下文</Typography.Text>
          )}
        </div>
      </Card>
      <Card title="标签列表">
        <Table<AssetTagRow>
          rowKey="id"
          columns={tagColumns}
          dataSource={tags}
          loading={loading}
          expandable={{
            expandedRowRender: (row) => (
              <EvidenceCard
                assetId={assetId}
                evidence={row.evidence}
                onEvidenceClick={handleEvidenceClick}
              />
            ),
            rowExpandable: (row) => row.evidence.length > 0,
          }}
          pagination={false}
        />
      </Card>
    </div>
  )
}
