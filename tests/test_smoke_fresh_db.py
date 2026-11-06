"""全新库端到端冒烟（v2 表结构全平台切换收官）。

create_app + TestClient + create_all（conftest client fixture 同口径——
应用自身不建表，先按 Base.metadata 建临时文件库再启动；alembic 迁移链由
test_migration_0007 独立覆盖，两路互斥不重复）。自建 app 不经 client
fixture：登录是冒烟链路的第 1 步，须由用例自己断言，而非 fixture 预置。

链路（真 app、真文件 DB、Mock 嵌入器/LLM 零网络）：
1. 登录 admin → 200 + HttpOnly 会话 cookie + me 可用；
2. POST /api/v1/tags/import 层级 Excel（openpyxl 内存造三级行）→
   {task_id, summary}，inserted == 30（目录行不计入）；
3. POST /api/v1/content/import（platform 适配器形状 JSON）→ asset_id，
   详情 recognized=True；
4. POST /api/v1/generation/tasks mode="closed"（MockLLM/MockEmbedder 经
   Settings 环境开关默认接入）→ 任务 succeeded；GET /generation/tasks/{id}
   results 非空（origin=closed、review_status=pending、场景证据非空）；
5. POST /api/v1/asset-tags/{id}/review action=approve → 挂载出现在
   GET /api/v1/assets/{asset_id}/tags；
6. GET /api/v1/metrics/summary 三键非零（pending / approved 计数 /
   任务 succeeded）+ GET /metrics/dir-coverage 与 GET
   /metrics/confidence-distribution 非空（承诺二③④的 metrics 三端点
   全部触达）。

Mock 咬合（消 flake 构造，同 test_api_generation/test_generation_closed）：
- 30 枚候选配 policy.min_confidence=0.5：MockLLM confidence ∈ [0.5,1) 恒
  过线，零选中概率 0.6^30 ≈ 2e-7（recall_top_k_per_type 默认 50，每目录
  10 枚全量召回，候选数 = 导入标签数）；
- 治理导入自动算标签向量（passage = "名称。描述"）——内容场景描述
  「两名人物在仓库中发生激烈争执」与标签「两名人物」。「在仓库中发生激烈
  争执」的 passage 文本规范化形（丢弃标点不补空格）逐字相同，MockEmbedder
  同形同向量、余弦恰 1.0——召回命中为构造保证而非哈希运气。
"""

import io
import json
import os
import re
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from tagplatform.db.models import MediaAsset

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "content_movie.json"
CONTENT_FIXTURE_DICT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

# 层级模板表头（governance.excel.NEW_EXCEL_COLUMNS 六列，三级行 = 一级 +
# 二级目录被下级行引用成目录链，标签挂二级下）
_HIER_HEADER = ["一级标签名称", "二级标签名称", "三级标签名称",
                "标签", "标签描述", "标签生成方式"]

# 三个二级目录 × 各 10 枚标签 = 30 枚候选（消 MockLLM 零选中 flake）；
# 「类型」目录首枚为召回构造位（见模块 docstring 与 _hierarchy_xlsx）
_LEVEL2_DIRS = ["类型", "情感", "风格"]


