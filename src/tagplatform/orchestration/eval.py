"""评测域编排（闭环 13 个 + 开放 7 个 admin 用例）——orchestration/eval 域。

用例清单（原 api/routers/eval.py 的装配与读侧容错，Task 11 原样下沉；
开放评测 7 用例随 open-eval Task 6 追加——错误映射与 `_run_dir` 守卫
逐条镜像闭环同名函数，数据集族直接复用闭环函数零新逻辑）。闭环服务层
是 tagplatform.services.eval.service.ClosedEvalService，开放服务层是
tagplatform.services.eval.open_service.OpenEvalService（数据集扫描/
干跑运行器/报告与历史，DB 零写入；SQL 装配零语义变化）：

- `get_dataset_root`     当前生效数据集根（override ?? .env 默认）
- `set_dataset_root`     设置数据集根（目录存在 + 扫描出数据集才生效）
- `list_datasets`        可评数据集列表（overlap 对当前标签库）
- `list_dataset_movies`  数据集 usable 影片清单（选片列表）
- `create_eval_run`      起跑（错误映射集中处：404 → 400 → busy 409）
- `list_eval_runs`       历史 run 摘要（磁盘扫描）
- `get_eval_run`         run 进度详情（注册表优先、磁盘兜底）
- `cancel_eval_run`      请求取消（幂等 {ok}）
- `delete_eval_run`      删除终态 run（活跃 409 / 不存在 404）
- `get_run_report`       报告 JSON（缺失/截断 404，不 500）
- `get_run_file`         产物下载（白名单五件套）
- `get_run_movies`       run 逐片摘要（badcase 视图主表，θ 门可拆）
- `get_run_movie_detail` 单片明细（摘要行展开懒加载三表）

开放评测（open-eval Task 6，读侧走 settings.open_eval_output_root）：

- `create_open_run`      开放评测起跑（404 → 400 → busy 409，busy 含
                         跨服务互斥——detail 带对端活跃 run_id）
- `list_open_runs`       开放评测历史行（磁盘扫描 + 摘要五键，None 安全）
- `get_open_run`         开放评测 run 进度详情（注册表优先、磁盘兜底）
- `cancel_open_run`      请求取消（幂等 {ok}）
- `delete_open_run`      删除终态 run（活跃 409 / 不存在 404）
- `get_open_run_report`  开放评测报告 JSON（缺失/截断 404，不 500）
- `get_open_run_file`    开放评测产物下载（白名单五件套）
- `get_open_movie_diagnostics` 单片诊断（生产审计 + 评审原始响应）

run_id 守卫（`_run_dir` / `_open_run_dir` 路径越界防拼接）、读侧容错
（非原子写序窗口不 500）随逻辑下沉本层原样保留；数据集三别名
（/open/dataset-root、/open/datasets、/open/datasets/{id}/movies）与
闭环共用上述 dataset 用例——override 状态挂在闭环服务上，open 路由
Depends(get_closed_eval) 直读，一处写入两端可见。
"""

import json
import re
from pathlib import Path
from typing import Sequence

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tagplatform.services.eval.closed_ranking import DEFAULT_KS, DEFAULT_THETAS
from tagplatform.services.eval.dataset import movie_entries, scan_datasets
from tagplatform.services.eval.movie_breakdown import breakdown_movie, summarize_movies
from tagplatform.services.eval.open_report import (
    OPEN_REPORT_SCHEMA_VERSION,
    upgrade_open_report,
)
from tagplatform.services.eval.open_service import (
    DEFAULT_CONCURRENCY as OPEN_DEFAULT_CONCURRENCY,
    EvalValidationError as OpenEvalValidationError,
    RunActiveError as OpenRunActiveError,
    RunBusyError as OpenRunBusyError,
)
from tagplatform.services.eval.service import (
    DEFAULT_CONCURRENCY,
    EvalValidationError,
    RunActiveError,
    RunBusyError,
)
from tagplatform.services.tags.library import try_load_library
from tagplatform.settings import Settings

# files 端点白名单（plan 钉死五件套）：report.json 有专用 /report 端点、
# 不在白名单——报告结构化读走 JSON 端点，文件端点只供下载原始产物
_FILE_WHITELIST = ("report.md", "metrics.csv", "raw_results.json",
                   "meta.json", "progress.jsonl")


