import json

import pytest

from tagplatform.services.generation.open.mtg.parse import MTGParseError
from tagplatform.services.generation.open.mtg.verdicts import (
    VERDICT_FLOORS, VerdictTarget, _normalize_quote, finalize_verdict,
    parse_verdicts, verdict_pass)
from tagplatform.services.tags.value_scoring import (
    VALUE_DIRECTIONS, aggregate_value_score, weights_dict_for_kind)

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


def test_parse_verdicts_prose_wrapped_id_indexed_dict():
    """无围栏 prose + 按 id 索引对象 → 首尾大括号抠取再展平（legacy 缺口修复）。"""
    body = json.dumps({"reviews": {"t0": _FUSED_ENTRY}}, ensure_ascii=False)
    items = parse_verdicts(f"评审结果如下：\n{body}\n请核对。")
    assert [it["id"] for it in items] == ["t0"]
    assert items[0]["relevance"]["score"] == 4
    assert items[0]["value"]["presentation"]["score"] == 3


def test_parse_verdicts_fenced_prose_id_indexed_dict():
    """prose + ```json 围栏 + 按 id 索引对象 → 抠取展平，不被容错三误判为标签名映射。"""
    body = json.dumps({"reviews": {"t0": _FUSED_ENTRY}}, ensure_ascii=False)
    items = parse_verdicts(f"以下是评审结果：\n```json\n{body}\n```\n请核对。")
    assert [it["id"] for it in items] == ["t0"]
    assert items[0]["id"] == "t0" and items[0]["tag"] == ""


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


# ---------------------------------------------------------------------------
# 证据核验与终判 finalize_verdict（评审归一 Task 3）
# ---------------------------------------------------------------------------

# 固定中文材料（带标点）。调用方按 Task 6/7 现口径传入归一化后的核验文本，
# 本节 fixture 用 _normalize_quote 预归一 scene_texts/haystack，quote 用原样
# 摘录（模拟模型输出带标点的形态）。
_SCENE_RAW = {
    "scene_001": "他终于说出了真相，然后夺门而出。",
    "scene_002": "雨夜里两人并肩走过长街。",
    "scene_003": "片尾字幕缓缓升起，灯光渐暗。",
}
_SCENE_TEXTS = {sid: _normalize_quote(text) for sid, text in _SCENE_RAW.items()}
_HAYSTACK = _normalize_quote("".join(_SCENE_RAW.values()))

_GOOD_QUOTE = "他终于说出了真相"            # 8 字，落在 scene_001
_OTHER_QUOTE = "雨夜里两人并肩走过长街"      # 全文有，但不在 scene_001
_GARBAGE_QUOTE = "完全不存在于文本里的话"    # 归一化后 ≥8 字，全文找不到
_SHORT_QUOTE = "真相大白"                    # 归一化后不足 8 字


def _target(**overrides) -> VerdictTarget:
    """film 级全场景目标，按需覆盖字段。"""
    params = dict(key="t0", tag="逃离安排", level="film",
                  allowed_scenes=frozenset(_SCENE_TEXTS),
                  tag_kind="content", has_desc=True)
    params.update(overrides)
    return VerdictTarget(**params)


def _entry(**overrides) -> dict:
    """标准合格条目（parse_verdicts 输出形状），按需覆盖字段。"""
    entry = {
        "id": "t0",
        "relevance": {"score": 4, "reason": "主线强相关"},
        "semantics": {"score": 3, "reason": "语义清楚"},
        "value": {
            "presentation": {"score": 3, "reason": "有看点"},
            "search": {"score": 4, "reason": "类型明确"},
            "recommendation": {"score": 3, "reason": "强度一般"},
        },
        "quote": _GOOD_QUOTE,
        "scenes": ["scene_002", "scene_001", "scene_999"],
        "confidence": 0.85,
    }
    entry.update(overrides)
    return entry


def _agg_score(entry: dict, tag_kind: str, weights=None):
    """value 聚合对拍：aggregate_value_score 同参复算（brief 钉死的公式）。"""
    dims = entry["value"]
    return aggregate_value_score(
        {d: dims[d]["score"] for d in VALUE_DIRECTIONS}, tag_kind, weights=weights)


