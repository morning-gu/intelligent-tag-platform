"""闭环评测：数据集层（发现/加载/选片）+ 干跑运行器服务层。

为什么需要这个模块
==================

闭环评测以 `/cache/MovieTags` 基准（gt CSV 三列 `movieId,tagId,relevance`
+ 同根 `data/<ttID>/content.json`）为真值。数据集层回答三件事：

1. **有哪些数据集可评**：扫 `gt/*.csv`，每个文件 = 一个数据集；合法性
   = 首行三列 `movieId,tagId,relevance` 且数据行可解析。畸形文件**跳过
   该文件**而不是让整个扫描崩掉（宁缺勿崩）——三份 gt 有一份手滑改坏
   列名，其余两份照常可评，坏的那份从列表里消失即可见；
2. **每部片怎么读**：`load_gt` 取 {movieId: {tagId: relevance}} 真值表；
   `load_movie_content` 经 `content_io.load_content` 走上游形态自动识别
   转换——远端 content.json 的台词字段是 `speak_id`（历史上游笔误），
   平台契约是 `speaker_id`，读入口统一改名，否则该字段被上游模型的
   extra=ignore 静默吞掉、说话人退化成「未知说话人」；
3. **评哪些片**：`select_movies` 三种模式（preset30 固定 seed 抽样 /
   all / custom 越权显式拒绝）。

干跑运行器（ClosedEvalService，Task 4）
=====================================

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
（report.json / report.md / metrics.csv，Task 5）。

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

报告组装与历史扫描（Task 5）
============================

`assemble_report` 把 ok 片构造成 ClosedSample 喂 `evaluate_closed`：
`ranked` = 产出行限「库∩gt」交集、防御性再排 (-sf, tag_id)；交集外
产出条目剔除、只计 `sample_accounting.no_gt`（spec §5 保护原则——白给
precision / 冤枉 recall 都不要）；先验基线（tag_universe 按 gt 全影片
均值降序、并列 tag_id 字典序）用同一机制并排给出，格值才可比。部分
口径响亮：status != succeeded 或 done < total → banner 明写「部分运行」；
失败率 > 10% → 小样本警示；样本核算（total/ok/failed/invalid/no_gt）
永远随报告落盘。`history()` 扫 `eval_output_root/*/meta.json` 出历史行：
meta.status=running 且不在进程内注册表 → interrupted（进程死亡在磁盘
上可辨认）；读侧对缺失/截断的 report.json 一律按「该 run 无报告」处理
，不抛异常（meta→raw→report 写序的非原子窗口）。

缓存纪律
========

- gt 表按 (path, mtime) 进程内缓存：genome-scores 14.7 万行首读秒级，
  复扫/复评不重读；文件被替换（mtime 变化）自动失效；
- 片名（content.json 的 asset.title）按路径进程内缓存：评测期间数据集
  根只读，同名路径不会变。

分层红线：本模块不 import fastapi（设计文档 §9.4），不 import/call
persist_tags（落库走正式执行器，评测只读）；数据集根只读，评测产物只
写 `settings.eval_output_root`（报告层只读写文件，DB 零写入不变）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import random
import secrets
import shutil
import threading
import time
from collections.abc import Collection, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from tagplatform.services.content.content_io import load_content
from tagplatform.eval.closed_ranking import (
    DEFAULT_K_P,
    DEFAULT_K_R,
    DEFAULT_KS,
    DEFAULT_THETA_P,
    DEFAULT_THETA_R,
    DEFAULT_THETAS,
    ClosedSample,
    evaluate_closed,
    spearman,
)
from tagplatform.services.generation.assemble import (
    AssemblyContext,
    build_spec,
    load_assembly_context,
)
from tagplatform.schemas import ContentJSON
from tagplatform.settings import Settings

__all__ = [
    "ABORT_CONSECUTIVE",
    "DEFAULT_CONCURRENCY",
    "RETRY_PER_MOVIE",
    "DatasetInfo",
    "EvalValidationError",
    "MovieResult",
    "RunBusyError",
    "RunState",
    "ClosedEvalService",
    "assemble_report",
    "load_gt",
    "load_movie_content",
    "movie_entries",
    "render_report_md",
    "scan_datasets",
    "select_movies",
    "write_report",
]

logger = logging.getLogger(__name__)

# 试点 30 部：seed 钉死保证换机器重跑抽到同一批片（spec §6）
PRESET_SIZE = 30
PRESET_SEED = 20261120

# gt CSV 的合法首行（spec §5：统一三列 movieId,tagId,relevance）
_GT_HEADER = ["movieId", "tagId", "relevance"]

# gt 表缓存：path → ((mtime, size), {movieId: {tagId: relevance}})。
# 键含 size：Windows mtime 精度为秒级文件时间戳，同秒内追加写入 mtime
# 不变（实测 delta=0.0），仅按 mtime 判失效会复用旧表；size 与内容长度
# 同步变化，同秒写入也能正确失效（Linux 上更稳健）
_GT_CACHE: dict[str, tuple[tuple[float, int], dict[str, dict[str, float]]]] = {}
# 片名缓存：content.json 路径 → asset.title
_TITLE_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------- 数据集发现

@dataclass
class DatasetInfo:
    """扫描出的一份可评数据集：gt 概况 + 与当前标签库的可用性。

    - `n_movies/n_tags`：gt 里的影片数与标签数（去重）；
    - `library_overlap`：gt 标签 ∩ 库标签——换库后哪个 gt 还能用一眼可见；
    - `usable_movies`：gt 有分 ∩ `data/<movieId>/content.json` 存在，
      按 movieId 排序（评测只评内容齐备的片）。
    """

    id: str
    gt_path: str
    n_movies: int
    n_tags: int
    library_overlap: int
    usable_movies: list[str]


def _read_gt_table(path: str) -> dict[str, dict[str, float]] | None:
    """读一份 gt CSV → {movieId: {tagId: relevance}}；畸形 → None（宁缺勿崩）。

    首行不是三列 `movieId,tagId,relevance`、或任何数据行列数/浮点解析
    不对 → 整份文件判无效（调用方跳过）。按 (path, mtime+size) 进程内
    缓存（size 防 Windows 同秒写入 mtime 不变的失效盲区）。
    """
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return None
    cached = _GT_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    table: dict[str, dict[str, float]] = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            if next(reader, None) != _GT_HEADER:  # 空文件/首行不对 → 无效
                return None
            for row in reader:
                if not row:
                    continue  # 空行不算数据行
                if len(row) != 3:
                    return None
                movie_id, tag_id, raw = row
                table.setdefault(movie_id, {})[tag_id] = float(raw)
    except (OSError, ValueError, csv.Error):
        return None
    _GT_CACHE[path] = (stamp, table)
    return table


def scan_datasets(root: str, library_tag_ids: Collection[str]) -> list[DatasetInfo]:
    """扫 `root/gt/*.csv` → 可评数据集列表（按 id 排序）。

    畸形 gt（首行不对/数据行不可解析）跳过该文件，不影响其余（宁缺勿崩）；
    根目录不存在 → 空列表。gt 表读取走 (path, mtime) 进程内缓存。
    """
    gt_dir = os.path.join(root, "gt")
    try:
        names = sorted(os.listdir(gt_dir))
    except OSError:
        return []
    library = set(library_tag_ids)
    infos: list[DatasetInfo] = []
    for name in names:
        if not name.endswith(".csv"):
            continue
        path = os.path.join(gt_dir, name)
        table = _read_gt_table(path)
        if table is None:
            continue
        tag_ids = {tag for tags in table.values() for tag in tags}
        infos.append(DatasetInfo(
            id=name[:-len(".csv")],
            gt_path=path,
            n_movies=len(table),
            n_tags=len(tag_ids),
            library_overlap=len(tag_ids & library),
            usable_movies=[
                movie_id for movie_id in sorted(table)
                if os.path.isfile(os.path.join(root, "data", movie_id,
                                               "content.json"))
            ],
        ))
    infos.sort(key=lambda info: info.id)
    return infos


# ---------------------------------------------------------------- gt 与影片加载

def _gt_path(root: str, dataset_id: str) -> str:
    return os.path.join(root, "gt", f"{dataset_id}.csv")


def load_gt(root: str, dataset_id: str) -> dict[str, dict[str, float]]:
    """一份 gt → {movieId: {tagId: relevance}}；数据集不存在/畸形 → ValueError。

    返回缓存表的逐层浅拷贝：调用方改返回值不影响缓存。
    """
    path = _gt_path(root, dataset_id)
    table = _read_gt_table(path)
    if table is None:
        raise ValueError(f"gt 数据不存在或无法解析：dataset_id={dataset_id!r}"
                         f"（{path}）")
    return {movie_id: dict(tags) for movie_id, tags in table.items()}


def _rename_speak_id(data: dict) -> None:
    """把台词的 `speak_id` 键改名为平台契约的 `speaker_id`（就地改）。

    /cache/MovieTags 的 content.json 台词字段是 `speak_id`（spec §4 记录的
    已知差异），平台上游 schema 是 `speaker_id`；不改名会被上游模型的
    extra="ignore" 静默吞掉，说话人退化成「未知说话人」。浅遍历
    content.scenes[*].dialogues[*]，键存在才改。
    """
    content = data.get("content")
    if not isinstance(content, dict):
        return
    for scene in content.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for dialogue in scene.get("dialogues") or []:
            if isinstance(dialogue, dict) and "speak_id" in dialogue:
                dialogue["speaker_id"] = dialogue.pop("speak_id")


def load_movie_content(root: str, tt: str) -> ContentJSON:
    """读 `data/<tt>/content.json` → 平台内部 ContentJSON。

    读 JSON 后先改 `speak_id` → `speaker_id`，再交 `content_io.load_content`
    （上游形态自动识别转换；平台内部形态的旧数据同样可读）。文件缺失由
    open 抛 FileNotFoundError（调用方按"该片不可评"处理）。
    """
    path = os.path.join(root, "data", tt, "content.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _rename_speak_id(data)
    return load_content(json.dumps(data, ensure_ascii=False))


def _read_title(path: str) -> str:
    """content.json → asset.title（进程内缓存；读不出 → 空串，宁缺勿崩）。"""
    title = _TITLE_CACHE.get(path)
    if title is None:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            title = str((data.get("asset") or {}).get("title") or "")
        except (OSError, ValueError):
            title = ""
        _TITLE_CACHE[path] = title
    return title


def movie_entries(root: str, dataset_id: str) -> list[dict[str, str]]:
    """数据集的 usable 影片 → [{tt, title}]，按 tt 排序（前端选片列表）。

    标题读 content.json 的 asset.title（进程内缓存）；数据集不存在/畸形
    → ValueError（与 load_gt 同口径）。
    """
    info = next((i for i in scan_datasets(root, set()) if i.id == dataset_id),
                None)
    if info is None:
        raise ValueError(f"数据集不存在或无法解析：dataset_id={dataset_id!r}"
                         f"（{_gt_path(root, dataset_id)}）")
    return [{"tt": tt,
             "title": _read_title(os.path.join(root, "data", tt, "content.json"))}
            for tt in info.usable_movies]


# ---------------------------------------------------------------- 选片

def select_movies(usable: list[str], mode: str,
                  custom: list[str] | None = None, *,
                  size: int = PRESET_SIZE,
                  seed: int = PRESET_SEED) -> list[str]:
    """按模式从 usable 选片，返回排序去重列表。

    - `preset30`：`sorted` 后 `random.Random(seed).sample(..., min(size, len))`
      ——不足 size 全取；固定 seed 保证换机器重跑抽到同一批片。sample 的
      输出顺序随输入顺序漂移，末尾再排序让结果与输入顺序无关；
    - `all`：全部（排序去重）；
    - `custom`：只接受 usable 子集，越权 id → ValueError 列出未知项
      （前端只发可见列表，越权 = 调用方 bug，早失败不静默缩圈）。
    """
    pool = sorted(set(usable))
    if mode == "preset30":
        return sorted(random.Random(seed).sample(pool, min(size, len(pool))))
    if mode == "all":
        return pool
    if mode == "custom":
        chosen = set(custom or [])
        unknown = sorted(chosen - set(pool))
        if unknown:
            raise ValueError(f"custom 选片含 usable 之外的影片 id：{unknown}")
        return sorted(chosen)
    raise ValueError(f"未知选片模式：{mode!r}（支持 preset30 / all / custom）")


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


def _dump_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


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

    def __init__(self, session_factory, llm, embedder, settings: Settings) -> None:
        """session_factory 留自池饿死改造时代：Task 5 装配两级化后干跑循环
        零 DB（起跑装配走全局引擎，见 load_assembly_context），服务体内
        不再开会话；参数保留给 API 装配层（Task 11 编排接手时裁决去留）。"""
        self.settings = settings
        self._session_factory = session_factory
        self._llm = llm
        self._embedder = embedder
        self._lock = threading.Lock()
        self._runs: dict[str, RunState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_run_id: str | None = None
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
        Busy。校验失败抛 `EvalValidationError`（不占 busy 位）；已有
        run 在跑抛 `RunBusyError`。`wait=True` 供测试同步等完（join
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

        # Busy 检查 + 注册（同一锁内原子完成：并发 start 只放行一个）
        with self._lock:
            if self._active_run_id is not None:
                raise RunBusyError(f"已有评测 run 进行中：{self._active_run_id}")
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

    def history(self) -> list[dict]:
        """历史扫描：`eval_output_root/*/meta.json` → 摘要行（started_at 降序）。

        磁盘是持久层、注册表是进程内存：meta.status=running 且不在注册表 →
        进程已死，标 `interrupted`（与正常终态在磁盘上可区分，spec §6）。
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
            if status == "running":
                with self._lock:
                    state = self._runs.get(run_id)
                    # 在注册表 → 用注册表现值（终态落盘前的窄窗口）；
                    # 不在 → 进程死亡残留，标 interrupted
                    status = state.status if state is not None else "interrupted"
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


