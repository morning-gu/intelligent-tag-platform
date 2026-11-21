// 闭环评测页（Task 8，admin）：以 genome 真值干跑闭环管线并出量化报告的页签。
// 三块视图：① 创建表单——数据集下拉（选项带交集数提示）、影片集三态
// （试点 30 / 全部 / 自定义多选，清单来自 datasets/{id}/movies）、θ/K 逗号
// 档位（留空不携带 → 服务端回退默认网格）、并发度（默认 4）；② 进行中卡片
// ——进度条 done/total + 取消（取消是受理语义，status 转 cancelled 终态由
// 轮询推进发现，受理后不清轮询）；③ 历史表——多轮 nDCG@10 趋势一眼可见，
// null 一律「暂无」（None≠0 纪律，时间列也走本口径而非 formatTime 的 '—'）。
//
// 单活跃 run 是后端硬约束（POST 二发 → 409）：前端在三个入口同步挡——
// 挂载从历史发现 running 行、提交受理新 run、轮询推进到终态释放——
// activeRun 非空期间起跑禁用并提示；409 竞态兜底透出后端 detail。
// 轮询：受理/接管后立即首拉 + setInterval 2s，到终态（或查询失败）清定时器
// 并刷新历史；卸载清定时器。清定时器时 bump 会话号——在途响应回来对不上号
// 即丢弃，防卸载后/换 run 后把旧响应写进新状态。
// 行点击取报告：GET report 在终态落盘（meta→raw→report）非原子的毫秒级窗口
// 可能 404（后端 Task 6 披露）——优雅提示重试，不崩页面。报告视图渲染委托给
// Task 9 的 ClosedEvalReport 组件，本页只负责取数与 runId 透传。
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import {
  cancelRun,
  deleteRun,
  getDatasetRoot,
  getReport,
  getRun,
  listDatasets,
  listMovies,
  listRuns,
  setDatasetRoot,
  startRun,
  type ClosedEvalReport,
  type DatasetInfo,
  type MovieRef,
  type MovieSelection,
  type RunStatus,
  type RunSummary,
  type StartRunRequest,
} from '../api/eval'
import { formatTime } from '../api/time'
import ClosedEvalReportView from '../components/ClosedEvalReport'

/** 轮询间隔：单片 1–3 分钟量级的进度节奏，2s 足够跟手（spec §6）。 */
const POLL_INTERVAL_MS = 2000

/** localStorage 记上次输入的数据集根：仅作输入预填（服务端重启后 override
 * 消失、is_default=true 时提示上次选择）；生效值真相以 GET 返回为准。 */
const ROOT_LS_KEY = 'closed-eval-dataset-root'

/** run 终态集合（后端状态机：running → succeeded | cancelled | aborted |
 * interrupted | failed——interrupted 是磁盘推断的进程死亡态，failed 是
 * worker 收敛异常/起跑失败的兜底终态，closed_eval.py:692/:571）。 */
const TERMINAL_STATUSES = new Set([
  'succeeded',
  'cancelled',
  'aborted',
  'interrupted',
  'failed',
])

/** run 状态中文文案（键集与后端状态机一致）。 */
const STATUS_LABELS: Record<string, string> = {
  running: '进行中',
  succeeded: '已成功',
  cancelled: '已取消',
  aborted: '已中止',
  interrupted: '已中断',
  failed: '已失败',
}

/** run 状态 Tag 颜色（running 蓝/succeeded 绿/cancelled 橙/aborted 红/
 * interrupted 灰/failed 橙红——与 aborted 同族红系）。 */
const STATUS_COLORS: Record<string, string> = {
  running: 'blue',
  succeeded: 'green',
  cancelled: 'orange',
  aborted: 'red',
  interrupted: 'default',
  failed: 'volcano',
}

/** 表单值：dataset 必选；movieMode 三态；ids 仅 custom 需要；θ/K 是原始
 * 输入串（提交时才解析，空串 = 不携带）；concurrency 初始 4。 */
interface EvalFormValues {
  dataset: string
  movieMode: MovieSelection['mode']
  ids?: string[]
  thetas?: string
  ks?: string
  concurrency?: number
}

/** 逗号分隔数字串 → number[]；空串/全空 token → undefined（不携带，服务端
 * 回退默认网格——起跑可选项缺省即不携带是 eval.ts 的既定口径）。容忍全角
 * 逗号（中文输入法顺手）。 */
function parseNumList(raw?: string): number[] | undefined {
  const nums = (raw ?? '')
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter((s) => s !== '')
    .map(Number)
    .filter((n) => Number.isFinite(n))
  return nums.length > 0 ? nums : undefined
}

