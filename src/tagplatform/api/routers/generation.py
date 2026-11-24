"""`/api/v1/generation` 与 `/api/v1/assets/{asset_id}/tags` 薄路由。

接入层只做协议转换：请求模型声明校验（policy 形状）→
Depends 注入（session/executor）→ 调编排 → 返回。业务规则、读路径 SQL
与响应装配全部在编排层：

- tagplatform.orchestration.tasks——生成任务生命周期（创建/列表/详情/
  任务重跑）；
- tagplatform.orchestration.generation——资产正式挂载视图；
- tagplatform.orchestration._shared——AssetTagOut 行模型与装配三件套。

契约要点（详见编排层模块 docstring）：v2 任务直连 asset_id，创建时场景
子资产归并到所属影片；同步模式（Settings.generation_workers=0，测试与
单步调试）创建响应即真实终态，异步模式回 queued/running 中间态；重试
响应按契约恒回 queued（提交受理语义）；/assets/{id}/tags 只回正式挂载
（asset_tag 审核通过复制行，评分/证据留在 pre 侧）。HTTP 错误映射
（404/422）随逻辑在编排层原样保留。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tagplatform.api import get_executor, get_session
from tagplatform.orchestration import generation as generation_orch
from tagplatform.orchestration import tasks as tasks_orch
from tagplatform.orchestration._shared import AssetTagOut
from tagplatform.orchestration.tasks import (
    GenerationTaskCreateResponse,
    GenerationTaskDetail,
    GenerationTaskOut,
    GenerationTaskRetryResponse,
)
from tagplatform.tasks import TaskExecutor

router = APIRouter(prefix="/api/v1")


# ---------- 请求模型 ----------

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


# ---------- 路由 ----------

@router.post("/generation/tasks", response_model=GenerationTaskCreateResponse)
def create_generation_tasks(
    body: GenerationTaskCreateRequest,
    session: Session = Depends(get_session),
    executor: TaskExecutor = Depends(get_executor),
) -> GenerationTaskCreateResponse:
    """批量创建生成任务并提交执行（校验/归并/建任务/提交在编排层）。"""
    policy_json = body.policy.model_dump_json() if body.policy is not None else None
    return tasks_orch.create_generation_tasks(
        session, executor, body.asset_ids, body.mode, policy_json)


@router.get("/generation/tasks", response_model=list[GenerationTaskOut])
def list_generation_tasks(
    status: str | None = Query(None),
    mode: str | None = Query(None),
    asset_id: str | None = Query(None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[GenerationTaskOut]:
    """任务列表：status/mode/asset_id 过滤，created_at 降序，limit/offset 分页。"""
    return tasks_orch.list_generation_tasks(
        session, status, mode, asset_id, limit, offset)


@router.get("/generation/tasks/{task_id}", response_model=GenerationTaskDetail)
def get_generation_task(
    task_id: str, session: Session = Depends(get_session)
) -> GenerationTaskDetail:
    """任务详情 + 结果摘要列表（tag_name 按 candidate_name/目录名解析）。"""
    return tasks_orch.get_task_detail(session, task_id)


@router.post("/generation/tasks/{task_id}/retry",
             response_model=GenerationTaskRetryResponse)
def retry_generation_task(
    task_id: str,
    session: Session = Depends(get_session),
    executor: TaskExecutor = Depends(get_executor),
) -> GenerationTaskRetryResponse:
    """重试失败任务（仅 failed 可重试，否则 422；受理态 queued）。"""
    return tasks_orch.retry_generation_task(session, executor, task_id)


@router.get("/assets/{asset_id}/tags", response_model=list[AssetTagOut])
def list_asset_tags(
    asset_id: str, session: Session = Depends(get_session)
) -> list[AssetTagOut]:
    """资产正式挂载标签（asset_tag 权威口径；origin/目录列编排层解析）。"""
    return generation_orch.list_asset_tags(session, asset_id)
