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

VERIFY_MARKER = "## 相似度复核"

# 单次最多复核多少对：一部片子通常 5-10 对，40 是上限保险
_MAX_PAIRS = 40

_RETRY_HINT = (
    "\n\n上一次输出无法解析：只输出一个 JSON 对象，形如 "
    '{"results":[{"candidate":"候选原文","same":true}]}，'
    "不要解释、不要 Markdown 代码块。"
)

logger = logging.getLogger(__name__)


def build_verify_messages(pairs: list[tuple[str, str, float]]) -> list[dict]:
    """构造复核请求：每行一对（候选｜库标签｜相似度）。"""
    lines = "\n".join(f"- {cand}｜{lib}｜{cos:.2f}"
                      for cand, lib, cos in pairs[:_MAX_PAIRS])
    text = (
        f"{VERIFY_MARKER}\n"
        "下面每行是一组「候选标签｜已有标签｜相似度」。判断两者是不是同一个概念。\n"
        "判断标准只有一条：能不能直接拿已有标签替换候选，而不丢这部片子的信息。\n"
        "以下都不算同一个概念：\n"
        "1. 意思相反或方向相反（例如“非线性叙事”与“线性叙事”）；\n"
        "2. 只是字面相近、侧重点不同（例如“家族惨剧”与“家族恩怨”）；\n"
        "3. 候选比已有标签多出实质信息、替换会丢掉这层信息。\n"
        f"{lines}\n"
        '只输出一个 JSON 对象：{"results":[{"candidate":"候选原文","same":true}]}，'
        "每行都要给一条，不要解释、不要 Markdown 代码块。"
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
                 retries: int = 1) -> dict[str, bool]:
    """批量复核；失败返回已解析的部分（可能为空），不抛异常。"""
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
            usage["calls"] = usage.get("calls", 0) + 1
            for key in ("prompt_tokens", "completion_tokens"):
                usage[key] = (usage.get(key, 0)
                              + int((response.usage or {}).get(key, 0) or 0))
        if not getattr(response, "ok", True):
            logger.warning("相似度复核响应异常：%s",
                           (response.raw or {}).get("error"))
            return {}
        try:
            return parse_verify_results(response.content)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("相似度复核输出无法解析（第 %d 次）：%s", attempt + 1, exc)
    return {}
