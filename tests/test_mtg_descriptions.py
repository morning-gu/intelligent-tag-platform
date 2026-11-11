"""标签描述的"通用性"：校验、补写、以及流水线接线。

背景：生成侧原本把 `reason`（本片里的内容依据）直接当 `candidate_description`
落库，于是标签库里出现"总统蜷身藏于货舱"这种只对某部片成立的文字。库里要的
是"这个词本身是什么"的通用释义，例：宏大史诗 → 整体氛围宏大壮阔，具有史诗般
的叙事格局。
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import MOCK_EMBEDDER

from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation.mtg import pipeline as mtg_pipeline
from tagplatform.generation.mtg.descriptions import (
    clean_description,
    content_names,
    fill_descriptions,
    is_general,
)
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.library import try_load_library
from tagplatform.llm.client import LLMResponse
from tagplatform.settings import Settings


@pytest.fixture
def mtg_settings(tmp_path, monkeypatch):
    settings = Settings(
        use_mock_llm=True,
        dataset_root=str(tmp_path / "movienet"),
        mtg_report_dir=str(tmp_path / "audit"),
    )
    monkeypatch.setattr(mtg_pipeline, "get_settings", lambda: settings)
    return settings


def _spec(content, run_id: str) -> GenerationSpec:
    return GenerationSpec(
        task_id=f"t-{run_id}", asset_id=content.asset.asset_id, content=content,
        policy=GenerationPolicy(), prompt_text="", run_id=run_id,
        library=try_load_library(),
    )


class _Judge:
    """全判 supported 的 judge 桩：引用首个场景行的原文。"""

    def complete(self, messages, run_id):
        text = messages[0]["content"]
        match = re.search(r"^- \S+ \[\d+-\d+\] (.+)$", text, re.MULTILINE)
        quote = match.group(1)[:12] if match else ""
        items = []
        for line in text.splitlines():
            if line.startswith("- ") and "｜" in line:
                items.append({"tag": line[2:].split("｜")[0], "verdict": "supported",
                              "importance": "core", "confidence": 0.9,
                              "scenes": [], "quote": quote, "reason": "桩"})
        return LLMResponse(content=json.dumps(items, ensure_ascii=False),
                           usage={"prompt_tokens": 10, "completion_tokens": 5},
                           raw={}, ok=True)


class _TwoStageLLM:
    """按请求类型分流的桩：生成回 tags，补写描述回 items。

    记录两种请求的调用次数，用于断言"描述合规时不再多调一次"。
    """

    def __init__(self, tags, descs=(), desc_invalid_first: bool = False):
        self.tags = tags
        self.descs = list(descs)
        self.desc_invalid_first = desc_invalid_first
        self.gen_calls = 0
        self.desc_calls = 0

    def complete(self, messages, run_id):
        text = messages[0]["content"]
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if "## 通用释义" in text:
            self.desc_calls += 1
            if self.desc_invalid_first and self.desc_calls == 1:
                payload = {"items": [{"tag": "多线叙事", "desc": "本片开头的多线并行"}]}
            else:
                payload = {"items": [{"tag": tag, "desc": desc}
                                     for tag, desc in self.descs]}
            return LLMResponse(content=json.dumps(payload, ensure_ascii=False),
                               usage=usage, raw={}, ok=True)
        self.gen_calls += 1
        return LLMResponse(content=json.dumps(self.tags, ensure_ascii=False),
                           usage=usage, raw={}, ok=True)


# ---- 校验 ----


def test_is_general_accepts_dictionary_style():
    assert is_general("整体氛围宏大壮阔，具有史诗般的叙事格局")
    assert is_general("以愤世嫉俗、道德模糊、阴暗氛围为特征的犯罪题材类型")


@pytest.mark.parametrize("text, names", [
    ("总统蜷身藏于货舱，空间封闭压迫", ("总统",)),   # 具体人物 + 剧情
    ("本片贯穿始终的幽闭压迫感", ()),                 # 指代本片
    ("主角在货舱里的压抑", ()),                       # 指代主角
    ("scene_001 的空间压迫感", ()),                   # 场景 id
    ("压抑", ()),                                     # 太短
    ("场景" + "压" * 40, ()),                          # 太长
])
def test_is_general_rejects_plot_specific(text, names):
    assert not is_general(text, names)


def test_clean_description_strips_wrappers():
    assert clean_description("  “以多线并行推进”  ") == "以多线并行推进"


def test_content_names_collects_title(content_fixture):
    names = content_names(content_fixture)
    assert content_fixture.asset.title in names


# ---- 补写 ----


class _DescClient:
    def __init__(self, payloads, ok: bool = True):
        self.payloads = list(payloads)
        self.ok = ok
        self.calls = 0

    def complete(self, messages, run_id):
        self.calls += 1
        payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
        return LLMResponse(content=payload,
                           usage={"prompt_tokens": 10, "completion_tokens": 5},
                           raw={}, ok=self.ok)


def test_fill_descriptions_keeps_only_general_items():
    client = _DescClient([json.dumps({"items": [
        {"tag": "幽闭压迫", "desc": "以封闭狭窄空间带来的压抑窒息感为特征的氛围"},
        {"tag": "硬汉总统", "desc": "总统亲自搏斗并协助驾机稳控"},
    ]}, ensure_ascii=False)])
    filled = fill_descriptions(
        client, [{"tag": "幽闭压迫", "dir": "整体情绪基调"},
                 {"tag": "硬汉总统", "dir": "人物"}],
        names=("总统",), run_id="t1")
    assert set(filled) == {"幽闭压迫"}


def test_fill_descriptions_retries_when_first_round_leaks():
    good = json.dumps({"items": [
        {"tag": "多线叙事", "desc": "以多条时间线并行推进的叙事结构"}]},
        ensure_ascii=False)
    client = _DescClient([good, good])
    filled = fill_descriptions(
        client, [{"tag": "多线叙事", "dir": "叙事结构"}], run_id="t2")
    assert filled["多线叙事"] == "以多条时间线并行推进的叙事结构"
    assert client.calls == 1


def test_fill_descriptions_survives_client_failure():
    class _Boom:
        def complete(self, messages, run_id):
            raise RuntimeError("网络断了")

    assert fill_descriptions(_Boom(), [{"tag": "多线叙事", "dir": ""}],
                             run_id="t3") == {}


# ---- 流水线接线 ----


def _tag(tag="多线叙事", desc=None, scope="scene"):
    item = {"tag": tag, "dimension": "叙事", "scope": scope,
            "scenes": ["scene_001"], "reason": "本片开头的多线并行"}
    if desc is not None:
        item["desc"] = desc
    return item


def test_pipeline_uses_model_desc_without_extra_call(session, content_fixture,
                                                     mtg_settings):
    llm = _TwoStageLLM(
        [_tag(desc="以多条时间线并行推进的叙事结构")],
        descs=[("多线叙事", "不该被用到")])
    out = OpenTagProducer().produce(
        _spec(content_fixture, "d1"), session, llm, MOCK_EMBEDDER,
        judge_client=_Judge())
    assert len(out) == 1
    assert out[0].candidate_description == "以多条时间线并行推进的叙事结构"
    assert llm.desc_calls == 0  # 合规就不再补写


def test_pipeline_backfills_when_desc_missing(session, content_fixture, mtg_settings):
    llm = _TwoStageLLM([_tag()], descs=[("多线叙事", "以多条时间线并行推进的叙事结构")])
    out = OpenTagProducer().produce(
        _spec(content_fixture, "d2"), session, llm, MOCK_EMBEDDER,
        judge_client=_Judge())
    assert out[0].candidate_description == "以多条时间线并行推进的叙事结构"
    assert llm.desc_calls == 1


def test_pipeline_drops_plot_style_desc_instead_of_storing_it(
        session, content_fixture, mtg_settings):
    """模型把剧情当描述写 → 补写；补写也不合规 → 留空，绝不落剧情描述。"""
    plot_style = "示例电影里主角在仓库争执，空间封闭压迫"
    llm = _TwoStageLLM([_tag(desc=plot_style)],
                       descs=[("多线叙事", "示例电影的多线叙事")])  # 仍含片名
    out = OpenTagProducer().produce(
        _spec(content_fixture, "d3"), session, llm, MOCK_EMBEDDER,
        judge_client=_Judge())
    assert out[0].candidate_description is None