# ---------------------------------------------------------------- 响应模型


class DatasetRootOut(BaseModel):
    """当前生效根与是否 .env 默认（重启后 override 消失 → 回落默认）。"""

    root: str
    is_default: bool


class DatasetInfoOut(BaseModel):
    """数据集视图：usable_movies 输出为**数量**（服务层是清单——列表页只要
    计数，选片清单走 /movies 端点）。"""

    id: str
    gt_path: str
    n_movies: int
    n_tags: int
    library_overlap: int
    usable_movies: int


class MovieRefOut(BaseModel):
    """影片引用（前端选片列表条目）。"""

    tt: str
    title: str


class RunCreatedOut(BaseModel):
    """起跑受理：仅 run_id（进度经 GET /runs/{run_id} 轮询）。"""

    run_id: str


class RunStateOut(BaseModel):
    """run 进度快照（服务层 RunState 形；磁盘兜底行的 done/total/failed
    允许缺省——损坏 meta 不该把详情端点打成 500）。finished_at 终态时刻，
    运行中/老 run → None。"""

    run_id: str
    status: str
    done: int | None = None
    total: int | None = None
    failed: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class RunSummaryOut(BaseModel):
    """历史行（history()）：摘要四键按统一取格约定出（p10/ndcg10 @θ≈0.2、
    r20 @θ≈0.8 的最近有值格，custom-θ run 回退；无报告/无有值格 → None
    宁缺勿崩）；finished_at 终态时刻（meta 终态写盘带出，老 run 无 → None）。"""

    run_id: str
    dataset_id: str | None = None
    status: str
    done: int | None = None
    total: int | None = None
    failed: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    ndcg10: float | None = None
    p10: float | None = None
    r20: float | None = None
    coverage_median: float | None = None


class OpenRunSummaryOut(BaseModel):
    """开放评测历史行（OpenEvalService.history()）：基础行字段与闭环同形；
    摘要五键（pass_rate / new_pass / hit_pass / new_ratio / three_high）
    由 open_report._read_open_summary 读 report.json 出（报告缺失/截断 →
    对应键 None 宁缺勿假，None 安全）。闭环侧的 θ/排序指标不在开放口径
    内，不设字段。"""

    run_id: str
    dataset_id: str | None = None
    status: str
    done: int | None = None
    total: int | None = None
    failed: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    pass_rate: float | None = None
    new_pass: float | None = None
    hit_pass: float | None = None
    new_ratio: float | None = None
    three_high: float | None = None


class MovieSummaryRowOut(BaseModel):
    """单片摘要行（badcase 视图主表行）：指标 float | None（None=算不出，
    前端「暂无」——θ-空相关集/无交集/失败片）；n_produced 为原始产出条数
    含空间外；n_relevant 为 gt∩库 ≥θ 计数（失败片也照数）。行序由服务层
    给出：失败置顶 → p_at_k 升序 → null 垫底 → 同分按 tt。"""

    tt: str
    title: str | None = None
    ok: bool | None = None
    error: str | None = None
    spearman: float | None = None
    p_at_k: float | None = None
    r_at_k: float | None = None
    elapsed_ms: int | None = None
    n_produced: int
    n_relevant: int


class MovieSummaryOut(BaseModel):
    """单片摘要包层（增补 2026-11-22 拆 θ）：theta_p/theta_r 为 P 门与
    R 门各自的实际取值（query 参数或统一取格约定的缺省 θ≈0.2/θ≈0.8）；
    thetas 回传
    该 run 的网格档（前端选择器的选项源）；k_p/k_r 仍是确定选取
    （P 看 top-10、R 看 top-20）。前端表头按这些键拼字面量
    （P@{k_p}(θ{theta_p}) / R@{k_r}(θ{theta_r})）。"""

    theta_p: float
    theta_r: float
    k_p: int
    k_r: int
    thetas: list[float]
    rows: list[MovieSummaryRowOut]


class ProducedTagOut(BaseModel):
    """计分清单条目（GT 空间内、按 -sf 序）：hit = gt_score 非 None 且
    ≥ theta_r；tag_name 沿用产出原文（misses 才做库回填）。"""

    tag_id: str
    tag_name: str | None = None
    sf: float | None = None
    confidence: float | None = None
    evidence_count: int | None = None
    gt_score: float | None = None
    hit: bool


