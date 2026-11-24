"""闭环评测干跑运行器（ClosedEvalService）：守护线程 + run 生命周期。

干跑运行器（Task 4）
====================

起跑校验（spec 钉死顺序）→ 起跑时 `load_assembly_context` 装配**一次**
（快照语义，四层重构 Task 5：库/策略/生产器/模板常量在起跑时定格，随
_EvalJob 快照携带）→ daemon worker 持线程池逐片生成 → 终态落盘。
每片走与正式生成**零漂移**的路径：`build_spec`（同一装配函数对的纯内存
第二级，起跑快照渲染 content 文本）→ `ClosedTagProducer.produce`（producer
本就不落库——落库是正式执行器的职责），所以干跑全程对业务表只读（DB
零写入红线，且干跑循环零 DB 交互——装配只在起跑发生一次），评测
产物只写 `settings.eval_output_root/<run_id>/` 六个文件：起跑即写
meta.json 骨架（status=running——进程中断可被历史扫描标 interrupted）、
每片完成锁内追加 progress.jsonl（增量落盘，中止/取消/崩溃不丢已完成片）、
终态由 worker 主线程写 meta + raw_results（gt 向量并入）+ 报告三件套
（report.json / report.md / metrics.csv，Task 5；组装/渲染/写在 report.py）。

容错语义：每片共 1+RETRY_PER_MOVIE 次尝试（异常兜底记 error，单片失败
不断跑）；**完成序**连续 ABORT_CONSECUTIVE 次失败 → 中止余量（系统性
故障保险丝：网关整体挂掉时别把 131 部全烧完）；`cancel` 置事件后不再
提交新片（在途片跑完，已完成结果保留）。提交按 tt 序滑动补位（在途数
< concurrency 才提交下一部），停止判定发生在提交之前：停止后绝不再提
交，尚未提交的余量片从未进池；在途片照常跑完、结果照记；done<total
只在停止时仍存在未提交余量的场景下确定成立（测试以并发 1 钉死该场景；
并发高、片少时停止前可能已全部提交——如 C=4、N=6 全失败，第 5 连败落
地前 m5/m6 已填满腾出的窗口位，此时 done 可达 total）。一次性全量提交
连「停止后不再有片进池」都保不住：主线取消余量与池线程取任务存在竞态，
停止后仍会有片被取走执行。

历史扫描（Task 5）：`history()` 扫 `eval_output_root/*/meta.json` 出历史
行：meta.status=running 且不在进程内注册表 → interrupted（进程死亡在磁盘
上可辨认）；读侧对缺失/截断的 report.json 一律按「该 run 无报告」处理
，不抛异常（meta→raw→report 写序的非原子窗口）。

共享类型归属（四层重构 Task 10，Ruling 1）：MovieResult / RunState /
_EvalJob 与三个异常都定义在本模块（运行器是生产方）；`report.assemble_report`
以类型标注消费 MovieResult（report 侧 TYPE_CHECKING 引用，运行时零反向
依赖——依赖单向 service → report）。数据集读与选片在 dataset.py，报告
组装/渲染/落盘在 report.py。

分层红线：本模块不 import fastapi（设计文档 §9.4），不 import/call
persist_tags（落库走正式执行器，评测只读）；评测产物只写
`settings.eval_output_root`（报告层只读写文件，DB 零写入不变）。
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
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from tagplatform.services.eval.closed_ranking import (
    DEFAULT_K_P,
    DEFAULT_K_R,
    DEFAULT_KS,
    DEFAULT_THETA_P,
    DEFAULT_THETA_R,
    DEFAULT_THETAS,
)
from tagplatform.services.eval.dataset import (
    _gt_path,
    load_gt,
    load_movie_content,
    scan_datasets,
    select_movies,
)
from tagplatform.services.eval.report import (
    _dump_json,
    assemble_report,
    write_report,
)
from tagplatform.services.generation.assemble import (
    AssemblyContext,
    build_spec,
    load_assembly_context,
)
from tagplatform.settings import Settings

__all__ = [
    "ABORT_CONSECUTIVE",
    "DEFAULT_CONCURRENCY",
    "RETRY_PER_MOVIE",
    "ClosedEvalService",
    "EvalValidationError",
    "MovieResult",
    "RunActiveError",
    "RunBusyError",
    "RunState",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 干跑运行器（服务层）

# 单进程默认并发：与主模型网关的批处理并发（4）对齐，131 部全量评测不压垮网关
DEFAULT_CONCURRENCY = 4
# 每片重试次数（含首发共 1+2=3 次尝试）：单片偶发失败（超时/解析抖动）不断跑
RETRY_PER_MOVIE = 2
# 完成序连续失败达该数 → 中止余量：系统性故障保险丝（网关整体不可用时
# 别把整批烧完），单片失败远达不到该阈值
ABORT_CONSECUTIVE = 5


class RunBusyError(RuntimeError):
    """已有评测 run 进行中（单进程同时只允许一个 run，重复起跑显式拒绝）。"""


class RunActiveError(RuntimeError):
    """run 仍活跃（worker 在跑或在终态落盘收尾），不可删除——先取消/等收尾。"""


class EvalValidationError(ValueError):
    """评测起跑校验失败：数据集不存在 / 选片越权或为空 / 库未就绪 / 库∩gt 零交集。"""


@dataclass
class MovieResult:
    """单片干跑结果（评测的原子产出，报告组装的输入）。

    - `produced` 行按 (-sf, tag_id) 排序：`{tag_id, tag_name, sf,
      confidence, evidence_count}`——sf 是终分（relevance_score），
      confidence 是证据维独立读数（(E−1)/4），语义不同，都随行保留；
    - `ok=False` 时 `error` 记最后一次异常（含异常类型名），`produced`
      清空（半截产出不可信）；
    - `elapsed_ms` 覆盖该片的全部尝试（含重试）。
    """

    tt: str
    title: str
    ok: bool
    error: str | None
    produced: list[dict]
    elapsed_ms: int


@dataclass
class RunState:
    """一次 run 的进度快照（`get` 返回副本，读方无锁安全）。

    finished_at：终态时刻（_worker 落终态时盖章，运行中 None）；磁盘侧
    meta.json 同名键由 _write_artifacts 终态写盘（老 run 无此键 → None）。
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
class _EvalJob:
    """一次 run 的不可变上下文（start 时快照，worker 线程消费）。

    `gt` 与 `tag_universe`（库∩gt，排序）随 job 持有：终态写
    raw_results 时并入 gt 向量；报告组装按 universe 限口径（后续任务）。
    库/模板版本在 start 时定格——run 中途换库/换模板不影响本 run 的
    口径记录（meta 记的是起跑时快照）。`context` 是起跑时
    `load_assembly_context` 的装配快照（Task 5）：干跑循环经它走
    build_spec（纯内存），run 中途改库行不影响本 run 产出。
    """

    run_id: str
    movies: list[str]
    gt: dict[str, dict[str, float]]
    tag_universe: list[str]
    concurrency: int
    started_at: str
    meta: dict
    # 数据集根起跑快照：worker 懒加载 content.json 一律用 job 根——
    # 运行中页面切 override 不影响在跑 run（spec 隔离红线）
    dataset_root: str
    # 装配快照（Task 5）：库/模板/策略/生产器起跑时定格，干跑循环
    # 只经它 build_spec，全程零 DB 装配
    context: AssemblyContext


