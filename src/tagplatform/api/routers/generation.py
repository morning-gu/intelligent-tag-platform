"""`/api/v1/generation` 与 `/api/v1/assets/{asset_id}/tags` 路由：生成任务创建、查询与重试。

薄路由层：校验（mode 注册表/资产存在/识别内容）→ 建任务（queued）→
executor.submit → 响应模型；业务规则全部在 tagplatform.generation
（producer 注册表）、tagplatform.tasks（执行器状态机与生成流水线）与
DB 层，router 只做协议转换与错误映射（mode 未知/无识别内容/非 failed
重试 → 422；未知资产/任务 → 404）。

同步模式（Settings.generation_workers=0，测试与单步调试）：executor.submit
返回即任务执行完——创建响应重读库回真实终态（succeeded/failed）；异步
模式 submit 仅入队，响应回 queued/running 中间态。重试响应按契约恒回
queued（提交受理语义），实际终态经 GET 查询。
"""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from tagplatform.api import get_executor, get_session
from tagplatform.db.models import AssetTag, GenerationTask, MediaAsset, MediaContext, TagCatalog
from tagplatform.generation import list_producers
from tagplatform.tasks import TaskExecutor

router = APIRouter(prefix="/api/v1")


# ---------- 响应模型 ----------

class GenerationPolicyIn(BaseModel):
    """生成策略请求体：缺省值与 GenerationPolicy（生成引擎契约）一致。"""

    max_tags: int = 30
    min_confidence: float = 0.7


class GenerationTaskCreateRequest(BaseModel):
    """批量创建请求体：资产 id 非空列表 + mode + 可选策略。"""

    asset_ids: list[str] = Field(min_length=1)
    mode: str
    policy: GenerationPolicyIn | None = None


class GenerationTaskItemOut(BaseModel):
    """创建响应单条：任务标识 + 资产 + 提交后状态（同步模式即终态）。"""

    task_id: str
    asset_id: str
    status: str


class GenerationTaskCreateResponse(BaseModel):
    """批量创建响应：逐资产一条任务。"""

    tasks: list[GenerationTaskItemOut]


class GenerationTaskOut(BaseModel):
    """任务列表项（asset_id 经 media_context 关联解析，无上下文时为空）。"""

    task_id: str
    asset_id: str | None = None
    mode: str
    status: str
    model_version: str | None = None
    prompt_version: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


class EvidenceOut(BaseModel):
    """单条证据明细（与 generation.Evidence 字段一一对应）。"""

    scene_id: str
    start_ms: int
    end_ms: int
    description: str


class GenerationResultOut(BaseModel):
    """生成结果摘要行：id 为 asset_tag 行主键（阶段 3 B3 暴露——Task 4
    审核操作路由与前端行操作的定位键）；tag_name 展示解析 candidate_*
    优先、目录名回退（catalog 行经审核 modify 后显示修改值）。"""

    id: int
    tag_name: str
    origin: str
    relevance_score: float | None = None
    confidence: float | None = None
    review_status: str
    level: str = "film"
    evidence: list[EvidenceOut] = []


class GenerationTaskDetail(GenerationTaskOut):
    """任务详情：在列表字段之上附该任务全部结果摘要行。"""

    results: list[GenerationResultOut]


class GenerationTaskRetryResponse(BaseModel):
    """重试响应：提交受理语义（status 恒 queued，实际终态经 GET 查询）。"""

    task_id: str
    status: str


class AssetTagOut(GenerationResultOut):
    """资产标签行：摘要形状（含行主键 id）+ 标识原样（tag_id/catalog 行、
    candidate_name/generated 行）。"""

    tag_id: str | None = None
    candidate_name: str | None = None


# ---------- 组装辅助 ----------

