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

起跑校验（spec 钉死顺序）→ daemon worker 持线程池逐片生成 → 终态落盘。
每片走与正式生成**零漂移**的路径：`assemble_spec`（同一组装段，读激活
closed 模板）→ `ClosedTagProducer.produce`（producer 本就不落库——落库
是正式执行器的职责），所以干跑全程对业务表只读（DB 零写入红线），评测
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
import os
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from tagplatform.eval.closed_ranking import (
    DEFAULT_KS,
    DEFAULT_THETAS,
    ClosedSample,
    evaluate_closed,
    spearman,
)
from tagplatform.eval_dataset import parse_movie_content, read_movie_data
from tagplatform.eval_runs import (
    ABORT_CONSECUTIVE,
    DEFAULT_CONCURRENCY,
    RETRY_PER_MOVIE,
    PRESET_SEED,
    PRESET_SIZE,
    EvalJob,
    EvalRunService,
    EvalValidationError,
    MovieResult,
    RunActiveError,
    RunBusyError,
    RunState,
    dump_json,
    select_movies,
)
from tagplatform.generation import get_producer
from tagplatform.library import try_load_library
from tagplatform.prompts import get_active_prompt
from tagplatform.schemas import ContentJSON
from tagplatform.settings import Settings
from tagplatform.tasks.runner import assemble_spec

__all__ = [
    "ABORT_CONSECUTIVE",
    "DEFAULT_CONCURRENCY",
    "RETRY_PER_MOVIE",
    "PRESET_SEED",
    "PRESET_SIZE",
    "DatasetInfo",
    "EvalValidationError",
    "MovieResult",
    "RunActiveError",
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

# gt CSV 的合法首行（spec §5：统一三列 movieId,tagId,relevance）
_GT_HEADER = ["movieId", "tagId", "relevance"]

# gt 表缓存：path → (mtime, {movieId: {tagId: relevance}})
_GT_CACHE: dict[str, tuple[float, dict[str, dict[str, float]]]] = {}
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
    不对 → 整份文件判无效（调用方跳过）。按 (path, mtime) 进程内缓存。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _GT_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
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
    _GT_CACHE[path] = (mtime, table)
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


def load_movie_content(root: str, tt: str) -> ContentJSON:
    """读 `data/<tt>/content.json` → 平台内部 ContentJSON。

    读 JSON 后先改 `speak_id` → `speaker_id`，再交 `content_io.load_content`
    （上游形态自动识别转换；平台内部形态的旧数据同样可读）。文件缺失由
    open 抛 FileNotFoundError（调用方按"该片不可评"处理）。
    """
    return parse_movie_content(read_movie_data(Path(root) / "data" / tt))


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

# ---------------------------------------------------------------- 干跑运行器（服务层）

@dataclass(frozen=True)
class _ClosedJob(EvalJob):
    gt: dict[str, dict[str, float]]
    tag_universe: list[str]


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


class ClosedEvalService(EvalRunService[_ClosedJob, MovieResult]):
    """闭环评测复用生产组装和生成器，模型执行期间不占用数据库连接。"""

    def __init__(self, session_factory, llm, embedder, settings: Settings) -> None:
        super().__init__(settings.eval_output_root, settings.eval_dataset_root)
        self.settings = settings
        self._session_factory = session_factory
        self._llm = llm
        self._embedder = embedder

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
        library = try_load_library()
        if library is None:
            raise EvalValidationError("标签库未就绪，无法启动闭环评测")
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
        prompt_version = self._active_prompt_version()

        def make_job(run_id: str, started_at: str) -> _ClosedJob:
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
            return _ClosedJob(run_id=run_id, movies=selected, gt=gt,
                              tag_universe=sorted(library_ids & gt_tags),
                              concurrency=concurrency, started_at=started_at,
                              meta=meta, dataset_root=root)

        return self._launch(selected, make_job, wait=wait)

    @staticmethod
    def _read_summary(run_dir: Path) -> dict:
        """report.json → 历史摘要四键（θ=0.4、K=10/20；无报告/无该档 → None）。"""
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
            for point in grid:
                if not isinstance(point, dict):
                    continue
                theta = point.get("theta")
                if (not isinstance(theta, (int, float))
                        or abs(theta - 0.4) > 1e-9):
                    continue
                if point.get("k") == 10:
                    summary["ndcg10"] = point.get("ndcg")
                    summary["p10"] = point.get("precision")
                elif point.get("k") == 20:
                    summary["r20"] = point.get("recall")
        coverage = report.get("coverage")
        if isinstance(coverage, dict):
            summary["coverage_median"] = coverage.get("median")
        return summary

    # ---------------------------------------------------------------- worker

    def _run_movie(self, job: _ClosedJob, tt: str) -> MovieResult:
        """单片干跑（线程池内执行）：共 1+RETRY_PER_MOVIE 次尝试。

        每次尝试独立会话（异常后不依赖会话状态）：load → assemble_spec
        （mode="closed"，读激活模板）→ produce（不写库）→ 产出按
        (-sf, tag_id) 排序。**会话在 assemble 后立即归还**：produce 含
        全部 LLM 调用（分钟级）不得持有 DB 连接（池饿死事故 2026-11-21：
        并发 16 > QueuePool 5+10，会话横跨 produce 把池占死、平台 API 全
        超时）；closed producer 不消费 session（全读 spec.library，assemble
        已装好），第二参传 None——协议签名不动，正式执行器照常传真会话。
        内容从起跑快照根加载（dataset_root）——运行中切 override 不影响
        本 run。
        """
        started = time.perf_counter()
        title = ""
        error: str | None = None
        produced: list[dict] = []
        for _attempt in range(1 + RETRY_PER_MOVIE):
            try:
                with self._session_factory() as session:
                    content = load_movie_content(job.dataset_root, tt)
                    title = content.asset.title
                    spec, _template = assemble_spec(
                        session, mode="closed", content=content,
                        task_id=f"eval-{job.run_id}", asset_id=tt,
                        run_id=f"{job.run_id}:{tt}")
                # produce 传 None（见 docstring）：未来 producer 改用会话
                # 会以 AttributeError 显式失败，不静默漂移
                tags = get_producer("closed").produce(
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

    def _write_artifacts(self, job: _ClosedJob, results: list[MovieResult],
                         status: str, error: str | None, finished_at: str) -> None:
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
            "finished_at": finished_at,
        })
        if error is not None:
            meta["error"] = error
        rows = [{"tt": r.tt, "title": r.title, "ok": r.ok, "error": r.error,
                 "produced": r.produced, "gt": job.gt.get(r.tt, {}),
                 "elapsed_ms": r.elapsed_ms}
                for r in results]
        dump_json(run_dir / "meta.json", meta)
        dump_json(run_dir / "raw_results.json", rows)
        report = assemble_report(results, job.gt, job.tag_universe,
                                 job.meta.get("thetas") or DEFAULT_THETAS,
                                 job.meta.get("ks") or DEFAULT_KS,
                                 meta=meta)
        write_report(run_dir, report)

    def _active_prompt_version(self) -> int | None:
        """读激活 closed 模板版本（meta 口径记录）；读不到返回 None。

        不放进 start 校验段——模板缺失时每片会在 assemble_spec 失败并走
        单片失败/中止路径，起跑校验保持 spec 钉死的五步。
        """
        try:
            with self._session_factory() as session:
                return get_active_prompt(session, "closed").version
        except Exception:  # noqa: BLE001 —— 口径缺失不拦截起跑，记 None
            return None

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
    dump_json(run_dir / "report.json", report)
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
