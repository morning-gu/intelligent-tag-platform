"""生成任务生命周期编排（创建/列表/详情/重跑）——orchestration/tasks 域。

用例清单（原 api/routers/generation.py 的任务端点内嵌逻辑，Task 11 原样
下沉；详情行装配已端点级单次库加载——入口 try_load_library 一次传参，
消逐行 _result_out→_dir_info 的 O(行×库) N+1，Task 12）：

- `create_generation_tasks`  批量创建生成任务并提交执行（mode 注册表校验、
  场景资产归并上溯、逐资产建 queued 任务、executor 提交、重读真实状态）
- `list_generation_tasks`    任务列表（status/mode/asset_id 过滤 + 分页）
- `get_task_detail`          任务详情 + 结果摘要行（_shared._result_out 装配）
- `retry_quality_evaluation` 影片质量评估重跑（queue_retry + 质量执行器提交）
- `retry_generation_task`    失败任务重跑（状态回 queued、清错误、重新提交）

薄路由契约：HTTP 错误映射随逻辑下沉本层原样保留（404 未知资产/任务、
422 未知 mode/无识别内容/仅失败可重试、409 仅失败或部分完成评估可重试）；
接入层 router 只做 入参解析 → 调编排 → 返回。
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from tagplatform.orchestration._shared import (
    GenerationResultOut,
    UtcDatetime,
    _result_out,
    _tag_info,
)
from tagplatform.capabilities.storage.models import MediaAsset, PreAssetTag, TagGenerationTask
from tagplatform.services.generation import list_producers
from tagplatform.services.tags.library import try_load_library
from tagplatform.tasks import TaskExecutor
from tagplatform.tasks.tag_quality import QualityExecutor, QualityReport, quality_report, queue_retry


# ---------- 响应模型 ----------

class GenerationTaskItemOut(BaseModel):
    """创建响应单条：任务标识 + 资产 + 提交后状态（同步模式即终态）。"""

    task_id: str
    asset_id: str
    status: str


class GenerationTaskCreateResponse(BaseModel):
    """批量创建响应：逐资产一条任务。"""

    tasks: list[GenerationTaskItemOut]


class GenerationTaskOut(BaseModel):
    """任务列表项（asset_id 直连 media_asset，未知资产时为空）。"""

    task_id: str
    asset_id: str | None = None
    mode: str
    status: str
    model_version: str | None = None
    prompt_version: str | None = None
    error: str | None = None
    created_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None


class GenerationTaskDetail(GenerationTaskOut):
    """任务详情：在列表字段之上附该任务全部结果摘要行。"""

    results: list[GenerationResultOut]
    quality_evaluation: QualityReport | None


class GenerationTaskRetryResponse(BaseModel):
    """重试响应：提交受理语义（status 恒 queued，实际终态经 GET 查询）。"""

    task_id: str
    status: str


# ---------- 组装辅助 ----------

def _resolve_target_asset(session: Session, asset_id: str) -> MediaAsset:
    """请求资产 → 真正带识别内容的打标目标（场景资产上溯父影片）。

    v2 内容只落影片级：场景子资产（asset_type='scene'）的 content_json
    恒为 NULL（content_import 只给父级写内容），但用户在创建任务时选中
    某个场景是常见诉求（"按场景挑片"）。本函数把这类资产沿 parent_id
    上溯到**最近一个带 content_json 的祖先**，任务仍挂在影片级资产上跑：

    - 资产不存在 → 404（沿原契约）；
    - 资产自带 content_json → 直接返回自身（影片/剧集/已识别的场景行）；
    - 自身无内容但父级有内容 → 返回该父级（场景 → 影片的归并）；
    - 整条父链都没有内容（上传占位资产/父级未识别）→ 422，detail 指明
      是被归并的原资产与最终父级，便于定位是"没识别"还是"选错了"。
    """
    asset = session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    if asset.content_json:
        return asset
    # 上溯父链：visited 防御脏数据形成的环（parent 自指/互指）导致死循环
    current = asset
    visited: set[str] = {asset.asset_id}
    while current.parent_id and current.parent_id not in visited:
        parent = session.get(MediaAsset, current.parent_id)
        if parent is None:
            break
        if parent.content_json:
            return parent
        visited.add(parent.asset_id)
        current = parent
    # 无任何带内容的祖先：区分"归并失败"与"自身未识别"两种文案
    if asset.asset_type == "scene" or asset.parent_id:
        target = asset.parent_id or "（无父级）"
        raise HTTPException(
            status_code=422,
            detail=f"资产 {asset_id} 无识别内容，其父级资产 {target} 也未完成内容识别")
    raise HTTPException(status_code=422, detail=f"资产 {asset_id} 无识别内容")


# ---------- 用例 ----------

def create_generation_tasks(
    session: Session,
    executor: TaskExecutor,
    asset_ids: list[str],
    mode: str,
    policy_json: str | None,
    quality_model: str,
) -> GenerationTaskCreateResponse:
    """批量创建生成任务并提交执行。

    校验全部通过才建任务（任一失败整请求拒绝，零部分创建）：
    - mode 未注册 → 422（detail 列出可用 mode）；
    - 资产不存在 → 404；
    - 资产无识别内容 → 422。**场景子资产按归属影片归并**（v2 内容只落
      影片级，场景行 content_json 恒为 NULL）：请求里的场景资产沿
      parent_id 上溯到最近带 content_json 的祖先，任务挂影片级资产跑；
      自身上溯后仍无内容（上传占位/父级未识别）才 422，detail 指明原资产
      与父级。
    归并后按 asset_id 去重（同时选中影片与其场景只建一条任务，保持请求
    顺序）。每资产建一条 queued 任务（policy_json 持久化请求策略），先
    落库（执行器线程用独立会话读行）再逐条 executor.submit；随后重读真实
    状态返回——同步模式（generation_workers=0）submit 返回即执行完，回
    succeeded/failed 终态；异步模式回 queued/running 中间态。
    """
    if mode not in list_producers():
        raise HTTPException(
            status_code=422,
            detail=f"未知 mode: {mode}（可用: {', '.join(list_producers())}）")

    assets: list[MediaAsset] = []
    seen: set[str] = set()
    for asset_id in asset_ids:
        asset = _resolve_target_asset(session, asset_id)
        if asset.asset_id in seen:
            continue  # 归并后重复（影片与其场景同选）：只建一条任务
        seen.add(asset.asset_id)
        assets.append(asset)

    now = datetime.now(timezone.utc)
    batch_id = f"batch_{uuid.uuid4().hex}"
    # 策略序列化持久化（执行器按 JSON 解析 GenerationPolicy）；未传则
    # policy_json 置空，执行器回退默认策略（缺省值与请求模型一致）
    created: list[TagGenerationTask] = []
    for asset in assets:
        task = TagGenerationTask(
            task_id=f"gen_{uuid.uuid4().hex[:12]}",
            asset_id=asset.asset_id,
            mode=mode,
            batch_id=batch_id,
            quality_model=quality_model if mode == "open" else None,
            policy_json=policy_json,
            status="queued",
            created_at=now,
        )
        session.add(task)
        created.append(task)
    session.commit()

    executor.submit_batch([task.task_id for task in created], mode=mode)

    # 重读执行状态（expire_on_commit=False 下对象属性不含线程内写入，须 refresh）
    items = []
    for task in created:
        session.refresh(task)
        items.append(GenerationTaskItemOut(
            task_id=task.task_id, asset_id=task.asset_id, status=task.status))
    return GenerationTaskCreateResponse(tasks=items)


def list_generation_tasks(
    session: Session,
    status: str | None,
    mode: str | None,
    asset_id: str | None,
    limit: int,
    offset: int,
) -> list[GenerationTaskOut]:
    """任务列表：status/mode/asset_id 过滤，created_at 降序（task_id 决胜），
    limit/offset 分页（默认 100/0）。资产直接来自任务行。"""
    query = session.query(TagGenerationTask)
    if status is not None:
        query = query.filter(TagGenerationTask.status == status)
    if mode is not None:
        query = query.filter(TagGenerationTask.mode == mode)
    if asset_id is not None:
        query = query.filter(TagGenerationTask.asset_id == asset_id)
    rows = query.order_by(
        TagGenerationTask.created_at.desc(), TagGenerationTask.task_id,
    ).offset(offset).limit(limit).all()
    return [
        GenerationTaskOut(
            task_id=task.task_id,
            asset_id=task.asset_id,
            mode=task.mode,
            status=task.status,
            model_version=task.model_version,
            prompt_version=task.prompt_version,
            error=task.error,
            created_at=task.created_at,
            finished_at=task.finished_at,
        )
        for task in rows
    ]


def get_task_detail(session: Session, task_id: str) -> GenerationTaskDetail:
    """任务详情 + 结果摘要列表（tag_name 按 candidate_name/目录名解析）；
    未知任务 → 404。"""
    task = session.get(TagGenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    rows = (
        session.query(PreAssetTag)
        .filter(PreAssetTag.task_id == task_id)
        .order_by(PreAssetTag.id)  # 入库行序（producer 产出序：相关度降序截断后）
        .all()
    )
    # 端点级单次库加载（Task 12 消 N+1）：入口 load 一次传参，逐行
    # _result_out 的目录解析共用同一份库视图
    library = try_load_library(session)
    info = _tag_info(rows, library)
    return GenerationTaskDetail(
        task_id=task.task_id,
        asset_id=task.asset_id,
        mode=task.mode,
        status=task.status,
        model_version=task.model_version,
        prompt_version=task.prompt_version,
        error=task.error,
        created_at=task.created_at,
        finished_at=task.finished_at,
        results=[_result_out(row, info, library) for row in rows],
        quality_evaluation=quality_report(session, task) if task.mode == "open" else None,
    )


def retry_quality_evaluation(
    session: Session,
    executor: QualityExecutor,
    task_id: str,
    model: str | None,
) -> GenerationTaskRetryResponse:
    """影片质量评估重跑：仅 failed/部分完成可重试（否则 409）；受理回 queued。"""
    task = session.get(TagGenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if task.mode != "open":
        raise HTTPException(status_code=409, detail="仅开放生成任务支持质量评估")
    if not queue_retry(session, task_id, model):
        raise HTTPException(status_code=409, detail="仅失败或部分完成的影片评估可重试")
    executor.submit(task_id)
    return GenerationTaskRetryResponse(task_id=task_id, status="queued")


def retry_generation_task(
    session: Session,
    executor: TaskExecutor,
    task_id: str,
) -> GenerationTaskRetryResponse:
    """重试失败任务：仅 failed 可重试（否则 422）；状态回 queued、
    error/finished_at 清空并重新提交执行。响应回提交受理态 queued，
    实际终态（同步模式 submit 返回时可能已执行完）经 GET 查询。"""
    task = session.get(TagGenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if task.status != "failed":
        raise HTTPException(status_code=422, detail="仅失败任务可重试")
    task.status = "queued"
    task.error = None
    task.finished_at = None
    session.commit()
    executor.submit_batch([task_id], mode=task.mode)
    return GenerationTaskRetryResponse(task_id=task_id, status="queued")