def test_finalize_film_happy_path():
    """film 快乐路径：quote 可寻 + 两个有效场景 → 分数原样、scenes 排序去无效、value 聚合对拍。"""
    weights = {"content": (2.0, 1.0, 1.0)}
    verdict = finalize_verdict(_entry(), _target(), haystack=_HAYSTACK,
                               scene_texts=_SCENE_TEXTS, value_weights=weights)
    assert verdict["verify_fail"] is None
    assert verdict["relevance"] == {"score": 4, "reason": "主线强相关"}
    assert verdict["semantics"] == {"score": 3, "reason": "语义清楚"}
    assert verdict["scenes"] == ["scene_001", "scene_002"]
    assert verdict["quote"] == _GOOD_QUOTE
    assert verdict["confidence"] == 0.85
    assert verdict["value"]["score"] == _agg_score(_entry(), "content", weights) == 3.25
    assert verdict["value"]["weights"] == weights_dict_for_kind("content", weights)
    assert verdict["value"]["tag_kind"] == "content"
    assert verdict["value"]["dimensions"] == _entry()["value"]


@pytest.mark.parametrize("quote", ["", _SHORT_QUOTE, _GARBAGE_QUOTE],
                         ids=["empty", "too-short", "not-found"])
def test_finalize_quote_fail_presses_relevance(quote):
    """quote 空串 / 不足 8 字 / 全文找不到（relevance≥3）→ 压 1 + verify_fail=="quote"。"""
    verdict = finalize_verdict(_entry(quote=quote), _target(),
                               haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["verify_fail"] == "quote"
    assert verdict["relevance"]["score"] == 1
    assert "未引用可核对原文" in verdict["relevance"]["reason"]
    assert verdict["reason"] == verdict["relevance"]["reason"]
    assert verdict["semantics"]["score"] == 3
    assert verdict["value"]["score"] == _agg_score(_entry(), "content")
    assert verdict["scenes"] == ["scene_001", "scene_002"]


def test_finalize_film_single_scene_presses_relevance():
    """film 级只引 1 个有效场景（relevance≥3）→ 压 1 + verify_fail=="scenes"。"""
    verdict = finalize_verdict(_entry(scenes=["scene_001"]), _target(),
                               haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["verify_fail"] == "scenes"
    assert verdict["relevance"]["score"] == 1
    assert "引用场景不足" in verdict["relevance"]["reason"]
    assert verdict["reason"] == verdict["relevance"]["reason"]
    assert verdict["scenes"] == ["scene_001"]


def test_finalize_scene_citation_outside_allowed_not_counted():
    """scene 级引用不在 allowed_scenes 内 → 不计数 → 压 1 + verify_fail=="scenes"。"""
    target = _target(level="scene", allowed_scenes=frozenset({"scene_001"}))
    verdict = finalize_verdict(_entry(scenes=["scene_002"]), target,
                               haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["verify_fail"] == "scenes"
    assert verdict["relevance"]["score"] == 1
    assert verdict["scenes"] == []


@pytest.mark.parametrize("quote,expect_fail", [
    pytest.param(_GOOD_QUOTE, None, id="quote-ok"),
    pytest.param(_GARBAGE_QUOTE, "quote", id="quote-bad"),
])
def test_finalize_scene_empty_allowed_falls_back_to_haystack(quote, expect_fail):
    """scene 级 allowed_scenes 空集 → 计数免查、quote 核验退回全文 haystack。"""
    target = _target(level="scene", allowed_scenes=frozenset())
    verdict = finalize_verdict(_entry(quote=quote), target,
                               haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["verify_fail"] == expect_fail
    assert verdict["relevance"]["score"] == (4 if expect_fail is None else 1)
    assert verdict["scenes"] == []


def test_finalize_scene_quote_must_land_in_allowed_scene():
    """scene 级 quote 全文有、allowed 场景文本内没有 → 压 1 + verify_fail=="quote"。"""
    target = _target(level="scene", allowed_scenes=frozenset({"scene_001"}))
    verdict = finalize_verdict(_entry(scenes=["scene_001"], quote=_OTHER_QUOTE),
                               target, haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["verify_fail"] == "quote"
    assert verdict["relevance"]["score"] == 1
    assert verdict["scenes"] == ["scene_001"]


@pytest.mark.parametrize("score,flag,has_desc,expected", [
    pytest.param(3, True, False, {"score": None, "reason": "缺少释义"}, id="nulled"),
    pytest.param(2, True, False, {"score": None, "reason": "缺少释义"}, id="boundary-2"),
    pytest.param(1, True, False, {"score": 1, "reason": "低分理由"}, id="below-2-kept"),
    pytest.param(3, False, False, {"score": 3, "reason": "语义清楚"}, id="flag-off"),
    pytest.param(3, True, True, {"score": 3, "reason": "语义清楚"}, id="has-desc"),
])
def test_finalize_semantics_null_without_desc(score, flag, has_desc, expected):
    """缺释义置 null 规则：仅 flag 开 + has_desc=False + 分 ≥2 触发。"""
    entry = _entry(semantics={"score": score, "reason": expected["reason"]})
    verdict = finalize_verdict(entry, _target(has_desc=has_desc),
                               haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS,
                               semantics_null_without_desc=flag)
    assert verdict["semantics"] == expected


def test_finalize_semantics_null_stays_null_with_desc_flag_on():
    """semantics 已是 null（无法判断）→ 缺释义规则不改写，原样保留。"""
    entry = _entry(semantics={"score": None, "reason": "材料缺失"})
    verdict = finalize_verdict(entry, _target(has_desc=False),
                               haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS,
                               semantics_null_without_desc=True)
    assert verdict["semantics"] == {"score": None, "reason": "材料缺失"}


def test_finalize_derived_reason_concatenates_axes():
    """无压分 → 顶层 reason = relevance/semantics 简要 + value 三方向汇总拼接。"""
    verdict = finalize_verdict(_entry(), _target(),
                               haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["reason"] == ("相关性(4)：主线强相关；语义(3)：语义清楚；"
                                 "内容呈现(3)：有看点；内容搜索(4)：类型明确；"
                                 "内容推荐(3)：强度一般")


@pytest.mark.parametrize("relevance", [
    pytest.param({"score": None, "reason": "材料缺失"}, id="null"),
    pytest.param({"score": 2, "reason": "偏题"}, id="below-floor"),
])
def test_finalize_no_verification_when_relevance_null_or_below_floor(relevance):
    """relevance 为 None 或 <3 → 不核验不压分（None=无法判断，不是证据不足）。"""
    verdict = finalize_verdict(_entry(relevance=relevance, quote=_GARBAGE_QUOTE),
                               _target(), haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["verify_fail"] is None
    assert verdict["relevance"] == relevance


def test_finalize_derived_reason_renders_null_score():
    """relevance 为 null 时派生 reason 按「null」渲染（_summarize_value_reasons 同款）。"""
    verdict = finalize_verdict(_entry(relevance={"score": None, "reason": "材料缺失"}),
                               _target(), haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["reason"].startswith("相关性(null)：材料缺失；")


def test_finalize_value_overall_reason_priority_and_kind_alias():
    """item value 带整体 reason → value 块与顶层 reason 都用它；tag_kind 别名归一。"""
    dims = {
        "reason": "整体价值中上",
        "presentation": {"score": 3, "reason": "有看点"},
        "search": {"score": 4, "reason": "类型明确"},
        "recommendation": {"score": 3, "reason": "强度一般"},
    }
    verdict = finalize_verdict(
        _entry(value=dims, scenes=["scene_001", "scene_002"]),
        _target(tag_kind="角色级"), haystack=_HAYSTACK, scene_texts=_SCENE_TEXTS)
    assert verdict["value"]["reason"] == "整体价值中上"
    assert verdict["reason"].endswith("；整体价值中上")
    assert verdict["value"]["tag_kind"] == "character"
    assert set(verdict["value"]["dimensions"]) == set(VALUE_DIRECTIONS)
