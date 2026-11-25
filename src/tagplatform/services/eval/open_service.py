"""开放评测干跑运行器（OpenEvalService）：守护线程 + run 生命周期（Task 4）。

与闭环运行器（service.py ClosedEvalService）同构：起跑校验（spec 钉死
顺序）→ 起跑时 `load_assembly_context("open")` 装配一次（快照语义，库/
策略/生产器起跑时定格，随 _OpenJob 快照携带）→ daemon worker 持线程池
逐片生成 → 终态落盘。每片走与正式生成零漂移的路径：`build_spec`（同一
装配函数对的纯内存第二级）→ `OpenTagProducer.produce`（producer 本就
不落库），干跑全程对业务表零写入；评测产物只写
`settings.open_eval_output_root/<run_id>/`：起跑即写 meta.json 骨架
（status=running——进程中断可被历史扫描标 interrupted）、每片完成锁内
追加 progress.jsonl、终态由 worker 主线程写 meta + raw + 报告三件套
（assemble_open_report → write_open_report，report.json / report.md /
metrics.csv）。**audit/<tt>.json 由 mtg 管线自己落盘**：run 级 settings
副本把 mtg_report_dir 重定向进 run 目录、task_id=影片 id，审计随 run
归档（报告组装的 judge 段按 run_dir/audit 读）。

run 语义契约（本模块的三个核心语义，改动前先读）
================================================

**评审失败契约**：produce 成功后对该片全部产出标签（命中 + 新词）做
一次三维独立评审（judge 三维分仅在报告中展示）；评审失败
（ReviewRequestError 或任何异常）时**产出不作废**——hits/new_words 行
原样保留（review=None 占位）、review_error 记录进结果与 raw_results，
该片仍计 ok（failed 只统计 produce 失败）。通过率/三维指标按「该片
评审失败」整体排除出分母（None≠0：不是评审 0 分）——排除逻辑在报告
组装，本层只负责如实落盘。

**双熔断（完成序，互相独立）**：
- produce 熔断：完成序连续 ABORT_CONSECUTIVE(5) 片 produce 失败 →
  aborted，余量片不再提交（系统性故障保险丝，与闭环同款语义）；
- 评审熔断：完成序连续 ABORT_REVIEW_CONSECUTIVE(5) 片「produce 成功但
  评审失败」（ok=True 且 review_error）→ aborted，meta.error 注明
  「评审端点熔断」。两个计数互为反例：produce 失败片重置评审连败、
  评审失败/干净片重置 produce 连败——系统性故障只会以同一种形态到达。

**跨服务互斥（busy_probe）**：closed 与 open 两个运行器单进程各自只
允许一个活跃 run，且互相排斥（同一时刻全进程只跑一个评测）。装配在
app 装配层（Task 6）互挂 `busy_probe = 对端.active_run_id_lockless`。
probe 必须是**无锁读**：探测发生在本服务 start 持有自身锁的窗口内，
若再取对端锁，两服务互挂探针时即经典锁序倒置死锁（A 持 A 锁探 B、
B 持 B 锁探 A）。读单个 Python 属性是原子的，无锁读安全；代价是良性
竞态窗口——对端可能恰在本服务探测通过与其自身注册之间放行（双方都
探测到对方空闲），窗口为各自锁内注册前几条语句的微秒级，且互斥的
目的是友好拒绝而非硬事务边界，接受（同 ClosedEvalService.busy_probe
docstring 的说明）。

**无 gt / 无 θ**（2026-11-23 三维化）：开放生成没有排序真值任务，
闭环遗留的 gt 加载、θ 网格、集合准确率与标定全部移除；头条指标是
评审通过率（按模型给出的保留结论统计）与新词占比。

历史扫描：`history()` 扫
`open_eval_output_root/*/meta.json` 出历史行：meta.status=running 且
不在进程内注册表 → interrupted（进程死亡在磁盘上可辨认）；五摘要键
（pass_rate / new_pass / hit_pass / new_ratio / three_high）由
open_report._read_open_summary 读该 run 的 report.json（缺失全 None，
宁缺勿假）。

分层红线：本模块不 import fastapi（设计文档 §9.4），不 import/call
persist_tags（评测只读）；评测产物只写 `settings.open_eval_output_root`。
熔断/重试常量本地重声明（不跨模块 import 闭环运行器的名字——两个运行
器互不依赖，仅语义对齐）。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from tagplatform.schemas import ContentJSON
from tagplatform.capabilities.prompts.open_review import REVIEW_PROMPT_VERSION
from tagplatform.services.eval.dataset import (
    load_movie_content,
    scan_datasets,
    select_movies,
)
from tagplatform.services.eval.open_report import (
    OPEN_MOUNT_FIELDS,
    _read_open_summary,
    assemble_open_report,
    write_open_report,
)
from tagplatform.services.eval.open_review import (
    make_review_client,
    review_calibre,
    review_movie_tags,
)
from tagplatform.services.eval.report import _dump_json
from tagplatform.services.generation.assemble import (
    AssemblyContext,
    build_spec,
    load_assembly_context,
)
from tagplatform.settings import Settings

__all__ = [
    "ABORT_CONSECUTIVE",
    "ABORT_REVIEW_CONSECUTIVE",
    "DEFAULT_CONCURRENCY",
    "RETRY_PER_MOVIE",
    "EvalValidationError",
    "OpenEvalService",
    "OpenMovieResult",
    "REVIEW_PROMPT_VERSION",
    "RunActiveError",
    "RunBusyError",
    "RunState",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 干跑运行器（服务层）

# 单进程默认并发：与闭环运行器同值——对齐主模型网关的批处理并发（4），
# 全量评测不压垮网关（语义对齐，本地重声明，勿跨模块 import）
DEFAULT_CONCURRENCY = 4
# 每片重试次数（含首发共 1+2=3 次尝试）：单片偶发失败（超时/解析抖动）
# 不断跑——沿用闭环语义，本地重声明
RETRY_PER_MOVIE = 2
# 完成序连续失败达该数 → 中止余量：produce 系统性故障保险丝——沿用闭环
# 语义，本地重声明（勿跨模块 import 私有）
ABORT_CONSECUTIVE = 5
# 评审独立熔断：完成序连续 N 片「produce 成功但评审失败」→ aborted
# （评审端点整体挂掉时别把整批烧完；produce 产出不作废，已完成的照记）
ABORT_REVIEW_CONSECUTIVE = 5

# 起跑允许的模态（v1 文本模态）：台词在 script 通道渲染，缺 script 评审
# 材料为空（open_review 的空材料自防会抛错），故 script 必含
_ALLOWED_MODALITIES = ("script", "dialogue")


class RunBusyError(RuntimeError):
    """已有评测 run 进行中（本服务或对端服务——重复起跑显式拒绝）。"""


class RunActiveError(RuntimeError):
    """run 仍活跃（worker 在跑或在终态落盘收尾），不可删除——先取消/等收尾。"""


class EvalValidationError(ValueError):
    """评测起跑校验失败：数据集不存在 / 选片越权或为空 / modalities
    非法 / 库未就绪 / 并发非法。"""


@dataclass
class OpenMovieResult:
    """单片干跑结果（评测的原子产出，Task 5 报告组装的输入）。

    - `hits` 行 `{"tag_id","tag_name","judge_reference","review"}`：
      tag_id 非空的产出（库内命中）；review 为
      `{"pass","relevance","semantics","value","reason","quote"}`（三维
      各 {"score","reason"}，pass 由评审模型直接给出）或 None（该片评审失败）；
    - `new_words` 行 `{"name","desc","level","judge_reference","review"}`：
      tag_id 为空的产出（模型新词）；desc 取 candidate_description（生成侧
      通用释义）；judge_reference 为生产 judge 三维分（审计缺失时 None）；
    - `review_error`：该片评审失败原因（produce 失败片为 None——评审
      根本未发起）；评审成功（含零产出）为 None；
    - `ok=False` 时 `error` 记最后一次 produce 异常（含异常类型名），
      hits/new_words 清空（半截产出不可信）；
    - `elapsed_ms` 覆盖该片全部尝试（含重试）。
    """

    tt: str
    title: str
    ok: bool
    error: str | None
    hits: list[dict]
    new_words: list[dict]
    review_error: str | None
    elapsed_ms: int


@dataclass
class RunState:
    """一次 run 的进度快照（`get` 返回副本，读方无锁安全）。

    形状与闭环 RunState 一致（Task 6 复用同一响应模型）；finished_at：
    终态时刻（_worker 落终态时盖章，运行中 None）。
    """

    run_id: str
    status: str
    done: int
    total: int
    failed: int
    started_at: str
    error: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class _OpenJob:
    """一次 run 的不可变上下文（start 时快照，worker 线程消费）。

    - `run_settings`：run 级 settings 副本——mtg_modalities 收敛为本次
      起跑模态、mtg_report_dir 重定向进 run 目录的 audit/，随片传给
      produce（审计落位 + 文本模态由副本钉死，run 中途改全局设置不影响
      在跑 run）；
    - 库/模板版本在 start 时定格——run 中途换库不影响本 run 的口径记录；
    - `context` 是起跑时 `load_assembly_context("open")` 的装配快照：
      干跑循环经它走 build_spec（纯内存），run 中途改库行不影响本 run
      产出。
    """

    run_id: str
    movies: list[str]
    concurrency: int
    started_at: str
    meta: dict
    # 数据集根起跑快照：worker 懒加载 content.json 一律用 job 根——
    # 运行中页面切 override 不影响在跑 run（spec 隔离红线）
    dataset_root: str
    context: AssemblyContext
    run_settings: Settings
    # 实时报告状态：tt → OpenMovieResult（整片评审完成后回填，_publish_live 增量
    # 落 live.json）。受 service._lock 保护；仅运行期使用，终态以 report.json 为准。
    live: dict = field(default_factory=dict)
    live_lock: threading.Lock = field(default_factory=threading.Lock)


def _judge_references(run_settings: Settings, tt: str) -> dict[tuple, dict]:
    """按最终标签和挂载点取参考分，旧审计按最终标签名回退。"""
    try:
        audit = json.loads((Path(run_settings.mtg_report_dir) / f"{tt}.json")
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(audit, dict):
        return {}
    verdicts = {row.get("candidate_id") or row["tag"]: row
                for row in audit.get("verdicts") or []
                if isinstance(row, dict) and row.get("tag")}
    references: dict[tuple, dict] = {}
    for row in audit.get("accepted") or []:
        if not isinstance(row, dict) or not row.get("tag"):
            continue
        verdict = verdicts.get(row.get("candidate_id") or row.get("candidate"))
        if verdict is None:
            continue
        key = (row["tag"], row.get("origin"), row.get("target_type"), row.get("target_id"))
        if not row.get("target_id"):
            key = (row["tag"],)
        references[key] = {
            "relevance": verdict.get("relevance"),
            "semantics": verdict.get("semantics"),
            "value": verdict.get("value"),
            "reason": verdict.get("reason") or "",
            "quote": verdict.get("quote"),
        }
    return references


def _now_iso() -> str:
    """UTC ISO 时间戳（含微秒：历史按 started_at 排序需可分辨同秒 run）。"""
    return datetime.now(timezone.utc).isoformat()


class OpenEvalService:
    """开放评测干跑运行器：起跑校验 → 线程池逐片生成+评审 → 终态落盘。

    DB 零写入红线（spec §5/§9，与闭环同款）：起跑时
    `load_assembly_context("open")` 装配一次，`_run_movie` 只调
    `build_spec` 与 `context.producer.produce`（第二参传 None——producer
    本不落库），干跑循环全程零 DB 交互；评测唯一的写盘位置是
    `settings.open_eval_output_root/<run_id>/`（audit/ 由管线经 run 级
    settings 副本落进同一目录）。

    线程模型（镜像闭环）：`start` 起 daemon worker；worker 主线程持
    ThreadPoolExecutor 按 tt 序滑动提交 `_run_movie`（每片独立线程）
    并在完成序上收敛——每片完成即锁内更新注册表 + 追加 progress.jsonl；
    双熔断（见模块 docstring）→ status=aborted（余量 future.cancel，
    在途片跑完）；`cancel` 置事件后不再提交（在途片照常跑完，已完成
    结果保留，status=cancelled）。终态（succeeded/cancelled/aborted，
    failed 为 worker 意外兜底）由 worker 主线程写 meta.json、
    raw_results.json 与报告三件套（open_report 组装）；`history()` 扫
    磁盘出历史行（interrupted 推断见其 docstring）。注册表更新、cancel、
    progress 追加都在 `threading.Lock` 内。
    """

    def __init__(self, llm, embedder, settings: Settings, *,
                 dataset_root_owner=None, busy_probe: Callable[[], str | None] | None = None) -> None:
        """`dataset_root_owner`：ClosedEvalService 实例——数据集根共享其
        override 状态（open 的数据集三端点 Depends(get_closed_eval)，
        override 只写闭环服务；本服务的 effective_root 直读 owner）；
        `busy_probe`：对端评测服务的活跃 run 无锁读
        （active_run_id_lockless）——跨服务互斥用，见模块 docstring。
        评审客户端在构造时按 settings 建立一次（make_review_client：
        未配独立评审端点或替身场景回落主客户端，同模评审）；测试可整只
        替换 `_review_client` 属性注入替身。
        """
        self.settings = settings
        self._llm = llm
        self._embedder = embedder
        self._lock = threading.Lock()
        self._runs: dict[str, RunState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_run_id: str | None = None
        # 评审客户端（构造时建立一次；测试经属性替换注入替身）
        self._review_client = make_review_client(settings, llm)
        self._review_slots = threading.BoundedSemaphore(settings.open_eval_review_concurrency)
        self._rereview_source: str | None = None
        self.dataset_root_owner = dataset_root_owner
        # 对端活跃 run 无锁探针（app 装配层互挂；默认 None = 单服务部署）
        self.busy_probe = busy_probe

    @property
    def effective_root(self) -> str:
        """当前生效的数据集根：owner（ClosedEvalService）优先——共享闭环
        页的 override 状态；无 owner 时回落 settings.eval_dataset_root。"""
        if self.dataset_root_owner is not None:
            return self.dataset_root_owner.effective_root
        return self.settings.eval_dataset_root

    # ---------------------------------------------------------------- 对外接口

    def start(self, dataset_id: str, movies: dict,
              modalities: Sequence[str] = ("script", "dialogue"),
              concurrency: int = DEFAULT_CONCURRENCY, *,
              wait: bool = False) -> str:
        """起一次开放评测 run（校验 → 注册 → 骨架 → daemon worker）→ run_id。

        校验序：数据集存在 → 选片非空 → concurrency ≥ 1 → modalities
        非空 ⊆ {script, dialogue} 且必含 script（video 显式拒绝：v1 文本
        模态，关键帧接入为后续扩展）→ 库就绪 → busy（自有 + 对端
        probe）。校验失败抛 `EvalValidationError`（不占 busy 位）；
        已有 run 在跑抛 `RunBusyError`。`wait=True` 供测试同步等完
        （join worker 线程后返回）。
        """
        root = self.effective_root
        info = next((i for i in scan_datasets(root, set())
                     if i.id == dataset_id), None)
        if info is None:
            raise EvalValidationError(
                f"评测数据集不存在或不可解析：{dataset_id!r}（{root}）")
        try:
            selected = select_movies(info.usable_movies,
                                     (movies or {}).get("mode"),
                                     (movies or {}).get("ids"))
        except ValueError as exc:
            raise EvalValidationError(str(exc)) from None
        if not selected:
            raise EvalValidationError("评测影片集为空：选片结果 0 部")
        if concurrency < 1:
            raise EvalValidationError(f"concurrency 必须 ≥ 1（收到 {concurrency}）")
        mods = self._validate_modalities(modalities)
        # 装配一次（快照语义）：库/策略/生产器在起跑时定格——干跑循环
        # 只走 build_spec（纯内存），run 中途改库行不影响本 run 产出
        context = load_assembly_context("open")
        if context.library is None:
            raise EvalValidationError("标签库未就绪，无法启动开放评测")
        library = context.library

        # Busy 检查 + 注册（同一锁内原子完成：并发 start 只放行一个）。
        # probe 是对端服务的无锁读——本锁内再取对端锁会与对端的对称探测
        # 形成锁序倒置死锁，详见模块 docstring「跨服务互斥」。
        with self._lock:
            if self._active_run_id is not None:
                raise RunBusyError(f"已有评测 run 进行中：{self._active_run_id}")
            peer = self.busy_probe() if self.busy_probe else None
            if peer is not None:
                raise RunBusyError(f"另一评测进行中：{peer}")
            run_id = self._new_run_id()
            started_at = _now_iso()
            self._runs[run_id] = RunState(
                run_id=run_id, status="running", done=0, total=len(selected),
                failed=0, started_at=started_at)
            self._cancel_events[run_id] = threading.Event()
            self._active_run_id = run_id

        meta = {
            "run_id": run_id,
            "dataset": {"id": dataset_id},
            "library": {"version": library.version,
                        "n_dirs": len(library.dirs()),
                        "n_tags": len(library.tags())},
            "prompt": {"mode": "open",
                       "version": context.producer.prompt_version},
            "review": {**review_calibre(self.settings, self._llm, self._review_client),
                       "prompt_version": REVIEW_PROMPT_VERSION},
            "llm_model": getattr(self._llm, "model", ""),
            "embedding_model": getattr(self._embedder, "name", ""),
            "movies": {"mode": (movies or {}).get("mode"),
                       "ids": list(selected)},
            "modalities": list(mods),
            "concurrency": concurrency,
        }
        # run 级 settings 副本：模态收敛为本次起跑取值、审计目录重定向进
        # run 目录（audit/<task_id>.json，task_id=影片 id，Task 5 judge
        # 段按此读）；原 settings 不被 run 污染
        run_dir = Path(self.settings.open_eval_output_root) / run_id
        run_settings = self.settings.model_copy(update={
            "mtg_modalities": ",".join(mods),
            "mtg_report_dir": str(run_dir / "audit"),
        })
        job = _OpenJob(run_id=run_id, movies=list(selected),
                       concurrency=concurrency, started_at=started_at,
                       meta=meta, dataset_root=root, context=context,
                       run_settings=run_settings)

        # run 目录骨架：meta 先行落盘 status=running（进程中断后可被历史
        # 扫描识别为 interrupted，不留无名目录）
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            _dump_json(run_dir / "meta.json",
                       {**meta, "status": "running", "done": 0,
                        "total": len(selected), "failed": 0,
                        "started_at": started_at, "finished_at": None})
            thread = threading.Thread(target=self._worker, args=(job,),
                                      name=f"open-eval-{run_id}", daemon=True)
            thread.start()
        except Exception as exc:
            with self._lock:
                state = self._runs.get(run_id)
                if state is not None:
                    state.status = "failed"
                    state.error = f"run 启动失败：{exc}"
                self._cancel_events.pop(run_id, None)
                if self._active_run_id == run_id:
                    self._active_run_id = None
            raise
        if wait:
            thread.join()
        return run_id

    @staticmethod
    def _validate_modalities(modalities: Sequence[str]) -> tuple[str, ...]:
        """modalities 校验：非空 ⊆ {script, dialogue} 且必含 script。

        video 显式拒绝（专属文案——v1 文本模态，关键帧接入为后续扩展，
        静默放行会去读 MovieNet 关键帧目录）；缺 script 拒绝——台词在
        script 通道渲染，缺 script 评审材料为空（open_review 的空材料
        自防会让每片评审都失败）。
        """
        mods = tuple(modalities or ())
        unknown = [m for m in mods if m not in _ALLOWED_MODALITIES]
        if "video" in unknown:
            raise EvalValidationError(
                "modalities 含 video：v1 文本模态，关键帧接入为后续扩展")
        if unknown:
            raise EvalValidationError(
                f"modalities 含未知模态：{sorted(unknown)}"
                f"（只支持 {list(_ALLOWED_MODALITIES)}）")
        if not mods:
            raise EvalValidationError(
                "modalities 为空：至少含 script（台词随 script 通道渲染）")
        if "script" not in mods:
            raise EvalValidationError(
                "modalities 必须含 script：台词在 script 通道渲染，"
                "缺 script 评审材料为空")
        return mods

    def get(self, run_id: str) -> RunState | None:
        """run 进度快照（副本）；未知 run_id → None。"""
        with self._lock:
            state = self._runs.get(run_id)
            return None if state is None else replace(state)

    def cancel(self, run_id: str) -> bool:
        """请求取消：置事件后 worker 不再提交新片（在途片跑完）。

        返回是否受理（run 不存在或已终态 → False）。幂等：重复取消
        已终态的 run 返回 False，不影响已落盘结果。
        """
        with self._lock:
            state = self._runs.get(run_id)
            event = self._cancel_events.get(run_id)
            if state is None or event is None or state.status != "running":
                return False
        event.set()
        return True

    def delete(self, run_id: str) -> bool:
        """删除终态 run：磁盘 run 目录 + 进程内注册表/取消事件一并清除。

        活跃守卫用 `_active_run_id`（不是注册表 status——worker 落终态在
        status 翻转之后、写盘收尾之前，窄窗口内 status 已终态但删除会与
        写盘竞态）；run_id 解析后必须是输出根下的**单段**目录——越界形态
        （`..`/绝对路径）与「解析后等于根」（`.`，防 rmtree 整个输出根）、
        嵌套段（`a/b`）一律按不存在处理（False，与闭环 delete 及 router
        _run_dir 的 404 同口径）。磁盘 meta 残留 status=running 但不在
        注册表 → interrupted 语义（进程死亡残留），可删。返回是否实际
        存在并删除。DB 零写入红线不变：只动 open_eval_output_root 下本
        run 目录。
        """
        root = Path(self.settings.open_eval_output_root)
        try:
            rel = (root / run_id).resolve().relative_to(root.resolve())
        except ValueError:
            return False  # 越界：按不存在处理，绝不越根动手
        if len(rel.parts) != 1:
            return False  # "."（→根本身）/嵌套段：同按不存在，防 rmtree 根
        run_dir = (root / run_id).resolve()
        with self._lock:
            if run_id in (self._active_run_id, self._rereview_source):
                raise RunActiveError(
                    f"运行中的 run 不可删除（先取消或等收尾）：{run_id}")
            state = self._runs.get(run_id)
            existed = state is not None or run_dir.exists()
            self._runs.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            if run_dir.exists():
                shutil.rmtree(run_dir)
        return existed

    def active_run_id(self) -> str | None:
        """当前进行中的 run_id（无 → None；锁内读，语义快照）。"""
        with self._lock:
            return self._active_run_id

    def active_run_id_lockless(self) -> str | None:
        """当前活跃 run_id 的无锁读——专供对端服务的 busy_probe。

        死锁规避：busy 探测发生在对端 start 持有其自身锁的窗口内，这里
        再取本服务锁即锁序倒置（A 持 A 锁探 B、B 持 B 锁探 A 互相等）；
        读单个 Python 属性是原子的，无锁读安全。良性竞态窗口（探测通过
        与注册之间对端可能放行）见模块 docstring「跨服务互斥」。
        """
        return self._active_run_id

    def history(self) -> list[dict]:
        """历史扫描：`open_eval_output_root/*/meta.json` → 摘要行（started_at 降序）。

        磁盘是持久层、注册表是进程内存：meta.status=running 且在注册表 →
        status/done/total/failed 取注册表活值（meta.json 只在起跑/终态写盘，
        运行中逐片进度不落 meta）；不在注册表 → 进程已死，标 `interrupted`
        （与正常终态在磁盘上可区分，spec §6）。
        读侧容错（宁缺勿崩）：meta 缺失/损坏的 run 跳过。五摘要键
        （pass_rate / new_pass / hit_pass / new_ratio / three_high）由
        `_read_open_summary` 读该 run 的 report.json；报告缺失/截断 →
        五键 None（宁缺勿假）。
        """
        root = Path(self.settings.open_eval_output_root)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return []
        rows = []
        for name in names:
            try:
                meta = json.loads((root / name / "meta.json")
                                  .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            run_id = meta.get("run_id") or name
            status = meta.get("status")
            done, total, failed = (meta.get("done"), meta.get("total"),
                                   meta.get("failed"))
            if status == "running":
                with self._lock:
                    state = self._runs.get(run_id)
                    # 在注册表 → status 与进度都用注册表活值：meta.json
                    # 只在起跑/终态写盘，运行中 done 恒为起跑值 0，逐片
                    # 进度只进注册表与 progress.jsonl（前端进度条轮询本
                    # 列表，取 meta 会一路 0% 到终态才跳 100%）；
                    # 不在 → 进程死亡残留，标 interrupted（进度保留 meta 值）
                    if state is not None:
                        status = state.status
                        done, total, failed = (state.done, state.total,
                                               state.failed)
                    else:
                        status = "interrupted"
            summary = _read_open_summary(root / name)
            rows.append({
                "run_id": run_id,
                "dataset_id": (meta.get("dataset") or {}).get("id"),
                "status": status,
                "done": done,
                "total": total,
                "failed": failed,
                "started_at": meta.get("started_at"),
                "finished_at": meta.get("finished_at"),
                "error": meta.get("error"),
                # 历史摘要五键（pass_rate/new_pass/hit_pass/new_ratio/
                # three_high）：_read_open_summary 读 report.json，缺失全 None
                "pass_rate": summary["pass_rate"],
                "new_pass": summary["new_pass"],
                "hit_pass": summary["hit_pass"],
                "new_ratio": summary["new_ratio"],
                "three_high": summary["three_high"],
            })
        rows.sort(key=lambda row: row.get("started_at") or "", reverse=True)
        return rows

    # ---------------------------------------------------------------- worker

    def _worker(self, job: _OpenJob) -> None:
        """daemon worker 线程体：收敛 → 终态落盘 → 释放 busy 位。

        任何意外（含收敛循环自身异常）都落终态、不滞留 running：
        status=failed + error 留痕，已完成的结果照常写盘。
        """
        results: list[OpenMovieResult] = []
        status, error = "failed", None
        try:
            status, error = self._converge(job, results)
        except Exception as exc:  # noqa: BLE001 —— worker 兜底：不滞留 running
            error = f"{type(exc).__name__}: {exc}"
        state = self._runs.get(job.run_id)
        with self._lock:
            if state is not None:
                state.status = status
                state.finished_at = _now_iso()  # 终态时刻（get() 侧透出）
                if error is not None:
                    state.error = error
        try:
            self._write_artifacts(job, results, status, error)
        except Exception as exc:  # noqa: BLE001 —— 终态已在注册表，落盘失败只记日志
            logger.warning("开放评测 run %s 终态落盘失败：%s", job.run_id, exc)
        finally:
            with self._lock:
                if self._active_run_id == job.run_id:
                    self._active_run_id = None

    def _converge(self, job: _OpenJob,
                  results: list[OpenMovieResult]) -> tuple[str, str | None]:
        """主收敛循环：滑动提交 + 完成序收敛，返回（终态 status, meta.error）。

        停止条件（停止后余量片从未进池、在途片跑完、结果照记）：
        - 完成序连续 ABORT_CONSECUTIVE 片 produce 失败 → aborted；
        - 完成序连续 ABORT_REVIEW_CONSECUTIVE 片评审失败（ok=True 且
          review_error）→ aborted，error 注明「评审端点熔断」；
        - cancel 事件置位 → cancelled。
        双计数互为反例（produce 失败片重置评审连败、评审失败/干净片重置
        produce 连败）。停止后到达的结果仍记入（增量落盘：停/崩不丢已
        完成片），但不再参与连败计数（终态已定，计数无意义）。
        """
        event = self._cancel_events[job.run_id]
        order = job.movies
        inflight: dict[Future, str] = {}
        next_index = 0
        produce_streak = 0
        review_streak = 0
        aborted = False
        fuse_error: str | None = None
        with ThreadPoolExecutor(max_workers=job.concurrency,
                                thread_name_prefix="open-eval") as pool:
            while True:
                stopping = aborted or event.is_set()
                if not stopping:
                    # 按 tt 序滑动补位：停止判定先于提交，余量片不进池
                    while (next_index < len(order)
                           and len(inflight) < job.concurrency):
                        future = pool.submit(self._run_movie, job,
                                             order[next_index])
                        inflight[future] = order[next_index]
                        next_index += 1
                elif inflight:
                    self._cancel_pending(inflight)
                if not inflight:
                    break
                done, _not_done = wait(set(inflight),
                                       return_when=FIRST_COMPLETED)
                # 同刻多完成按 tt 序处理：完成序语义对任何合法完成序成立，
                # 固定序只为输出可复现
                for future in sorted(done, key=lambda f: inflight[f]):
                    inflight.pop(future)
                    if future.cancelled():
                        continue  # 余量取消：未启动的片不算完成
                    result = future.result()
                    results.append(result)
                    self._record_result(job.run_id, result)
                    self._publish_live(job)
                    if aborted or event.is_set():
                        continue
                    if not result.ok:
                        produce_streak += 1
                        review_streak = 0
                    elif result.review_error is not None:
                        review_streak += 1
                        produce_streak = 0
                    else:
                        produce_streak = 0
                        review_streak = 0
                    if produce_streak >= ABORT_CONSECUTIVE:
                        aborted = True
                        self._cancel_pending(inflight)
                    elif review_streak >= ABORT_REVIEW_CONSECUTIVE:
                        aborted = True
                        fuse_error = (
                            f"评审端点熔断：完成序连续 "
                            f"{ABORT_REVIEW_CONSECUTIVE} 片评审失败，余量中止"
                            "（produce 产出不作废，已完成的评审失败片照记）")
                        self._cancel_pending(inflight)
        if aborted:
            return "aborted", fuse_error
        if event.is_set():
            return "cancelled", None
        return "succeeded", None

    @staticmethod
    def _cancel_pending(inflight: dict[Future, str]) -> None:
        """取消未启动的余量 future（已取消/在途的留在 inflight 由循环收敛）。"""
        for future in list(inflight):
            if future.cancel():
                inflight.pop(future)

    def _run_movie(self, job: _OpenJob, tt: str) -> OpenMovieResult:
        """单片干跑（线程池内执行）：produce 共 1+RETRY_PER_MOVIE 次尝试，
        成功后评审一次（评审不重试——评审失败按契约保留产出）。

        每次尝试：load（数据集快照根）→ build_spec（起跑装配快照渲染，
        纯内存零 DB）→ produce（不写库；session=None——producer 本不落
        库，未来 producer 改用会话会以 AttributeError 显式失败，不静默
        漂移；run 级 settings 副本随片传入，模态与审计目录在起跑定格）。
        produce 失败清空产出（半截产出不可信）。成功后产出二分：
        tag_id 非空 = 命中行，否则新词
        行；两路行合拼成评审清单（id 按位置合成，open_review 侧再按批次
        位置重编）——评审异常 → review_error 记录、行保留 review=None、
        产出不作废（spec §6 契约）。内容从起跑快照根加载（job.dataset_root）
        ——运行中切 override 不影响本 run。
        """
        started = time.perf_counter()
        title = ""
        error: str | None = None
        hits: list[dict] = []
        new_words: list[dict] = []
        review_error: str | None = None
        produced: list = []
        for _attempt in range(1 + RETRY_PER_MOVIE):
            try:
                content = load_movie_content(job.dataset_root, tt)
                title = content.asset.title
                spec = build_spec(job.context, content=content,
                                  task_id=tt, asset_id=tt,
                                  run_id=f"{job.run_id}:{tt}")
                produced = job.context.producer.produce(
                    spec, None, self._llm, self._embedder,
                    settings=job.run_settings)
                error = None
                break
            except Exception as exc:  # noqa: BLE001 —— 单片失败兜底：记 error 继续收敛
                error = f"{type(exc).__name__}: {exc}"
                produced = []
        result = OpenMovieResult(tt=tt, title=title, ok=error is None,
                                 error=error, hits=hits, new_words=new_words,
                                 review_error=review_error, elapsed_ms=0)
        with self._lock:
            job.live[tt] = result
        if error is None:
            # 生产 judge 的分数和摘录仅供报告展示，快照另外保存供重评。
            references = _judge_references(job.run_settings, tt)
            library = job.context.library
            snapshot = [{
                "tag_id": tag.tag_id, "name": tag.tag_name,
                "desc": (definition.description if (definition := library.tag(tag.tag_id))
                         else tag.candidate_description), "level": tag.level,
                "dir_id": tag.dir_id,
                "dir_name": (directory.dir_name if (directory := library.directory(tag.dir_id)) else None),
                "target_type": tag.target_type,
                "scene_ids": [ev.scene_id for ev in (tag.evidence or [])],
                "judge_reference": references.get(
                    (tag.tag_name, tag.origin, tag.target_type, tag.target_id),
                    references.get((tag.tag_name,))),
            } for tag in produced]
            try:
                self._save_snapshot(job.run_id, tt, snapshot, content)
            except OSError:
                logger.exception("无法保存重评快照 %s/%s", job.run_id, tt)
            # 发布待评产出，整片评审完成后更新报告。
            self._review_produced(tt, snapshot, content, job.run_id,
                                  result=result, publish=lambda: self._publish_live(job),
                                  modalities=job.meta["modalities"])
        with self._lock:
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    def _save_snapshot(self, run_id: str, tt: str, labels: list[dict], content: ContentJSON) -> None:
        """保存标签和原始评审材料，避免重评时数据集漂移。"""
        path = Path(self.settings.open_eval_output_root) / run_id / "produced" / f"{tt}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _dump_json(path, {"labels": labels, "content": content.model_dump(mode="json")})

    def _publish_live(self, job: _OpenJob) -> None:
        run_dir = Path(self.settings.open_eval_output_root) / job.run_id
        try:
            with job.live_lock:
                with self._lock:
                    results = [copy.deepcopy(job.live[tt]) for tt in job.movies if tt in job.live]
                    state = self._runs[job.run_id]
                    meta = {**job.meta, "status": "running", "done": state.done,
                            "total": state.total, "failed": state.failed}
                report = assemble_open_report(results, meta, audit_dir=run_dir / "audit")
                temporary = run_dir / "live.json.tmp"
                _dump_json(temporary, report)
                temporary.replace(run_dir / "live.json")
        except Exception:
            logger.exception("实时报告发布失败 %s", job.run_id)

    def _review_produced(self, tt: str, snapshot: list[dict], content: ContentJSON,
                         run_id: str, *, result: OpenMovieResult, publish=None,
                         modalities=("script", "dialogue"),
                         trace_dir: Path | None = None) -> None:
        """按产出划分命中和新词，整片盲评后原子回填结果。

        trace_dir 给出时原始响应写进该目录（重评审先写暂存目录，避免提前
        建出终态 run 目录让最后的 rename 撞名）。
        """
        hits: list[dict] = []
        new_words: list[dict] = []
        review_rows: list[dict] = []
        owners: list[dict] = []
        for item in snapshot:
            reference = item.get("judge_reference")
            if item.get("tag_id") is not None:
                row = {"tag_id": item["tag_id"], "tag_name": item["name"],
                       "level": item.get("level"), "desc": item.get("desc"),
                       "judge_reference": reference, "review": None}
                hits.append(row)
            else:
                row = {"name": item["name"], "desc": item.get("desc"),
                       "level": item.get("level"),
                       "judge_reference": reference, "review": None}
                new_words.append(row)
            row.update({key: item[key] for key in OPEN_MOUNT_FIELDS if key in item})
            row["scene_ids"] = list(item.get("scene_ids") or [])
            review_rows.append({
                "name": item["name"], "desc": item.get("desc"),
                "level": item.get("level"),
                "scene_ids": row["scene_ids"],
            })
            owners.append(row)
        with self._lock:
            result.hits, result.new_words = hits, new_words
        if publish is not None:
            publish()

        reviews = []
        review_error = None
        review_trace: dict = {}
        try:
            if review_rows:
                reviewed = review_movie_tags(
                    self._review_client, content, review_rows, modalities=modalities,
                    run_id=f"{run_id}:{tt}", request_slot=self._review_slots,
                    trace=review_trace)
                fields = ("pass", "relevance", "semantics", "value", "reason",
                          "evidence_scene_ids", "evaluated")
                reviews = [{key: row[key] for key in fields} for row in reviewed]
        except Exception as exc:  # noqa: BLE001 —— 评审失败时保留生成产出
            review_error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            for owner, review in zip(owners, reviews):
                owner["review"] = review
            result.review_error = review_error
        if review_rows:
            directory = trace_dir if trace_dir is not None else (
                Path(self.settings.open_eval_output_root) / run_id)
            self._save_review_trace(directory, tt, review_trace, review_error)
        if publish is not None:
            publish()

    def _save_review_trace(self, directory: Path, tt: str, trace: dict,
                           error: str | None) -> None:
        """评审逐批原始响应与失败原因落盘（review/<tt>.json）。

        评审失败时报告只留一句错误，无法还原是漏字段、id 错误还是顶层结构
        漂移；原始响应留在盘上，诊断端点按片透出。写失败只告警，不影响结果。
        """
        path = directory / "review" / f"{tt}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _dump_json(path, {"error": error, **trace})
        except OSError:
            logger.exception("无法保存评审原始响应 %s/%s", directory.name, tt)

    def rereview(self, run_id: str) -> dict:
        """使用原 run 的材料和产出快照，另存一份重评报告。"""
        root = Path(self.settings.open_eval_output_root).resolve()
        source = (root / run_id).resolve()
        if source.parent != root or source.name != run_id:
            raise EvalValidationError(f"无效的 run_id：{run_id}")
        with self._lock:
            if self._active_run_id is not None or (self.busy_probe and self.busy_probe()):
                raise RunActiveError("已有评测运行，无法启动重评审")
            child_id = self._new_run_id()
            self._active_run_id = child_id
            self._rereview_source = run_id
        try:
            try:
                meta = json.loads((source / "meta.json").read_text(encoding="utf-8"))
                original = json.loads((source / "raw_results.json").read_text(encoding="utf-8"))
                if meta["status"] == "running":
                    raise EvalValidationError(f"run 非终态，无法重评审：{run_id}")
                modalities = self._validate_modalities(meta["modalities"])
                results = [OpenMovieResult(**row) for row in original]
                snapshots = {}
                for result in results:
                    if result.ok and (result.hits or result.new_words):
                        snapshot = json.loads((source / "produced" / f"{result.tt}.json")
                                              .read_text(encoding="utf-8"))
                        snapshots[result.tt] = (snapshot["labels"],
                                                ContentJSON.model_validate(snapshot["content"]))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise EvalValidationError(f"原评测缺少完整材料快照或结果不可读：{run_id}") from exc

            started_at = _now_iso()
            with tempfile.TemporaryDirectory(prefix=".rereview-", dir=root) as stage:
                staged = Path(stage) / child_id
                staged.mkdir()
                for result in results:
                    if result.tt in snapshots:
                        labels, content = snapshots[result.tt]
                        started = time.perf_counter()
                        self._review_produced(result.tt, labels, content, child_id,
                                              result=result, modalities=modalities,
                                              trace_dir=staged)
                        result.elapsed_ms = int((time.perf_counter() - started) * 1000)

                meta.update({
                    "run_id": child_id, "source_run_id": run_id,
                    "started_at": started_at, "finished_at": _now_iso(),
                    "review": {**review_calibre(self.settings, self._llm, self._review_client),
                               "prompt_version": REVIEW_PROMPT_VERSION},
                    "review_concurrency": 1,
                })
                for name in ("produced", "audit"):
                    if (source / name).is_dir():
                        shutil.copytree(source / name, staged / name)
                report = assemble_open_report(results, meta, audit_dir=staged / "audit")
                _dump_json(staged / "raw_results.json", [asdict(result) for result in results])
                write_open_report(staged, report)
                _dump_json(staged / "meta.json", meta)
                staged.rename(root / child_id)

            return {"run_id": child_id, "source_run_id": run_id, "reviewed": len(snapshots),
                    "review_failed": sum(r.review_error is not None for r in results)}
        finally:
            with self._lock:
                self._active_run_id = None
                self._rereview_source = None

    def _record_result(self, run_id: str, result: OpenMovieResult) -> None:
        """锁内更新注册表并追加 progress.jsonl 一行（增量落盘）。

        `review` 三态：ok（评审成功或零产出无需评审）/ failed（评审
        失败）/ skipped（produce 失败未发起评审）。failed 只统计 produce
        失败（评审失败片仍 ok——spec §6 契约）。
        """
        state = self._runs.get(run_id)
        if state is None:
            return
        review_status = ("skipped" if not result.ok
                         else "failed" if result.review_error is not None
                         else "ok")
        line = {"tt": result.tt, "ok": result.ok, "review": review_status,
                "hits": result.hits, "new_words": result.new_words,
                "elapsed_ms": result.elapsed_ms}
        if result.error is not None:
            line["error"] = result.error
        payload = json.dumps(line, ensure_ascii=False)
        with self._lock:
            state.done += 1
            if not result.ok:
                state.failed += 1
            with open(self._progress_path(run_id), "a", encoding="utf-8") as f:
                f.write(payload + "\n")

    def _write_artifacts(self, job: _OpenJob, results: list[OpenMovieResult],
                         status: str, error: str | None) -> None:
        """终态写盘（worker 主线程）：meta.json → raw_results.json → 报告三件套。

        写序非原子（meta 先行、报告最后）：读侧（history）对缺失/截断的
        report.json 按「该 run 无报告」容错。报告口径用终态 meta（banner
        需要 status/done/total）；audit/ 已由 mtg 管线经 run 级 settings
        副本落进本目录，judge 段按 run_dir/audit 读。组装/落盘异常向上抛
        （worker 兜底记日志）——meta/raw 已先行落盘，报告缺失按「无报告」
        容错，不滞留 running。
        """
        run_dir = Path(self.settings.open_eval_output_root) / job.run_id
        meta = dict(job.meta)
        meta.update({
            "status": status,
            "done": len(results),
            "total": len(job.movies),
            "failed": sum(1 for r in results if not r.ok),
            "started_at": job.started_at,
            "finished_at": _now_iso(),
        })
        if error is not None:
            meta["error"] = error
        _dump_json(run_dir / "meta.json", meta)
        _dump_json(run_dir / "raw_results.json", [asdict(result) for result in results])
        # 报告三件套（report.json / report.md / metrics.csv）：
        # assemble_open_report → write_open_report（镜像闭环 _write_artifacts
        # 的写序）；judge 段读 run 目录的 audit/<tt>.json
        report = assemble_open_report(results, meta,
                                      audit_dir=run_dir / "audit")
        write_open_report(run_dir, report)

    def _progress_path(self, run_id: str) -> str:
        return os.path.join(self.settings.open_eval_output_root, run_id,
                            "progress.jsonl")

    def _new_run_id(self) -> str:
        """`run-YYYYMMDD-HHMMSS-xxxx`：时间戳人读 + 2 字节随机防同秒撞名。

        输出根已有同名目录（撞名/残留）时换 token 重试，8 次仍撞 →
        EvalValidationError（多半是输出根异常，显式失败优于静默并入）。
        """
        for _ in range(8):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_id = f"run-{stamp}-{secrets.token_hex(2)}"
            if not os.path.isdir(os.path.join(
                    self.settings.open_eval_output_root, run_id)):
                return run_id
        raise EvalValidationError(
            "run_id 生成重试 8 次仍撞名，请检查 open_eval_output_root")
