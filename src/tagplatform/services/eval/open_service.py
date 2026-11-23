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

**评审失败契约（spec §6）**：produce 成功后对该片全部产出标签（命中 +
新词）做一次二值评审；评审失败（ReviewRequestError 或任何异常）时
**产出不作废**——hits/new_words 行原样保留（review=None 占位）、
review_error 记录进结果与 raw_results，该片仍计 ok（failed 只统计
produce 失败）。通过率/标定侧按「该片评审失败」整体排除出分母
（None≠0：不是评审 0 分），准确率族照常可算——排除逻辑在 Task 5
报告组装，本层只负责如实落盘。

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

**库∩gt 零交集不拒绝**（与闭环的差异点）：open 的头条指标之二（评审
通过率/新词占比）不需要 gt，零交集只让集合准确率/标定无 gt 可评——
meta 落 warning、报告侧按 None≠0 报 None，绝不产出假 0。

历史扫描：`history()` 扫
`open_eval_output_root/*/meta.json` 出历史行：meta.status=running 且
不在进程内注册表 → interrupted（进程死亡在磁盘上可辨认）；五摘要键
（accuracy40 / new_pass / hit_pass / agreement40 / new_ratio）由
open_report._read_open_summary 读该 run 的 report.json（缺失全 None，
宁缺勿假）。

