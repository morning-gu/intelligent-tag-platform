"""LLM 客户端层测试：OpenAI 兼容客户端（transport 注入）与工厂/懒导入。

零网络红线：全程不触发真实请求，`openai` 包不得进入 sys.modules（懒导入）。
"""

import sys
from types import SimpleNamespace

from tagplatform.capabilities.llm import MockLLMClient, OpenAIClient, make_llm_client
from tagplatform.settings import Settings


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


# ---------- 工厂与懒导入 ----------


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
    assert make_llm_client(Settings()).extra_body == {}


def test_openai_lazy_import_red_line():
    """懒导入红线：mock 路径与注入 transport 路径全程不 import openai。"""
    MockLLMClient().complete([{"role": "user", "content": "x"}], "r")
    OpenAIClient("m", transport=lambda m, ms, t, rf, eb: {"choices": [], "usage": {}},
                 request_interval=0).complete([{"role": "user", "content": "x"}], "r")
    make_llm_client(Settings(use_mock_llm=True))
    assert "openai" not in sys.modules