def _now_iso() -> str:
    """UTC ISO 时间戳（含微秒：历史按 started_at 排序需可分辨同秒 run）。"""
    return datetime.now(timezone.utc).isoformat()


def _produced_rows(tags: list) -> list[dict]:
    """ProducedTag 列表 → produced 行（五键，按 (-sf, tag_id) 排序）。"""
    rows = [
        {"tag_id": tag.tag_id or "", "tag_name": tag.tag_name,
         "sf": float(tag.relevance_score or 0.0),
         "confidence": float(tag.confidence or 0.0),
         "evidence_count": len(tag.evidence or [])}
        for tag in tags
    ]
    rows.sort(key=lambda row: (-row["sf"], row["tag_id"]))
    return rows


class ClosedEvalService:
    """闭环评测干跑运行器：起跑校验 → 线程池逐片生成 → 终态落盘。

    DB 零写入红线（spec §5/§9）：起跑时 `load_assembly_context` 装配一次
    （Task 5 快照语义），`_run_movie` 只调 `build_spec`（起跑快照纯内存
    渲染）与 `context.producer.produce`（producer 不落库——落库是正式
    执行器的事），干跑循环全程零 DB 交互；评测唯一的写盘位置是
    `settings.eval_output_root/<run_id>/`。与正式生成复用同一装配函数对
    与 producer，评测结果因此可直接对照生产口径。

    线程模型：`start` 起 daemon worker；worker 主线程持
    ThreadPoolExecutor 按 tt 序滑动提交 `_run_movie`（每片独立线程，
    经 job.context 走零 DB 干跑）并
    在完成序上收敛——每片完成即锁内更新注册表 + 追加 progress.jsonl；
    完成即锁内更新注册表 + 追加 progress.jsonl；完成序连续
    `ABORT_CONSECUTIVE` 次失败 → status=aborted（余量 future.cancel，
    在途片跑完）；`cancel` 置事件后不再提交（在途片照常跑完，已完成
    结果保留，status=cancelled）。终态（succeeded/cancelled/aborted，
    failed 为 worker 意外兜底）由 worker 主线程写 meta.json、
    raw_results.json 与报告三件套（assemble_report + write_report）；
    `history()` 扫磁盘出历史行（interrupted 推断见其 docstring）。
    注册表更新、cancel、progress 追加都在 `threading.Lock` 内。
    """

    def __init__(self, llm, embedder, settings: Settings) -> None:
        """session_factory 参数已随四层重构 Task 11 裁定删除：Task 5 装配
        两级化后干跑循环零 DB（起跑装配走全局引擎，见 load_assembly_context），
        服务体内不再开会话，该参数自池饿死改造时代起零消费者（仅赋值从不
        读取），删除为零行为变化。"""
        self.settings = settings
        self._llm = llm
        self._embedder = embedder
        self._lock = threading.Lock()
        self._runs: dict[str, RunState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_run_id: str | None = None
        # 跨服务互斥探针（open-eval Task 4）：对端评测服务的活跃 run 无锁
        # 读（OpenEvalService.active_run_id_lockless）——app 装配层互挂
        # （Task 6）。默认 None = 零行为变化（单服务部署零探测）
        self.busy_probe = None
        # 数据集根页面级 override（POST /eval/closed/dataset-root 写入）：
        # 进程内生效，重启回落 settings 默认；起跑时经 effective_root 快照
        self.dataset_root_override: str | None = None

    @property
    def effective_root(self) -> str:
        """当前生效的数据集根：override 优先，回落 settings.eval_dataset_root。"""
        return (self.dataset_root_override
                if self.dataset_root_override is not None
                else self.settings.eval_dataset_root)

    # ---------------------------------------------------------------- 对外接口

    def start(self, dataset_id: str, movies: dict,
              thetas: Sequence[float] = DEFAULT_THETAS,
              ks: Sequence[int] = DEFAULT_KS,
              concurrency: int = DEFAULT_CONCURRENCY, *,
              wait: bool = False) -> str:
        """起一次评测 run（校验 → 注册 → 骨架 → daemon worker）→ run_id。

        校验序（spec 钉死）：数据集存在 → 影片集（select_movies，custom
        越权/未知模式在此报错）→ gt 可读 → 库就绪 → 库∩gt 非空 →
        Busy（自有活跃 + 对端评测服务 probe）。校验失败抛
        `EvalValidationError`（不占 busy 位）；已有 run 在跑抛
        `RunBusyError`。`wait=True` 供测试同步等完（join
        worker 线程后返回）。
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
        try:
            gt = load_gt(root, dataset_id)
        except ValueError as exc:
            raise EvalValidationError(str(exc)) from None
        # 装配一次（Task 5 快照语义）：库/策略/生产器/模板常量在起跑时
        # 定格——干跑循环只走 build_spec（纯内存），run 中途改库行/换模板
        # 不影响本 run 产出（Review Focus #4 的结构保证）
        context = load_assembly_context("closed")
        if context.library is None:
            raise EvalValidationError("标签库未就绪，无法启动闭环评测")
        library = context.library
        gt_tags = {tag for tags in gt.values() for tag in tags}
        library_ids = {tag.tag_id for tag in library.tags()}
        if not gt_tags & library_ids:
            raise EvalValidationError(
                f"标签库与 gt 零交集（gt {len(gt_tags)} 标签 ∩ 库 "
                f"{len(library_ids)} 标签）——先换库或换数据集，绝不产出"
                "全 0 指标的假报告")

        # run 上下文快照（口径起跑时定格；纯读，失败前置到校验段）
        gt_path = _gt_path(root, dataset_id)
        n_rows = sum(len(row) for row in gt.values())
        try:
            with open(gt_path, "rb") as f:
                gt_sha8 = hashlib.sha256(f.read()).hexdigest()[:8]
        except OSError:
            gt_sha8 = ""
        # meta 口径沿用 DB 版本机制时代的整数语义（既有契约 version=1）：
        # 单一来源仍是 producer 协议声明（字符串留痕口径），消费方转 int
        prompt_version = int(context.producer.prompt_version)

        # Busy 检查 + 注册（同一锁内原子完成：并发 start 只放行一个）。
        # 跨服务互斥（open-eval Task 4）：自有活跃检查之后探测对端评测
        # 服务。probe 是对端的无锁读（active_run_id_lockless）——本锁内
        # 若再取对端锁，两服务互挂探针时即经典锁序倒置死锁（A 持 A 锁探
        # B、B 持 B 锁探 A 互相等）；无锁读单个 Python 属性是原子的，
        # 死锁不可能。良性竞态窗口：对端可能恰在本探测通过与对端自身注册
        # 之间放行（双方都探测到对方空闲），窗口为各自锁内注册前几条语句
        # 的微秒级，且互斥语义是友好拒绝而非硬事务边界——接受（与
        # OpenEvalService 模块 docstring「跨服务互斥」同款说明）。
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
            "prompt": {"mode": "closed", "version": prompt_version},
            "llm_model": getattr(self._llm, "model", ""),
            "embedding_model": getattr(self._embedder, "name", ""),
            "movies": {"mode": (movies or {}).get("mode"),
                       "ids": list(selected)},
            "thetas": list(thetas),
            "ks": list(ks),
            "concurrency": concurrency,
        }
        job = _EvalJob(run_id=run_id, movies=list(selected), gt=gt,
                       tag_universe=sorted(library_ids & gt_tags),
                       concurrency=concurrency, started_at=started_at,
                       meta=meta, dataset_root=root, context=context)

        # run 目录骨架：meta 先行落盘 status=running（进程中断后可被历史
        # 扫描识别为 interrupted，不留无名目录）
        run_dir = Path(self.settings.eval_output_root) / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            _dump_json(run_dir / "meta.json",
                       {**meta, "status": "running", "done": 0,
                        "total": len(selected), "failed": 0,
                        "started_at": started_at, "finished_at": None})
            thread = threading.Thread(target=self._worker, args=(job,),
                                      name=f"closed-eval-{run_id}", daemon=True)
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
        （`..`/绝对路径）与「解析后等于根」（`.`，终审 Critical：曾放行
        rmtree 整个输出根）、嵌套段（`a/b`）一律按不存在处理（False，
        与 router _run_dir 的 404 同口径）。磁盘 meta 残留 status=running
        但不在注册表 → interrupted 语义（进程死亡残留），可删（与 history
        推断同口径）。返回是否实际存在并删除。DB 零写入红线不变：只动
        eval_output_root 下本 run 目录。
        """
        root = Path(self.settings.eval_output_root)
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
        """当前进行中的 run_id（无 → None）。"""
        with self._lock:
            return self._active_run_id

    def active_run_id_lockless(self) -> str | None:
        """当前活跃 run_id 的无锁读——专供对端评测服务的 busy_probe（Task 4）。

        死锁规避：busy 探测发生在对端 start 持有其自身锁的窗口内，这里
        再取本服务锁即锁序倒置（A 持 A 锁探 B、B 持 B 锁探 A 互相等）；
        读单个 Python 属性是原子的，无锁读安全。良性竞态窗口（探测通过
        与注册之间对端可能放行）见 busy_probe 注释与 OpenEvalService 模块
        docstring「跨服务互斥」。
        """
        return self._active_run_id

    def history(self) -> list[dict]:
        """历史扫描：`eval_output_root/*/meta.json` → 摘要行（started_at 降序）。

        磁盘是持久层、注册表是进程内存：meta.status=running 且在注册表 →
        status/done/total/failed 取注册表活值（meta.json 只在起跑/终态写盘，
        运行中逐片进度不落 meta）；不在注册表 → 进程已死，标 `interrupted`
        （与正常终态在磁盘上可区分，spec §6）。
        读侧容错（宁缺勿崩）：meta 缺失/损坏的 run 跳过；report.json 缺失或
        截断（meta→raw→report 写序的非原子窗口）→ 摘要四键为 None，不抛
        异常。摘要取格走统一约定（closed_ranking 常量）：p10/ndcg10 =
        θ≈0.2 的最近**有值**格 @K=10、r20 = θ≈0.8 的最近有值格 @K=20
        （并列取小、无有值格 → None，custom-θ run 回退最近有值档），
        coverage 中位与 θ 无关照常读。
        """
        root = Path(self.settings.eval_output_root)
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
                    # 进度只进注册表与 progress.jsonl（取 meta 会让列表
                    # 「进度」列一路 0/N 到终态才跳满）；
                    # 不在 → 进程死亡残留，标 interrupted（进度保留 meta 值）
                    if state is not None:
                        status = state.status
                        done, total, failed = (state.done, state.total,
                                               state.failed)
                    else:
                        status = "interrupted"
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
                **self._read_summary(root / name),
            })
        rows.sort(key=lambda row: row.get("started_at") or "", reverse=True)
        return rows

    @staticmethod
    def _read_summary(run_dir: Path) -> dict:
        """report.json → 历史摘要四键（统一取格约定，2026-11-22）。

        p10/ndcg10 = θ≈DEFAULT_THETA_P(0.2) 的最近**有值**格 @K=10；
        r20 = θ≈DEFAULT_THETA_R(0.8) 的最近有值格 @K=20。「有值」= 字段
        值为数值（该 θ 无有效影片 → 格值 None 不算）；距离并列取小
        （round 9 防浮点噪声）；无有值格 → None（宁缺勿假——custom-θ
        run 回退最近有值档，不再因缺 0.4 档空列）。
        """
        summary = {"ndcg10": None, "p10": None, "r20": None,
                   "coverage_median": None}
        try:
            report = json.loads((run_dir / "report.json")
                                .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return summary
        if not isinstance(report, dict):
            return summary
        grid = report.get("grid")
        if isinstance(grid, list):

            def _pick_valued_cell(theta_target: float, k: int,
                                  field: str):
                """统一取格：最接近 theta_target 的有值格（并列取小）。
                与前端 ClosedEvalReport.pickField 同规则，勿单侧漂移。"""
                valued = [
                    point for point in grid
                    if isinstance(point, dict)
                    and isinstance(point.get("theta"), (int, float))
                    and point.get("k") == k
                    and isinstance(point.get(field), (int, float))]
                if not valued:
                    return None
                return min(valued, key=lambda point: (
                    round(abs(point["theta"] - theta_target), 9),
                    point["theta"]))[field]

            summary["p10"] = _pick_valued_cell(
                DEFAULT_THETA_P, DEFAULT_K_P, "precision")
            summary["ndcg10"] = _pick_valued_cell(
                DEFAULT_THETA_P, DEFAULT_K_P, "ndcg")
            summary["r20"] = _pick_valued_cell(
                DEFAULT_THETA_R, DEFAULT_K_R, "recall")
        coverage = report.get("coverage")
        if isinstance(coverage, dict):
            summary["coverage_median"] = coverage.get("median")
        return summary

    # ---------------------------------------------------------------- worker

    def _worker(self, job: _EvalJob) -> None:
        """daemon worker 线程体：收敛 → 终态落盘 → 释放 busy 位。

        任何意外（含收敛循环自身异常）都落终态、不滞留 running：
        status=failed + error 留痕，已完成的结果照常写盘。
        """
        results: list[MovieResult] = []
        status, error = "failed", None
        try:
            status = self._converge(job, results)
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
            logger.warning("评测 run %s 终态落盘失败：%s", job.run_id, exc)
        finally:
            with self._lock:
                if self._active_run_id == job.run_id:
                    self._active_run_id = None

    def _converge(self, job: _EvalJob, results: list[MovieResult]) -> str:
        """主收敛循环：滑动提交 + 完成序收敛，返回终态 status。

        停止条件（停止后余量片从未进池、在途片跑完、结果照记）：
        - 完成序连续 `ABORT_CONSECUTIVE` 失败 → aborted；
        - cancel 事件置位 → cancelled。
        停止后到达的结果仍记入（增量落盘：停/崩不丢已完成片），但不再
        参与连败计数（终态已定，计数无意义）。
        """
        event = self._cancel_events[job.run_id]
        order = job.movies
        inflight: dict[Future, str] = {}
        next_index = 0
        consecutive = 0
        aborted = False
        with ThreadPoolExecutor(max_workers=job.concurrency,
                                thread_name_prefix="closed-eval") as pool:
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
                    consecutive = 0 if result.ok else consecutive + 1
                    if consecutive >= ABORT_CONSECUTIVE:
                        aborted = True
                        self._cancel_pending(inflight)
        if aborted:
            return "aborted"
        if event.is_set():
            return "cancelled"
        return "succeeded"

    @staticmethod
    def _cancel_pending(inflight: dict[Future, str]) -> None:
        """取消未启动的余量 future（已取消/在途的留在 inflight 由循环收敛）。"""
        for future in list(inflight):
            if future.cancel():
                inflight.pop(future)

    def _run_movie(self, job: _EvalJob, tt: str) -> MovieResult:
        """单片干跑（线程池内执行）：共 1+RETRY_PER_MOVIE 次尝试。

        每次尝试：load（数据集快照根）→ build_spec（起跑装配快照渲染，
        纯内存零 DB）→ produce（不写库）→ 产出按 (-sf, tag_id) 排序。
        干跑循环全程零 DB 交互：库/模板/策略随 job.context 起跑快照携带
        （Task 5 快照语义——装配后变更库行，干跑产出不变），池饿死事故
        （2026-11-21：会话横跨 produce 占死 QueuePool）的"produce 不持
        会话"纪律由结构保证——循环里根本没有会话可持。produce 第二参传
        None（producer 本不落库；未来 producer 改用会话会以 AttributeError
        显式失败，不静默漂移）。内容从起跑快照根加载（job.dataset_root）
        ——运行中切 override 不影响本 run。
        """
        started = time.perf_counter()
        title = ""
        error: str | None = None
        produced: list[dict] = []
        for _attempt in range(1 + RETRY_PER_MOVIE):
            try:
                content = load_movie_content(job.dataset_root, tt)
                title = content.asset.title
                spec = build_spec(job.context, content=content,
                                  task_id=f"eval-{job.run_id}", asset_id=tt,
                                  run_id=f"{job.run_id}:{tt}")
                tags = job.context.producer.produce(
                    spec, None, self._llm, self._embedder)
                produced = _produced_rows(tags)
                error = None
                break
            except Exception as exc:  # noqa: BLE001 —— 单片失败兜底：记 error 继续收敛
                error = f"{type(exc).__name__}: {exc}"
                produced = []
        return MovieResult(tt=tt, title=title, ok=error is None, error=error,
                           produced=produced,
                           elapsed_ms=int((time.perf_counter() - started) * 1000))

    def _record_result(self, run_id: str, result: MovieResult) -> None:
        """锁内更新注册表并追加 progress.jsonl 一行（增量落盘）。"""
        state = self._runs.get(run_id)
        if state is None:
            return
        line = {"tt": result.tt, "ok": result.ok,
                "produced": result.produced, "elapsed_ms": result.elapsed_ms}
        if result.error is not None:
            line["error"] = result.error
        payload = json.dumps(line, ensure_ascii=False)
        with self._lock:
            state.done += 1
            if not result.ok:
                state.failed += 1
            with open(self._progress_path(run_id), "a", encoding="utf-8") as f:
                f.write(payload + "\n")

    def _write_artifacts(self, job: _EvalJob, results: list[MovieResult],
                         status: str, error: str | None) -> None:
        """终态写盘（worker 主线程）：meta.json → raw_results.json → 报告三件套。

        写序非原子（meta 先行、报告最后）：读侧（history）对缺失/截断的
        report.json 按「该 run 无报告」容错。报告口径用 job 携带的
        gt/tag_universe（起跑时定格，run 中途换库不影响本 run）与终态
        meta（banner/warning 需要终态 status/done/total/failed）。
        """
        run_dir = Path(self.settings.eval_output_root) / job.run_id
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
                 "produced": r.produced, "gt": job.gt.get(r.tt, {}),
                 "elapsed_ms": r.elapsed_ms}
                for r in results]
        _dump_json(run_dir / "meta.json", meta)
        _dump_json(run_dir / "raw_results.json", rows)
        report = assemble_report(results, job.gt, job.tag_universe,
                                 job.meta.get("thetas") or DEFAULT_THETAS,
                                 job.meta.get("ks") or DEFAULT_KS,
                                 meta=meta)
        write_report(run_dir, report)

    def _progress_path(self, run_id: str) -> str:
        return os.path.join(self.settings.eval_output_root, run_id,
                            "progress.jsonl")

    def _new_run_id(self) -> str:
        """`run-YYYYMMDD-HHMMSS-xxxx`：时间戳人读 + 2 字节随机防同秒撞名。

        输出根已有同名目录（撞名/残留）时换 token 重试，8 次仍撞 →
        EvalValidationError（多半是输出根异常，显式失败优于静默并入）。
        """
        for _ in range(8):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_id = f"run-{stamp}-{secrets.token_hex(2)}"
            if not os.path.isdir(
                    os.path.join(self.settings.eval_output_root, run_id)):
                return run_id
        raise EvalValidationError(
            "run_id 生成重试 8 次仍撞名，请检查 eval_output_root")