分层红线：本模块不 import fastapi（设计文档 §9.4），不 import/call
persist_tags（评测只读）；评测产物只写 `settings.open_eval_output_root`。
熔断/重试常量本地重声明（不跨模块 import 闭环运行器的名字——两个运行
器互不依赖，仅语义对齐）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from tagplatform.capabilities.prompts.open_review import REVIEW_PROMPT_VERSION
from tagplatform.services.eval.dataset import (
    _gt_path,
    load_gt,
    load_movie_content,
    scan_datasets,
    select_movies,
)
from tagplatform.services.eval.open_ranking import DEFAULT_THETAS
from tagplatform.services.eval.open_report import (
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
    "DEFAULT_THETAS",
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
    """评测起跑校验失败：数据集不存在 / 选片越权或为空 / thetas 或
    modalities 非法 / 库未就绪（库∩gt 零交集**不**在此列——open 落
    warning 照常起跑）。"""


@dataclass
class OpenMovieResult:
    """单片干跑结果（评测的原子产出，Task 5 报告组装的输入）。

    - `hits` 行 `{"tag_id","tag_name","gt","review"}`：tag_id 非空的产出
      （库内命中）；gt 查该片 gt 表（None = 交集外），review 为
      `{"pass","reason","quote"}` 或 None（该片评审失败）；
    - `new_words` 行 `{"name","desc","level","review"}`：tag_id 为空的
      产出（模型新词）；desc 取 candidate_description（生成侧通用释义）；
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

    - `gt` 与 `library_ids`（库标签 id，排序）随 job 持有：终态写
      raw_results 时并入 gt 向量；Task 5 报告组装按 library_ids 求
      库∩gt 交集口径；
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
    gt: dict[str, dict[str, float]]
    library_ids: list[str]
    concurrency: int
    started_at: str
    meta: dict
    # 数据集根起跑快照：worker 懒加载 content.json 一律用 job 根——
    # 运行中页面切 override 不影响在跑 run（spec 隔离红线）
    dataset_root: str
    context: AssemblyContext
    run_settings: Settings


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
              thetas: Sequence[float] = DEFAULT_THETAS,
              modalities: Sequence[str] = ("script", "dialogue"),
              concurrency: int = DEFAULT_CONCURRENCY, *,
              wait: bool = False) -> str:
        """起一次开放评测 run（校验 → 注册 → 骨架 → daemon worker）→ run_id。

        校验序（spec 钉死）：数据集存在 → 选片非空 → concurrency ≥ 1 →
        thetas 非空且 ∈ (0,1] → modalities 非空 ⊆ {script, dialogue} 且
        必含 script（video 显式拒绝：v1 文本模态，关键帧接入为后续扩展）
        → gt 可读 → 库就绪 → busy（自有 + 对端 probe）。**库∩gt 零交集
        不拒绝**（open 头条指标之二不需要 gt）：meta 落 warning。校验
        失败抛 `EvalValidationError`（不占 busy 位）；已有 run 在跑抛
        `RunBusyError`。`wait=True` 供测试同步等完（join worker 线程后
        返回）。
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
        theta_values = self._validate_thetas(thetas)
        mods = self._validate_modalities(modalities)
        try:
            gt = load_gt(root, dataset_id)
        except ValueError as exc:
            raise EvalValidationError(str(exc)) from None
        # 装配一次（快照语义）：库/策略/生产器在起跑时定格——干跑循环
        # 只走 build_spec（纯内存），run 中途改库行不影响本 run 产出
        context = load_assembly_context("open")
        if context.library is None:
            raise EvalValidationError("标签库未就绪，无法启动开放评测")
        library = context.library
        library_ids = {tag.tag_id for tag in library.tags()}
        # 库∩gt 零交集不拒绝（open 头条指标之二不需要 gt）：meta 落
        # warning，报告侧按 None≠0 报 None（绝不产出假 0 指标）
        gt_tags = {tag for tags in gt.values() for tag in tags}
        zero_warning = None
        if not gt_tags & library_ids:
            zero_warning = (
                f"标签库与 gt 零交集（gt {len(gt_tags)} 标签 ∩ 库 "
                f"{len(library_ids)} 标签）——集合准确率与标定无 gt 可评"
                "（报告侧 None≠0），评审通过与产出统计不受影响")

        # run 上下文快照（口径起跑时定格；纯读，失败前置到校验段）
        gt_path = _gt_path(root, dataset_id)
        n_rows = sum(len(row) for row in gt.values())
        try:
            with open(gt_path, "rb") as f:
                gt_sha8 = hashlib.sha256(f.read()).hexdigest()[:8]
        except OSError:
            gt_sha8 = ""

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
            "dataset": {"id": dataset_id, "gt_path": gt_path,
                        "gt_sha8": gt_sha8, "n_rows": n_rows},
            "library": {"version": library.version,
                        "n_dirs": len(library.dirs()),
                        "n_tags": len(library.tags())},
            "prompt": {"mode": "open",
                       "version": context.producer.prompt_version},
            "review": {**review_calibre(self.settings, self._llm),
                       "prompt_version": REVIEW_PROMPT_VERSION},
            "llm_model": getattr(self._llm, "model", ""),
            "embedding_model": getattr(self._embedder, "name", ""),
            "movies": {"mode": (movies or {}).get("mode"),
                       "ids": list(selected)},
            "thetas": theta_values,
            "modalities": list(mods),
            "concurrency": concurrency,
        }
        if zero_warning is not None:
            meta["warning"] = zero_warning
        # run 级 settings 副本：模态收敛为本次起跑取值、审计目录重定向进
        # run 目录（audit/<task_id>.json，task_id=影片 id，Task 5 judge
        # 段按此读）；原 settings 不被 run 污染
        run_dir = Path(self.settings.open_eval_output_root) / run_id
        run_settings = self.settings.model_copy(update={
            "mtg_modalities": ",".join(mods),
            "mtg_report_dir": str(run_dir / "audit"),
        })
        job = _OpenJob(run_id=run_id, movies=list(selected), gt=gt,
                       library_ids=sorted(library_ids),
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
    def _validate_thetas(thetas: Sequence[float]) -> list[float]:
        """thetas 校验：非空、可数值化、值 ∈ (0,1]；返回 float 列表入 meta。"""
        try:
            values = [float(t) for t in (thetas or ())]
        except (TypeError, ValueError):
            raise EvalValidationError(
                f"thetas 含非数值项：{list(thetas)!r}") from None
        if not values:
            raise EvalValidationError("thetas 为空：至少一个相关门槛（θ ∈ (0,1]）")
        bad = [t for t in values if not 0 < t <= 1]
        if bad:
            raise EvalValidationError(f"thetas 越界：{bad}（须 ∈ (0,1]）")
        return values

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
            if self._active_run_id == run_id:
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

        磁盘是持久层、注册表是进程内存：meta.status=running 且不在注册表 →
        进程已死，标 `interrupted`（与正常终态在磁盘上可区分，spec §6）。
        读侧容错（宁缺勿崩）：meta 缺失/损坏的 run 跳过。五摘要键
        （accuracy40 / new_pass / hit_pass / agreement40 / new_ratio）由
        `_read_open_summary` 读该 run 的 report.json（取格规则见其
        docstring）；报告缺失/截断 → 五键 None（宁缺勿假）。
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
            if status == "running":
                with self._lock:
                    state = self._runs.get(run_id)
                    # 在注册表 → 用注册表现值（终态落盘前的窄窗口）；
                    # 不在 → 进程死亡残留，标 interrupted
                    status = state.status if state is not None else "interrupted"
            summary = _read_open_summary(root / name)
            rows.append({
                "run_id": run_id,
                "dataset_id": (meta.get("dataset") or {}).get("id"),
                "status": status,
                "done": meta.get("done"),
                "total": meta.get("total"),
                "failed": meta.get("failed"),
                "started_at": meta.get("started_at"),
                "finished_at": meta.get("finished_at"),
                "error": meta.get("error"),
                # 历史摘要五键（accuracy40/new_pass/hit_pass/agreement40/
                # new_ratio）：_read_open_summary 读 report.json，缺失全 None
                "accuracy40": summary["accuracy40"],
                "new_pass": summary["new_pass"],
                "hit_pass": summary["hit_pass"],
                "agreement40": summary["agreement40"],
                "new_ratio": summary["new_ratio"],
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
        tag_id 非空 = 命中行（gt 查该片 gt 表，None = 交集外），否则新词
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
        if error is None and produced:
            # 产出二分：命中行带 gt 查表（交集外 → None），新词行带 desc/level
            gt_table = job.gt.get(tt, {})
            review_rows: list[dict] = []
            owners: list[dict] = []  # 评审行序 → 结果行（hits/new_words 混排）
            for tag in produced:
                if tag.tag_id is not None:
                    row = {"tag_id": tag.tag_id, "tag_name": tag.tag_name,
                           "gt": gt_table.get(tag.tag_id), "review": None}
                    hits.append(row)
                else:
                    row = {"name": tag.tag_name,
                           "desc": tag.candidate_description,
                           "level": tag.level, "review": None}
                    new_words.append(row)
                review_rows.append({
                    "id": f"t{len(review_rows)}",
                    "name": tag.tag_name,
                    "desc": tag.candidate_description,
                    "level": tag.level,
                    "scenes": [ev.scene_id for ev in (tag.evidence or [])],
                })
                owners.append(row)
            try:
                reviewed = review_movie_tags(
                    self._review_client, content, review_rows,
                    run_id=f"{job.run_id}:{tt}")
                for row, verdict in zip(owners, reviewed):
                    row["review"] = {"pass": verdict["pass"],
                                     "reason": verdict["reason"],
                                     "quote": verdict["quote"]}
            except Exception as exc:  # noqa: BLE001 —— 评审失败：产出不作废（spec §6）
                review_error = f"{type(exc).__name__}: {exc}"
        return OpenMovieResult(tt=tt, title=title, ok=error is None,
                               error=error, hits=hits, new_words=new_words,
                               review_error=review_error,
                               elapsed_ms=int((time.perf_counter() - started) * 1000))

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
        report.json 按「该 run 无报告」容错。报告口径用 job 携带的
        gt/library_ids（起跑时定格，run 中途换库不影响本 run）与终态
        meta（banner 需要 status/done/total）；audit/ 已由 mtg 管线经
        run 级 settings 副本落进本目录，judge 段按 run_dir/audit 读。
        组装/落盘异常向上抛（worker 兜底记日志）——meta/raw 已先行落盘，
        报告缺失按「无报告」容错，不滞留 running。raw 行结构（组装
        open_report 的输入形状）：gt 向量并入（无内容片的满表向量也在
        ——报告侧按交集口径自行取用）。
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
        rows = [{"tt": r.tt, "title": r.title, "ok": r.ok, "error": r.error,
                 "hits": r.hits, "new_words": r.new_words,
                 "review_error": r.review_error, "gt": job.gt.get(r.tt, {}),
                 "elapsed_ms": r.elapsed_ms}
                for r in results]
        _dump_json(run_dir / "meta.json", meta)
        _dump_json(run_dir / "raw_results.json", rows)
        # 报告三件套（report.json / report.md / metrics.csv）：
        # assemble_open_report → write_open_report（镜像闭环 _write_artifacts
        # 的写序）；judge 段读 run 目录的 audit/<tt>.json
        report = assemble_open_report(
            results, job.gt, job.library_ids,
            meta.get("thetas") or DEFAULT_THETAS, meta,
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
