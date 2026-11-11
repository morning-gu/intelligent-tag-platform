"""生成任务 API 测试（Task 9）。

覆盖契约（错误映射约定见 brief）：
- POST /api/v1/generation/tasks：端到端同步执行（conftest client fixture
  generation_workers=0，submit 返回即执行完）——closed 模式成功路径
  （响应 tasks 状态 succeeded、详情 results 取 pre_asset_tag、
  /assets/{id}/tags 仅 approved 挂载）、open 模式 mtg 产线成功路径
  （全部 open 自由候选行：origin=open、candidate_name 原文、tag_id 空）、
  mode 未知 422（列可用 mode）、资产未识别 422（自身及父链均无
  content_json；场景子资产则会归并到所属影片，归并后去重）、空 asset_ids
  422、未知资产 404；
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
# （id 为 pre_asset_tag 行主键，审核操作路由的定位键）
RESULT_KEYS = {"id", "tag_name", "tag_id", "candidate_name", "origin",
               "relevance_score", "confidence", "review_status", "target_type",
               "dir_id", "dir_name", "dir_path", "evidence"}
ASSET_TAG_KEYS = RESULT_KEYS | {"created_at"}
# 任务列表项键集（brief 钉死九字段）
TASK_KEYS = {"task_id", "asset_id", "mode", "status", "model_version",
             "prompt_version", "error", "created_at", "finished_at"}


def _import_content(client) -> dict:
    """导入示例影片内容，返回导入响应（asset_id/scene_asset_ids）。"""
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 200
    return resp.json()


def _seed_library(per_type: int = 15) -> None:
    """写 per_type×2 枚标签的 JSON 标签库（两个目录，closed 召回源头）。

    30 枚候选配 min_confidence=0.5 消 MockLLM 采样 flake（见模块 docstring）。
    本期生成只认 JSON 库：/tags/import 仍写 DB（走旧表），不进生成链路。
    """
    from tests.library_fixture import library_root, write_library

    dirs = [
        {"dir_id": "dir_genre", "parent_id": None, "level": 1,
         "dir_name": "主类型", "dir_type": "dir_static",
         "applicable_scope": "content", "description": "故事类型", "aliases": []},
        {"dir_id": "dir_mood", "parent_id": None, "level": 1,
         "dir_name": "情绪基调", "dir_type": "dir_dynamic",
         "applicable_scope": "content", "description": "情绪与氛围", "aliases": []},
    ]
    tags = []
    for i in range(per_type):
        tags.append({"tag_id": f"tag_g_{i:02d}", "dir_id": "dir_genre",
                     "tag_name": f"类型标签{i}", "tag_type": "val_fixed",
                     "description": f"类型描述文本{i}", "status": "active"})
        tags.append({"tag_id": f"tag_m_{i:02d}", "dir_id": "dir_mood",
                     "tag_name": f"情绪标签{i}", "tag_type": "val_fixed",
                     "description": f"情绪描述文本{i}", "status": "active"})
    write_library(library_root(), dirs=dirs, tags=tags)


def _import_catalog(client, per_type: int = 15) -> None:
    """兼容旧调用名：现在等同写 JSON 标签库（closed 生成的数据源）。"""
    _seed_library(per_type)


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
    assert first["origin"] == "closed"
    assert first["tag_name"].startswith(("类型标签", "情绪标签"))
    assert first["dir_id"] in {"dir_genre", "dir_mood"}
    # 命中库内 → 系统自动通过（不进人工队列）
    assert first["review_status"] == "approved"
    assert 0.5 <= first["confidence"] <= 1.0
    assert 0.0 <= first["relevance_score"] <= 1.0
    # MockLLM 场景证据（Task 6）：runner 渲染文本 prompt 含场景行 → 证据卡
    # 一元素非空，数据来自该场景行（fixture 单场景：scene_001 [120000-180000]）
    assert first["evidence"] == [{
        "scene_id": "scene_001", "start_ms": 120000, "end_ms": 180000,
        "description": "两名人物在仓库中发生激烈争执"}]

    # 自动通过的行直接进资产标签视图（无需人工动作）
    tags_resp = client.get("/api/v1/assets/movie_001/tags")
    assert tags_resp.status_code == 200
    tags = tags_resp.json()
    assert len(tags) == len(results)
    assert all(tag["review_status"] == "approved" for tag in tags)
    for row, res in zip(tags, results):
        assert set(row.keys()) == ASSET_TAG_KEYS
        assert row["tag_id"] is not None and row["candidate_name"] is None
        assert row["tag_name"] == res["tag_name"]  # 同按入库行序，逐行对应
        assert row["id"] == res["id"]  # 两端点同一批行的主键一致


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
        assert row["origin"] == "open"
        assert isinstance(row["id"], int) and row["id"] > 0  # 行主键（B3 暴露）
        # 相关度 = 与库内最相近标签的余弦（对齐 top-1）：判成新词也有值，
        # 它是"离库内最近的东西有多近"的信号；库为空/未参与对齐才是 None。
        assert isinstance(row["relevance_score"], float)
        assert 0.0 <= row["relevance_score"] <= 1.0
        assert row["review_status"] == "pending"
        # MockLLM 场景证据（Task 6）：open 行证据卡同样来自场景行（单场景恒定）
        assert row["evidence"] == [{
            "scene_id": "scene_001", "start_ms": 120000, "end_ms": 180000,
            "description": "两名人物在仓库中发生激烈争执"}]

    # 资产标签视图只收 approved 行：新词行全部通过后可见（tag_id 为空）
    assert client.get("/api/v1/assets/movie_001/tags").json() == []
    for row in results:
        assert client.post(f"/api/v1/asset-tags/{row['id']}/review",
                           json={"action": "approve"}).status_code == 200
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


def test_create_task_scene_resolves_to_parent_movie(client):
    """选中场景子资产 → 归并到所属影片建任务（v2 内容只落影片级）。

    场景行 content_json 恒为 NULL，但"按场景挑片建任务"是常见操作：创建
    时沿 parent_id 上溯到带内容的影片，任务挂影片级资产跑，响应 asset_id
    即影片 id（任务列表里出现的 asset_id 与所选场景不同是有意为之）。
    """
    imported = _import_content(client)
    movie_id = imported["asset_id"]
    scene_id = imported["scene_asset_ids"][0]
    _import_catalog(client)
    resp = client.post("/api/v1/generation/tasks", json={
        "asset_ids": [scene_id], "mode": "closed",
        "policy": {"max_tags": 30, "min_confidence": 0.5}})
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["asset_id"] == movie_id  # 归并到影片
    assert tasks[0]["status"] == "succeeded"
    # 结果行同样落影片级资产（pre 行 asset_id = 影片）
    detail = client.get(f"/api/v1/generation/tasks/{tasks[0]['task_id']}").json()
    assert detail["asset_id"] == movie_id and detail["results"]


def test_create_task_scene_and_movie_deduped(client):
    """同时选中影片与其场景 → 归并后去重，只建一条任务。"""
    imported = _import_content(client)
    movie_id = imported["asset_id"]
    scene_id = imported["scene_asset_ids"][0]
    _import_catalog(client)
    resp = client.post("/api/v1/generation/tasks", json={
        "asset_ids": [movie_id, scene_id], "mode": "closed",
        "policy": {"max_tags": 30, "min_confidence": 0.5}})
    assert resp.status_code == 200
    assert [t["asset_id"] for t in resp.json()["tasks"]] == [movie_id]


def test_create_task_orphan_scene_422(client):
    """父级影片也无识别内容（上传占位场景）→ 422，detail 指明场景与父级。"""
    from tagplatform.db.models import MediaAsset

    with client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id="movie_blank", asset_type="movie",
                         title="占位影片", created_at=None))
        s.flush()
        s.add(MediaAsset(asset_id="movie_blank::scene_1", asset_type="scene",
                         parent_id="movie_blank", title="占位场景",
                         created_at=None))
        s.commit()
    resp = client.post("/api/v1/generation/tasks",
                       json={"asset_ids": ["movie_blank::scene_1"], "mode": "closed"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "movie_blank::scene_1" in detail and "movie_blank" in detail


def test_create_task_movie_without_content_422(client):
    """非场景资产自身未识别内容且无父级 → 422 保持原文案。"""
    from tagplatform.db.models import MediaAsset

    with client.app.state.session_factory() as s:
        s.add(MediaAsset(asset_id="movie_raw", asset_type="movie",
                         title="未识别影片", created_at=None))
        s.commit()
    resp = client.post("/api/v1/generation/tasks",
                       json={"asset_ids": ["movie_raw"], "mode": "closed"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "资产 movie_raw 无识别内容"


def test_create_task_unknown_asset_404(client):
    resp = client.post("/api/v1/generation/tasks",
                       json={"asset_ids": ["asset_none"], "mode": "closed"})
    assert resp.status_code == 404
    assert "asset_none" in resp.json()["detail"]


# ---- POST /generation/tasks/{task_id}/retry ----


def test_retry_failed_task_then_succeeds(client):
    """重试路径：标签库未导入时 closed 任务失败 → 补导后 retry → queued →
    succeeded；顺带覆盖非 failed 重试 422（重试后的 succeeded 任务）。"""
    from tests.library_fixture import library_root, write_library

    _import_content(client)
    # 标签库为空：closed 模式召回空候选，同步执行已落 failed 终态
    write_library(library_root(), dirs=[], tags=[])
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
    assert listing[0]["prompt_version"] == "mtg-v3"  # open 自拼 prompt 的口径版本
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
    from tagplatform.db.models import Base, MediaAsset, TagGenerationTask
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


def test_result_out_falls_back_to_tag_id_for_legacy_row():
    """历史行（tag_id 不在 JSON 库、candidate_name 为空）显示 id，不显示空白。

    上一轮已落库的行指向旧 tag_catalog 的 id，切库后查不到名称；这类行
    必须回退到 tag_id，否则资产标签视图会出现无名标签。
    """
    from tagplatform.api.routers.generation import _result_out
    from tagplatform.db.models import PreAssetTag

    row = PreAssetTag(asset_id='a1', target_type='content', target_id='a1',
                      tag_id='tag_legacy_0001', origin='closed',
                      review_status='approved', evidence_json='[]')
    row.id = 1  # 未入库行没有自增主键，摘要里的行 id 需要显式给
    out = _result_out(row, {})
    assert out.tag_name == 'tag_legacy_0001'
    assert out.dir_id is None and out.dir_name is None
