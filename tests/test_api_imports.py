"""导入任务 API 测试（/api/v1/import/tasks，v2 由 test_api_tags 批次用例迁移）。

覆盖契约：
- GET /api/v1/import/tasks：created_at 降序，file_name 为回填后的真实上传
  文件名（治理流水线写入的是占位 "governance"），字段 batch_id→task_id；
- GET /api/v1/import/tasks/{task_id}：result_summary 解析后的 issues 明细；
  未知任务 404；
- POST /api/v1/import/tasks/{task_id}/revoke：删除批内新建目录与标签、
  复活行还原 before 状态；409 四分支——已撤销 / 非 completed / 批内标签
  被 asset_tag 挂载引用 / 批内目录被他批子节点（标签或子目录）引用。
  撤销对象经 tag_change_log（action='import'，after_json.task_id + kind）
  反查；播种/值断言经 client.app.state.session_factory 直查临时文件库
  （test_api_review 同款模式）。

xlsx 字节流由 build_xlsx 用 openpyxl 现场构造，不依赖外部文件。
export 端点（GET /{task_id}/export）本任务仅保留路由注册，双表化归
Task 5——本文件不触达。
"""

import io
import time
from datetime import datetime, timezone

import openpyxl

from tagplatform.db.models import (
    AssetTag,
    MediaAsset,
    TagCatalog,
    TagDefine,
    TagImportTask,
)


def build_xlsx(rows: list[list]) -> bytes:
    """用 openpyxl 现场构造 xlsx 字节流（首行为表头）。"""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# 扁平模板：1 行标签（别名供 v1 q 匹配沿用；v2 落「未分类」目录）
XLSX_BYTES = build_xlsx([
    ["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"],
    ["悬疑", "genre", "以悬念为核心", None, "悬念,推理", "active"],
])

# 层级模板：一级/二级目录 + 标签（2 目录 + 1 标签）
HIER_XLSX_BYTES = build_xlsx([
    ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
    ["内容", "剧情", None, "反转", "", None],
])