# ---------------------------------------------------------------- 报告组装与历史扫描

# 部分运行口径（banner 文案；eval.md §4 同口径）
BANNER_PARTIAL = "部分运行：指标仅覆盖完成影片，与全量口径不可直接横比"
# 银标准警示（MovieTags README §6.5 同口径）：gt 是相关性模型输出非人工标注
NOTE_SILVER = (
    "银标准警示：genome relevance 是相关性模型输出，近似而非人工标注"
    "（MovieTags README §6.5 同口径）——指标衡量的是系统与该模型口径的一致"
    "性，不是与人工真理的一致；不同口径（LLM/嵌入/模板/标签库/数据集）的"
    "分数不可跨组比较。")

# metrics.csv 长表列（spec §8：Excel/后续分析消费）
_CSV_HEADER = ("theta", "k", "metric", "macro", "ci_low", "ci_high",
               "random_baseline")


def _grid_rows(grid) -> list[dict]:
    """ClosedReport.grid → report.json 格列表（CI 元组 → [lo, hi] 列表）。"""
    def _ci(value):
        return None if value is None else [value[0], value[1]]

    return [{"theta": gp.theta, "k": gp.k, "n_valid": gp.n_valid,
             "precision": gp.precision, "recall": gp.recall,
             "ndcg": gp.ndcg,
             "precision_ci": _ci(gp.precision_ci),
             "recall_ci": _ci(gp.recall_ci), "ndcg_ci": _ci(gp.ndcg_ci),
             "precision_random": gp.precision_random,
             "recall_random": gp.recall_random}
            for gp in grid]