def _parse_evidence(raw: str | None) -> list[EvidenceOut]:
    """evidence_json → 证据列表；空/损坏 JSON/条目 shape 非法（缺必填字段
    或类型不符 → ValidationError）一律按空列表兜底（终审 deferred 落地：
    审核列表与任务详情两条消费路径对坏证据均不再 500）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    try:
        return [EvidenceOut(**item) for item in data if isinstance(item, dict)]
    except ValidationError:
        return []


def _catalog_names(session: Session, rows: list[AssetTag]) -> dict[str, str]:
    """catalog 来源行的 tag_id → 目录标签名映射（一次批量取全）。"""
    tag_ids = {r.tag_id for r in rows if r.origin == "catalog" and r.tag_id}
    if not tag_ids:
        return {}
    return {t.tag_id: t.tag_name
            for t in session.query(TagCatalog).filter(TagCatalog.tag_id.in_(tag_ids))}


def _result_out(row: AssetTag, names: dict[str, str]) -> GenerationResultOut:
    """asset_tag 行 → 结果摘要（详情 results 与资产标签视图共用，含行主键 id）。

    tag_name 展示解析与审核列表同规：candidate_name 非空优先（审核 modify
    落在行内的展示字段，改了什么显示什么），否则回退目录名（names 按
    origin=catalog 行的 tag_id 预取；merged 行 candidate 已清空不受影响）。
    """
    tag_name = row.candidate_name or names.get(row.tag_id or "", "")
    return GenerationResultOut(
        id=row.id,
        tag_name=tag_name,
        origin=row.origin,
        relevance_score=row.relevance_score,
        confidence=row.confidence,
        review_status=row.review_status,
        level=row.level,
        evidence=_parse_evidence(row.evidence_json),
    )


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
    - 资产无最新识别内容（无 media_context）→ 422。
    每资产建一条 queued 任务（policy_json 持久化请求策略），先落库（执行器
    线程用独立会话读行）再逐条 executor.submit；随后重读真实状态返回——
    同步模式（generation_workers=0）submit 返回即执行完，回 succeeded/failed
    终态；异步模式回 queued/running 中间态。
    """
    if body.mode not in list_producers():
        raise HTTPException(
            status_code=422,
            detail=f"未知 mode: {body.mode}（可用: {', '.join(list_producers())}）")

    contexts: list[MediaContext] = []
    for asset_id in body.asset_ids:
        if session.get(MediaAsset, asset_id) is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        # 最新 media_context：created_at 降序第一（context_id 降序决胜，保证确定性）
        context = (
            session.query(MediaContext)
            .filter(MediaContext.asset_id == asset_id)
            .order_by(MediaContext.created_at.desc(), MediaContext.context_id.desc())
            .first()
        )
        if context is None:
            raise HTTPException(
                status_code=422, detail=f"资产 {asset_id} 无识别内容")
        contexts.append(context)

    now = datetime.now(timezone.utc)
    # 策略序列化持久化（执行器按 JSON 解析 GenerationPolicy）；未传则
    # policy_json 置空，执行器回退默认策略（缺省值与 GenerationPolicyIn 一致）
    policy_json = body.policy.model_dump_json() if body.policy is not None else None
    created: list[tuple[GenerationTask, str]] = []
    for asset_id, context in zip(body.asset_ids, contexts):
        task = GenerationTask(
            task_id=f"gen_{uuid.uuid4().hex[:12]}",
            context_id=context.context_id,
            mode=body.mode,
            policy_json=policy_json,
            status="queued",
            created_at=now,
        )
        session.add(task)
        created.append((task, asset_id))
    session.commit()

    for task, _ in created:
        executor.submit(task.task_id)

    # 重读执行状态（expire_on_commit=False 下对象属性不含线程内写入，须 refresh）
    items = []
    for task, asset_id in created:
        session.refresh(task)
        items.append(GenerationTaskItemOut(
            task_id=task.task_id, asset_id=asset_id, status=task.status))
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
    limit/offset 分页（默认 100/0）。asset_id 经 media_context 关联。"""
    query = session.query(GenerationTask, MediaContext.asset_id).outerjoin(
        MediaContext, GenerationTask.context_id == MediaContext.context_id)
    if status is not None:
        query = query.filter(GenerationTask.status == status)
    if mode is not None:
        query = query.filter(GenerationTask.mode == mode)
    if asset_id is not None:
        query = query.filter(MediaContext.asset_id == asset_id)
    rows = query.order_by(
        GenerationTask.created_at.desc(), GenerationTask.task_id,
    ).offset(offset).limit(limit).all()
    return [
        GenerationTaskOut(
            task_id=task.task_id,
            asset_id=ctx_asset_id,
            mode=task.mode,
            status=task.status,
            model_version=task.model_version,
            prompt_version=task.prompt_version,
            error=task.error,
            created_at=task.created_at,
            finished_at=task.finished_at,
        )
        for task, ctx_asset_id in rows
    ]


@router.get("/generation/tasks/{task_id}", response_model=GenerationTaskDetail)
def get_generation_task(
    task_id: str, session: Session = Depends(get_session)
) -> GenerationTaskDetail:
    """任务详情 + 结果摘要列表（tag_name 按 origin 取、evidence 解析）；未知任务 → 404。"""
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    asset_id = (
        session.query(MediaContext.asset_id)
        .filter(MediaContext.context_id == task.context_id)
        .scalar()
        if task.context_id is not None else None
    )
    rows = (
        session.query(AssetTag)
        .filter(AssetTag.task_id == task_id)
        .order_by(AssetTag.id)  # 入库行序（producer 产出序：相关度降序截断后）
        .all()
    )
    names = _catalog_names(session, rows)
    return GenerationTaskDetail(
        task_id=task.task_id,
        asset_id=asset_id,
        mode=task.mode,
        status=task.status,
        model_version=task.model_version,
        prompt_version=task.prompt_version,
        error=task.error,
        created_at=task.created_at,
        finished_at=task.finished_at,
        results=[_result_out(row, names) for row in rows],
    )


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
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    if task.status != "failed":
        raise HTTPException(status_code=422, detail="仅失败任务可重试")
    task.status = "queued"
    task.error = None
    task.finished_at = None
    session.commit()
    executor.submit(task_id)
    return GenerationTaskRetryResponse(task_id=task_id, status="queued")


@router.get("/assets/{asset_id}/tags", response_model=list[AssetTagOut])
def list_asset_tags(
    asset_id: str, session: Session = Depends(get_session)
) -> list[AssetTagOut]:
    """资产全部生成标签（跨任务，按入库行序）：摘要形状 + 行主键 id +
    tag_id/candidate_name 原样；未知资产 → 404。"""
    if session.get(MediaAsset, asset_id) is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    rows = (
        session.query(AssetTag)
        .filter(AssetTag.asset_id == asset_id)
        .order_by(AssetTag.id)
        .all()
    )
    names = _catalog_names(session, rows)
    return [
        AssetTagOut(
            **_result_out(row, names).model_dump(),
            tag_id=row.tag_id,
            candidate_name=row.candidate_name,
        )
        for row in rows
    ]
