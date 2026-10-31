"""LLM 客户端层测试：OpenAI 兼容客户端（transport 注入）与确定性 Mock。

零网络红线：全程不触发真实请求，`openai` 包不得进入 sys.modules（懒导入）。
Mock 契约是后续任务（5/6 产出、8 执行器）的咬合面：模式 A/B 输出键形、
raw OpenAI 信封形状、同 run_id 确定性，均在此钉死。
"""

import json
import sys

from tagplatform.llm import MockLLMClient, OpenAIClient, make_llm_client
from tagplatform.settings import Settings


# ---------- MockLLMClient：模式 A（含 ## 候选标签 节） ----------

def test_mock_closed_selects_from_candidates():
    llm = MockLLMClient()
    msgs = [{"role": "user", "content": "## 候选标签\n- tag_001 | 悬疑 | genre | 以悬念为核心\n- tag_002 | 剧情 | genre | 叙事驱动"}]
    resp = llm.complete(msgs, "run_1")
    assert resp.ok and json.loads(resp.content)  # 列表，元素含 tag_id/relevance/confidence/evidence 键
    assert all(item["tag_id"] in {"tag_001", "tag_002"} for item in json.loads(resp.content))


def test_mock_closed_item_shape():
    """模式 A 元素键形与分值域（parse_closed_output 的输入契约）。"""
    msgs = [{"role": "user", "content": "## 候选标签\n- tag_001 | 悬疑 | genre | 以悬念为核心"}]
    items = json.loads(MockLLMClient().complete(msgs, "run_2").content)
    for it in items:
        assert {"tag_id", "tag_name", "relevance", "confidence", "evidence"} <= set(it)
        assert 0.5 <= it["relevance"] < 1.0 and 0.5 <= it["confidence"] < 1.0
        assert it["evidence"] == []


# ---------- MockLLMClient：模式 B（无候选节 → 固定 3 候选） ----------

def test_mock_open_returns_fixed_three():
    resp = MockLLMClient().complete([{"role": "user", "content": "为以下内容生成标签"}], "r1")
    out = json.loads(resp.content)
    assert [t["tag_name"] for t in out] == ["多线叙事", "情感张力", "视听风格"]


def test_mock_open_item_shape():
    """模式 B 元素键形：不含 tag_id（Review Focus 4 的 mock 侧保障）。"""
    items = json.loads(MockLLMClient().complete([{"role": "user", "content": "生成标签"}], "r").content)
    assert all({"tag_name", "tag_type", "description", "confidence", "evidence"} <= set(it) for it in items)
    assert all("tag_id" not in it for it in items)
    assert [it["tag_type"] for it in items] == ["narrative", "mood", "style"]
    assert all(0.5 <= it["confidence"] < 1.0 for it in items)
    assert all(it["evidence"] == [] for it in items)


# ---------- MockLLMClient：raw 信封与确定性 ----------

def test_mock_raw_envelope():
    """raw 为 OpenAI 信封形状：缓存命中路径从 choices[0].message.content 还原。"""
    resp = MockLLMClient().complete([{"role": "user", "content": "生成标签"}], "r")
    assert resp.raw["mock"] is True
    assert resp.raw["choices"][0]["message"]["content"] == resp.content
    assert resp.raw["choices"][0]["finish_reason"] == "stop"
    assert resp.raw["usage"] == {"prompt_tokens": 100, "completion_tokens": 50}


def test_mock_deterministic_same_run_id():
    """同 run_id 同输入 → 输出逐字节一致（零随机源，哈希采样可复现）。"""
    msgs = [{"role": "user", "content": "## 候选标签\n- tag_001 | 悬疑 | genre | 以悬念为核心"}]
    a = MockLLMClient().complete(msgs, "run_9")
    b = MockLLMClient().complete(msgs, "run_9")
    assert a.content == b.content and a.raw == b.raw


# ---------- OpenAIClient：transport 注入路径（不发真实请求） ----------

def test_openai_client_json_fallback_and_retry():
    calls = []
    def flaky_transport(model, messages, temperature, response_format, extra_body):
        calls.append(response_format)
        if len(calls) == 1: raise RuntimeError("json_object not supported")
        return {"choices": [{"message": {"content": "[]"}}], "usage": {}}
    client = OpenAIClient("m", transport=flaky_transport, request_interval=0, max_retries=3)
    resp = client.complete([{"role": "user", "content": "x"}], "r")
    assert resp.ok and resp.content == "[]" and calls[0] == {"type": "json_object"} and calls[1] is None


def test_openai_client_all_fail_returns_not_ok():
    def broken(model, messages, temperature, response_format, extra_body):
        raise RuntimeError("network down")
    client = OpenAIClient("m", transport=broken, request_interval=0, max_retries=2)
    resp = client.complete([{"role": "user", "content": "x"}], "r")
    assert not resp.ok and "network down" in resp.raw["error"]


# ---------- Settings 新字段与工厂 ----------

def test_settings_llm_defaults():
    """9 个新字段默认值钉死（计划 Global Constraints 逐字）。"""
    s = Settings()
    assert s.llm_model == "qwen3.7-plus"
    assert s.llm_base_url_env == "OPENAI_BASE_URL"
    assert s.llm_api_key_env == "OPENAI_API_KEY"
    assert s.llm_temperature == 0.7
    assert s.llm_request_interval == 0.5
    assert s.use_mock_llm is False
    assert s.recall_top_k_per_type == 50
    assert s.open_match_threshold == 0.85
    assert s.generation_workers == 2


def test_make_llm_client_branches():
    assert isinstance(make_llm_client(Settings(use_mock_llm=True)), MockLLMClient)
    assert isinstance(make_llm_client(Settings()), OpenAIClient)


def test_openai_lazy_import_red_line():
    """懒导入红线：mock 路径与注入 transport 路径全程不 import openai。"""
    MockLLMClient().complete([{"role": "user", "content": "x"}], "r")
    OpenAIClient("m", transport=lambda m, ms, t, rf, eb: {"choices": [], "usage": {}},
                 request_interval=0).complete([{"role": "user", "content": "x"}], "r")
    make_llm_client(Settings(use_mock_llm=True))
    assert "openai" not in sys.modules