/** θ/K 档位校验：非空 token 必须可解析为有限数字——顿号/混字手误显式报错，
 * 不静默丢档（少一档的网格结果会被误当成完整口径）。 */
function numListRule() {
  return {
    validator: (_rule: unknown, value?: string) => {
      const bad = (value ?? '')
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter((s) => s !== '' && !Number.isFinite(Number(s)))
      return bad.length > 0
        ? Promise.reject(new Error(`无法识别的档位：${bad.join('、')}（应为逗号分隔的数字）`))
        : Promise.resolve()
    },
  }
}

/** 数值列的 null 口径：None≠0，一律「暂无」。 */
function metricText(v: number | null): string {
  return v === null ? '暂无' : String(v)
}

const columns: NonNullable<TableProps<RunSummary>['columns']> = [
  // 时间 null 也走「暂无」——本表 null 口径统一（不用 formatTime 的 '—'）
  {
    title: '时间',
    dataIndex: 'started_at',
    render: (v: string | null) => (v === null ? '暂无' : formatTime(v)),
  },
  {
    title: '结束时间',
    dataIndex: 'finished_at',
    render: (v: string | null) => (v === null ? '暂无' : formatTime(v)),
  },
  { title: '数据集', dataIndex: 'dataset_id', render: (v: string | null) => v ?? '暂无' },
  {
    title: '状态',
    dataIndex: 'status',
    render: (v: string) => (
      <Tag color={STATUS_COLORS[v] ?? 'default'}>{STATUS_LABELS[v] ?? v}</Tag>
    ),
  },
  {
    title: '完成',
    key: 'progress',
    render: (_, r) => (r.done === null || r.total === null ? '暂无' : `${r.done}/${r.total}`),
  },
  { title: '失败', dataIndex: 'failed', render: metricText },
  // 列头标约定取格（后端 _read_summary 统一约定：θ0.2/θ0.8 最近有值格）
  { title: 'nDCG@10(θ0.2)', dataIndex: 'ndcg10', render: metricText },
  { title: 'P@10(θ0.2)', dataIndex: 'p10', render: metricText },
  { title: 'R@20(θ0.8)', dataIndex: 'r20', render: metricText },
  { title: '覆盖中位', dataIndex: 'coverage_median', render: metricText },
]

