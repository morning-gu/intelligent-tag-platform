"""相似度复核（mtg/verify.py）测试：请求形状、解析容错、失败不阻塞。"""

import json

from tagplatform.services.generation.open.mtg.verify import (
    build_verify_messages,
    parse_verify_results,
    verify_pairs,
)
from tagplatform.capabilities.llm.client import LLMResponse


class _Client:
    """按脚本返回内容的桩；error=True 时抛异常。"""

    def __init__(self, content: str = "", error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls = 0

    def complete(self, messages, run_id):
        self.calls += 1
        if self.error:
            raise self.error
        return LLMResponse(content=self.content,
                           usage={"prompt_tokens": 12, "completion_tokens": 6},
                           raw={}, ok=True)


def test_build_messages_caps_pair_count():
    pairs = [(f"候选{i}", f"库{i}", 0.9) for i in range(45)]
    text = build_verify_messages(pairs)[0]["content"]
    assert text.count("\n- 候选") == 40


def test_parse_tolerates_code_fence_and_noise():
    raw = ('好的，结果如下：\n```json\n'
           '{"results":[{"candidate":"悬疑神秘","same":true}]}\n```\n以上。')
    assert parse_verify_results(raw) == {"悬疑神秘": True}


def test_verify_pairs_returns_map_and_counts_usage():
    client = _Client(json.dumps({"results": [{"candidate": "悬疑神秘",
                                              "same": False}]}))
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    out = verify_pairs(client, [("悬疑神秘", "悬疑", 0.9)],
                       run_id="r1", usage=usage)
    assert out == {"悬疑神秘": False}
    assert usage == {"calls": 1, "prompt_tokens": 12, "completion_tokens": 6}


def test_verify_pairs_swallows_client_error():
    """复核失败不抛：任务继续跑，只是不替换。"""
    client = _Client(error=RuntimeError("boom"))
    assert verify_pairs(client, [("悬疑神秘", "悬疑", 0.9)], run_id="r1") == {}


def test_verify_pairs_gives_up_after_retries():
    client = _Client("这不是 JSON")
    assert verify_pairs(client, [("悬疑神秘", "悬疑", 0.9)],
                        run_id="r1", retries=1) == {}
    assert client.calls == 2  # 首次 + 一次重试


def test_verify_pairs_empty_input_makes_no_call():
    client = _Client("{}")
    assert verify_pairs(client, [], run_id="r1") == {}
    assert client.calls == 0


def test_verify_pairs_covers_candidates_after_first_batch():
    class EchoClient:
        def complete(self, messages, run_id):
            rows = [line.split("｜", 1)[0][2:] for line in messages[0]["content"].splitlines()
                    if line.startswith("- 候选")]
            return LLMResponse(content=json.dumps({"results": [
                {"candidate": name, "same": False} for name in rows
            ]}), usage={}, raw={}, ok=True)

    pairs = [(f"候选{i}", f"库{i}", 0.9) for i in range(45)]
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    trace = {}
    assert verify_pairs(EchoClient(), pairs, run_id="verify", usage=usage, trace=trace) == {
        name: False for name, _, _ in pairs
    }
    assert usage["calls"] == 2
    assert [row["batch"] for row in trace["batches"]] == [1, 2]
