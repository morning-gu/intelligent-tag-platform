"""服务器启动入口：API + 前端静态托管合并为单进程 uvicorn。

该机器无 nginx 且无 root，故由应用自身托管构建产物：
dist/assets 走 StaticFiles，其余未命中路径回退 index.html（SPA 路由）。
create_app() 内注册的 /health 与 /api/v1/* 路由先于本文件的兜底路由匹配。
"""

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tagplatform.api import create_app

app = create_app()

DIST = Path(__file__).resolve().parent / "frontend" / "dist"
app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    # /health 与 /api/v1/* 已在应用内注册且优先匹配，不会落到这里
    candidate = DIST / full_path
    if full_path and candidate.is_file():
        return FileResponse(str(candidate))
    return FileResponse(str(DIST / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
