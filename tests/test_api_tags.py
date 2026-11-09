"""标签 API 测试：import 用例（v2 契约，Task 4）+ 混合树/详情/生命周期/导出（Task 5）。

覆盖契约：
- POST /api/v1/tags/import：响应 {task_id, summary}（v1 batch_id 退役），
  total/inserted 均仅计标签行（目录行落 tag_catalog 不计入任何计数）；
  重复导入 skipped==1
  且 200（Review Focus 2：重复导入不 5xx）；层级模板同名标签挂不同目录
  合法共存；坏表头 422 {"detail": header_error}；损坏 xlsx 字节（openpyxl
  抛异常）由 API 层映射 422；行级解析错误进响应 parse_errors 并回写任务
  result_summary（该行不计入 total）；停用标签再导入复活（deprecated →
  active）。
- GET /api/v1/tags 混合树（TagNodeOut 契约）：parent_id 下同时返回子目录
  + 直挂叶子标签（目录先、名字序；kind/dir_type/status/has_children/
  aliases 按契约）、root 仅顶层目录（标签必挂目录，无根标签）、scope_id
  递归子树（标签的后代 = 其目录的后代目录下的标签，直挂 scope 本体的
  标签不在结果内）、q 对目录与标签的 name+aliases 都命中、kind 二值过滤；
- GET /api/v1/tags/{id}：id 可为 dir_id 或 tag_id，path/path_ids 含本节点；
- 生命周期三端点只作用于 tag_define（目录 id → 404 且文案含"目录"）；
  deactivate/activate 写 change_log（action deprecate/update，状态字面量
  v2 统一 deprecated，同状态重复调用不重复写日志）；
- DELETE：被 asset_tag 正式挂载或 pre_asset_tag 待审核候选引用 → 409；
  无引用删除并自下而上剪空祖先目录，响应带 parent 重定向信息
  （change_log action 沿 v1 写 "delete"——该值 2026-11-06 补入 doc §3.8
  枚举，v1 无 CHECK 属历史漏网的文档缺口修复）；
- 导出双端点（GET /tags/export 与 GET /import/tasks/{task_id}/export）：
  zip 含 xlsx+xml，叶子 = tag_define 行、祖先路径 = tag_catalog 链
  （xlsx 六列与 xml 嵌套逐字沿 v1 格式，仅叶子标签入库导出）。

xlsx 字节流由 build_xlsx 现场构造。
"""

import io
import json
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import openpyxl

from tagplatform.db.models import (
    AssetTag,
    MediaAsset,
    PreAssetTag,
    TagCatalog,
    TagChangeLog,
    TagDefine,
)

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def build_xlsx(rows: list[list]) -> bytes:
    """用 openpyxl 现场构造 xlsx 字节流（首行为表头）。"""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# 扁平模板：1 行标签（v2 落「未分类」目录），带别名供 q 命中断言
XLSX_BYTES = build_xlsx([
    ["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"],
    ["悬疑", "genre", "以悬念为核心", None, "悬念,推理", "active"],
])

# 坏表头：缺 description 必需列 → header_error 非空
BAD_HEADER_BYTES = build_xlsx([["tag_name", "tag_type"]])

# 层级模板：目录树 内容 > (剧情 > (反转, 叙事 > 多线), 情感 > 温馨)
# ——剧情下同时有直挂标签（反转）与子目录（叙事），供混合树断言
HIER_XLSX = build_xlsx([
    ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
    ["内容", "剧情", None, "反转", "情节反转", None],
    ["内容", "剧情", "叙事", "多线", "多线叙事", None],
    ["内容", "情感", None, "温馨", "", None],
])


