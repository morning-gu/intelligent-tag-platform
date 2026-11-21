"""编排层跨域公共装配（_shared）：pre_asset_tag 行输出三件套与共享 DTO。

四层重构 Task 11 收编：原散落在 api/routers/generation.py（行输出三件套
`_result_out`/`_tag_info`/`_parse_evidence` 与 `AssetTagOut`/`EvidenceOut`/
`GenerationResultOut`）与 api/routers/tags.py（导入结果摘要 `ImportSummary`
及导出归档 `_build_export_archive`）的跨 router 私有导入（review.py:40、
imports.py:27 的寄生 import）在此归位——编排层同层互不 import（红线），
共享装配只进本模块，消费方：orchestration/{tasks,generation,review,imports,tags}
与接入层对应 router。

内容三组：
- 时间戳序列化类型 `UtcDatetime`（原 api/schemas.py，响应模型随编排层
  下沉后定义随之迁移——编排层不得反向 import api）；
- pre_asset_tag 行输出三件套 + 三个行模型（生成任务详情 results、审核
  返回行、资产正式挂载视图共用同一套行装配）；
- 标签导入结果摘要三模型 + 导出归档构建器（/tags/export 与导入批次导出
  共用，zip 格式逐字沿 v1）。
"""

import io
import json
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Annotated

import openpyxl
from pydantic import BaseModel, WrapSerializer, ValidationError
from sqlalchemy.orm import Session

from tagplatform.capabilities.storage.models import PreAssetTag, TagCatalog, TagDefine
from tagplatform.services.tags.library import try_load_library


# ---------- 响应时间戳口径 ----------

def _serialize_utc(value: datetime, handler):
    """naive 值按 UTC 补 tzinfo 后交给 Pydantic 缺省序列化。

    历史行与现行写入点均为 UTC，故在序列化侧统一补 UTC（读侧不转换）；
    已带 tzinfo 的值原样通过。None 由 Pydantic 在进入序列化器前短路，
    不会调到这里。返回值不标类型：JSON 模式出 str、python 模式出
    datetime，标任一端都会触发 Pydantic 序列化警告。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return handler(value)


UtcDatetime = Annotated[datetime, WrapSerializer(_serialize_utc)]
"""响应模型 datetime 字段注解：naive 视为 UTC，输出 ISO-8601 带 Z
（api/schemas.py 反向 re-export 本名，接入层引用路径不变）。"""


# ---------- pre_asset_tag 行输出（三件套 + 行模型）----------

class EvidenceOut(BaseModel):
    """单条证据明细（与 generation.Evidence 字段一一对应）。"""

    scene_id: str
    start_ms: int
    end_ms: int
    description: str


class GenerationResultOut(BaseModel):
    """生成结果摘要行：id 为 pre_asset_tag 行主键（审核操作路由与前端行操作
    的定位键）；tag_name 展示解析 candidate_name 优先、目录名回退。"""

    id: int
    tag_name: str
    # 库内标签 id（closed 行/已合并行非空）；开放生成的新词行是 None
    tag_id: str | None = None
    # 行内候选名（开放生成新词非空；closed 行是 None）
    candidate_name: str | None = None
    origin: str
    relevance_score: float | None = None
    confidence: float | None = None
    review_status: str
    # 挂载粒度：由标签所属目录的 applicable_scope 决定
    target_type: str = "content"
    # 所属目录（JSON 标签库）：新词必填，catalog 行取标签自身目录
    dir_id: str | None = None
    dir_name: str | None = None
    # 目录全路径（L1/…/末级）；审核中心同样展示这一列，两边口径一致
    dir_path: str | None = None
    evidence: list[EvidenceOut] = []


class AssetTagOut(BaseModel):
    """资产正式挂载行（`asset_tag`）：审核通过后复制过来的挂载关系。

    - `id`：挂载行主键（审核接口返回的是**被审核的 pre 行**主键，两者语义
      不同，前端不要跨接口比较）；
    - `tag_name` 是库内规范名（审核 modify 的临时改名留在 pre 侧，正式挂载
      按 doc §3.6/§6.3 复制 define 现值）；
    - `origin` 由同键 pre 行回填（正式表不存来源），无对应 pre 行时为 None；
    - 目录列由 `tag_define → tag_catalog` 实时解析；
    - 评分/证据/审核状态是审核侧字段：挂载视图不带（恒 None/空），保留这些
      字段是为了让审核接口能复用同一个响应模型。
    """

    id: int
    tag_id: str | None = None
    tag_name: str
    candidate_name: str | None = None
    origin: str | None = None
    relevance_score: float | None = None
    confidence: float | None = None
    review_status: str = "approved"
    target_type: str = "content"
    dir_id: str | None = None
    dir_name: str | None = None
    dir_path: str | None = None
    evidence: list[EvidenceOut] = []
    created_at: UtcDatetime | None = None


def _parse_evidence(raw: str | None) -> list[EvidenceOut]:
    """evidence_json → 证据列表；空/损坏 JSON/条目 shape 非法（缺必填字段
    或类型不符 → ValidationError）一律按空列表兜底（终审 deferred 落地：
    审核列表与任务详情两条消费路径对坏证据均不再 500）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    try:
        return [EvidenceOut(**item) for item in data if isinstance(item, dict)]
    except ValidationError:
        return []


