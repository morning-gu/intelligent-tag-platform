"""内容 JSON 的存储形态与读取。

`media_asset.content_json` 一律存**上游模板形态**（`new_json.txt`：每个
字段都在，缺值 null、空列表 []），由 `content_template.to_template()` 归一化
后写入；`GET /assets/{id}/content` 也按同一形态返回。

平台内部仍需要归一化结构（生成标签、渲染 prompt、算证据），所以在**读**的
时候才转换一次：`load_content()` 判别存储形态后返回 `ContentJSON`。

判别规则（两种形态的互斥特征）：
- 上游模板：`content.description` + `scenes[].start_ms`
- 平台内部：`content.summary` + `scenes[].time_range`

历史数据（2026-11 之前导入的内部形态）继续可读，不需要迁移；读端点会把它
按模板反推（情绪 code 词表反查，见 `content_template`）。
"""
from __future__ import annotations

import json

from tagplatform.adapters.vision import to_content_json
from tagplatform.schemas.content import ContentJSON, parse_content
from tagplatform.schemas.upstream import parse_upstream


def is_upstream_shape(data: dict) -> bool:
    """判断一份内容 JSON 是否是上游模板形态（而不是平台内部形态）。"""
    content = data.get("content")
    if not isinstance(content, dict):
        return False
    if "description" in content and "summary" not in content:
        scenes = content.get("scenes") or []
        if not scenes:
            return True
        first = scenes[0] if isinstance(scenes[0], dict) else {}
        return "start_ms" in first and "time_range" not in first
    return False


def load_content(raw_json: str) -> ContentJSON:
    """库里存的 JSON 文本 → 平台内部 ContentJSON（两种形态都认）。"""
    data = json.loads(raw_json)
    if is_upstream_shape(data):
        return to_content_json(parse_upstream(data))
    return parse_content(data)


def dump_for_storage(data: dict) -> str:
    """把**原始**内容 JSON 序列化后入库（不做字段改名/裁剪）。"""
    return json.dumps(data, ensure_ascii=False)