def _prior_ranking(gt: dict[str, dict[str, float]],
                   tag_universe: Sequence[str]
                   ) -> tuple[tuple[str, ...], dict[str, float]]:
    """先验序：tag_universe 按 gt 全影片均值降序（并列 tag_id 字典序）。

    均值用 gt **全表**（数据集全部影片，非仅选片/成功片——spec 决定清单
    #7：先验基线衡量「不看电影只看先验」能到哪）。按「有分的影片」取
    均值：genome 矩阵稠密无缺失，两种算法同数；稀疏 gt 里缺行 = 没评过
    ，不是「不相关」（None≠0 的均值版）。universe ⊆ gt 标签集，均值分母
    恒 ≥ 1。
    """
    means: dict[str, float] = {}
    for tag in tag_universe:
        values = [scores[tag] for scores in gt.values() if tag in scores]
        means[tag] = sum(values) / len(values) if values else 0.0
    order = tuple(sorted(tag_universe, key=lambda tag: (-means[tag], tag)))
    return order, means


def assemble_report(results: list[MovieResult],
                    gt: dict[str, dict[str, float]],
                    tag_universe: Sequence[str],
                    thetas: Sequence[float],
                    ks: Sequence[int], *, meta: dict) -> dict:
    """干跑结果 → report.json 结构（前端 Task 7 契约，report.md 同数据）。

    样本构造（spec §5/§7 钉死）：每部 ok 片一条 ClosedSample——

    - `ranked` = 产出行中 tag_universe 内的条目，防御性再排
      (-sf, tag_id)（produced 本已排好，但本函数是公共入口，不信任输入序）；
      交集外产出条目剔除、计入 `sample_accounting.no_gt`（白给 precision /
      冤枉 recall 都不要）；失败片 produced 已清空，只进 failed_movies；
    - `relevance` = gt 影片向量限 tag_universe（空表 → evaluate_closed 记
      invalid，不进任何平均）；
    - `system_scores` = {tag_id: sf}（Spearman 里未产出的交集标签按 0 分
      补位）。

    先验基线（钉死）：先验序 = `_prior_ranking`，每部 ok 片用**同一
    ranked** 构造先验样本、同机制喂 `evaluate_closed`——基线与系统吃完全
    相同的指标管线，格值才可比。

    banner / warning 语义：status != succeeded 或 done < total → 部分运行
    banner（取消/中止/缩量跑都不可与全量横比）；失败率（failed/total）
    > 10% → 小样本警示，否则 None。
    """
    universe = set(tag_universe)
    n_failed = sum(1 for r in results if not r.ok)
    no_gt = 0
    samples: list[ClosedSample] = []
    per_movie: list[dict] = []
    for result in results:
        if not result.ok:
            continue
        rows = [row for row in result.produced
                if row.get("tag_id") in universe]
        no_gt += len(result.produced) - len(rows)
        rows.sort(key=lambda row: (-float(row.get("sf", 0.0)),
                                   row["tag_id"]))
        system_scores = {row["tag_id"]: float(row.get("sf", 0.0))
                         for row in rows}
        relevance = {tag: score for tag, score
                     in gt.get(result.tt, {}).items() if tag in universe}
        samples.append(ClosedSample(
            ranked=tuple(row["tag_id"] for row in rows),
            relevance=relevance, system_scores=system_scores,
            label=result.tt))
        if relevance:  # 无交集影片 Spearman 算不出，不入 per_movie
            keys = sorted(relevance)
            per_movie.append({"tt": result.tt, "spearman": spearman(
                [system_scores.get(tag, 0.0) for tag in keys],
                [relevance[tag] for tag in keys])})
    closed = evaluate_closed(samples, thetas, ks)

    prior_ranked, prior_means = _prior_ranking(gt, tag_universe)
    prior_closed = evaluate_closed(
        [ClosedSample(ranked=prior_ranked, relevance=s.relevance,
                      system_scores={tag: prior_means[tag]
                                     for tag in s.relevance},
                      label=s.label)
         for s in samples], thetas, ks)

    total = meta.get("total")
    if not isinstance(total, int):
        total = len(results)
    done = meta.get("done")
    if not isinstance(done, int):
        done = len(results)
    banner = (None
              if meta.get("status") == "succeeded" and done >= total
              else BANNER_PARTIAL)
    warning = None
    if total and n_failed / total > 0.1:
        warning = (f"失败率 {n_failed / total:.0%}（{n_failed}/{total}）超过"
                   " 10%：成功样本少，指标波动大，勿单独引用；失败影片见"
                   " failed_movies")
    return {
        "banner": banner,
        "sample_accounting": {"total": total, "ok": len(samples),
                              "failed": n_failed,
                              "invalid": closed.n_invalid, "no_gt": no_gt},
        "grid": _grid_rows(closed.grid),
        "baselines": {"prior": _grid_rows(prior_closed.grid)},
        "coverage": {"min": closed.coverage_min,
                     "median": closed.coverage_median,
                     "max": closed.coverage_max,
                     "values": closed.coverage},
        "spearman": {"macro": closed.spearman_macro, "per_movie": per_movie},
        "failed_movies": [{"tt": r.tt, "error": r.error}
                          for r in results if not r.ok],
        "warning": warning,
        "calibre": {
            "llm_model": meta.get("llm_model", ""),
            "embedding_model": meta.get("embedding_model", ""),
            "prompt_version": (meta.get("prompt") or {}).get("version"),
            "library_version": (meta.get("library") or {}).get("version"),
            "dataset_id": (meta.get("dataset") or {}).get("id"),
            "gt_sha8": (meta.get("dataset") or {}).get("gt_sha8"),
        },
        "note": NOTE_SILVER,
    }


