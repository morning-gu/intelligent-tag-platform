"""eval/open_review 的单测：二值评审层的解析纪律与摘录必核钉死。

被测层（open-eval Task 3）在 produce 之后对每片产出标签做二值评审
（spec §6）。测试用评审替身（.complete(messages, run_id) 直接回 canned
响应）钉死几件容易写错的事：

1. 解析容错（Review Focus 1，147.3 端点家族畸形前科四起）——围栏包裹、
   reviews 单对象（按 id 索引）、pass/is_pass 别名键、id 容忍 "t3"/3/"3"；
2. 漏判语义——漏判条目 pass=None（不判死、排除出分母），全部漏判才
   ReviewRequestError（评审失败 ≠ 评审 0 分，产出不作废）；
3. 摘录必核（Review Focus 4）——pass 且 quote 核不中渲染文本 → 改判
   fail、reason 注明「摘录未命中原文」，空口通过在这里拦下；
4. 超长切分与格式重试——镜像 tag_quality 的二分行为与 judge 的重试后缀；
5. 客户端工厂——三键全空/mock/零网络替身回落主客户端（同模评审），
   独立评审模型按三键构造（env 名逐键回落主 LLM 设置），构造即可断言、
   不发请求（OpenAIClient 的 transport 懒构建）。
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from tagplatform.capabilities.llm import LLMResponse, MockLLMClient, OpenAIClient
from tagplatform.capabilities.prompts.open_review import (
    REVIEW_OUTPUT_FORMAT,
    REVIEW_PROMPT,
    REVIEW_RETRY_HINT,
)
from tagplatform.schemas import ContentJSON
from tagplatform.services.eval.open_review import (
    ReviewRequestError,
    make_review_client,
    review_calibre,
    review_movie_tags,
)
from tagplatform.settings import Settings

# 与渲染文本对齐的台词锚点（摘录必核的正例引用）
QUOTE = "我要逃离这个被安排好的世界"


def _content() -> ContentJSON:
    """迷你影片：一段场景一句台词（渲染文本可预测，quote 断言有锚点）。"""
    return ContentJSON.model_validate({
        "schema_version": "1.0",
        "asset": {"asset_id": "m1", "asset_type": "movie", "title": "逃离摄影棚",
                  "duration_ms": 60000},
        "content": {
            "summary": "一名察觉生活处处被安排的男子决意查出真相并逃离。",
            "characters": [{"character_id": "c1", "name": "陈晓",
                            "description": "察觉异常的男主角"}],
            "scenes": [{
                "scene_id": "scene_001",
                "time_range": {"start_ms": 0, "end_ms": 30000},
                "description": "陈晓在小镇街道晨跑，电台广播精准说出他的行踪",
                "dialogues": [{"speaker": "陈晓", "text": QUOTE + "。",
                               "start_ms": 1000, "end_ms": 2000}],
            }],
        },
        "metadata": {"source": "test", "recognition_status": "done"},
    })


def _rows(count: int = 2) -> list[dict]:
    """评审入参行（id 用合成序号；前两行带真实材料锚点，其余为通用行）。"""
    head = [
        {"id": "t0", "name": "逃离安排", "desc": "主角察觉生活被全程安排并决意逃离",
         "level": "film", "scenes": []},
        {"id": "t1", "name": "街道晨跑", "desc": "清晨在小镇街道跑步的情节",
         "level": "scene", "scenes": ["scene_001"]},
    ]
    tail = [{"id": f"t{i}", "name": f"标签{i}", "desc": f"第 {i} 条测试标签",
             "level": "film", "scenes": []} for i in range(2, count)]
    return head + tail


def _reviews(*items) -> str:
    return json.dumps({"reviews": list(items)}, ensure_ascii=False)


def _pass(row_id: str, *, quote: str | None = QUOTE,
          reason: str = "台词有据") -> dict:
    item = {"id": row_id, "pass": True, "reason": reason}
    if quote is not None:
        item["quote"] = quote
    return item


def _fail(row_id: str, reason: str = "材料无相关细节") -> dict:
    return {"id": row_id, "pass": False, "reason": reason}


# 与 _rows() 默认两行对齐的规范成功响应（t0 pass 带命中摘录，t1 fail）
_OK_TEXT = _reviews(_pass("t0"), _fail("t1"))


class StubClient:
    """评审替身：按调用序吐出 canned 响应并记录调用。

    响应条目：str = 成功文本（Task 3 替身契约的最小形状）、LLMResponse =
    真客户端信封（失败/截断用）、Exception = 调用时抛出；handler 给出时
    优先按 (调用序号, 消息) 动态生成（切分测试用）。
    """

    def __init__(self, *responses, handler=None):
        self.responses = list(responses)
        self.handler = handler
        self.calls: list[list[dict]] = []
        self.run_ids: list[str] = []

    def complete(self, messages, run_id):
        self.calls.append(messages)
        self.run_ids.append(run_id)
        if self.handler is not None:
            return self.handler(len(self.calls), messages)
        if not self.responses:
            raise AssertionError("替身响应耗尽（收到的调用比预期多）")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _wire_ids(messages) -> list[str]:
    """从调用消息里抠出本次送审的 id 清单（清单 JSON 无换行，格式行单行）。"""
    blob = messages[1]["content"].split("## 待评审标签\n", 1)[1]
    return [item["id"] for item in json.loads(blob.rsplit("\n", 1)[0])]


class MainLLM:
    """无 zero_network 能力位的最小主客户端替身（触发真实构造分支）。"""

    model = "main-model"


# ---- 评审主流程：正常解析 / 消息形状 ----


def test_review_normal_pass_and_fail():
    client = StubClient(_OK_TEXT)
    rows = _rows()
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert result is rows  # 同序同对象，追加而非重建
    assert [row["id"] for row in result] == ["t0", "t1"]
    assert result[0]["pass"] is True
    assert result[0]["reason"] == "台词有据"
    assert result[0]["quote"] == QUOTE
    assert result[1]["pass"] is False
    assert result[1]["reason"] == "材料无相关细节"
    assert result[1]["quote"] is None
    # run_id 原样透传；system = REVIEW_PROMPT 逐字
    assert client.run_ids == ["r1:m1"]
    assert client.calls[0][0]["role"] == "system"
    assert client.calls[0][0]["content"] == REVIEW_PROMPT
    user = client.calls[0][1]["content"]
    assert "## 待评审标签" in user
    assert REVIEW_OUTPUT_FORMAT in user
    assert "台词：陈晓：我要逃离这个被安排好的世界。" in user  # 渲染材料含台词


def test_review_listing_projects_five_fields_only():
    """送审清单只投影 id/name/desc/level/scenes 五字段——调用方行里即便
    带了生成侧 reason 也不进提示（防同模锚定）。"""
    rows = _rows()
    rows[0]["reason"] = "生成侧理由不该进评审提示"
    client = StubClient(_OK_TEXT)
    review_movie_tags(client, _content(), rows, run_id="r1:m1")
    listing = json.loads(
        client.calls[0][1]["content"].split("## 待评审标签\n", 1)[1].rsplit("\n", 1)[0])
    assert [item["id"] for item in listing] == ["t0", "t1"]
    assert all(set(item) == {"id", "name", "desc", "level", "scenes"}
               for item in listing)


def test_review_honors_modalities():
    """关掉 dialogue：台词不进渲染文本 → 台词摘录核不中 → pass 改判 fail。"""
    client = StubClient(_reviews(_pass("t0")))
    rows = _rows()
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1",
                               modalities=("script",))
    assert result[0]["pass"] is False
    assert "摘录未命中原文" in result[0]["reason"]
    assert "台词：" not in client.calls[0][1]["content"]


def test_review_empty_material_raises():
    """dialogue-only modalities：render_content 对 script 关闭静默返回空
    材料（台词在 script 通道渲染）→ 摘录核验无从进行，按评审失败语义
    响亮抛错（绝不产出貌似合法的全 fail 报告）。"""
    client = StubClient(_OK_TEXT)
    with pytest.raises(ReviewRequestError) as exc_info:
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1",
                          modalities=("dialogue",))
    assert "评审材料为空" in str(exc_info.value)
    assert "摘录核验无从进行" in str(exc_info.value)
    assert client.calls == []  # 材料不足不出请求，不烧 token


# ---- 解析容错（Review Focus 1：畸形家族） ----


def test_review_fenced_output():
    client = StubClient("```json\n" + _OK_TEXT + "\n```")
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert result[1]["pass"] is False


def test_review_fenced_output_with_crlf():
    """围栏行带 \r\n（端点流式输出的换行漂移）同样剥净。"""
    client = StubClient("```json\r\n" + _OK_TEXT + "\r\n```")
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert result[1]["pass"] is False


def test_review_single_object_reviews_form():
    """reviews 为按 id 索引的单对象（147.3 漂移形状）时展平。"""
    text = json.dumps({"reviews": {
        "t0": {"pass": True, "reason": "台词有据", "quote": QUOTE},
        "t1": {"pass": False, "reason": "材料无相关细节"},
    }}, ensure_ascii=False)
    client = StubClient(text)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert result[1]["pass"] is False


def test_review_alias_key_and_non_string_ids():
    """is_pass 别名键兼容；id 容忍 0 / "1" 等非 "t" 前缀形状。"""
    text = json.dumps({"reviews": [
        {"id": 0, "is_pass": True, "reason": "台词有据", "quote": QUOTE},
        {"id": "1", "pass": False, "reason": "材料无相关细节"},
    ]}, ensure_ascii=False)
    client = StubClient(text)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert result[1]["pass"] is False


# ---- 漏判语义 ----


def test_review_partial_miss_marks_none():
    """漏判一行：该行 pass=None 占位（不判死），其余行正常。"""
    client = StubClient(_reviews(_pass("t0")))
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert result[1]["pass"] is None
    assert result[1]["reason"] == "评审未返回该条判定"
    assert result[1]["quote"] is None


def test_review_unmatched_ids_all_missed_raises():
    """模型按名称而非 id 回填（147.3 前科）：全部漏判 → ReviewRequestError。"""
    client = StubClient(_reviews({"id": "逃离安排", "pass": True, "reason": "有据"}))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")


def test_review_empty_reviews_list_all_missed_raises():
    client = StubClient(_reviews())
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")


# ---- 摘录必核（Review Focus 4） ----


def test_review_pass_quote_must_hit_material():
    """pass + 摘录命中 → 保留 pass；pass + 摘录不命中 / 缺摘录 → 改判 fail
    且 reason 注明（空口通过在这里拦下）。"""
    text = _reviews(
        _pass("t0"),
        {"id": "t1", "pass": True, "reason": "有跑步"},  # 缺 quote
    )
    client = StubClient(text)
    rows = _rows(3)
    rows[2]["name"] = "标签2"
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert result[0]["pass"] is True  # 正例：命中摘录原样保留
    assert result[1]["pass"] is False
    assert result[1]["reason"] == "有跑步；摘录未命中原文"
    assert result[2]["pass"] is None  # t3 行漏判照旧占位（本响应只回两条）


def test_review_quote_miss_appends_reason():
    client = StubClient(_reviews(_pass("t0", quote="这句台词在影片材料里完全不存在")))
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is False
    assert result[0]["reason"] == "台词有据；摘录未命中原文"


def test_review_quote_miss_with_empty_reason():
    client = StubClient(_reviews(_pass("t0", quote="这句台词在影片材料里完全不存在",
                                       reason="")))
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is False
    assert result[0]["reason"] == "摘录未命中原文"  # 空理由不出现开头分号


def test_review_quote_spanning_punctuation_stays_pass():
    """摘录跨标点边界仍 pass：核验对齐生产 judge（_require_quotes 对渲染
    文本先 _normalize_quote 再核）——quote 的 8 字窗跨过「，」时 raw 核验
    会误杀真 pass，把通过率压低、标定 FN 抬高。"""
    text = _reviews(_pass("t0", quote="小镇街道晨跑，电台广播"))
    client = StubClient(text)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True  # 归一化后「小镇街道晨跑电台广播」命中
    assert result[0]["reason"] == "台词有据"
    assert result[0]["quote"] == "小镇街道晨跑，电台广播"


# ---- 调用失败 / 格式重试 / 超长切分 ----


def test_review_transport_exception_raises():
    client = StubClient(RuntimeError("连接评审端点超时"))
    with pytest.raises(ReviewRequestError) as exc_info:
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert "评审调用失败" in str(exc_info.value)
    assert "连接评审端点超时" in str(exc_info.value)


def test_review_failed_envelope_raises():
    """真客户端信封 ok=False（错误信息在 raw.error）→ ReviewRequestError。"""
    client = StubClient(LLMResponse("", {}, {"error": "评审端点 502"}, False))
    with pytest.raises(ReviewRequestError) as exc_info:
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert "评审端点 502" in str(exc_info.value)


def test_review_failed_envelope_not_mistaken_as_truncated():
    """ok=False 信封即便 raw 带着 finish_reason=length 也不判截断——失败
    请求走「评审调用失败」口径（tag_quality 的 ok 门镜像），不进切分。"""
    client = StubClient(LLMResponse(
        "", {}, {"error": "评审端点 502",
                 "choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
        False))
    with pytest.raises(ReviewRequestError) as exc_info:
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert "评审端点 502" in str(exc_info.value)
    assert "超出长度限制" not in str(exc_info.value)
    assert len(client.calls) == 1  # 失败信封不触发二分


def test_review_format_retry_once():
    """首回不合法 → 附格式要求重发一次；重试成功后照常评审。"""
    client = StubClient("抱歉，我无法以 JSON 输出，以下是逐条评审：t0 通过。",
                        _OK_TEXT)
    result = review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert result[0]["pass"] is True
    assert len(client.calls) == 2
    assert REVIEW_RETRY_HINT not in client.calls[0][1]["content"]
    assert client.calls[1][1]["content"].endswith(REVIEW_RETRY_HINT)


def test_review_two_parse_failures_raise():
    client = StubClient("第一次说不清", "第二次还是说不清")
    with pytest.raises(ReviewRequestError) as exc_info:
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert "无法解析" in str(exc_info.value)
    assert len(client.calls) == 2  # 格式重试恰好一次


def test_review_truncated_retry_reports_length_error():
    """重试响应截断 → 记「输出超出长度限制」而非误报「无法解析」（重试
    响应同样过 _truncated 检查，失败原因如实）。"""
    client = StubClient(
        "抱歉，我无法以 JSON 输出。",
        LLMResponse("", {},
                    {"choices": [{"message": {"content": ""},
                                  "finish_reason": "length"}]},
                    ok=True))
    with pytest.raises(ReviewRequestError) as exc_info:
        review_movie_tags(client, _content(), _rows(), run_id="r1:m1")
    assert "超出长度限制" in str(exc_info.value)
    assert "无法解析" not in str(exc_info.value)
    assert len(client.calls) == 2


def test_review_splits_on_input_limit():
    """首回报输入超长 → 二分重试（镜像 tag_quality 切分行为）。"""
    rows = _rows(4)

    def handler(index, messages):
        if index == 1:
            return LLMResponse("", {},
                               {"error": "该请求超出 maximum context length 上限"},
                               False)
        return _reviews(*(_pass(row_id) for row_id in _wire_ids(messages)))

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert len(client.calls) == 3  # 1 次整批（超长）+ 2 次对半
    assert _wire_ids(client.calls[0]) == ["t0", "t1", "t2", "t3"]
    assert _wire_ids(client.calls[1]) == ["t0", "t1"]
    assert _wire_ids(client.calls[2]) == ["t0", "t1"]  # 批内 id 按位置重编
    assert [row["pass"] for row in result] == [True] * 4


def test_review_single_row_input_limit_raises():
    """单行仍超长（切无可切）→ 评审失败，不静默判 0。"""
    client = StubClient(LLMResponse("", {}, {"error": "input is too long"}, False))
    with pytest.raises(ReviewRequestError):
        review_movie_tags(client, _content(), _rows(1), run_id="r1:m1")


def test_review_output_truncated_splits():
    """finish_reason=length（输出截断）同样走二分——镜像 tag_quality。"""
    rows = _rows(2)

    def handler(index, messages):
        if index == 1:
            return LLMResponse("", {},
                               {"choices": [{"message": {"content": ""},
                                             "finish_reason": "length"}]},
                               ok=True)
        return _reviews(*(_fail(row_id) for row_id in _wire_ids(messages)))

    client = StubClient(handler=handler)
    result = review_movie_tags(client, _content(), rows, run_id="r1:m1")
    assert len(client.calls) == 3  # 1 次整批（截断）+ 2 个单行子批
    assert _wire_ids(client.calls[1]) == ["t0"]
    assert _wire_ids(client.calls[2]) == ["t0"]  # 批内 id 按位置重编
    assert [row["pass"] for row in result] == [False, False]


def test_review_empty_rows_returns_empty():
    assert review_movie_tags(StubClient(), _content(), [], run_id="r1:m1") == []


# ---- 客户端工厂与评审口径 ----


def test_make_review_client_same_model_fallback_identity():
    """评审三键全空 = 回落主客户端本身（同模评审，spec §6）。"""
    settings = Settings()
    llm = MainLLM()
    assert make_review_client(settings, llm) is llm


def test_make_review_client_mock_and_zero_network_reuse_llm():
    """mock 开关或零网络替身（zero_network 能力位）→ 直接复用传入客户端。"""
    llm = MockLLMClient()
    assert make_review_client(Settings(use_mock_llm=True,
                                       open_eval_review_model="review-x"), llm) is llm
    assert make_review_client(Settings(open_eval_review_model="review-x"), llm) is llm


def test_make_review_client_builds_independent_client():
    settings = Settings(
        open_eval_review_model="review-x",
        open_eval_review_base_url_env="REVIEW_BASE",
        open_eval_review_api_key_env="REVIEW_KEY",
        llm_base_url_env="MAIN_BASE",
        llm_api_key_env="MAIN_KEY",
        llm_request_interval=0.7,
    )
    client = make_review_client(settings, MainLLM())
    assert isinstance(client, OpenAIClient)
    assert client.model == "review-x"
    assert client.base_url_env == "REVIEW_BASE"
    assert client.api_key_env == "REVIEW_KEY"
    assert client.temperature == 0.0  # 评审恒 0 温度
    assert client.request_interval == 0.7


def test_make_review_client_env_names_fall_back_to_main():
    """model 配了而 env 名空 → env 名逐键回落主 LLM 设置。"""
    settings = Settings(open_eval_review_model="review-x")
    client = make_review_client(settings, MainLLM())
    assert client.base_url_env == settings.llm_base_url_env
    assert client.api_key_env == settings.llm_api_key_env


def test_review_calibre_fallback_is_same_model():
    settings = Settings()
    assert review_calibre(settings, MainLLM()) == {
        "model": settings.llm_model, "same_model": True}


def test_review_calibre_independent_model():
    settings = Settings(open_eval_review_model="review-x", llm_model="main-model")
    assert review_calibre(settings, MainLLM()) == {
        "model": "review-x", "same_model": False}


def test_review_calibre_same_name_is_same_model():
    settings = Settings(open_eval_review_model="main-model", llm_model="main-model")
    assert review_calibre(settings, MainLLM())["same_model"] is True


# ---- settings 默认值 ----


def test_settings_open_eval_defaults():
    settings = Settings()
    assert settings.open_eval_output_root == "data/eval/open"
    assert settings.open_eval_review_model == ""
    assert settings.open_eval_review_base_url_env == ""
    assert settings.open_eval_review_api_key_env == ""
