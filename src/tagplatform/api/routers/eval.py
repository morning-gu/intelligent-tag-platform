"""闭环排序评测和开放生成评估的管理员端点。"""

import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from tagplatform.api import get_closed_eval
from tagplatform.api.depends import require_admin
from tagplatform.closed_eval import ClosedEvalService, movie_entries, scan_datasets
from tagplatform.db.models import UserAccount
from tagplatform.eval.closed_ranking import DEFAULT_KS, DEFAULT_THETAS
from tagplatform.library import try_load_library
from tagplatform.eval.tag_quality import DEFAULT_MODEL
from tagplatform.eval_runs import DEFAULT_CONCURRENCY, EvalValidationError, RunActiveError, RunBusyError
from tagplatform.open_eval import OpenEvalService, dataset_movie_entries, dataset_movies

router = APIRouter(prefix="/api/v1/eval/{mode}")

# files 端点白名单（plan 钉死五件套）：report.json 有专用 /report 端点、
# 不在白名单——报告结构化读走 JSON 端点，文件端点只供下载原始产物
_FILE_WHITELIST = ("report.md", "metrics.csv", "raw_results.json",
                   "meta.json", "progress.jsonl")


# ---------------------------------------------------------------- 请求/响应模型


class MovieSelection(BaseModel):
    """选片请求：mode 三态（preset30 固定 seed 抽样 / all / custom 显式清单），
    ids 仅 custom 需要（越权/为空由服务层校验 → 400）。"""

    mode: Literal["preset30", "all", "custom"]
    ids: list[str] | None = None


class StartRunRequest(BaseModel):
    """起跑请求：thetas/ks/concurrency 缺省 None → 服务端默认
    （DEFAULT_THETAS / DEFAULT_KS / DEFAULT_CONCURRENCY）。"""

    dataset: str
    movies: MovieSelection
    thetas: list[float] | None = None
    ks: list[int] | None = None
    concurrency: int | None = None
    quality_model: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ] | None = None


class SetDatasetRootRequest(BaseModel):
    """设置数据集根请求：非空字符串（空串 → 422 形状拒绝）；
    目录存在/扫描出有效数据集由 router 校验（不过 → 400 带原因）。"""

    root: str = Field(min_length=1)


class DatasetRootOut(BaseModel):
    """当前生效根与是否 .env 默认（重启后 override 消失 → 回落默认）。"""

    root: str
    is_default: bool


class DatasetInfoOut(BaseModel):
    """数据集视图：usable_movies 输出为**数量**（服务层是清单——列表页只要
    计数，选片清单走 /movies 端点）。"""

    id: str
    gt_path: str | None = None
    n_movies: int
    n_tags: int | None = None
    library_overlap: int | None = None
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
    """历史行（history()）：摘要四键无报告/无该档 → None（宁缺勿崩）；
    finished_at 终态时刻（meta 终态写盘带出，老 run 无该键 → None）。"""

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
    quality_coverage: float | None = None
    quality_full_rate: float | None = None


# ---------------------------------------------------------------- 内部装配


def _get_eval_service(request: Request, mode: Literal["closed", "open"]) -> ClosedEvalService | OpenEvalService:
    if mode == "open":
        return request.app.state.open_eval
    return get_closed_eval(request)


EvalService = Annotated[ClosedEvalService | OpenEvalService, Depends(_get_eval_service)]


def _library_tag_ids() -> list[str]:
    """当前标签库的全部 tag_id（库未就绪 → 空表：overlap 全 0，如实反映）。"""
    library = try_load_library()
    return [tag.tag_id for tag in library.tags()] if library else []


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


def _require_open_dataset(root: str, dataset_id: str) -> None:
    if dataset_id != Path(root).resolve().name or not dataset_movies(root):
        raise HTTPException(status_code=404, detail=f"开放评估数据集不可用：{dataset_id!r}")


def _run_state_out(state) -> RunStateOut:
    """服务层 RunState → 响应模型（逐字段搬运，不做加工）。"""
    return RunStateOut(run_id=state.run_id, status=state.status,
                       done=state.done, total=state.total,
                       failed=state.failed, started_at=state.started_at,
                       finished_at=state.finished_at, error=state.error)


# ---------------------------------------------------------------- 端点


