"""标签导入 API 测试（import 用例，v2 契约）。

覆盖契约（错误映射约定见 brief；批次列表/详情/撤销用例已迁移至
test_api_imports.py 的 /api/v1/import/tasks）：
- POST /api/v1/tags/import：响应 {task_id, summary}（v1 batch_id 退役），
  inserted 仅计标签行（目录行落 tag_catalog 不计入）；重复导入 skipped==1
  且 200（Review Focus 2：重复导入不 5xx）；层级模板同名标签挂不同目录
  合法共存；坏表头 422 {"detail": header_error}；损坏 xlsx 字节（openpyxl
  抛异常）由 API 层映射 422；行级解析错误进响应 parse_errors 并回写任务
  result_summary（该行不计入 total）；
- 停用标签再导入复活（deprecated → active）。

非 import 用例（/tags 列表/详情/导出/DELETE 生命周期）为 Task 5 域，
v1 body 原样保留、此时红属中期预期。xlsx 字节流由 build_xlsx 现场构造。
"""

import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import openpyxl

from tagplatform.db.models import TagCatalog, TagDefine


def build_xlsx(rows: list[list]) -> bytes:
    """用 openpyxl 现场构造 xlsx 字节流（首行为表头）。"""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# 扁平模板：1 行标签（v2 落「未分类」目录）
XLSX_BYTES = build_xlsx([
    ["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"],
    ["悬疑", "genre", "以悬念为核心", None, "悬念,推理", "active"],
])

# 坏表头：缺 description 必需列 → header_error 非空
BAD_HEADER_BYTES = build_xlsx([["tag_name", "tag_type"]])


def test_import_excel_via_api(client):
    resp = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"]
    assert body["summary"]["inserted"] == 1 and body["summary"]["issues"] == []
    assert body["summary"]["total"] == 1 and body["summary"]["skipped"] == 0


def test_export_tags_as_xlsx_and_xml(client):
    resp = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    assert resp.status_code == 200

    exported = client.get("/api/v1/tags/export?q=悬疑")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == {"tags-export.xlsx", "tags-export.xml"}
        workbook = openpyxl.load_workbook(io.BytesIO(archive.read("tags-export.xlsx")))
        xml_root = ET.fromstring(archive.read("tags-export.xml"))

    rows = list(workbook.active.iter_rows(values_only=True))
    assert rows[0][:4] == ("一级标签名称", "二级标签名称", "三级标签名称", "标签")
    assert rows[1][3] == "悬疑"
    assert xml_root.find("tag") is not None
    assert xml_root.find("tag").text == "悬疑"


def test_import_allows_same_tag_name_under_different_parents(client):
    payload = build_xlsx([
        ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
        ["内容", "剧情", None, "其他", "", None],
        ["风格", "视觉", None, "其他", "", None],
    ])
    resp = client.post("/api/v1/tags/import", files={"file": ("scoped.xlsx", payload)})

    assert resp.status_code == 200
    # v2 inserted 仅计标签行：4 个目录 + 2 个「其他」标签 → inserted == 2
    assert resp.json()["summary"]["inserted"] == 2
    assert resp.json()["summary"]["issues"] == []
    # 同名标签分属不同目录（(dir, name) 唯一键允许共存）
    with client.app.state.session_factory() as s:
        others = s.query(TagDefine).filter_by(tag_name="其他").all()
        assert len(others) == 2
        assert len({t.dir_id for t in others}) == 2


