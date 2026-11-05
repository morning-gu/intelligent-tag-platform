"""生成任务 API 测试（v2 重写，Task 6）。

覆盖契约（错误映射约定见 brief；open 模式端到端归 Task 7——其 mtg
流水线 origin 词表 v2 化之前无法成功落库）：
- POST /api/v1/generation/tasks：端到端同步执行（conftest client fixture
  generation_workers=0，submit 返回即执行完）——closed 模式成功路径
  （响应 tasks 状态 succeeded、详情 results 取 pre_asset_tag、
  /assets/{id}/tags 仅 approved 挂载）、mode 未知 422（列可用 mode）、
  资产未识别（content_json 空，如场景子资产）422、空 asset_ids 422、
  未知资产 404；
- GET /api/v1/generation/tasks：status/mode/asset_id 过滤 + created_at
  降序（task_id 决胜）+ limit/offset 分页（asset_id 直列，无外连）；
- GET /api/v1/generation/tasks/{task_id}：详情 + results 摘要（pre 行：
  tag_name 解析 = candidate_name 或目录定义名、origin 词表 closed|open、
  evidence 解析、行 id 主键、prompt_version 为字符串 "1"）；未知任务 404；
- POST /api/v1/generation/tasks/{task_id}/retry：仅 failed 可重试（否则
  422 "仅失败任务可重试"）——先造 failed 任务（标签库未导入时 closed
  生成失败），补导标签库后重试至 succeeded；未知任务 404；
- GET /api/v1/prompts?mode=：种子模板列表；
- GET /api/v1/assets/{asset_id}/tags：仅正式挂载（asset_tag 只含审核
  通过的复制行，评分/证据在 pre 侧），origin 经 pre 行解析；未知资产 404。

Mock 技巧（与 llm/mock.py 咬合，消 flake 构造同 v1）：closed 用
policy.min_confidence=0.5（MockLLM confidence 值域 [0.5,1) 恒过线）+
30 枚候选（零选中概率 0.6^30 ≈ 2e-7）；名称/描述逐枚不同，MockEmbedder
规范化后近似正交，治理 semantic_similar（阈值 0.90）不会误拦。

审核域过渡：review 路由/服务属 Task 8 域尚未切换——approved 挂载由测试
内直接置 pre 行终态并复制 asset_tag 行模拟（doc §6.2 的 approve 行操作），
/Assets/{id}/tags 的数据源契约验证不受审核路由影响。
"""

import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "content_movie.json"
CONTENT_FIXTURE_DICT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# 结果摘要形状（详情 results，pre_asset_tag 行）与正式挂载行（/assets/{id}/tags）
# 的键集契约（id 为行主键——审核操作路由的定位键；正式挂载无评分/证据）
RESULT_KEYS = {"id", "tag_name", "tag_id", "candidate_name", "origin",
               "relevance_score", "confidence", "review_status", "evidence"}
ASSET_TAG_KEYS = {"id", "tag_id", "tag_name", "origin", "created_at"}
# 任务列表项键集（brief 钉死九字段）
TASK_KEYS = {"task_id", "asset_id", "mode", "status", "model_version",
             "prompt_version", "error", "created_at", "finished_at"}


def _import_content(client) -> dict:
    """导入示例影片内容，返回导入响应（asset_id/scene_asset_ids）。"""
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 200
    return resp.json()


def _import_catalog(client, per_type: int = 15) -> None:
    """经 /api/v1/tags/import 导入 per_type×2 枚标签（v2 扁平导入落入
    自动根目录「未分类」，单目录 30 枚候选 → recall_by_dir 全量召回）。

    30 枚候选配 min_confidence=0.5 消 MockLLM 采样 flake（见模块 docstring）。
    """
    rows = [["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"]]
    for i in range(per_type):
        rows.append([f"类型标签{i}", "genre", f"类型描述文本{i}", None, None, "active"])
    for i in range(per_type):
        rows.append([f"情绪标签{i}", "mood", f"情绪描述文本{i}", None, None, "active"])
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    resp = client.post("/api/v1/tags/import",
                       files={"file": ("tags.xlsx", buffer.getvalue())})
    assert resp.status_code == 200
    assert resp.json()["summary"]["inserted"] == per_type * 2