class OutOfSpaceTagOut(BaseModel):
    """空间外产出条目：不在本批 GT 空间（不计分只列名，不占 P@K 的坑）。"""

    tag_id: str
    tag_name: str | None = None
    sf: float | None = None


class MissTagOut(BaseModel):
    """漏检条目：相关（gt∩库 ≥θ）未产出，按 gt 分降序；tag_name 从当前
    标签库回填（库缺该标签 → None）。"""

    tag_id: str
    tag_name: str | None = None
    gt_score: float


class MovieDetailOut(BaseModel):
    """单片明细（摘要行展开懒加载）：produced/out_of_space/misses 三表；
    失败片三表全空、error 原样；theta_p/theta_r 与摘要包层同源（明细侧
    hit/misses 分类跟 theta_r，theta_p 只回显）。"""

    tt: str
    title: str | None = None
    ok: bool | None = None
    error: str | None = None
    elapsed_ms: int | None = None
    theta_p: float
    theta_r: float
    k_p: int
    k_r: int
    produced: list[ProducedTagOut]
    out_of_space: list[OutOfSpaceTagOut]
    misses: list[MissTagOut]


# ---------------------------------------------------------------- 内部装配


def _library_tag_ids() -> list[str]:
    """当前标签库的全部 tag_id（库未就绪 → 空表：overlap 全 0，如实反映）。"""
    library = try_load_library()
    return [tag.tag_id for tag in library.tags()] if library else []


def _library_tag_map() -> dict[str, str]:
    """当前标签库 tag_id → tag_name 映射（库未就绪 → 空表：misses 名称
    留空、相关性按空库如实反映——与 _library_tag_ids 同款容错）。"""
    library = try_load_library()
    return ({tag.tag_id: tag.tag_name for tag in library.tags()}
            if library else {})


def _dataset_or_404(root: str, dataset_id: str):
    """dataset_id 先对 scan_datasets 结果校验再透传；未知 → 404。

    root 为当前生效根（service.effective_root）——页面级 override 后，
    数据集的存在性以新根为准。
    """
    info = next((i for i in scan_datasets(root, set())
                 if i.id == dataset_id), None)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail=f"评测数据集不存在或不可解析：{dataset_id!r}")
    return info


def _run_dir(settings: Settings, run_id: str) -> Path | None:
    """run 目录守卫：解析后必须仍在 eval_output_root 内（防 run_id 形态的
    路径拼接越界——`..`、绝对路径、分隔符变体一律 None → 404）。"""
    root = Path(settings.eval_output_root)
    candidate = (root / run_id).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _open_run_dir(settings: Settings, run_id: str) -> Path | None:
    """开放评测 run 目录守卫（open-eval Task 6）：逻辑镜像闭环 `_run_dir`，
    输出根换成 settings.open_eval_output_root。"""
    root = Path(settings.open_eval_output_root)
    candidate = (root / run_id).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _run_state_out(state) -> RunStateOut:
    """服务层 RunState → 响应模型（逐字段搬运，不做加工）。"""
    return RunStateOut(run_id=state.run_id, status=state.status,
                       done=state.done, total=state.total,
                       failed=state.failed, started_at=state.started_at,
                       finished_at=state.finished_at, error=state.error)