def _import(client, rows: list[list] | bytes) -> dict:
    """上传 xlsx（行集或现成字节流）→ 导入响应 JSON。"""
    payload = rows if isinstance(rows, bytes) else build_xlsx(rows)
    resp = client.post("/api/v1/tags/import", files={"file": ("t.xlsx", payload)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _node_id(client, keyword: str) -> str:
    """按 q 精确命中取单节点 id（测试辅助：关键词须全库唯一）。"""
    items = client.get("/api/v1/tags", params={"q": keyword}).json()["items"]
    assert len(items) == 1, f"q={keyword!r} 命中 {len(items)} 个节点"
    return items[0]["id"]


def _norm_row(row: tuple) -> tuple:
    """openpyxl 回读空白单元格为 None → 归一 ""（写侧写入的是空串列）。"""
    return tuple("" if c is None else c for c in row)


def _workbook_rows(archive_bytes: bytes) -> dict[str, tuple]:
    """zip 字节流 → {标签名: xlsx 数据行}（读 tags-export.xlsx，空白归一）。"""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        workbook = openpyxl.load_workbook(io.BytesIO(archive.read("tags-export.xlsx")))
        return {
            row[3]: _norm_row(row)
            for row in workbook.active.iter_rows(min_row=2, values_only=True)
        }


# ---------- import（Task 4 契约） ----------


def test_import_excel_via_api(client):
    resp = client.post("/api/v1/tags/import", files={"file": ("tags.xlsx", XLSX_BYTES)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"]
    assert body["summary"]["inserted"] == 1 and body["summary"]["issues"] == []
    assert body["summary"]["total"] == 1 and body["summary"]["skipped"] == 0


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
    # v2 total/inserted 均仅计标签行：2 个目录（内容/剧情）+ 1 个标签（喜剧）
    assert body["summary"]["total"] == 1
    assert body["summary"]["inserted"] == 1


def test_import_xml_detected_by_content(client):
    xml = "<root><level1 name='内容'><level2 name='剧情'/></level1></root>".encode()
    resp = client.post("/api/v1/tags/import", files={"file": ("tags", xml)})

    assert resp.status_code == 200
    # 无扩展名文件按内容嗅探走 XML 解析。注意：解析层把层级节点摊平为
    # TagRow（不保留"目录节点"标记），末级空目录无子引用时按标签行落位
    # （内容→目录、剧情→内容目录下的标签）。v2 口径：仅 1 个标签行进
    # total（目录行「内容」不计入）
    assert resp.json()["summary"]["total"] == 1
    assert resp.json()["summary"]["inserted"] == 1


def test_import_dual_sheet_template_via_api(client):
    """双表模板（docs/movietags-template.xlsx 格式）：说明页首位 + 两数据
    sheet，按 sheet 名探测解析（此前按 active sheet 判格式会误报
    「表头缺少必需列：tag_name、tag_type、description」）。
    """
    workbook = openpyxl.Workbook()
    workbook.active.title = "填写说明"
    workbook.active.append(["模板说明"])

    dir_sheet = workbook.create_sheet("标签目录")
    dir_sheet.append(["可选，留空由系统生成", "1=一级 2=二级 3=三级"])
    dir_sheet.append(["目录ID\n(dir_id)", "目录层级", "上级目录名称",
                      "目录名称\n(dir_name)", "目录类型\n(dir_type)",
                      "适用范围", "目录描述", "别名"])
    dir_sheet.append([None, 1, None, "内容", "dir_static", "content,scene", "根目录", None])
    dir_sheet.append([None, 2, "内容", "主类型", "dir_dynamic", None, None, "剧情类"])

    tag_sheet = workbook.create_sheet("标签定义")
    tag_sheet.append(["可选，留空由系统生成", "必填"])
    tag_sheet.append(["标签ID\n(tag_id)", "标签名称\n(tag_name)",
                      "所属目录名称", "标签类型\n(tag_type)", "标签描述", "别名"])
    tag_sheet.append([None, "剧情", "主类型", "val_generalized", "以剧情为核心", "情节"])
    tag_sheet.append([None, "喜剧", "主类型", None, None, None])

    buffer = io.BytesIO()
    workbook.save(buffer)

    resp = client.post(
        "/api/v1/tags/import",
        files={"file": ("movietags-template.xlsx", buffer.getvalue())},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # v2 口径：2 目录行不计入 → total 2（= 2 个标签行）；inserted == 2
    assert body["summary"]["total"] == 2
    assert body["summary"]["inserted"] == 2
    assert body["summary"]["issues"] == []
    assert body["summary"]["parse_errors"] == []

    # 混合树断言：目录 dir_type 与标签字段已入库（applicable_scope 不在
    # 列表契约内，改由节点详情/导出口径覆盖——此处断言可见字段）
    children = client.get("/api/v1/tags", params={"root": True}).json()["items"]
    content = next(c for c in children if c["name"] == "内容")
    assert content["dir_type"] == "dir_static"
    assert content["description"] == "根目录"

    subs = client.get(
        "/api/v1/tags", params={"parent_id": content["id"]}).json()["items"]
    main_type = next(s for s in subs if s["name"] == "主类型")
    assert main_type["dir_type"] == "dir_dynamic"
    assert main_type["aliases"] == ["剧情类"]

    tags = client.get(
        "/api/v1/tags", params={"parent_id": main_type["id"]}).json()["items"]
    by_name = {t["name"]: t for t in tags}
    assert by_name["剧情"]["description"] == "以剧情为核心"
    assert by_name["剧情"]["aliases"] == ["情节"]
    assert by_name["喜剧"]["description"] == ""  # 缺省空字符串


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
    result_summary（total 语义维持"进入治理的标签行数"，坏行不计入）。
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
    """停用标签（status=deprecated）按相同目录再导入时恢复为启用。"""
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(TagCatalog(
            dir_id="dir_seed", dir_name="未分类", dir_type="dir_static",
            description="", parent_id=None, aliases=None,
            applicable_scope="content", embedding=None,
            created_at=_NOW, updated_at=_NOW))
        s.add(TagDefine(
            tag_id="tag_seed", tag_name="悬疑", tag_type="val_fixed",
            description="旧描述", dir_id="dir_seed", aliases=None,
            status="deprecated", embedding=None, created_at=_NOW, updated_at=_NOW))
        s.commit()

    reimported = client.post("/api/v1/tags/import",
                             files={"file": ("status-again.xlsx", XLSX_BYTES)})
    assert reimported.status_code == 200
    assert reimported.json()["summary"]["inserted"] == 1
    with factory() as s:
        row = s.get(TagDefine, "tag_seed")
        assert row.status == "active" and row.tag_id == "tag_seed"


# ---------- 混合树列表（Task 5） ----------


def test_list_hybrid_children_mix_dirs_and_tags(client):
    """parent_id 下同时返回子目录与直挂标签：目录先、契约字段逐项断言。"""
    _import(client, HIER_XLSX)
    plot_id = _node_id(client, "剧情")

    resp = client.get("/api/v1/tags", params={"parent_id": plot_id})
    assert resp.status_code == 200
    items = resp.json()["items"]
    # 目录（叙事）在前、标签（反转）在后
    assert [(i["kind"], i["name"]) for i in items] == [("dir", "叙事"), ("tag", "反转")]

    story, reversal = items
    # 目录节点：id=dir_id、dir_type 有值（XML 未填目录类型 → 默认 dir_dynamic）、
    # status 无列为 None、has_children 真
    assert story["dir_type"] == "dir_dynamic"
    assert story["parent_id"] == plot_id
    assert story["has_children"] is True
    assert story["status"] is None
    assert story["aliases"] == []
    # 标签节点：id=tag_id、dir_type 无、status=active、parent=所属目录
    assert reversal["dir_type"] is None
    assert reversal["parent_id"] == plot_id
    assert reversal["has_children"] is False
    assert reversal["status"] == "active"
    assert reversal["aliases"] == []


def test_list_root_returns_only_top_dirs(client):
    _import(client, HIER_XLSX)
    roots = client.get("/api/v1/tags", params={"root": True}).json()["items"]
    assert [i["name"] for i in roots] == ["内容"]
    assert roots[0]["kind"] == "dir"
    assert roots[0]["parent_id"] is None
    assert roots[0]["has_children"] is True


def test_list_dirs_precede_tags_name_sorted(client):
    """全量列表：目录段在前按名字序，标签段在后按名字序；limit/offset 生效。"""
    _import(client, HIER_XLSX)
    items = client.get("/api/v1/tags").json()["items"]
    kinds = [i["kind"] for i in items]
    assert set(kinds) == {"dir", "tag"}
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "dir" else 1)  # 目录全在前

    dir_names = [i["name"] for i in items if i["kind"] == "dir"]
    tag_names = [i["name"] for i in items if i["kind"] == "tag"]
    assert dir_names == sorted(dir_names)
    assert tag_names == sorted(tag_names)
    assert set(dir_names) == {"内容", "剧情", "叙事", "情感"}
    assert set(tag_names) == {"反转", "多线", "温馨"}

    # 分页在合并排序后的列表上切（limit=2 取前两个目录）
    paged = client.get("/api/v1/tags", params={"limit": 2}).json()["items"]
    assert [i["name"] for i in paged] == sorted(dir_names)[:2]


def test_list_q_matches_dir_tag_names_and_aliases(client):
    """q：目录名/标签名/标签别名/目录别名都命中（LIKE 子串，纯字面）。"""
    _import(client, HIER_XLSX)
    _import(client, XLSX_BYTES)  # 扁平：未分类/悬疑（别名 悬念,推理）

    # 目录名命中
    assert [i["name"] for i in client.get("/api/v1/tags", params={"q": "剧情"}).json()["items"]] == ["剧情"]
    # 标签名命中
    assert [i["name"] for i in client.get("/api/v1/tags", params={"q": "反转"}).json()["items"]] == ["反转"]
    # 标签别名命中
    assert {i["name"] for i in client.get("/api/v1/tags", params={"q": "悬念"}).json()["items"]} == {"悬疑"}

    # 目录别名命中：给「内容」目录补别名后查询
    content_id = _node_id(client, "内容")
    factory = client.app.state.session_factory
    with factory() as s:
        s.get(TagCatalog, content_id).aliases = json.dumps(["故事内容"], ensure_ascii=False)
        s.commit()
    assert {i["name"] for i in client.get("/api/v1/tags", params={"q": "故事内容"}).json()["items"]} == {"内容"}


def test_list_kind_filter(client):
    _import(client, HIER_XLSX)
    tags = client.get("/api/v1/tags", params={"kind": "tag"}).json()["items"]
    assert {i["kind"] for i in tags} == {"tag"} and len(tags) == 3
    dirs = client.get("/api/v1/tags", params={"kind": "dir"}).json()["items"]
    assert {i["kind"] for i in dirs} == {"dir"} and len(dirs) == 4
    # kind 仅接受 dir|tag
    assert client.get("/api/v1/tags", params={"kind": "leaf"}).status_code == 422


def test_list_scope_subtree(client):
    """scope_id：后代目录 + 后代目录下的标签；scope 本体与其直挂标签不在内。"""
    _import(client, HIER_XLSX)
    # 直挂 scope 本体的标签（扁平行 parent_tag=内容 → 挂库内「内容」目录）
    _import(client, [
        ["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"],
        ["直接挂载", "genre", "直挂内容目录", "内容", None, None],
    ])
    content_id = _node_id(client, "内容")

    items = client.get("/api/v1/tags", params={"scope_id": content_id}).json()["items"]
    got = {(i["kind"], i["name"]) for i in items}
    assert got == {
        ("dir", "剧情"), ("dir", "叙事"), ("dir", "情感"),
        ("tag", "反转"), ("tag", "多线"), ("tag", "温馨"),
    }
    names = {i["name"] for i in items}
    assert "内容" not in names        # scope 本体不在结果内
    assert "直接挂载" not in names    # 直挂 scope 的标签：其目录非"后代目录"


# ---------- 详情 ----------


def test_detail_dir_and_tag_paths(client):
    _import(client, HIER_XLSX)
    content_id = _node_id(client, "内容")
    plot_id = _node_id(client, "剧情")
    story_id = _node_id(client, "叙事")
    multi_id = _node_id(client, "多线")

    d = client.get(f"/api/v1/tags/{story_id}")
    assert d.status_code == 200
    d = d.json()
    assert d["kind"] == "dir" and d["dir_type"] == "dir_dynamic"
    assert d["path"] == ["内容", "剧情", "叙事"]
    assert d["path_ids"] == [content_id, plot_id, story_id]
    assert d["has_children"] is True  # 多线挂在其下

    t = client.get(f"/api/v1/tags/{multi_id}")
    assert t.status_code == 200
    t = t.json()
    assert t["kind"] == "tag" and t["dir_type"] is None
    assert t["path"] == ["内容", "剧情", "叙事", "多线"]
    assert t["path_ids"] == [content_id, plot_id, story_id, multi_id]
    assert t["status"] == "active"

    assert client.get("/api/v1/tags/tag_none").status_code == 404


# ---------- 生命周期（仅 tag_define） ----------


def test_deactivate_activate_writes_change_log(client):
    _import(client, HIER_XLSX)
    tag_id = _node_id(client, "反转")

    off = client.post(f"/api/v1/tags/{tag_id}/deactivate")
    assert off.status_code == 200
    assert off.json()["status"] == "deprecated"
    on = client.post(f"/api/v1/tags/{tag_id}/activate")
    assert on.status_code == 200
    assert on.json()["status"] == "active"

    with client.app.state.session_factory() as s:
        logs = s.query(TagChangeLog).filter_by(tag_id=tag_id).filter(
            TagChangeLog.action != "import").order_by(  # 排除导入审计行
            TagChangeLog.log_id).all()
        assert [(l.action, json.loads(l.before_json), json.loads(l.after_json))
                for l in logs] == [
            ("deprecate", {"status": "active"}, {"status": "deprecated"}),
            ("update", {"status": "deprecated"}, {"status": "active"}),
        ]

    # 同状态重复 deactivate：早返回不重复写日志
    client.post(f"/api/v1/tags/{tag_id}/deactivate")
    with client.app.state.session_factory() as s:
        assert s.query(TagChangeLog).filter_by(tag_id=tag_id).filter(
            TagChangeLog.action != "import").count() == 3


def test_lifecycle_on_dir_id_404(client):
    """目录 id 打生命周期端点 → 404 且文案含"目录"字样；未知 id 同 404。"""
    _import(client, HIER_XLSX)
    dir_id = _node_id(client, "剧情")
    for method, path in [
        ("post", f"/api/v1/tags/{dir_id}/deactivate"),
        ("post", f"/api/v1/tags/{dir_id}/activate"),
        ("delete", f"/api/v1/tags/{dir_id}"),
    ]:
        resp = getattr(client, method)(path)
        assert resp.status_code == 404
        assert "目录" in resp.json()["detail"]
    assert client.post("/api/v1/tags/tag_none/deactivate").status_code == 404
    assert client.delete("/api/v1/tags/tag_none").status_code == 404


# ---------- 删除（409 / 剪空目录 / 重定向信息） ----------


def test_delete_tag_referenced_by_asset_tag_409(client):
    _import(client, HIER_XLSX)
    tag_id = _node_id(client, "反转")
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="mv1", asset_type="movie", title="引用影片",
                         created_at=_NOW))
        s.flush()  # 跨表插入分步 flush：父行先落库再插挂载行
        s.add(AssetTag(asset_id="mv1", target_type="content", target_id="mv1",
                       tag_id=tag_id, tag_name="反转",
                       created_at=_NOW, updated_at=_NOW))
        s.commit()

    resp = client.delete(f"/api/v1/tags/{tag_id}")
    assert resp.status_code == 409
    # 标签与挂载行均原样保留
    assert client.get(f"/api/v1/tags/{tag_id}").status_code == 200


def test_delete_tag_referenced_by_pending_pre_tag_409(client):
    """被 pre_asset_tag 待审核候选引用（任意 review_status）→ 409 而非 FK
    500（Ruling 15：v1 单表引用检查拆双表后的完整映射）。"""
    _import(client, HIER_XLSX)
    tag_id = _node_id(client, "反转")
    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="mv2", asset_type="movie", title="候选影片",
                         created_at=_NOW))
        s.flush()  # 跨表插入分步 flush：父行先落库再插候选行
        s.add(PreAssetTag(asset_id="mv2", target_type="content", target_id="mv2",
                          tag_id=tag_id, origin="closed", review_status="pending",
                          created_at=_NOW, updated_at=_NOW))
        s.commit()

    resp = client.delete(f"/api/v1/tags/{tag_id}")
    assert resp.status_code == 409
    assert "待审核候选" in resp.json()["detail"]
    # 标签与候选行均原样保留
    assert client.get(f"/api/v1/tags/{tag_id}").status_code == 200


