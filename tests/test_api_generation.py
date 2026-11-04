"""生成任务 API 测试（Task 9）。

覆盖契约（错误映射约定见 brief）：
- POST /api/v1/generation/tasks：端到端同步执行（conftest client fixture
  generation_workers=0，submit 返回即执行完）——closed 模式成功路径
  （响应 tasks 状态 succeeded、详情 results 非空、资产标签视图非空）、
  open 模式 generated 候选行、mode 未知 422（列可用 mode）、资产无识别
  内容 422、空 asset_ids 422、未知资产 404；
- GET /api/v1/generation/tasks：status/mode/asset_id 过滤 + created_at
  降序（task_id 决胜）+ limit/offset 分页；
- GET /api/v1/generation/tasks/{task_id}：详情 + results 摘要（tag_name
  按 origin 取、evidence 解析、行 id 主键（B3 暴露，Task 4 审核操作定位
  键）、prompt_version 为字符串 "1"——TEXT 列经 SQLite 亲和读回，T8 附
  注）；未知任务 404；
- POST /api/v1/generation/tasks/{task_id}/retry：仅 failed 可重试（否则
  422 "仅失败任务可重试"）——先造 failed 任务（标签库未导入时 closed
  生成失败），补导标签库后重试至 succeeded；未知任务 404；
- GET /api/v1/prompts?mode=：v1 种子模板列表（closed/open 各一）；
- GET /api/v1/assets/{asset_id}/tags：资产全部标签行（摘要形状 + 行 id
  主键 + tag_id/candidate_name 原样）；未知资产 404。

Mock 技巧（与 llm/mock.py 咬合，T8 测试同款消 flake 构造）：closed 用
policy.min_confidence=0.5（MockLLM confidence 值域 [0.5,1) 恒过线）+
30 枚候选（零选中概率 0.6^30 ≈ 2e-7）；名称/描述逐枚不同，MockEmbedder
规范化后近似正交，治理 semantic_similar（阈值 0.90）不会误拦。
"""

import io
import json
import time
from pathlib import Path

import openpyxl

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "content_movie.json"
CONTENT_FIXTURE_DICT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# 结果摘要形状（详情 results）与资产标签行（摘要 + 标识原样）的键集契约
# （id 为 asset_tag 行主键，阶段 3 B3 暴露——Task 4 审核操作路由的定位键）
RESULT_KEYS = {"id", "tag_name", "origin", "relevance_score", "confidence",
               "review_status", "level", "evidence"}
ASSET_TAG_KEYS = RESULT_KEYS | {"tag_id", "candidate_name"}
# 任务列表项键集（brief 钉死九字段）
TASK_KEYS = {"task_id", "asset_id", "mode", "status", "model_version",
             "prompt_version", "error", "created_at", "finished_at"}


def _import_content(client) -> dict:
    """导入示例影片内容，返回导入响应（asset_id/context_id/scene_asset_ids）。"""
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 200
    return resp.json()


