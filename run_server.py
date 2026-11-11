"""服务器启动入口：API + 前端静态托管合并为单进程 uvicorn。

该机器无 nginx 且无 root，故由应用自身托管构建产物：未命中的路径先按
磁盘文件尝试（dist 下真实产物，含 assets/*.js|css），否则回退
index.html（SPA 深链，如 /assets/{id}、/review）。

注意：**不要**把 dist/assets 单独 mount 到 /assets ——前端路由 /assets/:id
与 Vite 产物目录同名，mount 会先于 SPA 兜底匹配，深链刷新直接 404 JSON。
create_app() 内注册的 /health 与 /api/v1/* 路由先于本文件的兜底路由匹配。
"""

from pathlib import Path

from fastapi.responses import FileResponse

from tagplatform.api import create_app

app = create_app()

DIST = Path(__file__).resolve().parent / "frontend" / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    # /health 与 /api/v1/* 已在应用内注册且优先匹配，不会落到这里
    if full_path:
        candidate = (DIST / full_path).resolve()
        # 只放行 dist 内的文件（防 ../ 穿越），命中的静态产物直接返回
        if candidate.is_relative_to(DIST) and candidate.is_file():
            return FileResponse(str(candidate))
    return FileResponse(str(DIST / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
