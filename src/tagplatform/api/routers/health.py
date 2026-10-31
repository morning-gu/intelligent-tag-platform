"""`/health` 健康检查路由。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check() -> dict[str, str]:
    """健康检查：服务存活即返回 `{"status": "ok"}`。"""
    return {"status": "ok"}
