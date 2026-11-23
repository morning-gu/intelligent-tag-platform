"""LLM 客户端层测试：OpenAI 兼容客户端（transport 注入）与确定性 Mock。

零网络红线：全程不触发真实请求，`openai` 包不得进入 sys.modules（懒导入）。
Mock 契约是后续任务（5/6 产出、8 执行器）的咬合面：模式 A/B 输出键形、
raw OpenAI 信封形状、同 run_id 确定性，均在此钉死。
"""

import hashlib
import json
import sys
from types import SimpleNamespace

from tagplatform.capabilities.llm import MockLLMClient, OpenAIClient, make_llm_client
from tagplatform.settings import Settings


# ---------- MockLLMClient：模式 A（含 ## 候选标签分组 JSON 节） ----------

def _group_prompt(tags: list[dict]) -> str:
    """组装单组模式 A prompt（ClosedTagProducer.produce 的追加格式）。"""
    return ("## 候选标签分组\n" + json.dumps(
        {"dir_id": "dir_genre", "dir_name": "主类型", "dir_description": "故事类型",
         "tags": tags}, ensure_ascii=False, indent=2))


def test_mock_closed_selects_from_candidates():
    llm = MockLLMClient()
    msgs = [{"role": "user", "content": _group_prompt([
        {"tag_id": "tag_001", "tag_name": "悬疑", "description": "以悬念为核心"},
        {"tag_id": "tag_002", "tag_name": "剧情", "description": "叙事驱动"},
    ])}]
    resp = llm.complete(msgs, "run_1")
    assert resp.ok and json.loads(resp.content)
    assert all(item["tag_id"] in {"tag_001", "tag_002"} for item in json.loads(resp.content))


def test_mock_closed_item_shape():
    """模式 A 元素键形与分值域（parse_closed_output 的输入契约）。"""
    msgs = [{"role": "user", "content": _group_prompt(
        [{"tag_id": "tag_001", "tag_name": "悬疑", "description": "以悬念为核心"}])}]
    # run_1: _hash_unit("run_1tag_001")=0.2787 < 0.4 → 必选中（run_2 会零选中空转）
    items = json.loads(MockLLMClient().complete(msgs, "run_1").content)
    assert items != []  # 防哈希扰动再退化成空转通过（false-green）
    for it in items:
        assert {"tag_id", "applicability", "evidence"} <= set(it)
        assert it["applicability"] in {4, 5}
        # v8 嵌套三模态节：每节 evidence_strength 1–5 + evidences 数组
        for modality in ("content", "scene", "frame"):
            section = it["evidence"][modality]
            assert section["evidence_strength"] in {4, 5}
            # prompt 无场景行（仅候选分组节）→ 各模态 evidences 兜底空列表
            assert section["evidences"] == []


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
    """复算 Mock 场景证据选取：sha256(evidence|...) 前 8 hex → 均匀
    [0,1) 按场景数分桶（与 llm/mock.py _pick_evidence 同式）。

    v8：哈希键带模态维度 evidence|{modality}|{run_id}|{key}——三模态
    互不耦合，同一标签可从三个模态选到不同场景。
    """
    h = hashlib.sha256(f"evidence|{run_id}|{key}".encode("utf-8")).hexdigest()
    sid, start, end, desc = _SCENE_DATA[int(int(h[:8], 16) / 0x1_0000_0000
                                            * len(_SCENE_DATA))]
    return [{"scene_id": sid, "start_ms": start, "end_ms": end,
             "description": desc}]