def _approve_and_mount(client, task_id: str) -> None:
    """把任务首个 pre 行置 approved 并复制 asset_tag 挂载行。

    审核域（Task 8）切换前的过渡做法：直接库内模拟"审核通过 → 复制挂载"
    的行操作（doc §6.2），/assets/{id}/tags 的数据源验证不受审核路由影响。
    """
    from tagplatform.db.models import AssetTag, PreAssetTag, TagDefine

    with client.app.state.session_factory() as s:
        row = (s.query(PreAssetTag).filter(PreAssetTag.task_id == task_id)
               .order_by(PreAssetTag.id).first())
        define = s.get(TagDefine, row.tag_id)
        row.review_status = "approved"
        s.add(AssetTag(
            asset_id=row.asset_id, target_type=row.target_type,
            target_id=row.target_id, tag_id=row.tag_id,
            tag_name=define.tag_name,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)))
        s.commit()


# ---- POST /generation/tasks：端到端同步执行 ----


def test_generation_e2e_closed_succeeded(client):
    """导标签库 → 导内容 → POST closed → succeeded → 详情 results（pre 行）
    → /assets/{id}/tags 仅 approved 挂载（生成后为空，置终态后出挂载）。"""
    _import_catalog(client)
    _import_content(client)
    resp = client.post("/api/v1/generation/tasks", json={
        "asset_ids": ["movie_001"], "mode": "closed",
        "policy": {"max_tags": 30, "min_confidence": 0.5}})
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 1
    item = tasks[0]
    assert item["asset_id"] == "movie_001"
    # 同步模式（generation_workers=0）：POST 返回时任务已执行完
    assert item["status"] == "succeeded"
    task_id = item["task_id"]
    assert task_id

    # 任务详情：results 取 pre_asset_tag 且字段齐全
    detail_resp = client.get(f"/api/v1/generation/tasks/{task_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["task_id"] == task_id
    assert detail["asset_id"] == "movie_001"
    assert detail["mode"] == "closed" and detail["status"] == "succeeded"
    assert detail["model_version"] == "mock-1"
    # prompt_version 列为 TEXT：API 响应以字符串出现
    assert detail["prompt_version"] == "1"
    assert detail["error"] is None and detail["finished_at"] is not None
    assert detail["created_at"] is not None
    results = detail["results"]
    assert len(results) >= 1
    first = results[0]
    assert set(first.keys()) == RESULT_KEYS
    assert isinstance(first["id"], int) and first["id"] > 0  # 行主键
    assert len({r["id"] for r in results}) == len(results)  # 行 id 唯一
    assert first["origin"] == "closed"  # v2 词表（catalog 退役）
    assert first["tag_id"] is not None and first["candidate_name"] is None
    assert first["tag_name"].startswith(("类型标签", "情绪标签"))
    assert first["review_status"] == "pending"
    assert 0.5 <= first["confidence"] <= 1.0
    assert 0.0 <= first["relevance_score"] <= 1.0
    # MockLLM 场景证据：runner 渲染文本 prompt 含场景行 → 证据卡一元素
    # 非空，数据来自该场景行（fixture 单场景：scene_001 [120000-180000]）
    assert first["evidence"] == [{
        "scene_id": "scene_001", "start_ms": 120000, "end_ms": 180000,
        "description": "两名人物在仓库中发生激烈争执"}]

    # 资产正式挂载视图：生成结果尚全部 pending → 空（仅 approved 外露）
    tags_resp = client.get("/api/v1/assets/movie_001/tags")
    assert tags_resp.status_code == 200
    assert tags_resp.json() == []

    # 置终态 + 复制挂载后：唯一 approved 挂载出现，origin 经 pre 行解析
    _approve_and_mount(client, task_id)
    tags = client.get("/api/v1/assets/movie_001/tags").json()
    assert len(tags) == 1
    row = tags[0]
    assert set(row.keys()) == ASSET_TAG_KEYS
    assert row["id"] > 0 and row["tag_id"] == first["tag_id"]
    assert row["tag_name"] == first["tag_name"]  # 挂载行拷贝定义名
    assert row["origin"] == "closed"
    assert row["created_at"] is not None


def test_create_task_empty_asset_ids_422(client):
    resp = client.post("/api/v1/generation/tasks",
                       json={"asset_ids": [], "mode": "closed"})
    assert resp.status_code == 422


def test_create_task_unknown_mode_422(client):
    """mode 未知 → 422 且 detail 列出可用 mode（closed/open 均已注册）。"""
    resp = client.post("/api/v1/generation/tasks",
                       json={"asset_ids": ["movie_001"], "mode": "hybrid"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "closed" in detail and "open" in detail


def test_create_task_scene_without_content_422(client):
    """场景资产 content_json 为空（内容挂影片级）→ 整请求 422，不 500。"""
    imported = _import_content(client)
    scene_id = imported["scene_asset_ids"][0]
    resp = client.post("/api/v1/generation/tasks",
                       json={"asset_ids": [scene_id], "mode": "closed"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"资产 {scene_id} 无识别内容"


def test_create_task_unknown_asset_404(client):
    resp = client.post("/api/v1/generation/tasks",
                       json={"asset_ids": ["asset_none"], "mode": "closed"})
    assert resp.status_code == 404
    assert "asset_none" in resp.json()["detail"]


# ---- POST /generation/tasks/{task_id}/retry ----


def test_retry_failed_task_then_succeeds(client):
    """重试路径：标签库未导入时 closed 任务失败 → 补导后 retry → queued →
    succeeded；顺带覆盖非 failed 重试 422（重试后的 succeeded 任务）。"""
    _import_content(client)
    # 标签库未导入：closed 模式召回空候选，同步执行已落 failed 终态
    resp = client.post("/api/v1/generation/tasks", json={
        "asset_ids": ["movie_001"], "mode": "closed"})
    assert resp.status_code == 200
    assert resp.json()["tasks"][0]["status"] == "failed"
    failed_id = resp.json()["tasks"][0]["task_id"]
    detail = client.get(f"/api/v1/generation/tasks/{failed_id}").json()
    assert detail["status"] == "failed" and "候选标签为空" in detail["error"]

    # 补导标签库后重试：状态回 queued（提交受理语义），同步执行随即完成
    _import_catalog(client)
    retry = client.post(f"/api/v1/generation/tasks/{failed_id}/retry")
    assert retry.status_code == 200
    assert retry.json() == {"task_id": failed_id, "status": "queued"}

    detail = client.get(f"/api/v1/generation/tasks/{failed_id}").json()
    assert detail["status"] == "succeeded"
    assert len(detail["results"]) >= 1  # 标签库已补导，重试真触发生成
    assert detail["error"] is None and detail["finished_at"] is not None
    assert detail["prompt_version"] == "1"

    # 非 failed 不可重试（此时任务已 succeeded）
    denied = client.post(f"/api/v1/generation/tasks/{failed_id}/retry")
    assert denied.status_code == 422
    assert denied.json()["detail"] == "仅失败任务可重试"


# ---- GET /generation/tasks：列表过滤与分页 ----


def test_task_list_filters_and_pagination(client):
    _import_catalog(client)
    _import_content(client)
    body = {"asset_ids": ["movie_001"], "mode": "closed",
            "policy": {"min_confidence": 0.5}}
    first = client.post("/api/v1/generation/tasks", json=body).json()["tasks"][0]
    time.sleep(0.02)  # 保证 created_at 严格递增（Windows 时钟粒度粗）
    second = client.post("/api/v1/generation/tasks", json=body).json()["tasks"][0]

    listing = client.get("/api/v1/generation/tasks").json()
    assert len(listing) == 2
    assert all(set(row.keys()) == TASK_KEYS for row in listing)
    # created_at 降序：后创建的任务在前
    assert [row["task_id"] for row in listing] == [second["task_id"], first["task_id"]]
    assert listing[0]["asset_id"] == "movie_001"  # v2：asset_id 直列

    # status / mode / asset_id 过滤
    assert [r["task_id"] for r in
            client.get("/api/v1/generation/tasks?status=succeeded").json()] == \
        [second["task_id"], first["task_id"]]
    assert client.get("/api/v1/generation/tasks?status=failed").json() == []
    assert [r["task_id"] for r in
            client.get("/api/v1/generation/tasks?mode=closed").json()] == \
        [second["task_id"], first["task_id"]]
    assert client.get("/api/v1/generation/tasks?mode=open").json() == []
    assert [r["task_id"] for r in
            client.get("/api/v1/generation/tasks?asset_id=movie_001").json()] == \
        [second["task_id"], first["task_id"]]
    assert client.get("/api/v1/generation/tasks?asset_id=asset_none").json() == []

    # limit/offset 分页（默认 100/0）
    assert [r["task_id"] for r in
            client.get("/api/v1/generation/tasks?limit=1").json()] == \
        [second["task_id"]]
    assert [r["task_id"] for r in
            client.get("/api/v1/generation/tasks?limit=1&offset=1").json()] == \
        [first["task_id"]]


# ---- GET /prompts ----


def test_prompts_list(client):
    """种子模板：closed/open v1 激活；open 另有 v2 过时标注（inactive）。"""
    rows = client.get("/api/v1/prompts").json()
    assert {(r["mode"], r["version"], r["is_active"]) for r in rows} == {
        ("closed", 1, True), ("open", 1, True), ("open", 2, False)}
    assert all(r["created_at"] is not None for r in rows)
    # template_text 直接随列表返回（运营侧新建版本预填依赖）
    by_key = {(r["mode"], r["version"]): r for r in rows}
    assert "{content}" in by_key[("closed", 1)]["template_text"]
    assert "{candidates}" in by_key[("closed", 1)]["template_text"]
    assert "{content}" in by_key[("open", 1)]["template_text"]
    # open v2 是"已过时"标注行：inactive，且正文写明不再被消费
    assert "已过时" in by_key[("open", 2)]["template_text"]

    closed = client.get("/api/v1/prompts?mode=closed").json()
    assert [(r["mode"], r["version"], r["is_active"]) for r in closed] == \
        [("closed", 1, True)]
    assert "{candidates}" in closed[0]["template_text"]


# ---- 404：未知任务/资产 ----


def test_unknown_task_and_asset_404(client):
    assert client.get("/api/v1/generation/tasks/task_none").status_code == 404
    assert client.post(
        "/api/v1/generation/tasks/task_none/retry").status_code == 404
    assert client.get("/api/v1/assets/asset_none/tags").status_code == 404


# ---- lifespan 启动孤儿清理 ----


def test_lifespan_orphan_reset_on_startup(tmp_path):
    """create_app 启动即孤儿清理：预置 running 任务在 app 启动后被复位 failed
    （lifespan 三件之 reset_orphan_running，进程重启兜底语义的 API 装配验证）。"""
    from fastapi.testclient import TestClient

    from tagplatform.api import create_app
    from tagplatform.db import create_engine_for, make_session_factory
    from tagplatform.db.models import Base, MediaAsset, TagGenerationTask
    from tagplatform.settings import Settings

    db_path = str(tmp_path / "orphan.db")
    settings = Settings(database_path=db_path, use_mock_embedder=True,
                        use_mock_llm=True, generation_workers=0)
    engine = create_engine_for(db_path)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    # 预置资产/running 孤儿任务（模拟进程崩溃滞留；flush 保证外键父行
    # 先落库——同批 flush 按类名字母序会先插子行而炸外键）
    with factory() as s:
        s.add(MediaAsset(asset_id="movie_001", asset_type="movie",
                         title="示例电影", created_at=None))
        s.flush()
        s.add(TagGenerationTask(task_id="task_orphan", asset_id="movie_001",
                                mode="closed", status="running"))
        s.commit()
        task_id = "task_orphan"

    app = create_app(settings)
    with TestClient(app):  # 上下文进入触发 lifespan 启动（孤儿清理执行）
        pass
    with factory() as s:
        row = s.get(TagGenerationTask, task_id)
        assert row.status == "failed" and row.error == "进程重启中断"
        assert row.finished_at is not None
    app.state.engine.dispose()
    engine.dispose()
