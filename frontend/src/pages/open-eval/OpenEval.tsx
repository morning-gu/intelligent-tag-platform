// 开放评测页（open-eval Task 7，admin）：以评审官干跑开放管线（命中+新词
// 全过二值评审）并出集合准确率/通过率/标定报告的页签。四块视图镜像
// ClosedEval.tsx：① 创建表单——数据集下拉（选项带交集数提示）、影片集三态
// （试点 30 / 全部 / 自定义多选，清单来自 datasets/{id}/movies）、θ 网格
// （预填默认档 0.2,0.4,0.6,0.8 可编辑，清空不携带 → 服务端回退默认网格）、
// 模态勾选（script+dialogue 默认勾选，必含 script 由前后端双重校验；video
// 勾选禁用——v1 文本模态，关键帧接入为后续扩展）、并发度（默认 4）；
// ② 进行中卡片——进度条 done/total + 取消（取消是受理语义，status 转
// cancelled 终态由轮询推进发现，受理后不清轮询）；③ 历史表——五指标列
// （准确率@θ0.4/新词通过率/命中通过率/标定一致率@θ0.4/新词占比）多轮趋势
// 一眼可见，null 一律「暂无」（None≠0 纪律）；④ 报告卡片——行点击取
// report.json，OpenEvalReport 组件渲染报告视图（open-eval Task 8，镜像
// ClosedEval.tsx 的 ClosedEvalReportView 接线，下载走 openFileUrl 五件套）。
//
// 单活跃 run 是后端硬约束且**跨服务互斥**（open 与 closed 任意一边活跃，
// POST 二发 → 409）：前端在三个入口同步挡——挂载从 open 历史发现 running
// 行、提交受理新 run、轮询推进到终态释放；closed 侧活跃由**同一轮询周期
// 里闭环 listRuns() 的 running 行**发现（open 或 closed 任一活跃即起跑
// 禁用，禁用提示注明是哪个 run 占用；closed 清单失败只降级不拖垮 open
// 展示，409 兜底透出后端 detail）。轮询：受理/接管后立即首拉 + setInterval
// 2s，每周期 listOpenRuns()（历史行自带进度七字段）+ closed listRuns()
// 各查一次；轮询行推进到终态（或查询失败）清定时器并收起卡片——终态集合
// 含 failed（worker 收敛异常兜底终态，闭环第 11 轮教训）。卸载清定时器；
// 清定时器时 bump 会话号——在途响应回来对不上号即丢弃，防卸载后/换 run 后
// 把旧响应写进新状态。行点击取报告：GET report 在终态落盘（meta→raw→
// report）非原子的毫秒级窗口可能 404（后端 Task 6 披露）——优雅提示重试，
// 不崩页面。
import {
  Alert,
  Button,
  Card,
  Checkbox,
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
  Tooltip,
  Typography,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/client'
import { listRuns } from '../../api/eval'
import type { DatasetInfo, MovieRef, MovieSelection, RunStatus } from '../../api/eval'
import { formatTime } from '../../api/time'
import OpenEvalReportView from '../../components/OpenEvalReport'
import {
  cancelOpenRun,
  deleteOpenRun,
  getOpenDatasetRoot,
  getOpenReport,
  listOpenDatasets,
  listOpenMovies,
  listOpenRuns,
  setOpenDatasetRoot,
  startOpenRun,
  type OpenEvalReport,
  type OpenRunSummary,
  type StartOpenRunRequest,
} from '../../api/evalOpen'

/** 轮询间隔：单片 1–3 分钟量级的进度节奏，2s 足够跟手（镜像闭环页）。 */
const POLL_INTERVAL_MS = 2000

/** localStorage 记上次输入的数据集根：仅作输入预填（服务端重启后 override
 * 消失、is_default=true 时提示上次选择）；生效值真相以 GET 返回为准。 */
const ROOT_LS_KEY = 'open-eval-dataset-root'

/** run 终态集合（后端状态机与闭环同构：running → succeeded | cancelled |
 * aborted | interrupted | failed——interrupted 是磁盘推断的进程死亡态，
 * failed 是 worker 收敛异常/起跑失败的兜底终态，open_service.py 兜底段）。
 * failed 必含（闭环第 11 轮教训）。 */
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

/** 开放评测头条取格 θ（镜像后端 open_ranking.DEFAULT_THETA_HEADLINE；TS
 * 无法 import，值变更须两侧同步——闭环第 13 轮同款纪律）。历史表两个取格
 * 列头（准确率/标定一致率）与单片准确率口径都锚定本值。 */
export const OPEN_HEADLINE_THETA = 0.4

/** 起跑允许的模态词表（v1 文本模态，镜像 open_service._ALLOWED_MODALITIES）：
 * video 显式禁选（关键帧接入为后续扩展），UI 只渲染禁用勾选框作说明。 */
const VIDEO_TOOLTIP = '关键帧接入为后续扩展（v1 文本模态）'

/** 表单值：dataset 必选；movieMode 三态；ids 仅 custom 需要；θ 网格是原始
 * 输入串（预填默认档，提交时才解析，清空 = 不携带）；modalities 勾选集
 * （script+dialogue 默认）；concurrency 初始 4。 */
interface OpenEvalFormValues {
  dataset: string
  movieMode: MovieSelection['mode']
  ids?: string[]
  thetas?: string
  modalities?: string[]
  concurrency?: number
}

/** 逗号分隔数字串 → number[]；空串/全空 token → undefined（不携带，服务端
 * 回退默认网格——起跑可选项缺省即不携带是 eval.ts 的既定口径）。容忍全角
 * 逗号（中文输入法顺手）。与闭环页同款实现，本地持有勿跨页 import。 */
function parseNumList(raw?: string): number[] | undefined {
  const nums = (raw ?? '')
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter((s) => s !== '')
    .map(Number)
    .filter((n) => Number.isFinite(n))
  return nums.length > 0 ? nums : undefined
}

/** θ 档位校验：非空 token 必须可解析为有限数字——顿号/混字手误显式报错，
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

/** 模态勾选校验：必含 script（台词在 script 通道渲染，缺 script 评审材料
 * 为空——后端 open_service._validate_modalities 同款拒绝文案，前端先挡省
 * 一轮 400；dialogue-only/全空都在此拦下）。 */
function modalityRule() {
  return {
    validator: (_rule: unknown, value?: string[]) =>
      (value ?? []).includes('script')
        ? Promise.resolve()
        : Promise.reject(
            new Error('模态必须包含 script：台词在 script 通道渲染，缺 script 评审材料为空'),
          ),
  }
}

/** 模态勾选组（Form.Item 注入 value/onChange 的受控自定义控件）：左侧
 * script/dialogue 双勾选框进表单值，右侧 video 固定禁用 + tooltip——
 * v1 文本模态的 UI 说明位，勾选值恒不含 video。 */
function ModalityGroup(props: { value?: string[]; onChange?: (value: string[]) => void }) {
  return (
    <Space wrap>
      <Checkbox.Group
        options={[
          { label: 'script', value: 'script' },
          { label: 'dialogue', value: 'dialogue' },
        ]}
        value={props.value}
        onChange={props.onChange}
      />
      <Tooltip title={VIDEO_TOOLTIP}>
        {/* disabled 控件不吞 hover 的 antd 惯例：外层 span 承接 tooltip 触发 */}
        <span>
          <Checkbox disabled>video</Checkbox>
        </span>
      </Tooltip>
    </Space>
  )
}

/** 数值列的 null 口径：None≠0，一律「暂无」。 */
function metricText(v: number | null): string {
  return v === null ? '暂无' : String(v)
}

const columns: NonNullable<TableProps<OpenRunSummary>['columns']> = [
  // 时间 null 也走「暂无」——本表 null 口径统一（不用 formatTime 的 '—'）
  {
    title: '时间',
    dataIndex: 'started_at',
    render: (v: string | null) => (v === null ? '暂无' : formatTime(v)),
  },
  { title: '数据集', dataIndex: 'dataset_id', render: (v: string | null) => v ?? '暂无' },
  {
    title: '影片数',
    key: 'progress',
    render: (_, r) => (r.done === null || r.total === null ? '暂无' : `${r.done}/${r.total}`),
  },
  {
    title: '状态',
    dataIndex: 'status',
    render: (v: string) => (
      <Tag color={STATUS_COLORS[v] ?? 'default'}>{STATUS_LABELS[v] ?? v}</Tag>
    ),
  },
  // 两个取格列头锚定 OPEN_HEADLINE_THETA（后端 _read_open_summary 统一取格：
  // accuracy40/agreement40 = 最接近 θ0.4 的最近有值格，custom-θ run 回退）
  { title: `准确率(θ${OPEN_HEADLINE_THETA})`, dataIndex: 'accuracy40', render: metricText },
  { title: '新词通过率', dataIndex: 'new_pass', render: metricText },
  { title: '命中通过率', dataIndex: 'hit_pass', render: metricText },
  {
    title: `标定一致率(θ${OPEN_HEADLINE_THETA})`,
    dataIndex: 'agreement40',
    render: metricText,
  },
  { title: '新词占比', dataIndex: 'new_ratio', render: metricText },
]

export default function OpenEval() {
  // —— 数据集与历史（挂载双拉，两路独立容错——一路挂了不拖累另一路） ——
  const [datasets, setDatasets] = useState<DatasetInfo[]>([])
  const [datasetsError, setDatasetsError] = useState<string | null>(null)
  const [runs, setRuns] = useState<OpenRunSummary[]>([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [runsError, setRunsError] = useState<string | null>(null)

  // —— 活跃 run（提交受理 / 挂载从历史发现）与轮询生命周期 ——
  const [activeRun, setActiveRun] = useState<RunStatus | null>(null)
  // —— closed 侧活跃 run（跨服务互斥探测：closed 活跃同样禁起跑） ——
  const [closedBusyRunId, setClosedBusyRunId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // 轮询会话号：清定时器/换 run 时 bump，在途响应对不上号即丢弃
  const pollSessionRef = useRef(0)

  // —— custom 选片清单（按数据集拉取） ——
  const [movies, setMovies] = useState<MovieRef[]>([])
  const [moviesLoading, setMoviesLoading] = useState(false)

  // —— 报告取数位（Task 8 的 OpenEvalReport 组件消费 report.data） ——
  const [report, setReport] = useState<{ runId: string; data: OpenEvalReport } | null>(null)
  // —— 数据集根（页面级切换：输入值 / 服务端生效值 / 应用中） ——
  const [rootInput, setRootInput] = useState('')
  const [effectiveRoot, setEffectiveRoot] = useState<string | null>(null)
  const [applyingRoot, setApplyingRoot] = useState(false)

  const [createForm] = Form.useForm<OpenEvalFormValues>()
  const movieMode = Form.useWatch('movieMode', createForm)
  const datasetId = Form.useWatch('dataset', createForm)

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
    pollSessionRef.current += 1
  }, [])

  /** 历史 run 刷新 = 轮询周期：listOpenRuns()（历史行自带进度七字段）+
   * closed listRuns()（跨服务互斥探测）**同一周期各查一次**。两路独立
   * 容错——closed 清单失败只降级为「不据此禁用」（后端 409 兜底），不
   * 拖垮 open 历史展示。silent：轮询 tick 里每 2s 一次，不闪 loading
   * （挂载/终态/删除后的刷新才置 loading）。返回本次行供挂载方做活跃发现。 */
  const refreshRuns = useCallback(
    async (opts: { silent?: boolean } = {}): Promise<OpenRunSummary[]> => {
      if (!opts.silent) setRunsLoading(true)
      try {
        const openRows = await listOpenRuns()
        setRuns(openRows)
        setRunsError(null)
        try {
          const closedRows = await listRuns()
          setClosedBusyRunId(
            closedRows.find((r) => r.status === 'running')?.run_id ?? null,
          )
        } catch {
          setClosedBusyRunId(null)
        }
        return openRows
      } catch (err) {
        setRunsError(err instanceof Error ? err.message : '历史列表加载失败')
        return []
      } finally {
        if (!opts.silent) setRunsLoading(false)
      }
    },
    [],
  )

  /** 轮询一个 run 到终态：立即首拉 + 2s 间隔；每周期拉 open 全量清单找
   * 本 run 行——行推进到终态（TERMINAL_STATUSES 含 failed）或行消失（被
   * 删除/磁盘异常）即停表收卡片（历史已在本周期刷新，无需再拉）；查询
   * 失败（run 消失/网络断）同样收束——宁缺勿崩，不打死循环。 */
  const beginPolling = useCallback(
    (runId: string) => {
      stopPolling()
      const session = ++pollSessionRef.current
      const tick = async () => {
        try {
          const rows = await refreshRuns({ silent: true })
          if (pollSessionRef.current !== session) return
          const current = rows.find((r) => r.run_id === runId)
          if (current === undefined || TERMINAL_STATUSES.has(current.status)) {
            stopPolling()
            setActiveRun(null)
          } else {
            // OpenRunSummary 与 RunStatus 的进度七字段同形，摘出即用
            setActiveRun({
              run_id: current.run_id,
              status: current.status,
              done: current.done,
              total: current.total,
              failed: current.failed,
              started_at: current.started_at,
              finished_at: current.finished_at,
              error: current.error,
            })
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
    },
    [refreshRuns, stopPolling],
  )

  // 挂载取数据集根：展示服务端生效值；is_default 时（重启后 override 已
  // 消失）以 localStorage 存值预填输入（仅提示，非真相）。失败不阻塞表单。
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const info = await getOpenDatasetRoot()
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
  // 写的是闭环服务的同一 override，闭环页同刻可见。
  const applyRoot = async () => {
    const value = rootInput.trim()
    if (!value || applyingRoot) return
    setApplyingRoot(true)
    try {
      const info = await setOpenDatasetRoot(value)
      message.success('数据集根已应用')
      setEffectiveRoot(info.root)
      localStorage.setItem(ROOT_LS_KEY, value)
      try {
        const rows = await listOpenDatasets()
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
  useEffect(() => {
    let cancelled = false
    async function loadDatasets() {
      try {
        const rows = await listOpenDatasets()
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
  // ——与 ClosedEval 同款处理）；cancelled 丢弃换数据集后的过期响应，
  // 防旧清单覆盖新清单。
  useEffect(() => {
    if (movieMode !== 'custom' || datasetId === undefined) return
    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      void (async () => {
        setMoviesLoading(true)
        try {
          const rows = await listOpenMovies(datasetId)
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

  async function handleStart(values: OpenEvalFormValues) {
    // 双保险（按钮已禁用）：单活跃 run + 跨服务互斥的最终裁决在后端，
    // 这里不重复校验
    if (activeRun !== null || closedBusyRunId !== null) return
    const selection: MovieSelection =
      values.movieMode === 'custom'
        ? { mode: 'custom', ids: values.ids ?? [] }
        : { mode: values.movieMode }
    const req: StartOpenRunRequest = {
      dataset: values.dataset,
      movies: selection,
      // 模态恒携带（勾选集即请求体）：script+dialogue 默认勾选，必含
      // script 由表单校验保证；video 恒不在（v1 文本模态）
      modalities: values.modalities ?? ['script', 'dialogue'],
    }
    // θ 网格仅在解析出值时携带（清空 → 服务端默认网格 0.2,0.4,0.6,0.8）
    const thetas = parseNumList(values.thetas)
    if (thetas !== undefined) req.thetas = thetas
    if (values.concurrency !== undefined && values.concurrency !== null) {
      req.concurrency = values.concurrency
    }
    setSubmitting(true)
    try {
      const { run_id } = await startOpenRun(req)
      message.success(`已受理开放评测 run ${run_id}`)
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
      // 409（双发竞态——open 自身或 closed 占用，detail 带占用 run_id）
      // /400/404 透出后端 detail
      message.error(err instanceof Error ? err.message : '起跑失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleCancel() {
    if (activeRun === null) return
    try {
      const res = await cancelOpenRun(activeRun.run_id)
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
      await deleteOpenRun(runId)
      message.success(`已删除 run ${runId}`)
      setReport((prev) => (prev?.runId === runId ? null : prev))
      void refreshRuns()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '删除失败')
    }
  }

  async function openReport(runId: string) {
    try {
      const data = await getOpenReport(runId)
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

  /** 起跑禁用口径：open 或 closed 任一活跃（跨服务互斥的前端半边）。
   * 提示注明是哪个 run 占用——open 侧优先（本页语义），closed 侧注明
   * 「闭环评测 run」指向占用方。 */
  const busyHint =
    activeRun !== null
      ? `已有进行中的开放评测 run（${activeRun.run_id}），结束后才能再次起跑`
      : closedBusyRunId !== null
        ? `闭环评测 run（${closedBusyRunId}）进行中，与开放评测互斥，结束后才能再次起跑`
        : null

  /** 操作列（组件内构造：需要 handleDelete 闭包）：仅终态行可删，运行行
   * 禁用（后端 409 是最终裁决，前端先挡更省一轮）。外层 span 拦截点击
   * 冒泡——antd v6 的 Trigger 会替换子元素 onClick（按钮自身的
   * stopPropagation 不再生效），不拦的话删除点击会连带触发行点击
   * 查看报告。 */
  const actionColumns: TableProps<OpenRunSummary>['columns'] = [
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
          <Form.Item name="thetas" label="θ 网格" rules={[numListRule()]} initialValue="0.2,0.4,0.6,0.8">
            {/* 预填默认档（服务端 DEFAULT_THETAS 同值）供直接编辑；清空
                不携带 → 服务端回退默认网格 */}
            <Input aria-label="θ 网格" placeholder="默认档 0.2,0.4,0.6,0.8" allowClear />
          </Form.Item>
          <Form.Item
            name="modalities"
            label="生成模态"
            initialValue={['script', 'dialogue']}
            rules={[modalityRule()]}
          >
            <ModalityGroup />
          </Form.Item>
          <Form.Item name="concurrency" label="并发度" initialValue={4}>
            <InputNumber aria-label="并发度" min={1} style={{ width: 160 }} />
          </Form.Item>
          <Space wrap>
            <Button
              type="primary"
              htmlType="submit"
              loading={submitting}
              disabled={activeRun !== null || closedBusyRunId !== null}
            >
              起跑
            </Button>
            {busyHint !== null && (
              <Typography.Text type="secondary">{busyHint}</Typography.Text>
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
        <Table<OpenRunSummary>
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
          {/* Task 8: 报告视图（components/OpenEvalReport.tsx）接管占位 */}
          <OpenEvalReportView report={report.data} runId={report.runId} />
        </Card>
      )}
    </div>
  )
}