@router.get("/dataset-root", response_model=DatasetRootOut)
def get_dataset_root(
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> DatasetRootOut:
    """当前生效的数据集根（override ?? .env 默认）与是否默认。"""
    return DatasetRootOut(root=service.effective_root,
                          is_default=service.dataset_root_override is None)


@router.post("/dataset-root", response_model=DatasetRootOut)
def set_dataset_root(
    payload: SetDatasetRootRequest,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> DatasetRootOut:
    """设置数据集根（进程内 override，重启回落 .env 默认）。

    校验序：目录存在（is_dir）→ 扫描出 ≥1 个有效数据集；任一不过 →
    400 带原因且不变更生效值。运行中的 run 不受影响（起跑时已快照根）。
    """
    root = payload.root
    if not Path(root).is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"数据集根目录不存在或不是目录：{root}")
    if isinstance(service, OpenEvalService):
        if not dataset_movies(root):
            raise HTTPException(status_code=400, detail=f"目录 {root} 下未找到 data/<tt>/content.json")
    elif not scan_datasets(root, set()):
        raise HTTPException(
            status_code=400,
            detail=f"目录 {root} 下未扫描到有效数据集（需 gt 三列 CSV）")
    service.dataset_root_override = root
    return DatasetRootOut(root=service.effective_root, is_default=False)


@router.get("/datasets", response_model=list[DatasetInfoOut])
def list_datasets(
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> list[DatasetInfoOut]:
    """可评数据集列表（畸形 gt 已在扫描层跳过；overlap 对当前标签库）。"""
    root = service.effective_root
    if isinstance(service, OpenEvalService):
        movies = dataset_movies(root)
        return [DatasetInfoOut(id=Path(root).resolve().name, n_movies=len(movies),
                               usable_movies=len(movies))] if movies else []
    infos = scan_datasets(root, _library_tag_ids())
    return [DatasetInfoOut(id=i.id, gt_path=i.gt_path, n_movies=i.n_movies,
                           n_tags=i.n_tags, library_overlap=i.library_overlap,
                           usable_movies=len(i.usable_movies))
            for i in infos]


@router.get("/datasets/{dataset_id}/movies", response_model=list[MovieRefOut])
def list_dataset_movies(
    dataset_id: str,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> list[MovieRefOut]:
    """数据集的 usable 影片清单（选片列表用）；未知 dataset → 404。"""
    root = service.effective_root
    if isinstance(service, OpenEvalService):
        _require_open_dataset(root, dataset_id)
        return [MovieRefOut(**entry) for entry in dataset_movie_entries(root)]
    _dataset_or_404(root, dataset_id)
    try:
        entries = movie_entries(root, dataset_id)
    except ValueError:  # 扫描后被删/被改坏的窄窗口 → 同 404 口径
        raise HTTPException(
            status_code=404,
            detail=f"评测数据集不存在或不可解析：{dataset_id!r}")
    return [MovieRefOut(**entry) for entry in entries]


@router.post("/runs", response_model=RunCreatedOut)
def start_run(
    payload: StartRunRequest,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> RunCreatedOut:
    """起跑（错误映射集中处）：未知 dataset 404 → 校验失败 400 → busy 409。"""
    concurrency = payload.concurrency if payload.concurrency is not None else DEFAULT_CONCURRENCY
    movies = payload.movies.model_dump()
    try:
        if isinstance(service, OpenEvalService):
            _require_open_dataset(service.effective_root, payload.dataset)
            run_id = service.start(payload.dataset, movies, payload.quality_model or DEFAULT_MODEL,
                                   concurrency)
        else:
            _dataset_or_404(service.effective_root, payload.dataset)
            run_id = service.start(
                payload.dataset, movies,
                thetas=payload.thetas if payload.thetas is not None else DEFAULT_THETAS,
                ks=payload.ks if payload.ks is not None else DEFAULT_KS,
                concurrency=concurrency,
            )
    except EvalValidationError as exc:  # 选片越权/库未就绪/零交集/并发非法
        raise HTTPException(status_code=400, detail=str(exc))
    except RunBusyError as exc:  # 单进程同时只允许一个 run
        raise HTTPException(status_code=409, detail=str(exc))
    return RunCreatedOut(run_id=run_id)


@router.get("/runs", response_model=list[RunSummaryOut])
def list_runs(
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> list[RunSummaryOut]:
    """历史 run 摘要（磁盘扫描，started_at 降序；摘要档无 → None）。"""
    return [RunSummaryOut(**row) for row in service.history()]


@router.get("/runs/{run_id}", response_model=RunStateOut)
def get_run(
    run_id: str,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> RunStateOut:
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


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> dict:
    """请求取消 → {ok}：受理 true（worker 不再提交新片，在途片跑完）；
    run 不存在或已终态 → false（幂等意图，重复取消不报错）。"""
    return {"ok": service.cancel(run_id)}


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: str,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> dict:
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


@router.get("/runs/{run_id}/report")
def get_run_report(
    run_id: str,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> dict:
    """报告 JSON（report.json 原样透出）：无报告/截断 → 404（读侧容错，
    不把非原子写序窗口打成 500）。"""
    run_dir = service.run_directory(run_id)
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


@router.get("/runs/{run_id}/files/{name}")
def get_run_file(
    run_id: str,
    name: str,
    service: EvalService,
    _admin: UserAccount = Depends(require_admin),
) -> FileResponse:
    """产物下载（白名单五件套）：白名单外 404（report.json 走 /report 端点）；
    run 目录越界或文件不存在 → 404。"""
    if name not in _FILE_WHITELIST and not (name == "report.json" and isinstance(service, OpenEvalService)):
        raise HTTPException(status_code=404,
                            detail=f"评测产物文件不存在：{name!r}")
    run_dir = service.run_directory(run_id)
    path = run_dir / name if run_dir is not None else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404,
                            detail=f"评测产物文件不存在：{name!r}")
    return FileResponse(path, filename=name)
