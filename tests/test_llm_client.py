"""LLM 客户端层测试：OpenAI 兼容客户端（transport 注入）与确定性 Mock。

零网络红线：全程不触发真实请求，`openai` 包不得进入 sys.modules（懒导入）。
Mock 契约是后续任务（5/6 产出、8 执行器）的咬合面：模式 A/B 输出键形、
raw OpenAI 信封形状、同 run_id 确定性，均在此钉死。
"""

import hashlib
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
    # run_1: _hash_unit("run_1tag_001")=0.2787 < 0.4 → 必选中（run_2 会零选中空转）
    items = json.loads(MockLLMClient().complete(msgs, "run_1").content)
    assert items != []  # 防哈希扰动再退化成空转通过（false-green）
    for it in items:
        assert {"tag_id", "tag_name", "relevance", "confidence", "evidence"} <= set(it)
        assert 0.5 <= it["relevance"] < 1.0 and 0.5 <= it["confidence"] < 1.0
        # prompt 无场景行（仅候选节）→ evidence 兜底空列表（Task 6 兜底语义）
        assert it["evidence"] == []


# ---------- MockLLMClient：场景证据（Task 6，runner 渲染文本路径） ----------

# 场景行文本（runner._render_content_text 的组装格式，逐字对齐）
_SCENE_LINES = [
    "- scene_001 [120000-180000] 两名人物在仓库中发生激烈争执",
    "- scene_002 [300000-360000] 追车戏在隧道中展开",
]
_SCENE_DATA = [
    ("scene_001", 120000, 180000, "两名人物在仓库中发生激烈争执"),
    ("scene_002", 300000, 360000, "追车戏在隧道中展开"),
]


def _expected_evidence(run_id: str, key: str) -> list[dict]:
    """复算 Mock 场景证据选取：sha256(evidence|run_id|key) 前 8 hex → 均匀
    [0,1) 按场景数分桶（与 llm/mock.py _pick_evidence 同式，钉死哈希键与映射）。"""
    h = hashlib.sha256(f"evidence|{run_id}|{key}".encode("utf-8")).hexdigest()
    sid, start, end, desc = _SCENE_DATA[int(int(h[:8], 16) / 0x1_0000_0000
                                            * len(_SCENE_DATA))]
    return [{"scene_id": sid, "start_ms": start, "end_ms": end,
             "description": desc}]


def test_mock_closed_scene_evidence():
    """模式 A 场景证据（Task 6）：选中标签 evidence 为一元素证据卡，取自按
    sha256(evidence|run_id|tag_id) 选中的场景行（scene_id/时间戳来自该行）。"""
    prompt = ("《示例电影》\n摘要：影片整体剧情摘要\n" + "\n".join(_SCENE_LINES)
              + "\n## 候选标签\n- tag_001 | 悬疑 | genre | 以悬念为核心")
    items = json.loads(MockLLMClient().complete(
        [{"role": "user", "content": prompt}], "run_1").content)
    assert len(items) == 1  # run_1: sha256(run_1tag_001) < 0.4 必选中
    assert items[0]["evidence"] == _expected_evidence("run_1", "tag_001")


# ---------- MockLLMClient：开放式生成（mtg）与 judge 两条分支 ----------

def test_mock_open_returns_fixed_three():
    resp = MockLLMClient().complete([{"role": "user", "content": "为以下内容生成标签"}], "r1")
    out = json.loads(resp.content)
    assert [t["tag"] for t in out] == ["多线叙事", "情感张力", "视听风格"]


def test_mock_open_item_shape():
    """生成分支键形：tag/dimension/scenes/reason（与 mtg 解析契约一致）。"""
    items = json.loads(MockLLMClient().complete(
        [{"role": "user", "content": "生成标签"}], "r").content)
    assert all({"tag", "dimension", "scenes", "reason"} <= set(it) for it in items)
    assert [it["dimension"] for it in items] == ["叙事", "情绪", "风格"]
    # prompt 无场景行（title-only 兜底）→ scenes 仍空列表
    assert all(it["scenes"] == [] for it in items)


def test_mock_open_scene_evidence():
    """生成分支：有场景行时按顺序轮转给场景 id（证据可追溯）。"""
    prompt = "## 标签生成任务\n《示例电影》\n摘要：影片整体剧情摘要\n" + "\n".join(_SCENE_LINES)
    items = json.loads(MockLLMClient().complete(
        [{"role": "user", "content": prompt}], "r1").content)
    assert [it["tag"] for it in items] == ["多线叙事", "情感张力", "视听风格"]
    scene_ids = {row[0] for row in _SCENE_DATA}
    assert all(set(it["scenes"]) <= scene_ids for it in items)


def test_mock_judge_branch():
    """审核分支：候选行全部判 supported、confidence 0.9、引用首场景。"""
    prompt = ("## 审核任务\n## 候选标签\n- 多线叙事｜叙事｜多线索交替\n"
              "- 情感张力｜情绪｜持续的紧张感\n\n## 影片内容\n" + "\n".join(_SCENE_LINES))
    items = json.loads(MockLLMClient().complete(
        [{"role": "user", "content": prompt}], "r1").content)
    assert [it["tag"] for it in items] == ["多线叙事", "情感张力"]
    assert all(it["verdict"] == "supported" for it in items)
    assert all(it["confidence"] == 0.9 for it in items)
    assert all(it["scenes"] == ["scene_001"] for it in items)


# ---------- MockLLMClient：raw 信封与确定性 ----------

def test_mock_raw_envelope():
    """raw 为 OpenAI 信封形状：缓存命中路径从 choices[0].message.content 还原。"""
    resp = MockLLMClient().complete([{"role": "user", "content": "生成标签"}], "r")
    assert resp.raw["mock"] is True
    assert resp.raw["choices"][0]["message"]["content"] == resp.content
    assert resp.raw["choices"][0]["finish_reason"] == "stop"
    assert resp.raw["usage"] == {"prompt_tokens": 100, "completion_tokens": 50}


def test_mock_deterministic_same_run_id():
    """同 run_id 同输入 → 输出逐字节一致（零随机源，哈希采样可复现）。

    prompt 带场景行：确定性同时覆盖场景证据字段（Task 6）。"""
    msgs = [{"role": "user", "content": "《示例电影》\n摘要：摘要\n"
             + "\n".join(_SCENE_LINES)
             + "\n## 候选标签\n- tag_001 | 悬疑 | genre | 以悬念为核心"}]
    a = MockLLMClient().complete(msgs, "run_9")
    b = MockLLMClient().complete(msgs, "run_9")
    assert a.content == b.content and a.raw == b.raw


# ---------- OpenAIClient：transport 注入路径（不发真实请求） ----------

def test_openai_client_json_fallback_and_retry():
    calls = []
    def flaky_transport(model, messages, temperature, response_format, extra_body,
                   max_tokens=None):
        calls.append(response_format)
        if len(calls) == 1: raise RuntimeError("json_object not supported")
        return {"choices": [{"message": {"content": "[]"}}], "usage": {}}
    client = OpenAIClient("m", transport=flaky_transport, request_interval=0, max_retries=3)
    resp = client.complete([{"role": "user", "content": "x"}], "r")
    assert resp.ok and resp.content == "[]" and calls[0] == {"type": "json_object"} and calls[1] is None


def test_openai_client_all_fail_returns_not_ok():
    def broken(model, messages, temperature, response_format, extra_body,
             max_tokens=None):
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