def _hierarchy_xlsx() -> bytes:
    """openpyxl 内存造层级 Excel：1 个一级目录 + 3 个二级目录 + 30 枚标签。"""
    rows = [_HIER_HEADER]
    # 首枚（召回构造位）：passage 文本 "两名人物。在仓库中发生激烈争执" 的
    # 规范化形与 fixture 场景描述逐字相同 → MockEmbedder 余弦恰 1.0
    rows.append(["影视", "类型", None, "两名人物", "在仓库中发生激烈争执", None])
    for dir_name in _LEVEL2_DIRS:
        for i in range(10):
            if dir_name == "类型" and i == 0:
                continue  # 构造位已插入，补足该目录 10 枚
            rows.append(["影视", dir_name, None,
                         f"标签{dir_name}{i}", f"定义{dir_name}文本{i}", None])
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def smoke_client(tmp_path):
    """全新库冒烟客户端：临时文件库 create_all + create_app + TestClient。

    与 conftest client fixture 完全同口径（Settings mock 开关 + workers=0
    同步执行 + dataset_root 指向空目录）；区别仅在登录留给链路第 1 步。
    """
    from tagplatform.api import create_app
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base
    from tagplatform.settings import Settings

    db_path = tmp_path / "smoke.db"
    settings = Settings(database_path=str(db_path), use_mock_embedder=True,
                        use_mock_llm=True, generation_workers=0,
                        dataset_root=str(tmp_path / "movienet"))
    engine = create_engine_for(str(db_path))
    Base.metadata.create_all(engine)
    engine.dispose()  # 建表引擎即弃，app 内部自建引擎

    app = create_app(settings)
    with TestClient(app) as http_client:
        yield http_client
    app.state.engine.dispose()  # 释放 app 级连接（Windows 文件句柄）