def _movies_read(settings: Settings,
                 run_id: str) -> tuple[list[dict], Sequence[float],
                                       Sequence[int]]:
    """单片明细两端点的共同读侧（协议转换，模式同 get_run_report）：

    - run 目录守卫不过 → 404「run 不存在」（与 GET /runs/{run_id} 同口径）；
    - raw_results.json 缺失/截断/非 list、或行级不是带 tt 键的 dict（残缺
      行会在指标计算处 KeyError/AttributeError 打 500）→ 404「无单片明细」
      （运行中 run 与 meta→raw 写序非原子窗口的既定容错，不 500）；
    - meta.json 缺失/损坏 → 档位回落 DEFAULT_THETAS / DEFAULT_KS；thetas
      与 ks 两键独立读取独立回落（缺一不影响另一——老 run 半残 meta
      照常出数）。
    """
    run_dir = _run_dir(settings, run_id)
    if run_dir is None:
        raise HTTPException(status_code=404,
                            detail=f"评测 run 不存在：{run_id!r}")
    try:
        rows = json.loads((run_dir / "raw_results.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = None
    if not (isinstance(rows, list)
            and all(isinstance(r, dict) and "tt" in r for r in rows)):
        raise HTTPException(
            status_code=404,
            detail=f"该 run 无单片明细（未完成或产物缺失）：{run_id!r}")
    thetas: Sequence[float] = DEFAULT_THETAS
    ks: Sequence[int] = DEFAULT_KS
    try:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    if isinstance(meta, dict):
        meta_thetas = meta.get("thetas")
        if (isinstance(meta_thetas, list) and meta_thetas
                and all(isinstance(x, (int, float)) for x in meta_thetas)):
            thetas = meta_thetas
        meta_ks = meta.get("ks")
        if (isinstance(meta_ks, list) and meta_ks
                and all(isinstance(x, int) for x in meta_ks)):
            ks = meta_ks
    return rows, thetas, ks


def _theta_or_400(thetas: Sequence[float], value: float, name: str) -> float:
    """query 传入的 θ 必须是 run 网格档之一（浮点相等比较；增补
    2026-11-22）；否则 400 带合法值清单——语义校验集中本层（模式同
    set_dataset_root 的目录校验段），pydantic 只管 float 形状。"""
    if not any(value == t for t in thetas):
        raise HTTPException(
            status_code=400,
            detail=f"{name} 需为该 run 网格档之一：{list(thetas)}")
    return value


# ---------------------------------------------------------------- 用例


def get_dataset_root(service) -> DatasetRootOut:
    """当前生效的数据集根（override ?? .env 默认）与是否默认。"""
    return DatasetRootOut(root=service.effective_root,
                          is_default=service.dataset_root_override is None)


def set_dataset_root(service, root: str) -> DatasetRootOut:
    """设置数据集根（进程内 override，重启回落 .env 默认）。

    校验序：目录存在（is_dir）→ 扫描出 ≥1 个有效数据集；任一不过 →
    400 带原因且不变更生效值。运行中的 run 不受影响（起跑时已快照根）。
    """
    if not Path(root).is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"数据集根目录不存在或不是目录：{root}")
    if not scan_datasets(root, set()):
        raise HTTPException(
            status_code=400,
            detail=f"目录 {root} 下未扫描到有效数据集（需 gt 三列 CSV）")
    service.dataset_root_override = root
    return DatasetRootOut(root=service.effective_root, is_default=False)


def list_datasets(service) -> list[DatasetInfoOut]:
    """可评数据集列表（畸形 gt 已在扫描层跳过；overlap 对当前标签库）。"""
    infos = scan_datasets(service.effective_root, _library_tag_ids())
    return [DatasetInfoOut(id=i.id, gt_path=i.gt_path, n_movies=i.n_movies,
                           n_tags=i.n_tags, library_overlap=i.library_overlap,
                           usable_movies=len(i.usable_movies))
            for i in infos]


def list_dataset_movies(service, dataset_id: str) -> list[MovieRefOut]:
    """数据集的 usable 影片清单（选片列表用）；未知 dataset → 404。"""
    _dataset_or_404(service.effective_root, dataset_id)
    try:
        entries = movie_entries(service.effective_root, dataset_id)
    except ValueError:  # 扫描后被删/被改坏的窄窗口 → 同 404 口径
        raise HTTPException(
            status_code=404,
            detail=f"评测数据集不存在或不可解析：{dataset_id!r}")
    return [MovieRefOut(**entry) for entry in entries]


def create_eval_run(
    service,
    dataset: str,
    movies: dict,
    thetas: Sequence[float] | None,
    ks: Sequence[int] | None,
    concurrency: int | None,
) -> RunCreatedOut:
    """起跑（错误映射集中处）：未知 dataset 404 → 校验失败 400 → busy 409。"""
    _dataset_or_404(service.effective_root, dataset)
    try:
        run_id = service.start(
            dataset, movies,
            thetas=(thetas if thetas is not None else DEFAULT_THETAS),
            ks=(ks if ks is not None else DEFAULT_KS),
            concurrency=(concurrency if concurrency is not None
                         else DEFAULT_CONCURRENCY))
    except EvalValidationError as exc:  # 选片越权/库未就绪/零交集/并发非法
        raise HTTPException(status_code=400, detail=str(exc))
    except RunBusyError as exc:  # 单进程同时只允许一个 run
        raise HTTPException(status_code=409, detail=str(exc))
    return RunCreatedOut(run_id=run_id)


def list_eval_runs(service) -> list[RunSummaryOut]:
    """历史 run 摘要（磁盘扫描，started_at 降序；摘要档无 → None）。"""
    return [RunSummaryOut(**row) for row in service.history()]


def get_eval_run(service, run_id: str) -> RunStateOut:
    """run 进度详情：注册表优先（活值）；磁盘历史兜底（上一进程的
    run/中断 run）；两处都没有 → 404。"""
    state = service.get(run_id)
    if state is not None:
        return _run_state_out(state)
    row = next((r for r in service.history() if r["run_id"] == run_id), None)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"评测 run 不存在：{run_id!r}")
    return RunStateOut(run_id=row["run_id"], status=row["status"],
                       done=row["done"], total=row["total"],
                       failed=row["failed"], started_at=row["started_at"],
                       finished_at=row["finished_at"], error=row["error"])