def test_mock_closed_scene_evidence():
    """模式 A 场景证据（v8 嵌套）：选中标签各模态节按
    sha256(evidence|{modality}|{run_id}|{tag_id}) 各自确定性选一条场景，
    哈希键带模态维度，三模态选取互不耦合。"""
    prompt = ("《示例电影》\n摘要：影片整体剧情摘要\n" + "\n".join(_SCENE_LINES)
              + "\n" + _group_prompt(
                  [{"tag_id": "tag_001", "tag_name": "悬疑", "description": "以悬念为核心"}]))
    items = json.loads(MockLLMClient().complete(
        [{"role": "user", "content": prompt}], "run_1").content)
    assert len(items) == 1  # run_1: sha256(run_1tag_001) < 0.4 必选中
    for modality in ("content", "scene", "frame"):
        assert items[0]["evidence"][modality]["evidences"] == [{
            "scene_id": item["scene_id"],
            "evidence": item["description"],
        } for item in _expected_evidence(f"{modality}|run_1", "tag_001")]


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
    """审核分支：候选行全部给过线三维分（4/3/3）、confidence 0.9、引用首场景。"""
    prompt = ("## 审核任务\n## 候选标签\n- 多线叙事｜叙事｜多线索交替\n"
              "- 情感张力｜情绪｜持续的紧张感\n\n## 影片内容\n" + "\n".join(_SCENE_LINES))
    items = json.loads(MockLLMClient().complete(
        [{"role": "user", "content": prompt}], "r1").content)
    assert [it["tag"] for it in items] == ["多线叙事", "情感张力"]
    assert all((it["relevance"], it["semantics"], it["value"]) == (4, 3, 3)
               for it in items)
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
             + "\n" + _group_prompt(
                 [{"tag_id": "tag_001", "tag_name": "悬疑", "description": "以悬念为核心"}])}]
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


def test_openai_stream_preserves_finish_reason_and_trailing_usage(monkeypatch):
    usage = {"prompt_tokens": 10, "completion_tokens": 8192}
    stream = [
        {"choices": [{"delta": {"reasoning_content": "思考过程"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": '{"tags":['}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": '{"id":1}'}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
        {"choices": [], "usage": usage},
    ]

    def create(**kwargs):
        assert kwargs["stream"] is True and kwargs["max_tokens"] == 8192
        return (SimpleNamespace(model_dump=lambda chunk=chunk: chunk) for chunk in stream)

    def fake_openai(**kwargs):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=fake_openai))
    client = OpenAIClient("m", request_interval=0, max_retries=1, max_tokens=8192)

    response = client.complete([{"role": "user", "content": "评分"}], "r")

    assert response.ok
    assert response.content == '{"tags":[{"id":1}'
    assert response.raw["choices"][0]["finish_reason"] == "length"
    assert response.usage == usage


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


def test_make_llm_client_passes_extra_body():
    # vLLM 端点需要 {"chat_template_kwargs": {"enable_thinking": false}}
    # 关思维链（否则 closed 判定输出从数组漂成单对象，147.3 实测）；
    # OpenAIClient 的 extra_body 管道已有，缺的是 settings → 工厂透传
    eb = {"chat_template_kwargs": {"enable_thinking": False}}
    client = make_llm_client(Settings(llm_extra_body=eb))
    assert isinstance(client, OpenAIClient)
    assert client.extra_body == eb


def test_make_llm_client_default_no_extra_body():
    client = make_llm_client(Settings())
    assert client.extra_body == {}


def test_settings_llm_extra_body_parses_env_json(monkeypatch):
    monkeypatch.setenv("TAGPLT_LLM_EXTRA_BODY",
                       '{"chat_template_kwargs": {"enable_thinking": false}}')
    s = Settings()
    assert s.llm_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_openai_lazy_import_red_line():
    """懒导入红线：mock 路径与注入 transport 路径全程不 import openai。"""
    MockLLMClient().complete([{"role": "user", "content": "x"}], "r")
    OpenAIClient("m", transport=lambda m, ms, t, rf, eb: {"choices": [], "usage": {}},
                 request_interval=0).complete([{"role": "user", "content": "x"}], "r")
    make_llm_client(Settings(use_mock_llm=True))
    assert "openai" not in sys.modules