def _tag_info(rows: list[PreAssetTag]) -> dict[str, dict]:
    """行内 tag_id → 目录信息（名称/类型/目录），读 `tag_catalog`/`tag_define`
    的库视图（v2 起标签库就是这两张表，不再有 JSON 文件）。"""
    library = try_load_library()
    tag_ids = {r.tag_id for r in rows if r.tag_id}
    if library is None or not tag_ids:
        return {}
    info: dict[str, dict] = {}
    for tag_id in tag_ids:
        definition = library.tag(tag_id)
        if definition is None:
            continue
        directory = library.directory(definition.dir_id)
        info[tag_id] = {
            "tag_name": definition.tag_name,
            "tag_type": definition.tag_type,
            "dir_id": definition.dir_id,
            "dir_name": directory.dir_name if directory else None,
        }
    return info


def _dir_info(dir_id: str | None) -> tuple[str | None, str | None, str | None]:
    """dir_id → (dir_name, dir_path, 全路径最后一级名)；库不可用/目录缺失时
    全部返回 None（调用方按"没有目录"展示）。"""
    library = try_load_library()
    if library is None or not dir_id:
        return None, None, None
    directory = library.directory(dir_id)
    if directory is None:
        return None, None, None
    path = library.path_of(dir_id)
    return directory.dir_name, ("/".join(path) if path else None), None


def _result_out(row: PreAssetTag, info: dict[str, dict]) -> GenerationResultOut:
    """pre_asset_tag 行 → 结果摘要（详情 results 与资产标签视图共用）。

    tag_name 展示解析与审核列表同规：candidate_name 非空优先（审核 modify
    落在行内的展示字段，改了什么显示什么），否则回退库内标签名。
    """
    definition = info.get(row.tag_id or "", {})
    # 三级回退：行内展示名 → 库内标签名 → tag_id。历史行（上一轮落在旧
    # tag_catalog 的 id）在库里查不到，回退成 id，不能显示成空字符串。
    tag_name = (row.candidate_name or definition.get("tag_name")
                or row.tag_id or "")
    dir_id = row.candidate_dir_id or definition.get("dir_id")
    dir_name = definition.get("dir_name")
    dir_path: str | None = None
    if dir_id and not dir_name:
        dir_name, dir_path, _ = _dir_info(dir_id)
    elif dir_id:
        _, dir_path, _ = _dir_info(dir_id)
    return GenerationResultOut(
        id=row.id,
        tag_name=tag_name,
        tag_id=row.tag_id,
        candidate_name=row.candidate_name,
        origin=row.origin,
        relevance_score=row.relevance_score,
        confidence=row.confidence,
        review_status=row.review_status,
        target_type=row.target_type,
        dir_id=dir_id,
        dir_name=dir_name,
        dir_path=dir_path,
        evidence=_parse_evidence(row.evidence_json),
    )