def cancel_eval_run(service, run_id: str) -> dict:
    """请求取消 → {ok}：受理 true（worker 不再提交新片，在途片跑完）；
    run 不存在或已终态 → false（幂等意图，重复取消不报错）。"""
    return {"ok": service.cancel(run_id)}


def delete_eval_run(service, run_id: str) -> dict:
    """删除终态 run（磁盘产物 + 进程内注册表）→ {ok: true}。

    错误映射：活跃 run（含终态落盘窄窗口）→ 409 带原因；不存在/已删 →
    404（与 GET 未知 run 同口径，重复删除即 404）。只动 eval_output_root
    下本 run 目录，DB 零写入红线不变。"""
    try:
        ok = service.delete(run_id)
    except RunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"评测 run 不存在：{run_id!r}")
    return {"ok": True}


def get_run_report(settings: Settings, run_id: str) -> dict:
    """报告 JSON（report.json 原样透出）：无报告/截断 → 404（读侧容错，
    不把非原子写序窗口打成 500）。"""
    run_dir = _run_dir(settings, run_id)
    if run_dir is not None:
        try:
            report = json.loads((run_dir / "report.json")
                                .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
        if isinstance(report, dict):
            return report
    raise HTTPException(status_code=404,
                        detail=f"该 run 无报告：{run_id!r}")


def get_run_file(settings: Settings, run_id: str, name: str) -> FileResponse:
    """产物下载（白名单五件套）：白名单外 404（report.json 走 /report 端点）；
    run 目录越界或文件不存在 → 404。"""
    if name not in _FILE_WHITELIST:
        raise HTTPException(status_code=404,
                            detail=f"评测产物文件不存在：{name!r}")
    run_dir = _run_dir(settings, run_id)
    path = run_dir / name if run_dir is not None else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404,
                            detail=f"评测产物文件不存在：{name!r}")
    return FileResponse(path, filename=name)


def get_run_movies(
    settings: Settings,
    run_id: str,
    theta_p: float | None,
    theta_r: float | None,
) -> MovieSummaryOut:
    """run 的逐片摘要（badcase 视图主表）：读盘现算（落盘结构零改动，
    历史 run 即时可用）——失败置顶、P@K 升序的坏例优先；θ 门可拆
    （增补 2026-11-22：theta_p/theta_r query，非法档 400 带合法清单），
    档位包层回传、前端表头按其拼字面量。"""
    rows, thetas, ks = _movies_read(settings, run_id)
    payload = summarize_movies(
        rows, thetas, ks, set(_library_tag_map()),
        theta_p=(_theta_or_400(thetas, theta_p, "theta_p")
                 if theta_p is not None else None),
        theta_r=(_theta_or_400(thetas, theta_r, "theta_r")
                 if theta_r is not None else None))
    return MovieSummaryOut(**payload)


