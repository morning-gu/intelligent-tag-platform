"""`/api/v1/eval/closed` 路由：闭环评测 admin 端点（8 个，全部仅 admin）。

薄路由层（plan Task 6）：服务层是 tagplatform.closed_eval.ClosedEvalService
（数据集扫描/干跑运行器/报告与历史，DB 零写入），router 只做协议转换——

- 起跑请求的形状校验由 pydantic 声明（mode 词表外 / 类型不符 → 422 自动）；
- 错误映射集中在 start 端点的 try/except：未知 dataset → 404（先以
  scan_datasets 结果校验 dataset_id 再透传——404 语义要求 + 防未校验 id
  进下游路径拼接，Task 2 评审携带项）、服务层 EvalValidationError → 400
  （选片越权/库未就绪/库∩gt 零交集/并发非法）、RunBusyError → 409
  （detail 含活跃 run_id，前端据此提示谁占着）；
- run 详情/报告/文件端点读 `eval_output_root/<run_id>/`：run_id 先经
  `_run_dir` 守卫（解析后必须仍在输出根内——路径拼接不给 URL 手工 id
  越界机会），files 白名单钉死五件套（report.json 走专用端点、白名单外
  404）；
- 读侧容错与 history() 同口径：report.json 缺失/截断 → 404「无报告」，
  不抛 500（meta→raw→report 写序的非原子窗口）；进程内注册表查不到的
  run_id 按磁盘历史兜底（上一进程残留/中断 run 仍可查详情，与 /runs
  列表口径一致——列表里有的 id，详情必须能打开）。
"""

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tagplatform.api import get_closed_eval, get_settings_dep
from tagplatform.api.depends import require_admin
from tagplatform.closed_eval import (
    DEFAULT_CONCURRENCY,
    ClosedEvalService,
    EvalValidationError,
    RunBusyError,
    movie_entries,
    scan_datasets,
)
from tagplatform.db.models import UserAccount
from tagplatform.eval.closed_ranking import DEFAULT_KS, DEFAULT_THETAS
from tagplatform.library import try_load_library
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1/eval/closed")

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
    允许缺省——损坏 meta 不该把详情端点打成 500）。"""

    run_id: str
    status: str
    done: int | None = None
    total: int | None = None
    failed: int | None = None
    started_at: str | None = None
    error: str | None = None


class RunSummaryOut(BaseModel):
    """历史行（history()）：摘要四键无报告/无该档 → None（宁缺勿崩）。"""

    run_id: str
    dataset_id: str | None = None
    status: str
    done: int | None = None
    total: int | None = None
    failed: int | None = None
    started_at: str | None = None
    error: str | None = None
    ndcg10: float | None = None
    p10: float | None = None
    r20: float | None = None
    coverage_median: float | None = None


# ---------------------------------------------------------------- 内部装配


def _library_tag_ids() -> list[str]:
    """当前标签库的全部 tag_id（库未就绪 → 空表：overlap 全 0，如实反映）。"""
    library = try_load_library()
    return [tag.tag_id for tag in library.tags()] if library else []


def _dataset_or_404(settings: Settings, dataset_id: str):
    """dataset_id 先对 scan_datasets 结果校验再透传；未知 → 404。"""
    info = next((i for i in scan_datasets(settings.eval_dataset_root, set())
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


def _run_state_out(state) -> RunStateOut:
    """服务层 RunState → 响应模型（逐字段搬运，不做加工）。"""
    return RunStateOut(run_id=state.run_id, status=state.status,
                       done=state.done, total=state.total,
                       failed=state.failed, started_at=state.started_at,
                       error=state.error)


# ---------------------------------------------------------------- 端点


@router.get("/datasets", response_model=list[DatasetInfoOut])
def list_datasets(
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> list[DatasetInfoOut]:
    """可评数据集列表（畸形 gt 已在扫描层跳过；overlap 对当前标签库）。"""
    infos = scan_datasets(settings.eval_dataset_root, _library_tag_ids())
    return [DatasetInfoOut(id=i.id, gt_path=i.gt_path, n_movies=i.n_movies,
                           n_tags=i.n_tags, library_overlap=i.library_overlap,
                           usable_movies=len(i.usable_movies))
            for i in infos]


@router.get("/datasets/{dataset_id}/movies", response_model=list[MovieRefOut])
def list_dataset_movies(
    dataset_id: str,
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> list[MovieRefOut]:
    """数据集的 usable 影片清单（选片列表用）；未知 dataset → 404。"""
    _dataset_or_404(settings, dataset_id)
    try:
        entries = movie_entries(settings.eval_dataset_root, dataset_id)
    except ValueError:  # 扫描后被删/被改坏的窄窗口 → 同 404 口径
        raise HTTPException(
            status_code=404,
            detail=f"评测数据集不存在或不可解析：{dataset_id!r}")
    return [MovieRefOut(**entry) for entry in entries]


@router.post("/runs", response_model=RunCreatedOut)
def start_run(
    payload: StartRunRequest,
    _admin: UserAccount = Depends(require_admin),
    service: ClosedEvalService = Depends(get_closed_eval),
    settings: Settings = Depends(get_settings_dep),
) -> RunCreatedOut:
    """起跑（错误映射集中处）：未知 dataset 404 → 校验失败 400 → busy 409。"""
    _dataset_or_404(settings, payload.dataset)
    try:
        run_id = service.start(
            payload.dataset, payload.movies.model_dump(),
            thetas=(payload.thetas if payload.thetas is not None
                    else DEFAULT_THETAS),
            ks=(payload.ks if payload.ks is not None else DEFAULT_KS),
            concurrency=(payload.concurrency if payload.concurrency is not None
                         else DEFAULT_CONCURRENCY))
    except EvalValidationError as exc:  # 选片越权/库未就绪/零交集/并发非法
        raise HTTPException(status_code=400, detail=str(exc))
    except RunBusyError as exc:  # 单进程同时只允许一个 run
        raise HTTPException(status_code=409, detail=str(exc))
    return RunCreatedOut(run_id=run_id)


@router.get("/runs", response_model=list[RunSummaryOut])
def list_runs(
    _admin: UserAccount = Depends(require_admin),
    service: ClosedEvalService = Depends(get_closed_eval),
) -> list[RunSummaryOut]:
    """历史 run 摘要（磁盘扫描，started_at 降序；摘要档无 → None）。"""
    return [RunSummaryOut(**row) for row in service.history()]


@router.get("/runs/{run_id}", response_model=RunStateOut)
def get_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service: ClosedEvalService = Depends(get_closed_eval),
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
                       error=row["error"])


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service: ClosedEvalService = Depends(get_closed_eval),
) -> dict:
    """请求取消 → {ok}：受理 true（worker 不再提交新片，在途片跑完）；
    run 不存在或已终态 → false（幂等意图，重复取消不报错）。"""
    return {"ok": service.cancel(run_id)}


@router.get("/runs/{run_id}/report")
def get_run_report(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
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


@router.get("/runs/{run_id}/files/{name}")
def get_run_file(
    run_id: str,
    name: str,
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
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