def test_import_xml_via_api(client):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <root>
      <level1 name="内容">
        <level2 name="剧情">
          <tag description="带有喜剧元素" type="manual">喜剧</tag>
        </level2>
      </level1>
    </root>""".encode()
    resp = client.post("/api/v1/tags/import", files={"file": ("tags.xml", xml)})

    assert resp.status_code == 200
    body = resp.json()
    # v2 inserted 仅计标签行：2 个目录（内容/剧情）+ 1 个标签（喜剧）
    assert body["summary"]["total"] == 3
    assert body["summary"]["inserted"] == 1


def test_import_xml_detected_by_content(client):
    xml = "<root><level1 name='内容'><level2 name='剧情'/></level1></root>".encode()
    resp = client.post("/api/v1/tags/import", files={"file": ("tags", xml)})

    assert resp.status_code == 200
    # 无扩展名文件按内容嗅探走 XML 解析。注意：解析层把层级节点摊平为
    # TagRow（不保留"目录节点"标记），末级空目录无子引用时按标签行落位
    # （内容→目录、剧情→内容目录下的标签），inserted 仅计标签行 == 1
    assert resp.json()["summary"]["total"] == 2
    assert resp.json()["summary"]["inserted"] == 1


def test_import_twice_reports_duplicates(client):
    resp1 = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    resp2 = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    assert resp1.status_code == 200
    assert resp1.json()["task_id"]
    assert resp2.json()["summary"]["skipped"] == 1
    assert resp2.status_code == 200          # Review Focus 2：重复导入不 5xx


def test_bad_header_422(client):
    resp = client.post("/api/v1/tags/import", files={"file": ("x.xlsx", BAD_HEADER_BYTES)})
    assert resp.status_code == 422
    assert "表头" in resp.json()["detail"]


def test_corrupt_xlsx_422(client):
    """损坏/非 xlsx 字节：openpyxl 抛异常由 API 层捕获映射 422（Task 6 约定）。"""
    resp = client.post(
        "/api/v1/tags/import", files={"file": ("broken.xlsx", b"not an xlsx")})
    assert resp.status_code == 422


def test_import_parse_errors_in_response_and_batch(client):
    """行级解析错误不再整条丢弃——响应 parse_errors 明细 + 回写任务
    result_summary（total 语义维持"进入治理的行数"，坏行不计入）。
    """
    mixed = build_xlsx([
        ["tag_name", "tag_type", "description", "parent_tag"],
        ["悬疑", "genre", "以悬念为核心", None],   # 第 2 行：有效行
        [None, "genre", "缺名称的坏行", None],     # 第 3 行：missing_field
        ["坏单元格", "genre", "d", True],          # 第 4 行：bad_type（bool 不可文本化）
    ])
    resp = client.post("/api/v1/tags/import", files={"file": ("mixed.xlsx", mixed)})
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["total"] == 1 and summary["inserted"] == 1
    assert summary["skipped"] == 0
    assert len(summary["parse_errors"]) == 2
    by_row = {e["row_number"]: e for e in summary["parse_errors"]}
    assert by_row[3]["code"] == "missing_field" and by_row[3]["tag_name"] is None
    assert by_row[4]["code"] == "bad_type" and by_row[4]["tag_name"] == "坏单元格"

    # 导入任务详情的 result_summary 同样带 parse_errors 明细（API 层回写）
    detail = client.get(f"/api/v1/import/tasks/{resp.json()['task_id']}")
    assert detail.status_code == 200
    detail_summary = detail.json()["summary"]
    assert len(detail_summary["parse_errors"]) == 2
    assert {e["row_number"] for e in detail_summary["parse_errors"]} == {3, 4}
    assert detail_summary["total"] == 1 and detail_summary["inserted"] == 1


def test_reimport_reactivates_deprecated_tag(client):
    """停用标签（status=deprecated）按相同目录再导入时恢复为启用。

    v1 用例经 deactivate 端点造停用态（Task 5 域）；v2 改为直接播种
    deprecated 行，只覆盖本任务的导入复活语义。
    """
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

    reimported = client.post("/api/v1/tags/import",
                             files={"file": ("status-again.xlsx", XLSX_BYTES)})
    assert reimported.status_code == 200
    assert reimported.json()["summary"]["inserted"] == 1
    with factory() as s:
        row = s.get(TagDefine, "tag_seed")
        assert row.status == "active" and row.tag_id == "tag_seed"


# ---------- 以下为 Task 5 域（v1 body 原样保留，中期红属预期） ----------

def test_delete_tag_cleans_empty_parent_directories(client):
    imported = client.post(
        "/api/v1/tags/import",
        files={"file": ("delete.xlsx", build_xlsx([
            ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
            ["内容", "剧情", None, "反转", "", None],
        ]))},
    )
    assert imported.status_code == 200
    tag_id = client.get("/api/v1/tags?q=反转").json()["items"][0]["tag_id"]

    deleted = client.delete(f"/api/v1/tags/{tag_id}")
    assert deleted.status_code == 200
    assert deleted.json()["parent_id"] is None
    assert client.get("/api/v1/tags?root=true").json()["items"] == []


def test_delete_tag_keeps_non_empty_parent(client):
    client.post(
        "/api/v1/tags/import",
        files={"file": ("delete-two.xlsx", build_xlsx([
            ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
            ["内容", "剧情", None, "反转", "", None],
            ["内容", "剧情", None, "悬疑", "", None],
        ]))},
    )
    tag_id = client.get("/api/v1/tags?q=反转").json()["items"][0]["tag_id"]

    deleted = client.delete(f"/api/v1/tags/{tag_id}")
    assert deleted.status_code == 200
    assert deleted.json()["parent_name"] == "剧情"
    assert client.get("/api/v1/tags?q=悬疑").json()["items"]


def test_tag_404(client):
    assert client.get("/api/v1/tags/tag_none").status_code == 404