# ---------- 标签导入摘要 + 导出归档（tags / imports 两域共用）----------

class IssueOut(BaseModel):
    """单条治理问题明细（与 governance.Issue 字段一一对应）。"""

    tag_name: str
    code: str
    detail: str
    similar_to: str | None = None


class ParseErrorOut(BaseModel):
    """单行解析失败明细（与 governance.excel.RowError 字段一一对应）。"""

    row_number: int
    tag_name: str | None = None
    code: str
    detail: str


class ImportSummary(BaseModel):
    """导入结果摘要：total/inserted/skipped 计数 + issues/parse_errors 明细列表。

    语义与 tag_import_task.result_summary（治理流水线写入）一致：total 为
    传入治理的**标签定义行**数（目录行落 tag_catalog 不计入；行级解析错误
    的行不进 rows，不计入），inserted 仅计标签行，skipped = total -
    inserted，issues 为治理问题明细（与入库行不相交）；
    parse_errors 为行级解析错误明细，计数以列表长度为准。
    """

    total: int
    inserted: int
    skipped: int
    issues: list[IssueOut]
    parse_errors: list[ParseErrorOut] = []


def _tag_path(tag: TagDefine, dirs_by_id: dict[str, TagCatalog]) -> list[str]:
    """叶子全路径名：根→直属目录的目录名链 + 叶子标签名（含叶子，沿 v1）。"""
    ancestors: list[str] = []
    current: TagCatalog | None = dirs_by_id.get(tag.dir_id)
    while current is not None:
        ancestors.append(current.dir_name)
        current = dirs_by_id.get(current.parent_id) if current.parent_id else None
    ancestors.reverse()
    return [*ancestors, tag.tag_name]


def _build_export_archive(
    export_tags: list[TagDefine], dirs_by_id: dict[str, TagCatalog]
) -> bytes:
    """叶子标签行 + 目录祖先链 → zip（xlsx 六列 + xml 嵌套，v1 格式逐字）。

    xlsx 三级列取路径前三段（含叶子名，沿 v1 的 path 计算口径），xml 按
    路径嵌套 level1/level2/…、叶子为 <tag> 节点；标签生成方式列固定
    "系统生成"（v1 常量）。/tags/export 与导入批次导出共用（原 api/routers/
    tags.py 私有函数，imports router 寄生导入的另一半，随三件套一并收编）。
    """
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "标签"
    worksheet.append(["一级标签名称", "二级标签名称", "三级标签名称", "标签", "标签描述", "标签生成方式"])
    for tag in export_tags:
        path = _tag_path(tag, dirs_by_id)
        levels = (path + ["", "", ""])[:3]
        worksheet.append([*levels, tag.tag_name, tag.description, "系统生成"])

    workbook_output = io.BytesIO()
    workbook.save(workbook_output)

    xml_root = ET.Element("tags")
    xml_nodes: dict[tuple[str, ...], ET.Element] = {}
    for tag in export_tags:
        path = _tag_path(tag, dirs_by_id)
        if len(path) == 1:
            ET.SubElement(
                xml_root,
                "tag",
                {"type": "system", "description": tag.description or ""},
            ).text = path[0]
            continue

        parent = xml_root
        for depth, name in enumerate(path[:-1], start=1):
            key = tuple(path[:depth])
            node = xml_nodes.get(key)
            if node is None:
                node = ET.SubElement(parent, f"level{depth}", {"name": name})
                xml_nodes[key] = node
            parent = node
        ET.SubElement(
            parent,
            "tag",
            {"type": "system", "description": tag.description or ""},
        ).text = path[-1]

    xml_output = ET.tostring(xml_root, encoding="utf-8", xml_declaration=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("tags-export.xlsx", workbook_output.getvalue())
        archive.writestr("tags-export.xml", xml_output)
    return output.getvalue()
