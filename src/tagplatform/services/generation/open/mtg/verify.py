"""相似度复核：对"疑似重复"的（候选, 库标签）对子做一次 LLM 判定。

为什么需要这一步：规则层（余弦 / 名字包含）只能判断"像不像"，判断不了
"是不是同一个意思"。已知反例是"非线性叙事"与"线性叙事"——字面几乎相同，
意思相反；也有一批"相关但不同"的对子（"阶级偏见" vs "阶层与贫富"）分数
高得足以过线。实测在现行阈值下精确率只有 62%（人工标注 75 条），所以
"要不要把候选替换成库里的标准名"不能只靠分数。

成本控制：一批只调一次模型（一条 prompt 里放全部对子），temperature 与
judge 一致取 0。

失败口径与 judge 相反：复核失败**不阻塞任务**，只当"没复核"处理（不替换），
并在审计里留记录——替换错了会污染标签语义，宁可不替换。
"""
from __future__ import annotations

import json
import logging

from tagplatform.capabilities.llm.usage import accumulate_usage

VERIFY_MARKER = "## 相似度复核"

# 单次最多复核多少对：一部片子通常 5-10 对，40 是上限保险
_MAX_PAIRS = 40

_RETRY_HINT = (
    "\n\n上一次输出无法解析：只输出一个 JSON 对象，形如 "
    '{"results":[{"candidate":"候选原文","same":true}]}，'
    "不要解释、不要 Markdown 代码块。"
)

# ---- 指令文案（与解析器互为协议）----
# 提取自 _verify_messages 的函数体字面量，逐字未改。本模块的
# parse_verify_results 就是这段文案的解析端：候选行 `- 候选｜库标签｜相似度`
# 的行格式、输出 JSON 形状 {"results":[{"candidate","same"}]}、"每行都要给一条"
# 两侧必须同改——文案要求什么、解析就认什么，改动只落一侧=静默丢对子。
VERIFY_INSTRUCTIONS = (
    "下面每行是一组「候选标签｜已有标签｜相似度」。\n"
    "判断标准只有一条：如果这两个词都进了标签库，审核的人会不会认为它们是"
    "重复条目、只该留一条？会 → same=true，否则 false。\n"
    "答 true 的典型情况（同一含义的详略/近义说法，合并后信息不丢，"
    "原措辞会作为别名保留）：\n"
    "- 压抑沉重 / 压抑\n"
    "- 古典史诗 / 宏大史诗\n"
    "- 悬疑神秘 / 悬疑\n"
    "答 false 的三种情况：\n"
    "1. 意思相反（非线性叙事 / 线性叙事）；\n"
    "2. 字面像但指不同事物（友情与背叛 / 权力与背叛；超能力觉醒 / 绝境觉醒）；\n"
    "3. 候选是另一个独立概念，只是共享了词（暴君多疑 / 多疑 视情况，"
    "共享词但主体不同的判 false）。\n"
)
VERIFY_OUTPUT_FORMAT = (
    '只输出一个 JSON 对象：{"results":[{"candidate":"候选原文","same":true}]}，'
    "每行都要给一条，不要解释、不要 Markdown 代码块。"
)

logger = logging.getLogger(__name__)


def build_verify_messages(pairs: list[tuple[str, str, float]]) -> list[dict]:
    """构造复核请求：每行一对（候选｜库标签｜相似度）。"""
    lines = "\n".join(_pair_line(candidate, library, f"{cosine:.2f}")
                      for candidate, library, cosine in pairs[:_MAX_PAIRS])
    return _verify_messages(lines)


def build_verify_template() -> dict:
    return {
        "messages": _verify_messages(_pair_line(
            "{{候选标签}}", "{{库内标签}}", "{{余弦相似度，两位小数}}")),
        "retry_suffix": _RETRY_HINT,
    }


def _pair_line(candidate: str, library: str, score: str) -> str:
    return f"- {candidate}｜{library}｜{score}"


def _verify_messages(lines: str) -> list[dict]:
    text = (
        f"{VERIFY_MARKER}\n"
        f"{VERIFY_INSTRUCTIONS}"
        f"{lines}\n"
        f"{VERIFY_OUTPUT_FORMAT}"
    )
    return [{"role": "user", "content": text}]


def _loads(text: str) -> dict:
    """从模型输出里抠出 JSON 对象：容忍代码块围栏与前后废话。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
        raw = raw.split("```")[0]
        raw = raw.split("\n", 1)[1] if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没有 JSON 对象")
    return json.loads(raw[start:end + 1])


def parse_verify_results(text: str) -> dict[str, bool]:
    """模型输出 → {候选名: 是否同一概念}（缺字段的条目丢弃）。"""
    data = _loads(text)
    results: dict[str, bool] = {}
    for row in data.get("results") or []:
        candidate = str(row.get("candidate") or "").strip()
        if candidate:
            results[candidate] = bool(row.get("same"))
    return results


def verify_pairs(client, pairs: list[tuple[str, str, float]], *,
                 run_id: str, usage: dict | None = None,
                 retries: int = 1, trace: dict | None = None) -> dict[str, bool]:
    """批量复核；失败返回已解析的部分（可能为空），不抛异常。

    trace 传入 dict 时写入最后一次原始输出（截断），供审计核对判定理由。
    """
    if not pairs:
        return {}
    messages = build_verify_messages(pairs)
    for attempt in range(retries + 1):
        request = messages if attempt == 0 else [
            {"role": "user", "content": messages[0]["content"] + _RETRY_HINT}]
        try:
            response = client.complete(request,
                                       f"{run_id}:verify{'' if attempt == 0 else attempt}")
        except Exception as exc:  # noqa: BLE001 —— 复核失败不该拖垮任务
            logger.warning("相似度复核调用失败：%s", exc)
            return {}
        if usage is not None:
            accumulate_usage(usage, response.usage)
        if not getattr(response, "ok", True):
            logger.warning("相似度复核响应异常：%s",
                           (response.raw or {}).get("error"))
            return {}
        if trace is not None:
            trace["raw"] = (response.content or "")[:2000]
        try:
            return parse_verify_results(response.content)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("相似度复核输出无法解析（第 %d 次）：%s", attempt + 1, exc)
    return {}