def _import_catalog(client, per_type: int = 15) -> None:
    """经 /api/v1/tags/import 导入 per_type×2 枚目录标签（genre/mood 各 per_type）。

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


# ---- POST /generation/tasks：端到端同步执行 ----


def test_generation_e2e_closed_succeeded(client):
    """导标签库 → 导内容 → POST closed → succeeded → 详情 results 非空 →
    资产标签视图非空（计划 Step 1 主链路）。"""
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

    # 任务详情：results 非空且字段齐全
    detail_resp = client.get(f"/api/v1/generation/tasks/{task_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["task_id"] == task_id
    assert detail["asset_id"] == "movie_001"
    assert detail["mode"] == "closed" and detail["status"] == "succeeded"
    assert detail["model_version"] == "mock-1"
    # prompt_version 列为 TEXT：API 响应以字符串出现（T8 附注）
    assert detail["prompt_version"] == "1"
    assert detail["error"] is None and detail["finished_at"] is not None
    assert detail["created_at"] is not None
    results = detail["results"]
    assert len(results) >= 1
    first = results[0]
    assert set(first.keys()) == RESULT_KEYS
    assert isinstance(first["id"], int) and first["id"] > 0  # 行主键（B3 暴露）
    assert len({r["id"] for r in results}) == len(results)  # 行 id 唯一
    assert first["origin"] == "catalog"
    assert first["tag_name"].startswith(("类型标签", "情绪标签"))
    assert first["review_status"] == "pending"
    assert 0.5 <= first["confidence"] <= 1.0
    assert 0.0 <= first["relevance_score"] <= 1.0
    # MockLLM 场景证据（Task 6）：runner 渲染文本 prompt 含场景行 → 证据卡
    # 一元素非空，数据来自该场景行（fixture 单场景：scene_001 [120000-180000]）
    assert first["evidence"] == [{
        "scene_id": "scene_001", "start_ms": 120000, "end_ms": 180000,
        "description": "两名人物在仓库中发生激烈争执"}]

    # 资产标签视图：摘要形状 + tag_id/candidate_name 原样（catalog 行）
    tags_resp = client.get("/api/v1/assets/movie_001/tags")
    assert tags_resp.status_code == 200
    tags = tags_resp.json()
    assert len(tags) == len(results)
    for row, res in zip(tags, results):
        assert set(row.keys()) == ASSET_TAG_KEYS
        assert row["tag_id"] is not None and row["candidate_name"] is None
        assert row["tag_name"] == res["tag_name"]  # 同按入库行序，逐行对应
        assert row["id"] == res["id"]  # 两端点同一批行的主键一致（B3 暴露）


def test_generation_e2e_open_generated_rows(client):
    """open 模式：MockLLM 固定三候选，目录归一未命中 → 全部 generated 行。"""
    _import_catalog(client)
    _import_content(client)
    resp = client.post("/api/v1/generation/tasks", json={
        "asset_ids": ["movie_001"], "mode": "open",
        "policy": {"min_confidence": 0.5}})
    assert resp.status_code == 200
    assert resp.json()["tasks"][0]["status"] == "succeeded"
    task_id = resp.json()["tasks"][0]["task_id"]

    detail = client.get(f"/api/v1/generation/tasks/{task_id}").json()
    assert detail["status"] == "succeeded"
    results = detail["results"]
    assert len(results) == 3  # MockLLM open 固定返回 3 候选
    names = {r["tag_name"] for r in results}
    assert names == {"多线叙事", "情感张力", "视听风格"}
    for row in results:
        assert row["origin"] == "generated"
        assert isinstance(row["id"], int) and row["id"] > 0  # 行主键（B3 暴露）
        assert row["relevance_score"] is None  # 未命中目录：无相关度
        assert row["review_status"] == "pending"
        # MockLLM 场景证据（Task 6）：open 行证据卡同样来自场景行（单场景恒定）
        assert row["evidence"] == [{
            "scene_id": "scene_001", "start_ms": 120000, "end_ms": 180000,
            "description": "两名人物在仓库中发生激烈争执"}]

    # 资产标签视图：generated 行 tag_id 为空、candidate_name 原样
    tags = client.get("/api/v1/assets/movie_001/tags").json()
    assert len(tags) == 3
    for row in tags:
        assert set(row.keys()) == ASSET_TAG_KEYS
        assert row["tag_id"] is None
        assert row["candidate_name"] == row["tag_name"]
    assert {row["candidate_name"] for row in tags} == names


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


def test_create_task_scene_without_context_422(client):
    """资产存在但无 media_context（场景资产）→ 整请求 422。"""
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

    # 非 failed 不可重试（failed 状态本身合法，先验证 succeeded 任务被拒）
    _import_catalog(client)
    ok_resp = client.post("/api/v1/generation/tasks", json={
        "asset_ids": ["movie_001"], "mode": "open",
        "policy": {"min_confidence": 0.5}})
    ok_id = ok_resp.json()["tasks"][0]["task_id"]
    denied = client.post(f"/api/v1/generation/tasks/{ok_id}/retry")
    assert denied.status_code == 422
    assert denied.json()["detail"] == "仅失败任务可重试"

    # 重试失败任务：状态回 queued（提交受理语义），同步执行随即完成
    retry = client.post(f"/api/v1/generation/tasks/{failed_id}/retry")
    assert retry.status_code == 200
    assert retry.json() == {"task_id": failed_id, "status": "queued"}

    detail = client.get(f"/api/v1/generation/tasks/{failed_id}").json()
    assert detail["status"] == "succeeded"
    assert len(detail["results"]) >= 1  # 标签库已补导，重试真触发生成
    assert detail["error"] is None and detail["finished_at"] is not None
    assert detail["prompt_version"] == "1"


# ---- GET /generation/tasks：列表过滤与分页 ----


def test_task_list_filters_and_pagination(client):
    _import_catalog(client)
    _import_content(client)
    first = client.post("/api/v1/generation/tasks", json={
        "asset_ids": ["movie_001"], "mode": "closed",
        "policy": {"min_confidence": 0.5}}).json()["tasks"][0]
    time.sleep(0.02)  # 保证 created_at 严格递增（Windows 时钟粒度粗）
    second = client.post("/api/v1/generation/tasks", json={
        "asset_ids": ["movie_001"], "mode": "open",
        "policy": {"min_confidence": 0.5}}).json()["tasks"][0]

    listing = client.get("/api/v1/generation/tasks").json()
    assert len(listing) == 2
    assert all(set(row.keys()) == TASK_KEYS for row in listing)
    # created_at 降序：后创建的 open 任务在前
    assert [row["task_id"] for row in listing] == [second["task_id"], first["task_id"]]
    assert listing[0]["prompt_version"] == "mtg-v1"  # open 自拼 prompt 的口径版本
    assert listing[0]["asset_id"] == "movie_001"

    # status / mode / asset_id 过滤
    assert [r["task_id"] for r in
            client.get("/api/v1/generation/tasks?status=succeeded").json()] == \
        [second["task_id"], first["task_id"]]
    assert client.get("/api/v1/generation/tasks?status=failed").json() == []
    assert [r["task_id"] for r in
            client.get("/api/v1/generation/tasks?mode=closed").json()] == \
        [first["task_id"]]
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
    # template_text 直接随列表返回（运营侧新建版本预填依赖，阶段 4 审查轮 1）
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
    from tagplatform.db.models import Base, GenerationTask, MediaAsset, MediaContext
    from tagplatform.settings import Settings

    db_path = str(tmp_path / "orphan.db")
    settings = Settings(database_path=db_path, use_mock_embedder=True,
                        use_mock_llm=True, generation_workers=0)
    engine = create_engine_for(db_path)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    # 预置资产/上下文/running 孤儿任务（模拟进程崩溃滞留；flush 保证
    # 外键父行先落库——同批 flush 按类名字母序会先插子行而炸外键）
    with factory() as s:
        s.add(MediaAsset(asset_id="movie_001", asset_type="movie",
                         title="示例电影", created_at=None))
        s.flush()
        s.add(MediaContext(context_id="ctx_orphan", asset_id="movie_001",
                           content_json="{}"))
        s.flush()
        s.add(GenerationTask(task_id="task_orphan", context_id="ctx_orphan",
                             mode="closed", status="running"))
        s.commit()
        task_id = "task_orphan"

    app = create_app(settings)
    with TestClient(app):  # 上下文进入触发 lifespan 启动（孤儿清理执行）
        pass
    with factory() as s:
        row = s.get(GenerationTask, task_id)
        assert row.status == "failed" and row.error == "进程重启中断"
        assert row.finished_at is not None
    app.state.engine.dispose()
    engine.dispose()