def test_import_task_list_and_detail(client):
    r1 = client.post("/api/v1/tags/import", files={"file": ("a.xlsx", XLSX_BYTES)})
    time.sleep(0.01)  # Windows 时钟粒度：强制两任务 created_at 可分辨
    r2 = client.post("/api/v1/tags/import", files={"file": ("b.xlsx", XLSX_BYTES)})
    task1, task2 = r1.json()["task_id"], r2.json()["task_id"]

    listing = client.get("/api/v1/import/tasks")
    assert listing.status_code == 200
    tasks = listing.json()
    assert len(tasks) == 2
    # created_at 降序：后导入的任务在前
    assert [t["task_id"] for t in tasks] == [task2, task1]
    assert tasks[0]["file_name"] == "b.xlsx"   # API 层回填真实上传文件名
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["created_at"]

    detail = client.get(f"/api/v1/import/tasks/{task2}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["task_id"] == task2 and body["file_name"] == "b.xlsx"
    # result_summary 解析后的 issues 明细：第二次导入命中 duplicate_name
    assert body["summary"]["total"] == 1
    assert body["summary"]["inserted"] == 0
    assert body["summary"]["skipped"] == 1
    assert body["summary"]["issues"][0]["code"] == "duplicate_name"
    assert body["summary"]["issues"][0]["tag_name"] == "悬疑"

    # 未知任务 → 404
    assert client.get("/api/v1/import/tasks/task_none").status_code == 404


def test_revoke_removes_imported_tags_and_dirs(client):
    factory = client.app.state.session_factory
    imported = client.post("/api/v1/tags/import",
                           files={"file": ("revoke.xlsx", HIER_XLSX_BYTES)})
    task_id = imported.json()["task_id"]

    revoked = client.post(f"/api/v1/import/tasks/{task_id}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert client.get(f"/api/v1/import/tasks/{task_id}").json()["status"] == "revoked"
    # 批内新建目录与标签一并删除（层级目录链不留孤儿）
    with factory() as s:
        assert s.query(TagCatalog).count() == 0
        assert s.query(TagDefine).count() == 0

    again = client.post(f"/api/v1/import/tasks/{task_id}/revoke")
    assert again.status_code == 409  # 已撤销分支

    reimported = client.post("/api/v1/tags/import",
                             files={"file": ("reimport.xlsx", HIER_XLSX_BYTES)})
    assert reimported.status_code == 200
    assert reimported.json()["summary"]["inserted"] == 1


def test_revoke_nested_hierarchy_deletes_whole_dir_chain(client):
    # 三级链整批撤销：目录自引用外键无残留（v1 父外键回归位）
    factory = client.app.state.session_factory
    imported = client.post("/api/v1/tags/import", files={"file": ("nested.xlsx", build_xlsx([
        ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
        ["内容", "剧情", "悬疑", "反转", "", None],
    ]))})
    task_id = imported.json()["task_id"]
    assert imported.json()["summary"]["inserted"] == 1

    revoked = client.post(f"/api/v1/import/tasks/{task_id}/revoke")
    assert revoked.status_code == 200
    with factory() as s:
        assert s.query(TagCatalog).count() == 0
        assert s.query(TagDefine).count() == 0


def test_revoke_restores_reactivated_tag(client):
    # 复活行撤销后还原 before 状态；复用的「未分类」目录不删
    factory = client.app.state.session_factory
    with factory() as s:
        now = datetime.now(timezone.utc)
        s.add(TagCatalog(
            dir_id="dir_seed", dir_name="未分类", dir_type="dir_static",
            description="", parent_id=None, aliases=None,
            applicable_scope="content", embedding=None,
            created_at=now, updated_at=now))
        s.add(TagDefine(
            tag_id="tag_seed", tag_name="悬疑", tag_type="val_fixed",
            description="旧描述", dir_id="dir_seed", aliases=None,
            status="deprecated", embedding=None, created_at=now, updated_at=now))
        s.commit()

    imported = client.post("/api/v1/tags/import",
                           files={"file": ("reactivate.xlsx", XLSX_BYTES)})
    assert imported.json()["summary"]["inserted"] == 1  # 复活而非新建
    task_id = imported.json()["task_id"]

    revoked = client.post(f"/api/v1/import/tasks/{task_id}/revoke")
    assert revoked.status_code == 200
    with factory() as s:
        row = s.get(TagDefine, "tag_seed")
        assert row.status == "deprecated"          # 还原 before 状态
        assert s.query(TagCatalog).count() == 1    # 复用目录保留
        assert s.query(TagDefine).count() == 1


def test_revoke_rejected_when_status_not_completed(client):
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(TagImportTask(
            task_id="task_processing", file_name="x.xlsx", status="processing",
            created_at=datetime.now(timezone.utc)))
        s.commit()
    resp = client.post("/api/v1/import/tasks/task_processing/revoke")
    assert resp.status_code == 409
    assert "已完成" in resp.json()["detail"]


def test_revoke_rejected_when_tag_referenced_by_asset(client):
    factory = client.app.state.session_factory
    imported = client.post("/api/v1/tags/import",
                           files={"file": ("refs.xlsx", XLSX_BYTES)})
    task_id = imported.json()["task_id"]
    with factory() as s:
        tag = s.query(TagDefine).one()
        now = datetime.now(timezone.utc)
        s.add(MediaAsset(asset_id="movie_ref", asset_type="movie",
                         title="引用测试影片", created_at=now))
        # 无 relationship() 时 UoW 按类名字母序插入（asset_tag < media_asset），
        # 父行须先 flush 落库再挂 asset_tag
        s.flush()
        s.add(AssetTag(asset_id="movie_ref", target_type="content",
                       target_id="movie_ref", tag_id=tag.tag_id,
                       tag_name=tag.tag_name, created_at=now, updated_at=now))
        s.commit()

    resp = client.post(f"/api/v1/import/tasks/{task_id}/revoke")
    assert resp.status_code == 409
    assert "资产" in resp.json()["detail"]
    # 引用仍在：任务未被撤销
    assert client.get(f"/api/v1/import/tasks/{task_id}").json()["status"] == "completed"


def test_revoke_rejected_when_other_batch_references_dir(client):
    imported = client.post("/api/v1/tags/import",
                           files={"file": ("base.xlsx", HIER_XLSX_BYTES)})
    task_a = imported.json()["task_id"]
    # 批次 B：把标签挂到批次 A 建的目录下（parent_tag 解析到库内目录）
    child = client.post("/api/v1/tags/import", files={"file": ("child.xlsx", build_xlsx([
        ["tag_name", "tag_type", "description", "parent_tag"],
        ["其他", "genre", "挂靠 A 批目录", "剧情"],
    ]))})
    assert child.status_code == 200
    assert child.json()["summary"]["inserted"] == 1

    resp = client.post(f"/api/v1/import/tasks/{task_a}/revoke")
    assert resp.status_code == 409  # 批内目录被他批子标签引用
    assert "子" in resp.json()["detail"]
    assert client.get(f"/api/v1/import/tasks/{task_a}").json()["status"] == "completed"