def get_run_movie_detail(
    settings: Settings,
    run_id: str,
    tt: str,
    theta_p: float | None,
    theta_r: float | None,
) -> MovieDetailOut:
    """单片明细（摘要行展开懒加载）：计分清单（hit/gt_score）+ 空间外 +
    漏检三表；misses 的 tag_name 从当前标签库回填（库缺该标签 → None）；
    tt 只做 JSON 行内查找、不参与路径拼接，未知 tt → 404；θ 门同摘要
    端点（非法档 400）。"""
    rows, thetas, ks = _movies_read(settings, run_id)
    tag_map = _library_tag_map()
    detail = breakdown_movie(
        rows, tt, thetas, ks, set(tag_map), tag_map,
        theta_p=(_theta_or_400(thetas, theta_p, "theta_p")
                 if theta_p is not None else None),
        theta_r=(_theta_or_400(thetas, theta_r, "theta_r")
                 if theta_r is not None else None))
    if detail is None:
        raise HTTPException(status_code=404,
                            detail=f"影片不在该 run 明细中：{tt!r}")
    return MovieDetailOut(**detail)


# ---------------------------------------------------------------- 开放评测用例（open-eval Task 6）
#
# 与闭环同名函数逐条镜像：错误映射、`_run_dir` 守卫、读侧容错、白名单
# 全部同款，差异只有两处——服务层换成 OpenEvalService（busy 409 含跨
# 服务互斥：detail 带对端活跃 run_id，probe 语义见 open_service 模块
# docstring「跨服务互斥」）、读侧输出根换成 settings.open_eval_output_root。
# 数据集三别名不设新用例：open 路由 Depends(get_closed_eval) 直接调上面
# 的 get_dataset_root / set_dataset_root / list_datasets / list_dataset_movies
# （override 状态挂闭环服务，一处写入两端可见）。


def create_open_run(
    service,
    dataset: str,
    movies: dict,
    modalities: Sequence[str] | None,
    concurrency: int | None,
) -> RunCreatedOut:
    """开放评测起跑（错误映射集中处，镜像 create_eval_run）：未知 dataset
    404 → 服务层校验失败 400（选片越权/为空/modalities 非法/库未就绪/
    并发非法）→ busy 409（自有活跃或对端活跃——detail 含活跃 run_id）。
    modalities/concurrency 缺省 None → 服务端默认（("script", "dialogue")
    / OPEN_DEFAULT_CONCURRENCY）。"""
    _dataset_or_404(service.effective_root, dataset)
    try:
        run_id = service.start(
            dataset, movies,
            modalities=(tuple(modalities) if modalities is not None
                        else ("script", "dialogue")),
            concurrency=(concurrency if concurrency is not None
                         else OPEN_DEFAULT_CONCURRENCY))
    except OpenEvalValidationError as exc:  # 选片越权/为空/modalities/库未就绪
        raise HTTPException(status_code=400, detail=str(exc))
    except OpenRunBusyError as exc:  # 单进程同时只允许一个评测 run（含跨服务）
        raise HTTPException(status_code=409, detail=str(exc))
    return RunCreatedOut(run_id=run_id)


def list_open_runs(service) -> list[OpenRunSummaryOut]:
    """开放评测历史 run 摘要（磁盘扫描，started_at 降序；摘要五键无报告
    → None，history() 行已 None 安全——OpenRunSummaryOut 逐键直映射）。"""
    return [OpenRunSummaryOut(**row) for row in service.history()]


def get_open_run(service, run_id: str) -> RunStateOut:
    """开放评测 run 进度详情（镜像 get_eval_run）：注册表优先（活值）；
    磁盘历史兜底（上一进程的 run/中断 run）；两处都没有 → 404。"""
    state = service.get(run_id)
    if state is not None:
        return _run_state_out(state)
    row = next((r for r in service.history() if r["run_id"] == run_id), None)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"评测 run 不存在：{run_id!r}")
    return RunStateOut(run_id=row["run_id"], status=row["status"],
                       done=row["done"], total=row["total"],
                       failed=row["failed"], started_at=row["started_at"],
                       finished_at=row["finished_at"], error=row["error"])


def cancel_open_run(service, run_id: str) -> dict:
    """请求取消（镜像 cancel_eval_run）→ {ok}：受理 true（worker 不再提交
    新片，在途片跑完）；run 不存在或已终态 → false（幂等意图）。"""
    return {"ok": service.cancel(run_id)}