export default function ClosedEval() {
  // —— 数据集与历史（挂载双拉，两路独立容错——一路挂了不拖累另一路） ——
  const [datasets, setDatasets] = useState<DatasetInfo[]>([])
  const [datasetsError, setDatasetsError] = useState<string | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [runsError, setRunsError] = useState<string | null>(null)

  // —— 活跃 run（提交受理 / 挂载从历史发现）与轮询生命周期 ——
  const [activeRun, setActiveRun] = useState<RunStatus | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // 轮询会话号：清定时器/换 run 时 bump，在途响应对不上号即丢弃
  const pollSessionRef = useRef(0)

  // —— custom 选片清单（按数据集拉取） ——
  const [movies, setMovies] = useState<MovieRef[]>([])
  const [moviesLoading, setMoviesLoading] = useState(false)

  // —— 报告取数位（Task 9 的视图组件消费 report.data） ——
  const [report, setReport] = useState<{ runId: string; data: ClosedEvalReport } | null>(null)
  // —— 数据集根（页面级切换：输入值 / 服务端生效值 / 应用中） ——
  const [rootInput, setRootInput] = useState('')
  const [effectiveRoot, setEffectiveRoot] = useState<string | null>(null)
  const [applyingRoot, setApplyingRoot] = useState(false)

  const [createForm] = Form.useForm<EvalFormValues>()
  const movieMode = Form.useWatch('movieMode', createForm)
  const datasetId = Form.useWatch('dataset', createForm)

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
    pollSessionRef.current += 1
  }, [])

  /** 历史 run 刷新（两个触发点：挂载、run 终态——plan 钉死，不在提交后
   * 额外刷：进行中卡片已承担活跃期展示）。返回本次行供挂载方做活跃发现。 */
  const refreshRuns = useCallback(async (): Promise<RunSummary[]> => {
    setRunsLoading(true)
    try {
      const rows = await listRuns()
      setRuns(rows)
      setRunsError(null)
      return rows
    } catch (err) {
      setRunsError(err instanceof Error ? err.message : '历史列表加载失败')
      return []
    } finally {
      setRunsLoading(false)
    }
  }, [])

  /** 轮询一个 run 到终态：立即首拉 + 2s 间隔；终态清定时器、收起卡片并刷新
   * 历史。查询失败（run 消失/网络断）同样收束——宁缺勿崩，不打死循环。 */
  const beginPolling = useCallback((runId: string) => {
    stopPolling()
    const session = ++pollSessionRef.current
    const tick = async () => {
      try {
        const st = await getRun(runId)
        if (pollSessionRef.current !== session) return
        if (TERMINAL_STATUSES.has(st.status)) {
          stopPolling()
          setActiveRun(null)
          void refreshRuns()
        } else {
          setActiveRun(st)
        }
      } catch (err) {
        if (pollSessionRef.current !== session) return
        stopPolling()
        setActiveRun(null)
        message.error(err instanceof Error ? err.message : '评测 run 状态查询失败')
        void refreshRuns()
      }
    }
    void tick()
    pollTimerRef.current = setInterval(() => void tick(), POLL_INTERVAL_MS)
  }, [refreshRuns, stopPolling])

  // 挂载取数据集根：展示服务端生效值；is_default 时（重启后 override 已
  // 消失）以 localStorage 存值预填输入（仅提示，非真相）。失败不阻塞表单。
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const info = await getDatasetRoot()
        if (cancelled) return
        setEffectiveRoot(info.root)
        const stored = localStorage.getItem(ROOT_LS_KEY)
        setRootInput(stored !== null && info.is_default ? stored : info.root)
      } catch {
        if (!cancelled) setEffectiveRoot(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // 应用数据集根：POST 校验通过 → 更新生效展示 + 重拉数据集列表（新根的
  // 数据集）+ 存 localStorage；失败经 message 透出后端 detail，不变更状态。
  const applyRoot = async () => {
    const value = rootInput.trim()
    if (!value || applyingRoot) return
    setApplyingRoot(true)
    try {
      const info = await setDatasetRoot(value)
      message.success('数据集根已应用')
      setEffectiveRoot(info.root)
      localStorage.setItem(ROOT_LS_KEY, value)
      try {
        const rows = await listDatasets()
        setDatasets(rows)
        setDatasetsError(null)
      } catch (err) {
        setDatasetsError(err instanceof Error ? err.message : '数据集列表加载失败')
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '数据集根应用失败')
    } finally {
      setApplyingRoot(false)
    }
  }

  // 挂载双拉（数据集与历史两路独立容错，一路挂了不拖累另一路）；历史里
  // 发现 running 行 → 接管轮询（进程重启后页面重开也能恢复进行中视图）。
  // RunSummary 与 RunStatus 的进度七字段同形，摘出即用。
  useEffect(() => {
    let cancelled = false
    async function loadDatasets() {
      try {
        const rows = await listDatasets()
        if (!cancelled) {
          setDatasets(rows)
          setDatasetsError(null)
        }
      } catch (err) {
        if (!cancelled) {
          setDatasetsError(err instanceof Error ? err.message : '数据集列表加载失败')
        }
      }
    }
    async function loadRunsAndAdopt() {
      const rows = await refreshRuns()
      if (cancelled) return
      const running = rows.find((r) => r.status === 'running')
      if (running !== undefined) {
        setActiveRun({
          run_id: running.run_id,
          status: running.status,
          done: running.done,
          total: running.total,
          failed: running.failed,
          started_at: running.started_at,
          finished_at: running.finished_at,
          error: running.error,
        })
        beginPolling(running.run_id)
      }
    }
    void loadDatasets()
    void loadRunsAndAdopt()
    return () => {
      cancelled = true
      stopPolling()
    }
  }, [beginPolling, refreshRuns, stopPolling])

  // custom 模式（或 custom 下换数据集）拉影片清单；失败仅提示不阻塞表单。
  // 微任务后启动加载（loading 置位在异步边界内，不在 effect 同步置 state
  // ——与 GenerationTasks 资产预载同款处理）；cancelled 丢弃换数据集后的
  // 过期响应，防旧清单覆盖新清单。
  useEffect(() => {
    if (movieMode !== 'custom' || datasetId === undefined) return
    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      void (async () => {
        setMoviesLoading(true)
        try {
          const rows = await listMovies(datasetId)
          if (!cancelled) setMovies(rows)
        } catch (err) {
          if (!cancelled) message.error(err instanceof Error ? err.message : '影片清单加载失败')
        } finally {
          if (!cancelled) setMoviesLoading(false)
        }
      })()
    })
    return () => {
      cancelled = true
    }
  }, [datasetId, movieMode])

  async function handleStart(values: EvalFormValues) {
    // 双保险（按钮已禁用）：单活跃 run 的最终裁决在后端，这里不重复校验
    if (activeRun !== null) return
    const selection: MovieSelection =
      values.movieMode === 'custom'
        ? { mode: 'custom', ids: values.ids ?? [] }
        : { mode: values.movieMode }
    const req: StartRunRequest = { dataset: values.dataset, movies: selection }
    // θ/K/concurrency 仅在用户填写时携带（空 → 服务端默认网格/并发）
    const thetas = parseNumList(values.thetas)
    if (thetas !== undefined) req.thetas = thetas
    const ks = parseNumList(values.ks)
    if (ks !== undefined) req.ks = ks
    if (values.concurrency !== undefined && values.concurrency !== null) {
      req.concurrency = values.concurrency
    }
    setSubmitting(true)
    try {
      const { run_id } = await startRun(req)
      message.success(`已受理评测 run ${run_id}`)
      // 乐观占位（进度未知）：立即首拉马上补上真实进度
      setActiveRun({
        run_id,
        status: 'running',
        done: null,
        total: null,
        failed: null,
        started_at: null,
        finished_at: null,
        error: null,
      })
      beginPolling(run_id)
    } catch (err) {
      // 409（双发竞态）/400/404 透出后端 detail
      message.error(err instanceof Error ? err.message : '起跑失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleCancel() {
    if (activeRun === null) return
    try {
      const res = await cancelRun(activeRun.run_id)
      if (res.ok) {
        // 受理语义：worker 不再提交新片、在途片跑完按部分口径收尾——
        // 不清轮询，status 推进到 cancelled 终态由轮询发现
        message.success('已请求取消（在途影片完成后按部分口径收尾）')
      } else {
        // 后端幂等意图：run 不存在或已终态
        message.info('该 run 已结束，无需取消')
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '取消请求失败')
    }
  }

  /** 删除终态 run：磁盘产物 + 注册表一并清除；报告视图正展示该 run 时
   * 收起（悬空展示已删 run 会误导）；409（运行中/收尾窗口）/404（已删）
   * detail 经 message 透出，失败不刷新历史。报告收起走函数式 set——
   * 表格单元格 memo 下回调闭包可能持有陈旧 report，函数式读最新值。 */
  async function handleDelete(runId: string) {
    try {
      await deleteRun(runId)
      message.success(`已删除 run ${runId}`)
      setReport((prev) => (prev?.runId === runId ? null : prev))
      void refreshRuns()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '删除失败')
    }
  }

  async function openReport(runId: string) {
    try {
      const data = await getReport(runId)
      setReport({ runId, data })
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // 终态落盘非原子的毫秒级窗口（后端 Task 6 披露）：提示重试，不崩页面
        message.warning('报告生成中，稍后重试')
      } else {
        message.error(err instanceof Error ? err.message : '报告加载失败')
      }
    }
  }

  const datasetOptions = datasets.map((d) => ({
    value: d.id,
    label: `${d.id}（交集 ${d.library_overlap}）`,
  }))

  /** 操作列（组件内构造：需要 handleDelete 闭包）：仅终态行可删，运行行
   * 禁用（后端 409 是最终裁决，前端先挡更省一轮）。外层 span 拦截点击
   * 冒泡——antd v6 的 Trigger 会替换子元素 onClick（按钮自身的
   * stopPropagation 不再生效），不拦的话删除点击会连带触发行点击开报告。 */
  const actionColumns: TableProps<RunSummary>['columns'] = [
    {
      title: '操作',
      key: 'actions',
      render: (_, r) => {
        const deletable = TERMINAL_STATUSES.has(r.status)
        return (
          <span onClick={(e) => e.stopPropagation()}>
            <Popconfirm
              title="删除该 run 及其产物？"
              description="磁盘目录与历史记录一并移除，不可恢复"
              okText="确定"
              cancelText="取消"
              disabled={!deletable}
              onConfirm={() => void handleDelete(r.run_id)}
            >
              <Button danger size="small" disabled={!deletable}>
                删除
              </Button>
            </Popconfirm>
          </span>
        )
      },
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="新建评测">
        {datasetsError !== null && (
          <Alert
            type="error"
            showIcon
            message="数据集列表加载失败"
            description={datasetsError}
            style={{ marginBottom: 16 }}
          />
        )}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
          <Input
            aria-label="数据集根路径"
            value={rootInput}
            onChange={(e) => setRootInput(e.target.value)}
            placeholder="服务器上的数据集根目录，如 /cache/MovieTags"
            style={{ flex: 1 }}
          />
          <Button
            onClick={() => void applyRoot()}
            loading={applyingRoot}
            disabled={rootInput.trim() === ''}
          >
            应用
          </Button>
          <Typography.Text type="secondary">
            当前生效：{effectiveRoot ?? '未知'}
          </Typography.Text>
        </div>
        <Form
          form={createForm}
          layout="vertical"
          onFinish={(values) => void handleStart(values)}
        >
          <Form.Item
            name="dataset"
            label="数据集"
            rules={[{ required: true, message: '请选择评测数据集' }]}
          >
            <Select
              aria-label="数据集"
              options={datasetOptions}
              placeholder="选择评测数据集"
              // 换数据集后旧选中影片不再属于新清单：清掉 custom 选择
              // （越权/为空由服务端校验，但 UI 先挡住更省一轮 400）
              onChange={() => createForm.setFieldValue('ids', undefined)}
            />
          </Form.Item>
          <Form.Item name="movieMode" label="影片集" initialValue="preset30">
            <Radio.Group
              options={[
                { value: 'preset30', label: '试点 30' },
                { value: 'all', label: '全部' },
                { value: 'custom', label: '自定义' },
              ]}
            />
          </Form.Item>
          {movieMode === 'custom' && (
            <Form.Item
              name="ids"
              label="影片清单"
              preserve={false}
              rules={[{ required: true, message: '自定义影片集需至少选择一部影片' }]}
            >
              <Select
                aria-label="影片清单"
                mode="multiple"
                showSearch
                // 搜索同时匹配 tt 编号与标题（小写化）——只按标题时搜
                // tt0068646 无结果；空输入恒放行（antd 语义，恒真兜底）
                filterOption={(input, option) => {
                  const q = input.trim().toLowerCase()
                  if (q === '') return true
                  const value = String(option?.value ?? '').toLowerCase()
                  const label = String(option?.label ?? '').toLowerCase()
                  return value.includes(q) || label.includes(q)
                }}
                loading={moviesLoading}
                options={movies.map((m) => ({ value: m.tt, label: m.title }))}
                placeholder="搜索并选择影片（按标题或 tt 编号）"
              />
            </Form.Item>
          )}
          <Form.Item name="thetas" label="θ 档位" rules={[numListRule()]}>
            <Input placeholder="默认档 0.2,0.4,0.6,0.8" allowClear />
          </Form.Item>
          <Form.Item name="ks" label="K 档位" rules={[numListRule()]}>
            <Input placeholder="默认档 5,10,20,40" allowClear />
          </Form.Item>
          <Form.Item name="concurrency" label="并发度" initialValue={4}>
            <InputNumber aria-label="并发度" min={1} style={{ width: 160 }} />
          </Form.Item>
          <Space wrap>
            <Button
              type="primary"
              htmlType="submit"
              loading={submitting}
              disabled={activeRun !== null}
            >
              起跑
            </Button>
            {activeRun !== null && (
              <Typography.Text type="secondary">
                {`已有进行中的评测 run（${activeRun.run_id}），结束后才能再次起跑`}
              </Typography.Text>
            )}
          </Space>
        </Form>
      </Card>

      {activeRun !== null && (
        <Card
          title={`进行中：${activeRun.run_id}`}
          extra={
            <Button danger size="small" onClick={() => void handleCancel()}>
              取消
            </Button>
          }
        >
          <Progress
            status="active"
            percent={
              activeRun.total !== null && activeRun.total > 0
                ? Math.round(((activeRun.done ?? 0) / activeRun.total) * 100)
                : 0
            }
          />
          <Typography.Text>
            {`已完成 ${activeRun.done ?? '—'} / ${activeRun.total ?? '—'}，失败 ${activeRun.failed ?? '—'}`}
          </Typography.Text>
        </Card>
      )}

      <Card title="评测历史">
        {runsError !== null && (
          <Alert
            type="error"
            showIcon
            message="历史列表加载失败"
            description={runsError}
            style={{ marginBottom: 16 }}
          />
        )}
        <Table<RunSummary>
          rowKey="run_id"
          columns={[...columns, ...actionColumns]}
          dataSource={runs}
          loading={runsLoading}
          pagination={{ showTotal: (t) => `共 ${t} 条` }}
          onRow={(row) => ({
            onClick: () => void openReport(row.run_id),
            style: { cursor: 'pointer' },
          })}
        />
      </Card>

      {report !== null && (
        <Card title={`评测报告：${report.runId}`}>
          <ClosedEvalReportView report={report.data} runId={report.runId} />
        </Card>
      )}
    </div>
  )
}
