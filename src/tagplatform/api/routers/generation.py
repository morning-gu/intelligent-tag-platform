"""`/api/v1/generation` 与 `/api/v1/assets/{asset_id}/tags` 路由：生成任务创建、查询与重试。

薄路由层：校验（mode 注册表/资产存在/识别内容）→ 建任务（queued）→
executor.submit → 响应模型；业务规则全部在 tagplatform.services.generation
（producer 注册表）、tagplatform.tasks（执行器状态机与生成流水线）与
DB 层，router 只做协议转换与错误映射（mode 未知/资产及其父链均未完成
内容识别/非 failed 重试 → 422；未知资产/任务 → 404）。

v2 变更（doc §3.4-3.6，契约速查）：
- 任务直连 asset_id（media_context 关联退役），创建/列表/详情形状不变；
- 创建时**场景子资产归并到所属影片**（v2 内容只落影片级，场景行
  content_json 恒为 NULL）：选中场景沿 parent_id 上溯到最近带内容的
  祖先建任务，归并后按 asset_id 去重，响应 asset_id 即影片——"按场景
  挑片建任务"因此可用，同时不引入本期未启用的场景级挂载；
- 详情 results 取 pre_asset_tag（待审核行：review_status 三态，origin
  词表 closed|open），tag_name 解析 = candidate_name 或目录定义名，
  目录列（dir_id/dir_name/dir_path）由 JSON 标签库实时解析；
- /assets/{id}/tags 只回正式挂载（asset_tag 只含审核通过的复制行，
  评分/证据留在 pre 侧）；origin 经同键 pre 行解析（asset_tag 不存
  origin，无对应 pre 行时为 None）。

同步模式（Settings.generation_workers=0，测试与单步调试）：executor.submit
返回即任务执行完——创建响应重读库回真实终态（succeeded/failed）；异步
模式 submit 仅入队，响应回 queued/running 中间态。重试响应按契约恒回
queued（提交受理语义），实际终态经 GET 查询。
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from tagplatform.api import get_executor, get_quality_executor, get_session
from tagplatform.api.schemas import UtcDatetime
from tagplatform.capabilities.storage.models import AssetTag, MediaAsset, PreAssetTag, TagGenerationTask
from tagplatform.services.eval.tag_quality import DEFAULT_MODEL
from tagplatform.orchestration._shared import (
    AssetTagOut,
    GenerationResultOut,
    _dir_info,
    _result_out,
    _tag_info,
)
from tagplatform.services.generation import list_producers
from tagplatform.tasks import TaskExecutor
from tagplatform.tasks.tag_quality import QualityExecutor, QualityReport, quality_report, queue_retry

router = APIRouter(prefix="/api/v1")
QualityModelId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


# ---------- 响应模型 ----------

class GenerationPolicyIn(BaseModel):
    """生成策略请求体：缺省值与 GenerationPolicy（生成引擎契约）一致。

    `dir_ids` 限定本次生成的目录范围（空 = 全库）；`allow_new_tags=False`
    时只产出命中库内标签的行；closed 模式下 `max_tags` 为全局输出上限
    （每组最高分 1 条保底、保底间按分竞争截断，剩余按分补足），
    `max_tags_per_dir` 仅 open 模式使用。
    """

    max_tags: int = 30
    min_confidence: float = 0.7
    dir_ids: list[str] = Field(default_factory=list)
    allow_new_tags: bool = True
    max_tags_per_dir: int = 0


class GenerationTaskCreateRequest(BaseModel):
    """批量创建请求体：资产 id 非空列表 + mode + 可选策略。"""

    asset_ids: list[str] = Field(min_length=1)
    mode: str
    policy: GenerationPolicyIn | None = None
    quality_model: QualityModelId = DEFAULT_MODEL


class QualityRetryRequest(BaseModel):
    model: QualityModelId | None = None


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
    quality_evaluation: QualityReport


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


# ---------- 路由 ----------

@router.post("/generation/tasks", response_model=GenerationTaskCreateResponse)
def create_generation_tasks(
    body: GenerationTaskCreateRequest,
    session: Session = Depends(get_session),
    executor: TaskExecutor = Depends(get_executor),
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
    if body.mode not in list_producers():
        raise HTTPException(
            status_code=422,
            detail=f"未知 mode: {body.mode}（可用: {', '.join(list_producers())}）")

    assets: list[MediaAsset] = []
    seen: set[str] = set()
    for asset_id in body.asset_ids:
        asset = _resolve_target_asset(session, asset_id)
        if asset.asset_id in seen:
            continue  # 归并后重复（影片与其场景同选）：只建一条任务
        seen.add(asset.asset_id)
        assets.append(asset)

    now = datetime.now(timezone.utc)
    batch_id = f"batch_{uuid.uuid4().hex}"
    # 策略序列化持久化（执行器按 JSON 解析 GenerationPolicy）；未传则
    # policy_json 置空，执行器回退默认策略（缺省值与 GenerationPolicyIn 一致）
    policy_json = body.policy.model_dump_json() if body.policy is not None else None
    created: list[TagGenerationTask] = []
    for asset in assets:
        task = TagGenerationTask(
            task_id=f"gen_{uuid.uuid4().hex[:12]}",
            asset_id=asset.asset_id,
            mode=body.mode,
            batch_id=batch_id,
            quality_model=body.quality_model,
            policy_json=policy_json,
            status="queued",
            created_at=now,
        )
        session.add(task)
        created.append(task)
    session.commit()

    executor.submit_batch([task.task_id for task in created], mode=body.mode)

    # 重读执行状态（expire_on_commit=False 下对象属性不含线程内写入，须 refresh）
    items = []
    for task in created:
        session.refresh(task)
        items.append(GenerationTaskItemOut(
            task_id=task.task_id, asset_id=task.asset_id, status=task.status))
    return GenerationTaskCreateResponse(tasks=items)


@router.get("/generation/tasks", response_model=list[GenerationTaskOut])
def list_generation_tasks(
    status: str | None = Query(None),
    mode: str | None = Query(None),
    asset_id: str | None = Query(None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
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


@router.get("/generation/tasks/{task_id}", response_model=GenerationTaskDetail)
def get_generation_task(
    task_id: str, session: Session = Depends(get_session)
) -> GenerationTaskDetail:
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
    info = _tag_info(rows)
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
        results=[_result_out(row, info) for row in rows],
        quality_evaluation=quality_report(session, task),
    )


@router.post("/generation/tasks/{task_id}/quality/retry",
             response_model=GenerationTaskRetryResponse)
def retry_quality_evaluation(
    task_id: str,
    body: QualityRetryRequest | None = None,
    session: Session = Depends(get_session),
    executor: QualityExecutor = Depends(get_quality_executor),
) -> GenerationTaskRetryResponse:
    if session.get(TagGenerationTask, task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if not queue_retry(session, task_id, body.model if body else None):
        raise HTTPException(status_code=409, detail="仅失败或部分完成的影片评估可重试")
    executor.submit(task_id)
    return GenerationTaskRetryResponse(task_id=task_id, status="queued")


@router.post("/generation/tasks/{task_id}/retry",
             response_model=GenerationTaskRetryResponse)
def retry_generation_task(
    task_id: str,
    session: Session = Depends(get_session),
    executor: TaskExecutor = Depends(get_executor),
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


@router.get("/assets/{asset_id}/tags", response_model=list[AssetTagOut])
def list_asset_tags(
    asset_id: str, session: Session = Depends(get_session)
) -> list[AssetTagOut]:
    """资产正式挂载标签：读 `asset_tag`（审核通过的复制行才是权威口径，
    doc §3.6）；origin/候选名由同键 pre 行回填，目录列按 `tag_define →
    tag_catalog` 解析；未知资产 → 404。

    历史说明：早期本端点直接读 `pre_asset_tag` 的 approved 行（那时还没有
    "审核生效 → 复制进 asset_tag"这一步）。两表口径一旦分叉（历史 approved
    行没被复制、或 modify 改了 pre 侧名字），以 asset_tag 为准。
    """
    if session.get(MediaAsset, asset_id) is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    mounts = (
        session.query(AssetTag)
        .filter(AssetTag.asset_id == asset_id)
        .order_by(AssetTag.id)
        .all()
    )
    if not mounts:
        return []
    # 同键 pre 行：origin / candidate_name 只存在于审核侧（正式表不存）。
    # 新词入库后 pre 行的 tag_id 可能仍为空（ingest 只改 define/挂载），所以
    # 再按"候选名 = 挂载名"建一份索引兜底。
    pre_rows = session.query(PreAssetTag).filter(
        PreAssetTag.asset_id == asset_id,
    ).all()
    pres = {
        (p.target_type, p.target_id, p.tag_id): p
        for p in pre_rows if p.tag_id
    }
    pres_by_name = {
        (p.target_type, p.target_id, p.candidate_name): p
        for p in pre_rows if p.candidate_name
    }
    info = _tag_info(pre_rows)
    out: list[AssetTagOut] = []
    for mount in mounts:
        pre = pres.get((mount.target_type, mount.target_id, mount.tag_id))
        if pre is None:
            pre = pres_by_name.get(
                (mount.target_type, mount.target_id, mount.tag_name))
        # 目录：新词候选挂在 pre 的 candidate_dir_id；库内标签走 tag_define.dir_id
        dir_id = (pre.candidate_dir_id if pre is not None else None) or \
            info.get(mount.tag_id, {}).get("dir_id")
        dir_name, dir_path, _ = _dir_info(dir_id)
        out.append(AssetTagOut(
            id=mount.id,
            tag_id=mount.tag_id,
            tag_name=mount.tag_name,
            candidate_name=(pre.candidate_name if pre is not None else None),
            origin=(pre.origin if pre is not None else None),
            target_type=mount.target_type,
            dir_id=dir_id,
            dir_name=dir_name,
            dir_path=dir_path,
            created_at=mount.created_at,
        ))
    return out
