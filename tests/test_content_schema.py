"""统一内容 JSON 模型测试（Task 4）。

覆盖契约：
- 设计文档 §3.3.1 完整示例可经 parse_content 解析，关键字段逐字落位；
- 四类非法变体（end<start、负 duration、非法 asset_type、非法
  schema_version）均在 parse_content 处抛 pydantic.ValidationError。

非法变体通过"深拷贝 fixture + 按 path 覆写"构造：path 元素为 dict 键或
list 下标，统一用 node[key] 逐步定位后覆写末位。
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from tagplatform.schemas.content import parse_content

FIXTURE = Path("tests/fixtures/content_movie.json")


def _load_fixture() -> dict:
    """读取设计文档 §3.3.1 完整示例 JSON。"""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_spec_example_valid():
    data = json.loads(Path("tests/fixtures/content_movie.json").read_text(encoding="utf-8"))
    c = parse_content(data)
    assert c.asset.asset_id == "movie_001" and c.content.scenes[0].dialogues[0].speaker == "角色A"


@pytest.mark.parametrize("mutate", [
    {"path": ("content", "scenes", 0, "time_range", "end_ms"), "value": 1000},   # end<start
    {"path": ("asset", "duration_ms"), "value": -1},
    {"path": ("asset", "asset_type"), "value": "clip"},
    {"path": ("schema_version",), "value": "0.9"},
])
def test_invalid_rejected(mutate):
    # 深拷贝 fixture 按 path 覆写后 parse_content 应抛 ValidationError
    data = deepcopy(_load_fixture())
    node = data
    for key in mutate["path"][:-1]:
        node = node[key]
    node[mutate["path"][-1]] = mutate["value"]
    with pytest.raises(ValidationError):
        parse_content(data)
