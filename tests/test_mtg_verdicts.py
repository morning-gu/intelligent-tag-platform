import json

import pytest

from tagplatform.services.generation.open.mtg.parse import MTGParseError
from tagplatform.services.generation.open.mtg.verdicts import (
    VERDICT_FLOORS, VerdictTarget, parse_verdicts, verdict_pass)

def test_floors_values():
    assert VERDICT_FLOORS == {"relevance": 3, "semantics": 3, "value": 2}

@pytest.mark.parametrize("rel,sem,val,expected", [
    (4, 3, 3.0, True), (3, 3, 2.0, True),
    (2, 4, 3.0, False), (4, 2, 3.0, False), (4, 3, 1.99, False),
    (None, 3, 3.0, None), (4, None, 3.0, None), (4, 3, None, None),
])
def test_verdict_pass(rel, sem, val, expected):
    assert verdict_pass(rel, sem, val) == expected

def test_verdict_target_fields():
    t = VerdictTarget(key="t0", tag="逃离安排", level="film",
                      allowed_scenes=frozenset({"scene_001"}),
                      tag_kind="content", has_desc=True)
    assert t.key == "t0" and t.level == "film"


# ---------------------------------------------------------------------------
# 融合解析器 parse_verdicts（评审归一 Task 2）
# ---------------------------------------------------------------------------

def _text(entries) -> str:
    """标准包装形状：{"verdicts": [...]}（本节用例的默认输入）。"""
    return json.dumps({"verdicts": entries}, ensure_ascii=False)


_FUSED_ENTRY = {
    "id": "t0",
    "relevance": {"score": 4, "reason": "主线强相关"},
    "semantics": {"score": 3, "reason": "语义清楚"},
    "value": {
        "presentation": {"score": 3, "reason": "有看点"},
        "search": {"score": 4, "reason": "类型明确"},
        "recommendation": {"score": 2, "reason": "强度一般"},
    },
    "quote": "他终于说出了真相",
    "scenes": ["scene_001", "scene_002"],
    "confidence": 0.85,
}


def test_parse_verdicts_fused_shape():
    """融合形状：id + 每维 {score,reason} + value 三方向 + quote/scenes/confidence 全字段。"""
    items = parse_verdicts(_text([_FUSED_ENTRY]))
    assert items == [{
        "id": "t0",
        "tag": "",
        "relevance": {"score": 4, "reason": "主线强相关"},
        "semantics": {"score": 3, "reason": "语义清楚"},
        "value": {
            "presentation": {"score": 3, "reason": "有看点"},
            "search": {"score": 4, "reason": "类型明确"},
            "recommendation": {"score": 2, "reason": "强度一般"},
        },
        "quote": "他终于说出了真相",
        "scenes": ["scene_001", "scene_002"],
        "confidence": 0.85,
    }]


def test_parse_verdicts_judge_legacy_shape():
    """judge 旧形状：tag + candidate_id + 裸分 + value_scores 裸整数 → 等价解析。"""
    text = json.dumps({"verdicts": [{
        "tag": "逃离安排｜主角冒险",
        "candidate_id": "c-42",
        "relevance": 4,
        "semantics": 2.0,
        "value_scores": {"presentation": 3, "search": 1, "recommendation": 0},
        "quote": "  逐字摘录  ",
        "scenes": [" scene_009 "],
        "confidence": 1.7,
    }]}, ensure_ascii=False)
    items = parse_verdicts(text)
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "c-42"
    assert item["tag"] == "逃离安排"
    assert item["relevance"] == {"score": 4, "reason": ""}
    assert item["semantics"]["score"] == 2
    assert isinstance(item["semantics"]["score"], int)
    assert item["value"] == {
        "presentation": {"score": 3, "reason": ""},
        "search": {"score": 1, "reason": ""},
        "recommendation": {"score": 0, "reason": ""},
    }
    assert item["quote"] == "逐字摘录"
    assert item["scenes"] == ["scene_009"]
    assert item["confidence"] == 1.0


@pytest.mark.parametrize("wrap", [
    pytest.param(lambda e: json.dumps({"reviews": [e]}, ensure_ascii=False),
                 id="reviews-list"),
    pytest.param(lambda e: json.dumps({"tags": [e]}, ensure_ascii=False),
                 id="tags-list"),
    pytest.param(lambda e: json.dumps([e], ensure_ascii=False),
                 id="bare-list"),
    pytest.param(lambda e: json.dumps({"reviews": {"t7": e}}, ensure_ascii=False),
                 id="reviews-by-id"),
])
def test_parse_verdicts_wrapper_variants(wrap):
    """包装键族：reviews/tags/裸列表/按 id 索引对象（键即 id）都可用。"""
    entry = {
        "id": "t7",
        "relevance": {"score": 3, "reason": "r"},
        "semantics": {"score": 4, "reason": "s"},
        "value": {"presentation": 2, "search": 3, "recommendation": 4},
    }
    items = parse_verdicts(wrap(entry))
    assert len(items) == 1
    assert items[0]["id"] == "t7"
    assert items[0]["relevance"]["score"] == 3
    assert items[0]["value"]["presentation"] == {"score": 2, "reason": ""}


def test_parse_verdicts_fence_and_prose_stripped():
    """```json 围栏与前后 prose 都剥除。"""
    body = json.dumps({"verdicts": [_FUSED_ENTRY]}, ensure_ascii=False)
    text = f"以下是评审结果：\n```json\n{body}\n```\n请核对。"
    items = parse_verdicts(text)
    assert [it["id"] for it in items] == ["t0"]