def _fmt(value) -> str:
    """md 数值格：None → 暂无（None≠0 纪律的报表侧），浮点保留三位。"""
    return "暂无" if value is None else f"{value:.3f}"


def _fmt_ci(ci) -> str:
    """md 置信区间格：[lo, hi]；None → 暂无。"""
    return "暂无" if not ci else f"[{ci[0]:.3f}, {ci[1]:.3f}]"


def _fmt_cal(value) -> str:
    """md 口径格：None / 空串 → 暂无，其余原样。"""
    return "暂无" if value is None or value == "" else str(value)


def render_report_md(report: dict) -> str:
    """report dict → 人读 markdown（report.md，与 report.json 同数据）。

    头部固定渲染 banner / warning（部分口径不藏在正文里）与口径三件套
    （calibre）+ 银标准警示（note）；θ×K 网格与先验基线并排两张表，
    覆盖率/Spearman/失败清单各一节。None 一律「暂无」。
    """
    lines: list[str] = ["# 闭环排序评测报告", ""]
    if report.get("banner"):
        lines += [f"> ⚠ {report['banner']}", ""]
    if report.get("warning"):
        lines += [f"> ⚠ {report['warning']}", ""]
    acc = report["sample_accounting"]
    lines += [
        (f"样本核算：共 {acc['total']} 部——成功 {acc['ok']}、失败"
         f" {acc['failed']}、无交集不可评 {acc['invalid']}；产出行落在"
         f" 库∩gt 交集外 {acc['no_gt']} 条（不计分只计数）。"),
        "",
        "## θ×K 网格（宏平均 ± 95% 自举 CI）",
        "",
        "| θ | K | n | P@K | R@K | nDCG@K | 随机P | 随机R |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for g in report["grid"]:
        lines.append(
            f"| {g['theta']} | {g['k']} | {g['n_valid']} | "
            f"{_fmt(g['precision'])} {_fmt_ci(g['precision_ci'])} | "
            f"{_fmt(g['recall'])} {_fmt_ci(g['recall_ci'])} | "
            f"{_fmt(g['ndcg'])} {_fmt_ci(g['ndcg_ci'])} | "
            f"{_fmt(g['precision_random'])} | {_fmt(g['recall_random'])} |")
    lines += [
        "",
        "## 先验基线（tag_universe 按 gt 全影片均值降序，同机制并排）",
        "",
        "| θ | K | n | P@K | R@K | nDCG@K |",
        "|---|---|---|---|---|---|",
    ]
    for g in report["baselines"]["prior"]:
        lines.append(
            f"| {g['theta']} | {g['k']} | {g['n_valid']} | "
            f"{_fmt(g['precision'])} | {_fmt(g['recall'])} | "
            f"{_fmt(g['ndcg'])} |")
    coverage = report["coverage"]
    lines += [
        "",
        "## 覆盖率（产出行数 / 交集标签数）",
        "",
        (f"- min {_fmt(coverage['min'])} / 中位 {_fmt(coverage['median'])}"
         f" / max {_fmt(coverage['max'])}"
         f"（{len(coverage['values'])} 部有交集）——R@K 上限被产出行数封顶，"
         "读召回必须同读覆盖率。"),
        "",
        "## Spearman 秩相关（系统分 vs gt 分，未产出交集标签按 0 分补位）",
        "",
        f"- 宏平均：{_fmt(report['spearman']['macro'])}",
        "",
        "| tt | Spearman |",
        "|---|---|",
    ]
    for row in report["spearman"]["per_movie"]:
        lines.append(f"| {row['tt']} | {_fmt(row['spearman'])} |")
    if report["failed_movies"]:
        lines += ["", f"## 失败影片（{len(report['failed_movies'])} 部）", "",
                  "| tt | 错误 |", "|---|---|"]
        for row in report["failed_movies"]:
            lines.append(f"| {row['tt']} | {row['error']} |")
    cal = report["calibre"]
    lines += [
        "",
        "## 口径",
        "",
        (f"- LLM `{_fmt_cal(cal['llm_model'])}`；嵌入"
         f" `{_fmt_cal(cal['embedding_model'])}`；prompt"
         f" v{_fmt_cal(cal['prompt_version'])}；标签库"
         f" v{_fmt_cal(cal['library_version'])}"),
        (f"- 数据集 `{_fmt_cal(cal['dataset_id'])}`（gt sha8"
         f" `{_fmt_cal(cal['gt_sha8'])}`）"),
        "",
        f"> {report['note']}",
        "",
    ]
    return "\n".join(lines)


def write_report(run_dir: Path, report: dict) -> None:
    """报告三件套落盘：report.json / report.md / metrics.csv。

    长表行 `theta,k,metric,macro,ci_low,ci_high,random_baseline`（系统
    网格；None → 空格，ndcg 无解析随机基线）。写序在 meta/raw 之后
    （worker 终态序）；读侧（history）对缺失/截断的 report.json 容错。
    """
    _dump_json(run_dir / "report.json", report)
    (run_dir / "report.md").write_text(render_report_md(report),
                                       encoding="utf-8")
    with open(run_dir / "metrics.csv", "w", encoding="utf-8",
              newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for g in report["grid"]:
            for metric, random_key in (("precision", "precision_random"),
                                       ("recall", "recall_random"),
                                       ("ndcg", None)):
                ci = g.get(f"{metric}_ci")
                writer.writerow([
                    g["theta"], g["k"], metric, g[metric],
                    ci[0] if ci else "", ci[1] if ci else "",
                    g[random_key] if random_key else "",
                ])
