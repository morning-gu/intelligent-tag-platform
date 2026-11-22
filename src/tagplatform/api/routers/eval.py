"""评测 admin API 薄路由：闭环 `/api/v1/eval/closed`（13 端点）+ 开放
`/api/v1/eval/open`（11 端点，open-eval Task 6），全部仅 admin。

接入层只做协议转换：请求模型声明校验（mode 词表外 / 类型不符 → 422 自动）
→ Depends 注入（admin 门槛 require_admin、ClosedEvalService /
OpenEvalService 单例、Settings）→ 调编排 → 返回。起跑校验序、run 目录
守卫（路径越界防拼接）、读侧容错（非原子写序窗口不 500）与 θ 门校验全部
在 tagplatform.orchestration.eval（用例清单见该模块 docstring）；闭环服务
层是 tagplatform.services.eval.service.ClosedEvalService，开放服务层是
tagplatform.services.eval.open_service.OpenEvalService（数据集扫描/干跑
运行器/报告与历史，DB 零写入）。错误映射随逻辑在编排层原样保留：未知
dataset → 404、服务层 EvalValidationError → 400、RunBusyError → 409
（detail 含活跃 run_id——open 起跑的 busy 含跨服务互斥，对端活跃同样
409）、θ 非法档 → 400 带合法值清单。

open 端点族（第二个 router，同一文件）：dataset 三别名
（GET/POST dataset-root、GET datasets、GET datasets/{id}/movies）与闭环
共享 override 状态——Depends(get_closed_eval) 直调闭环编排函数零新逻辑；
runs 族 Depends(get_open_eval)，产物读侧走 settings.open_eval_output_root。
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from tagplatform.api import get_closed_eval, get_open_eval, get_settings_dep
from tagplatform.api.depends import require_admin
from tagplatform.capabilities.storage.models import UserAccount
from tagplatform.orchestration import eval as eval_orch
from tagplatform.orchestration.eval import (
    DatasetInfoOut,
    DatasetRootOut,
    MovieDetailOut,
    MovieRefOut,
    MovieSummaryOut,
    OpenRunSummaryOut,
    RunCreatedOut,
    RunStateOut,
    RunSummaryOut,
)
from tagplatform.settings import Settings

router = APIRouter(prefix="/api/v1/eval/closed")
open_router = APIRouter(prefix="/api/v1/eval/open")


# ---------------------------------------------------------------- 请求模型


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


class StartOpenRunRequest(BaseModel):
    """开放评测起跑请求（open-eval Task 6）：thetas/modalities/concurrency
    缺省 None → 服务端默认（DEFAULT_THETAS / ("script","dialogue") /
    DEFAULT_CONCURRENCY）；modalities 词表校验（非空 ⊆ {script, dialogue}
    且必含 script）在服务层 → 400。"""

    dataset: str
    movies: MovieSelection
    thetas: list[float] | None = None
    modalities: list[str] | None = None
    concurrency: int | None = None


class SetDatasetRootRequest(BaseModel):
    """设置数据集根请求：非空字符串（空串 → 422 形状拒绝）；
    目录存在/扫描出有效数据集由编排层校验（不过 → 400 带原因）。"""

    root: str = Field(min_length=1)


# ---------------------------------------------------------------- 端点


@router.get("/dataset-root", response_model=DatasetRootOut)
def get_dataset_root(
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> DatasetRootOut:
    """当前生效的数据集根（override ?? .env 默认）与是否默认。"""
    return eval_orch.get_dataset_root(service)


@router.post("/dataset-root", response_model=DatasetRootOut)
def set_dataset_root(
    payload: SetDatasetRootRequest,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> DatasetRootOut:
    """设置数据集根（进程内 override，重启回落 .env 默认）。"""
    return eval_orch.set_dataset_root(service, payload.root)


@router.get("/datasets", response_model=list[DatasetInfoOut])
def list_datasets(
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> list[DatasetInfoOut]:
    """可评数据集列表（畸形 gt 已在扫描层跳过；overlap 对当前标签库）。"""
    return eval_orch.list_datasets(service)


@router.get("/datasets/{dataset_id}/movies", response_model=list[MovieRefOut])
def list_dataset_movies(
    dataset_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> list[MovieRefOut]:
    """数据集的 usable 影片清单（选片列表用）；未知 dataset → 404。"""
    return eval_orch.list_dataset_movies(service, dataset_id)


@router.post("/runs", response_model=RunCreatedOut)
def start_run(
    payload: StartRunRequest,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> RunCreatedOut:
    """起跑（错误映射集中处）：未知 dataset 404 → 校验失败 400 → busy 409。"""
    return eval_orch.create_eval_run(
        service, payload.dataset, payload.movies.model_dump(),
        payload.thetas, payload.ks, payload.concurrency)


@router.get("/runs", response_model=list[RunSummaryOut])
def list_runs(
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> list[RunSummaryOut]:
    """历史 run 摘要（磁盘扫描，started_at 降序；摘要档无 → None）。"""
    return eval_orch.list_eval_runs(service)


@router.get("/runs/{run_id}", response_model=RunStateOut)
def get_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> RunStateOut:
    """run 进度详情（注册表优先；磁盘历史兜底；两处都没有 → 404）。"""
    return eval_orch.get_eval_run(service, run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> dict:
    """请求取消 → {ok}（幂等意图，重复取消不报错）。"""
    return eval_orch.cancel_eval_run(service, run_id)


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> dict:
    """删除终态 run（磁盘产物 + 进程内注册表）→ {ok: true}。"""
    return eval_orch.delete_eval_run(service, run_id)


@router.get("/runs/{run_id}/report")
def get_run_report(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    """报告 JSON（report.json 原样透出）：无报告/截断 → 404。"""
    return eval_orch.get_run_report(settings, run_id)


@router.get("/runs/{run_id}/files/{name}")
def get_run_file(
    run_id: str,
    name: str,
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
):
    """产物下载（白名单五件套；report.json 走 /report 端点）。"""
    return eval_orch.get_run_file(settings, run_id, name)


@router.get("/runs/{run_id}/movies", response_model=MovieSummaryOut)
def get_run_movies(
    run_id: str,
    theta_p: float | None = Query(
        None, description="P 门 θ（须为 run 网格档；缺省=统一取格 θ≈0.2）"),
    theta_r: float | None = Query(
        None, description="R 门 θ（同上，缺省=统一取格 θ≈0.8；n_relevant 与明细 hit/漏检跟此门）"),
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> MovieSummaryOut:
    """run 的逐片摘要（badcase 视图主表）：读盘现算，θ 门可拆。"""
    return eval_orch.get_run_movies(settings, run_id, theta_p, theta_r)


@router.get("/runs/{run_id}/movies/{tt}", response_model=MovieDetailOut)
def get_run_movie_detail(
    run_id: str,
    tt: str,
    theta_p: float | None = Query(
        None, description="P 门 θ（只回显；明细分类不使用）"),
    theta_r: float | None = Query(
        None, description="R 门 θ（hit 标记与 misses 按此门）"),
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> MovieDetailOut:
    """单片明细（摘要行展开懒加载）：计分清单 + 空间外 + 漏检三表。"""
    return eval_orch.get_run_movie_detail(settings, run_id, tt, theta_p, theta_r)


# ---------------------------------------------------------------- 开放评测端点（open-eval Task 6）
#
# dataset 三别名 Depends(get_closed_eval)：override 状态挂闭环服务，open
# 页与闭环页共享（一处写入两端可见）；runs 族 Depends(get_open_eval)。
# 端点函数名加 open_ 前缀防与闭环同名端点在模块作用域撞名。


@open_router.get("/dataset-root", response_model=DatasetRootOut)
def open_get_dataset_root(
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> DatasetRootOut:
    """当前生效的数据集根（开放评测与闭环共享同一 override 状态）。"""
    return eval_orch.get_dataset_root(service)


@open_router.post("/dataset-root", response_model=DatasetRootOut)
def open_set_dataset_root(
    payload: SetDatasetRootRequest,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> DatasetRootOut:
    """设置数据集根（写的是闭环服务的同一 override，重启回落 .env 默认）。"""
    return eval_orch.set_dataset_root(service, payload.root)


@open_router.get("/datasets", response_model=list[DatasetInfoOut])
def open_list_datasets(
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> list[DatasetInfoOut]:
    """可评数据集列表（与闭环同状态：读路径走共享 effective_root）。"""
    return eval_orch.list_datasets(service)


@open_router.get("/datasets/{dataset_id}/movies",
                 response_model=list[MovieRefOut])
def open_list_dataset_movies(
    dataset_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_closed_eval),
) -> list[MovieRefOut]:
    """数据集的 usable 影片清单（选片列表用）；未知 dataset → 404。"""
    return eval_orch.list_dataset_movies(service, dataset_id)


@open_router.post("/runs", response_model=RunCreatedOut)
def open_start_run(
    payload: StartOpenRunRequest,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_open_eval),
) -> RunCreatedOut:
    """开放评测起跑：未知 dataset 404 → 校验失败 400 → busy 409（含跨服务
    互斥——closed 活跃时 detail 带对端 run_id）。"""
    return eval_orch.create_open_run(
        service, payload.dataset, payload.movies.model_dump(),
        payload.thetas, payload.modalities, payload.concurrency)


@open_router.get("/runs", response_model=list[OpenRunSummaryOut])
def open_list_runs(
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_open_eval),
) -> list[OpenRunSummaryOut]:
    """开放评测历史 run 摘要（磁盘扫描，started_at 降序；摘要五键无报告
    → None）。"""
    return eval_orch.list_open_runs(service)


@open_router.get("/runs/{run_id}", response_model=RunStateOut)
def open_get_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_open_eval),
) -> RunStateOut:
    """开放评测 run 进度详情（注册表优先；磁盘历史兜底；两处都没有 → 404）。"""
    return eval_orch.get_open_run(service, run_id)


@open_router.post("/runs/{run_id}/cancel")
def open_cancel_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_open_eval),
) -> dict:
    """请求取消 → {ok}（幂等意图，重复取消不报错）。"""
    return eval_orch.cancel_open_run(service, run_id)


@open_router.delete("/runs/{run_id}")
def open_delete_run(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    service=Depends(get_open_eval),
) -> dict:
    """删除终态 open run（磁盘产物 + 进程内注册表）→ {ok: true}。"""
    return eval_orch.delete_open_run(service, run_id)


@open_router.get("/runs/{run_id}/report")
def open_get_run_report(
    run_id: str,
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    """开放评测报告 JSON（report.json 原样透出）：无报告/截断 → 404。"""
    return eval_orch.get_open_run_report(settings, run_id)


@open_router.get("/runs/{run_id}/files/{name}")
def open_get_run_file(
    run_id: str,
    name: str,
    _admin: UserAccount = Depends(require_admin),
    settings: Settings = Depends(get_settings_dep),
):
    """开放评测产物下载（白名单五件套；report.json 走 /report 端点）。"""
    return eval_orch.get_open_run_file(settings, run_id, name)
