"""标签导入/批次/标签查询 API 测试（Task 10）。

覆盖契约（错误映射约定见 brief）：
- POST /api/v1/tags/import：合法模板导入 inserted 计数与 q 查询（tag_name
  与 aliases 子串匹配）、type 过滤、详情含 aliases；重复导入 skipped==1 且
  200（Review Focus 2：重复导入不 5xx）；坏表头 422 {"detail": header_error}；
  损坏 xlsx 字节（openpyxl 抛异常）由 API 层映射 422；
- GET /api/v1/import/batches：created_at 降序，file_name 为回填后的真实
  上传文件名（治理流水线写入的是占位 "governance"）；
- GET /api/v1/import/batches/{batch_id}：result_summary 解析后的 issues 明细；
  未知批次 404；
- 修复 3：上传含行级解析错误的 xlsx → 响应 parse_errors 明细 + 批次详情
  result_summary 同样携带（该行不计入 total）；
- GET /api/v1/tags/{tag_id}：未知 tag 404。

xlsx 字节流由 build_xlsx 用 openpyxl 现场构造，不依赖外部文件。
"""

import io
import time

import openpyxl


def build_xlsx(rows: list[list]) -> bytes:
    """用 openpyxl 现场构造 xlsx 字节流（首行为表头）。"""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# 合法模板：1 行标签（别名 悬念/推理，供 q 的别名子串匹配断言）
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
    assert body["batch_id"]
    assert body["summary"]["inserted"] == 1 and body["summary"]["issues"] == []
    assert body["summary"]["total"] == 1 and body["summary"]["skipped"] == 0

    # q 对 tag_name 子串匹配
    got = client.get("/api/v1/tags?q=悬疑")
    assert got.status_code == 200
    items = got.json()["items"]
    assert [i["tag_name"] for i in items] == ["悬疑"]
    first = items[0]
    assert first["tag_type"] == "genre" and first["status"] == "active"
    assert first["parent_id"] is None and first["tag_id"]
    assert "description" in first

    # q 对 aliases 子串匹配：「推理」仅出现在别名中
    by_alias = client.get("/api/v1/tags?q=推理")
    assert [i["tag_name"] for i in by_alias.json()["items"]] == ["悬疑"]

    # type 过滤：genre 命中、movie 为空
    assert len(client.get("/api/v1/tags?type=genre").json()["items"]) == 1
    assert client.get("/api/v1/tags?type=movie").json()["items"] == []

    # 单条详情：在列表字段之上展开 aliases 列表
    detail = client.get(f"/api/v1/tags/{first['tag_id']}")
    assert detail.status_code == 200
    assert detail.json()["aliases"] == ["悬念", "推理"]


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
    assert body["summary"]["total"] == 3
    assert body["summary"]["inserted"] == 3
    assert client.get("/api/v1/tags?q=喜剧").json()["items"][0]["tag_type"] == "genre"


def test_import_xml_detected_by_content(client):
    xml = "<root><level1 name='内容'><level2 name='剧情'/></level1></root>".encode()
    resp = client.post("/api/v1/tags/import", files={"file": ("tags", xml)})

    assert resp.status_code == 200
    assert resp.json()["summary"]["inserted"] == 2


def test_import_twice_reports_duplicates(client):
    resp1 = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    resp2 = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    assert resp1.status_code == 200
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
    """修复 3：行级解析错误不再整条丢弃——响应 parse_errors 明细 + 回写批次
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

    # 批次详情的 result_summary 同样带 parse_errors 明细（API 层回写）
    detail = client.get(f"/api/v1/import/batches/{resp.json()['batch_id']}")
    assert detail.status_code == 200
    detail_summary = detail.json()["summary"]
    assert len(detail_summary["parse_errors"]) == 2
    assert {e["row_number"] for e in detail_summary["parse_errors"]} == {3, 4}
    assert detail_summary["total"] == 1 and detail_summary["inserted"] == 1


def test_batch_list_and_detail(client):
    r1 = client.post("/api/v1/tags/import", files={"file": ("a.xlsx", XLSX_BYTES)})
    time.sleep(0.01)  # Windows 时钟粒度：强制两批次 created_at 可分辨
    r2 = client.post("/api/v1/tags/import", files={"file": ("b.xlsx", XLSX_BYTES)})
    batch1, batch2 = r1.json()["batch_id"], r2.json()["batch_id"]

    listing = client.get("/api/v1/import/batches")
    assert listing.status_code == 200
    batches = listing.json()
    assert len(batches) == 2
    # created_at 降序：后导入的批次在前
    assert [b["batch_id"] for b in batches] == [batch2, batch1]
    assert batches[0]["file_name"] == "b.xlsx"   # API 层回填真实上传文件名
    assert batches[0]["status"] == "completed"
    assert batches[0]["created_at"]

    detail = client.get(f"/api/v1/import/batches/{batch2}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["batch_id"] == batch2 and body["file_name"] == "b.xlsx"
    # result_summary 解析后的 issues 明细：第二次导入命中 duplicate_name
    assert body["summary"]["total"] == 1
    assert body["summary"]["inserted"] == 0
    assert body["summary"]["skipped"] == 1
    assert body["summary"]["issues"][0]["code"] == "duplicate_name"
    assert body["summary"]["issues"][0]["tag_name"] == "悬疑"

    # 未知批次 → 404
    assert client.get("/api/v1/import/batches/batch_none").status_code == 404


def test_tag_404(client):
    assert client.get("/api/v1/tags/tag_none").status_code == 404