@pytest.mark.parametrize("form,reason", [
    ({"score": None, "reason": "材料缺失"}, "材料缺失"),
    (None, ""),
])
def test_parse_verdicts_relevance_explicit_null(form, reason):
    """relevance 显式 null（对象包裹与裸 null）= 无法判断，score None。"""
    entry = {"id": "t1", "relevance": form, "semantics": {"score": 3, "reason": "ok"},
             "value": {"presentation": 3, "search": 3, "recommendation": 3}}
    items = parse_verdicts(_text([entry]))
    assert items[0]["relevance"] == {"score": None, "reason": reason}
    assert items[0]["semantics"]["score"] == 3


@pytest.mark.parametrize("value_obj", [
    pytest.param({"presentation": None, "search": 3, "recommendation": 3},
                 id="null-direction"),
    pytest.param({"search": 3, "recommendation": 3},
                 id="missing-direction"),
    pytest.param({"presentation": 5, "search": 3, "recommendation": 3},
                 id="out-of-range"),
    pytest.param({"presentation": True, "search": 3, "recommendation": 3},
                 id="boolean"),
])
def test_parse_verdicts_value_protocol_error_salvages_rest(value_obj):
    """value 方向 null/漏方向/越界/布尔 → 该条目协议错误，其余条目 salvage 进 items。"""
    good = {"id": "t1", "relevance": 4, "semantics": 3,
            "value": {"presentation": 3, "search": 3, "recommendation": 3}}
    bad = {"id": "t2", "relevance": 3, "semantics": 3, "value": value_obj}
    with pytest.raises(MTGParseError) as excinfo:
        parse_verdicts(_text([bad, good]))
    assert [it["id"] for it in excinfo.value.items] == ["t1"]
    assert "value.presentation" in str(excinfo.value)


def test_parse_verdicts_missing_axis_key_protocol_error():
    """relevance/semantics 漏字段 = 协议错误（judge 严格侧保留），其余 salvage。"""
    good = {"id": "t1", "relevance": 3, "semantics": 3,
            "value": {"presentation": 3, "search": 3, "recommendation": 3}}
    bad = {"id": "t2", "relevance": 3, "value": {"presentation": 3, "search": 3, "recommendation": 3}}
    with pytest.raises(MTGParseError) as excinfo:
        parse_verdicts(_text([bad, good]))
    assert [it["id"] for it in excinfo.value.items] == ["t1"]
    assert "semantics 漏字段" in str(excinfo.value)


def test_parse_verdicts_duplicate_ids_both_dropped():
    """重复 id 两条都剔除（镜像评审规则：重复即不可靠，不是首条生效）。"""
    first = {"id": "t0", "relevance": 4, "semantics": 4,
             "value": {"presentation": 4, "search": 4, "recommendation": 4}}
    second = {"id": "t0", "relevance": 1, "semantics": 1,
              "value": {"presentation": 1, "search": 1, "recommendation": 1}}
    survivor = {"id": "t1", "relevance": 3, "semantics": 3,
                "value": {"presentation": 3, "search": 3, "recommendation": 3}}
    items = parse_verdicts(_text([first, second, survivor]))
    assert [it["id"] for it in items] == ["t1"]


def test_parse_verdicts_chinese_direction_keys_normalized():
    """中文方向键经 VALUE_DIRECTION_ALIASES 归一到三方向（输出顺序固定）。"""
    entry = {"id": "t0", "relevance": 3, "semantics": 3,
             "value": {"内容呈现": 3, "内容搜索": 2, "内容推荐": 4}}
    items = parse_verdicts(_text([entry]))
    assert list(items[0]["value"]) == ["presentation", "search", "recommendation"]
    assert items[0]["value"]["search"] == {"score": 2, "reason": ""}
    assert items[0]["value"]["recommendation"] == {"score": 4, "reason": ""}


def test_parse_verdicts_missing_optional_keys_default():
    """缺 scenes → []、缺 quote → ""、缺 confidence → None、缺 tag → ""、缺 id → None。"""
    entry = {"relevance": 3, "semantics": 3,
             "value": {"presentation": 2, "search": 2, "recommendation": 2}}
    items = parse_verdicts(_text([entry]))
    assert items[0]["id"] is None
    assert items[0]["tag"] == ""
    assert items[0]["scenes"] == []
    assert items[0]["quote"] == ""
    assert items[0]["confidence"] is None


@pytest.mark.parametrize("scenes", [
    pytest.param("scene_001", id="string-not-list"),
    pytest.param(["scene_001", 2], id="non-string-element"),
])
def test_parse_verdicts_scenes_non_list_protocol_error(scenes):
    """scenes 非列表 / 含非字符串元素 → 协议错误，其余条目 salvage。"""
    good = {"id": "t1", "relevance": 3, "semantics": 3,
            "value": {"presentation": 3, "search": 3, "recommendation": 3}}
    bad = {"id": "t2", "relevance": 3, "semantics": 3,
           "value": {"presentation": 3, "search": 3, "recommendation": 3},
           "scenes": scenes}
    with pytest.raises(MTGParseError) as excinfo:
        parse_verdicts(_text([bad, good]))
    assert [it["id"] for it in excinfo.value.items] == ["t1"]
    assert "scenes" in str(excinfo.value)


def test_parse_verdicts_id_stringified():
    """id 数字形式字符串化（评审回 3 / judge 回 candidate_id 同口径）。"""
    entry = {"id": 3, "relevance": 3, "semantics": 3,
             "value": {"presentation": 3, "search": 3, "recommendation": 3}}
    items = parse_verdicts(_text([entry]))
    assert items[0]["id"] == "3"