def test_delete_tag_prunes_empty_ancestor_directories(client):
    _import(client, [
        ["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"],
        ["内容", "剧情", None, "反转", "", None],
    ])
    tag_id = _node_id(client, "反转")

    deleted = client.delete(f"/api/v1/tags/{tag_id}")
    assert deleted.status_code == 200
    # 空祖先链（剧情、内容）全剪：无上级可跳转
    assert deleted.json()["tag_id"] == tag_id
    assert deleted.json()["parent_id"] is None
    assert deleted.json()["parent_name"] is None
    assert client.get("/api/v1/tags", params={"root": True}).json()["items"] == []
    with client.app.state.session_factory() as s:
        assert s.query(TagCatalog).count() == 0
        assert s.query(TagDefine).count() == 0


def test_delete_tag_keeps_non_empty_parent_and_redirects(client):
    _import(client, HIER_XLSX)
    multi_id = _node_id(client, "多线")
    story_id = _node_id(client, "叙事")

    deleted = client.delete(f"/api/v1/tags/{multi_id}")
    assert deleted.status_code == 200
    # 叙事被剪空后跳过，重定向到仍有子节点的剧情
    plot_id = _node_id(client, "剧情")
    assert deleted.json()["parent_id"] == plot_id
    assert deleted.json()["parent_name"] == "剧情"
    # 被删标签与剪空目录消失，非空兄弟保留
    assert client.get("/api/v1/tags", params={"q": "多线"}).json()["items"] == []
    assert client.get("/api/v1/tags", params={"q": "叙事"}).json()["items"] == []
    assert client.get("/api/v1/tags", params={"q": "反转"}).json()["items"]

    # 删除留痕：action 沿 v1 写 delete（doc §3.8 枚举 2026-11-06 补值）
    with client.app.state.session_factory() as s:
        log = s.query(TagChangeLog).filter_by(tag_id=multi_id).filter(
            TagChangeLog.action == "delete").one()  # 排除导入审计行
        before = json.loads(log.before_json)
        assert before["tag_name"] == "多线" and before["dir_id"] == story_id
        assert log.after_json is None