def test_fresh_db_smoke_full_chain(smoke_client):
    """全新库六步链：登录 → 导层级标签 → 导内容 → closed 生成 → 审核
    approve → metrics 三端点有数（每步断言到值级，不止状态码）。"""
    client = smoke_client

    # 1. 登录 admin（cookie 会话）：200 + 角色 + 会话 cookie + 登录态可用
    login = client.post("/api/v1/auth/login", json={
        "username": "admin",
        "password": os.environ.get("TAGPLT_ADMIN_PASSWORD", "admin123")})
    assert login.status_code == 200, login.text
    assert login.json()["role"] == "admin"
    assert "tagplatform_session=" in login.headers["set-cookie"]
    assert "httponly" in login.headers["set-cookie"].lower()
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["username"] == "admin"

    # 2. 导入层级标签 Excel → {task_id, summary}；inserted 仅计标签行
    #    （4 个目录行 + 30 个标签行进治理：total=34、inserted=30、skipped=4）
    resp = client.post("/api/v1/tags/import",
                       files={"file": ("tags.xlsx", _hierarchy_xlsx())})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"]
    summary = body["summary"]
    assert summary["inserted"] == 30 > 0
    assert summary["total"] == 34 and summary["skipped"] == 4
    assert summary["issues"] == [] and summary["parse_errors"] == []
    task_detail = client.get(f"/api/v1/import/tasks/{body['task_id']}")
    assert task_detail.status_code == 200
    assert task_detail.json()["status"] == "completed"

    # 3. 导入内容（platform 适配器形状 JSON）→ asset_id + 场景资产行
    resp = client.post("/api/v1/content/import", json={
        "kind": "platform", "video_root": None, "data": CONTENT_FIXTURE_DICT})
    assert resp.status_code == 200, resp.text
    asset_id = resp.json()["asset_id"]
    assert asset_id == "movie_001"
    assert resp.json()["scene_asset_ids"] == ["movie_001::scene_001"]
    detail = client.get(f"/api/v1/assets/{asset_id}")
    assert detail.status_code == 200 and detail.json()["recognized"] is True

    # 4. closed 生成任务：同步模式（workers=0）POST 返回即终态 succeeded；
    #    详情 results 非空，行形态 = closed 契约（tag_id 有、候选字段空）
    resp = client.post("/api/v1/generation/tasks", json={
        "asset_ids": [asset_id], "mode": "closed",
        "policy": {"max_tags": 30, "min_confidence": 0.5}})
    assert resp.status_code == 200, resp.text
    created = resp.json()["tasks"][0]
    assert created["asset_id"] == asset_id
    assert created["status"] == "succeeded", created
    gen_detail = client.get(f"/api/v1/generation/tasks/{created['task_id']}")
    assert gen_detail.status_code == 200
    assert gen_detail.json()["mode"] == "closed"
    assert gen_detail.json()["status"] == "succeeded"
    assert gen_detail.json()["model_version"] == "mock-1"
    results = gen_detail.json()["results"]
    assert len(results) >= 1
    first = results[0]
    assert first["origin"] == "closed"
    assert first["review_status"] == "pending"
    assert first["tag_id"] and first["candidate_name"] is None
    assert 0.5 <= first["confidence"] <= 1.0
    assert 0.0 <= first["relevance_score"] <= 1.0
    # 场景证据非空且锚定到内容场景（fixture 单场景 scene_001）
    assert first["evidence"]
    assert first["evidence"][0]["scene_id"] == "scene_001"
    assert first["evidence"][0]["start_ms"] == 120000
    assert first["evidence"][0]["end_ms"] == 180000

    # 召回中间量钉死（消静默退化：治理导入漏算向量/召回行被拦减少时在此
    # 暴露，而非仅靠 MockLLM 选中概率兜底）——按 runner 同款路径复算：
    # 30 枚导入标签全量召回（top_k=50 ≥ 每目录 10 枚）
    from tagplatform.content_io import load_content
    from tagplatform.generation.recall import embed_content_texts, recall_by_dir
    with client.app.state.session_factory() as s:
        content = load_content(s.get(MediaAsset, asset_id).content_json)
        candidates = recall_by_dir(s, embed_content_texts(content),
                                   client.app.state.embedder, 50)
    assert len(candidates) == 30
    # 产出 ⊆ 候选（同键闭合：LLM 只能从候选清单选择，不得编造）
    assert {r["tag_id"] for r in results} <= {c.tag_id for c in candidates}

    # 5. 审核 approve → 正式挂载出现在 /assets/{id}/tags（asset_tag 复制行）
    approve = client.post(f"/api/v1/asset-tags/{first['id']}/review",
                          json={"action": "approve"})
    assert approve.status_code == 200, approve.text
    assert approve.json()["review_status"] == "approved"
    tags = client.get(f"/api/v1/assets/{asset_id}/tags")
    assert tags.status_code == 200
    mounted = tags.json()
    assert len(mounted) == 1
    assert mounted[0]["tag_id"] == first["tag_id"]
    assert mounted[0]["tag_name"] == first["tag_name"]
    assert mounted[0]["origin"] == "closed"
    assert mounted[0]["created_at"] is not None

    # 6. metrics：summary 三键非零（pending / approved 计数 / 任务 succeeded）
    #    + 通过率与证据覆盖满值 + dir-coverage 非空且 approved 汇总一致
    metrics = client.get("/api/v1/metrics/summary")
    assert metrics.status_code == 200
    m = metrics.json()
    assert m["pending_count"] == len(results) - 1 > 0  # 其余行仍待审
    assert m["review_stats"]["approved"]["count"] == 1
    assert m["generated_pass_rate"] == 1.0  # 唯一终审行即 approve
    assert m["evidence_coverage"] == 1.0    # 全部生成行带场景证据
    assert m["task_stats"]["total"] == 1
    assert m["task_stats"]["succeeded"] == 1
    assert m["task_stats"]["success_rate"] == 1.0
    coverage = client.get("/api/v1/metrics/dir-coverage")
    assert coverage.status_code == 200
    rows = coverage.json()
    assert rows  # 非空
    assert sum(row["total"] for row in rows) == len(results)
    assert sum(row["approved"] for row in rows) == 1

    # metrics 第三端点：confidence 分桶——非空、桶标签格式、与互锁风格一致
    # （各桶 count 之和 == 全部带置信度行数；本链全部行带 confidence）
    buckets = client.get("/api/v1/metrics/confidence-distribution")
    assert buckets.status_code == 200
    dist = buckets.json()
    assert dist
    assert all(re.fullmatch(r"\d\.\d-\d\.\d", row["bucket"]) for row in dist)
    assert dist[0]["bucket"] == "0.0-0.1"  # 恒返回全部桶，首桶 0.0 起
    assert sum(row["count"] for row in dist) == len(results)