def rereview_open_run(service, run_id: str) -> dict:
    """复用产出与材料快照，另存重评结果。

    错误映射：活跃 run → 409；非终态/不存在 → 400。换评审模型或阈值后
    想看新口径效果时用，DB 零写入红线不变。"""
    try:
        return service.rereview(run_id)
    except OpenRunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except OpenEvalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def delete_open_run(service, run_id: str) -> dict:
    """删除终态 open run（磁盘产物 + 进程内注册表，镜像 delete_eval_run）。

    错误映射：活跃 run（含终态落盘窄窗口）→ 409 带原因；不存在/已删 →
    404（与 GET 未知 run 同口径，重复删除即 404）。只动
    open_eval_output_root 下本 run 目录，DB 零写入红线不变。"""
    try:
        ok = service.delete(run_id)
    except OpenRunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"评测 run 不存在：{run_id!r}")
    return {"ok": True}


def get_open_run_report(settings: Settings, run_id: str) -> dict:
    """开放评测报告 JSON（report.json 原样透出，镜像 get_run_report）：
    无报告/截断 → 404（读侧容错，不把非原子写序窗口打成 500）。

    旧 schema 报告先尝试按原始材料惰性升级（upgrade_open_report，含
    judge 数量漏斗等新段）；升级失败才 410。"""
    run_dir = _open_run_dir(settings, run_id)
    if run_dir is not None:
        # 终态 report.json 优先；运行中回落 live.json（评审逐条增量发布），
        # 前端轮询即可看到标签逐条出现。两者皆无 → 404。
        for name in ("report.json", "live.json"):
            try:
                report = json.loads((run_dir / name)
                                    .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = None
            if isinstance(report, dict):
                if report.get("schema_version") != OPEN_REPORT_SCHEMA_VERSION:
                    upgraded = upgrade_open_report(run_dir)
                    if upgraded is not None:
                        return upgraded
                    raise HTTPException(status_code=410, detail="旧版开放评测报告已停用")
                return report
    raise HTTPException(status_code=404,
                        detail=f"该 run 无报告：{run_id!r}")


# 影片号形态守卫：诊断端点按 tt 拼文件名，只认 IMDb 号（防路径拼接越界）
_TT_RE = re.compile(r"tt\d+")


def _read_json_object(path: Path) -> dict | None:
    """读一份 JSON 对象产物；缺失/截断/非对象一律 None（读侧容错，不 500）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def get_open_movie_diagnostics(settings: Settings, run_id: str, tt: str) -> dict:
    """单片诊断：生产审计（audit/<tt>.json）+ 评审原始响应（review/<tt>.json）。

    整片产出为 0 时报告里没有任何标签行，用户看不到"候选为什么全被淘汰"；
    本用例把两份现场原样透出，前端据此渲染候选去向。tt 形态非法、run 越界
    或两份都没有 → 404。
    """
    run_dir = _open_run_dir(settings, run_id)
    if run_dir is None or not _TT_RE.fullmatch(tt):
        raise HTTPException(status_code=404,
                            detail=f"评测影片不存在：{run_id!r}/{tt!r}")
    audit = _read_json_object(run_dir / "audit" / f"{tt}.json")
    review = _read_json_object(run_dir / "review" / f"{tt}.json")
    if audit is None and review is None:
        raise HTTPException(status_code=404,
                            detail=f"该影片无诊断产物：{run_id!r}/{tt!r}")
    return {"tt": tt, "audit": audit, "review": review}


def get_open_run_file(settings: Settings, run_id: str, name: str) -> FileResponse:
    """开放评测产物下载（镜像 get_run_file）：白名单五件套（report.json
    走 /report 端点不在白名单）；白名单外 404；run 目录越界或文件不存在
    → 404。"""
    if name not in _FILE_WHITELIST:
        raise HTTPException(status_code=404,
                            detail=f"评测产物文件不存在：{name!r}")
    run_dir = _open_run_dir(settings, run_id)
    path = run_dir / name if run_dir is not None else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404,
                            detail=f"评测产物文件不存在：{name!r}")
    return FileResponse(path, filename=name)