# ---------- 导出（双端点，zip = xlsx + xml） ----------


def test_export_zip_carries_dir_ancestor_paths(client):
    """/tags/export：叶子 = tag_define 行，祖先路径 = tag_catalog 链。"""
    _import(client, HIER_XLSX)

    exported = client.get("/api/v1/tags/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert set(archive.namelist()) == {"tags-export.xlsx", "tags-export.xml"}
        workbook = openpyxl.load_workbook(io.BytesIO(archive.read("tags-export.xlsx")))
        xml_root = ET.fromstring(archive.read("tags-export.xml"))

    rows = {r[3]: _norm_row(r)
            for r in workbook.active.iter_rows(values_only=True) if r[3] != "标签"}
    # 六列 v1 格式逐字沿用：三级列路径含叶子名沿 v1 计算，标签列重复叶子名
    assert rows["反转"][:5] == ("内容", "剧情", "反转", "反转", "情节反转")
    assert rows["多线"][:5] == ("内容", "剧情", "叙事", "多线", "多线叙事")
    assert rows["温馨"][:5] == ("内容", "情感", "温馨", "温馨", "")
    assert all(r[5] == "系统生成" for r in rows.values())

    # xml 嵌套：内容 > 剧情 > (反转, 叙事 > 多线) + 内容 > 情感 > 温馨
    top = xml_root.find("level1")
    assert top.get("name") == "内容"
    assert {t.text for t in top.iter("tag")} == {"反转", "多线", "温馨"}


def test_export_scope_and_q_filters(client):
    """scope_id 导出作用域全部标签（含直挂）；q 命中目录导出子树叶子、
    命中标签导出该标签；未知 scope → 404。"""
    _import(client, HIER_XLSX)
    _import(client, XLSX_BYTES)  # 未分类/悬疑
    _import(client, [
        ["tag_name", "tag_type", "description", "parent_tag", "aliases", "status"],
        ["直接挂载", "genre", "直挂内容目录", "内容", None, None],
    ])
    content_id = _node_id(client, "内容")

    scoped = _workbook_rows(client.get(
        "/api/v1/tags/export", params={"scope_id": content_id}).content)
    # v1 语义「该目录及其全部标签」：直挂标签一并导出，未分类不在作用域
    assert set(scoped) == {"反转", "多线", "温馨", "直接挂载"}

    by_name = _workbook_rows(client.get("/api/v1/tags/export", params={"q": "悬疑"}).content)
    assert set(by_name) == {"悬疑"}
    assert by_name["悬疑"][:4] == ("未分类", "悬疑", "", "悬疑")

    by_dir = _workbook_rows(client.get("/api/v1/tags/export", params={"q": "叙事"}).content)
    assert set(by_dir) == {"多线"}  # 目录名命中 → 其子树叶子

    assert client.get("/api/v1/tags/export",
                      params={"scope_id": "dir_none"}).status_code == 404


def test_import_task_export_zip(client):
    """GET /import/tasks/{task_id}/export：批内标签经同一构建器导出。"""
    imported = _import(client, XLSX_BYTES)
    task_id = imported["task_id"]

    resp = client.get(f"/api/v1/import/tasks/{task_id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    assert f"{task_id}-export.zip" in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        workbook = openpyxl.load_workbook(io.BytesIO(archive.read("tags-export.xlsx")))
        xml_root = ET.fromstring(archive.read("tags-export.xml"))
    rows = [_norm_row(r)
            for r in workbook.active.iter_rows(min_row=2, values_only=True)]
    assert rows == [("未分类", "悬疑", "", "悬疑", "以悬念为核心", "系统生成")]
    assert xml_root.find("level1").get("name") == "未分类"
    assert xml_root.find(".//tag").text == "悬疑"

    assert client.get("/api/v1/import/tasks/task_none/export").status_code == 404
