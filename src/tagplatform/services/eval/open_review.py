"""开放评测的三维独立评审层：调用 / 解析 / 摘录核验 / 超长切分。

open-eval 运行器在 produce 之后对每片全部产出标签（新词 + 库内命中）做
一次独立三维评审（open-review-v2）：复用 tag-quality-v4 canonical rubric
逐维评 relevance/semantics/value（0-4 或 null），每条附生产 judge 的三维
分作参考（judge_reference，明确仅供参考）。通过与否由本层按阈值推导
（REVIEW_PASS_FLOORS，与生产 judge 保留阈值对齐），模型只给分。语义边界：

- 一片一批（全部标签一次请求，超长才切分——镜像 tag_quality 的二分
  行为，输入超长与输出截断两种触发都切）；
- 解析纪律：剥首尾整圈 ``` 围栏、reviews/tags 单对象（按 id 索引的
  dict）容忍、裸列表容忍、id 容忍 "t3" / 3 / "3"；维度对象缺 score 键 /
  非法分值 / 重复 id 的行按漏判处理——pass=None（不判通过也不判不通过，
  排除出分母、计 review_missing），全部行漏判 → ReviewRequestError
  （评审失败 ≠ 评审 0 分）；显式 null 分是"无法判断"，pass=False 且计入
  undetermined，与漏判不同；
- 摘录必核：relevance ≥3 且 quote 经 _quote_ok 核不中**归一化**渲染文本
  （对齐生产 judge 的 _require_quotes 判法）→ relevance 降档到 1、理由
  记「摘录未命中原文」——与生产 judge「引用不过降档」同款纪律，防评审员
  空口给高分；
- 格式重试一次（附格式要求重发，镜像 judge/verify 的重试后缀），仍
  失败 → ReviewRequestError；请求失败（传输异常 / ok=False 信封）同样
  → ReviewRequestError。评审失败由上层记 review_failed（produce 产出
  完整保留，不判 0），本层不写盘、不碰 DB。

评审客户端与主客户端同构（make_review_client 通道）：评审三键
（open_eval_review_model / _base_url_env / _api_key_env）全空或
mock / 零网络替身时回落传入主客户端（同模评审）；否则按三键另建真实
客户端，env 名逐键回落主 LLM 设置。
"""
from __future__ import annotations

import json
import logging

from tagplatform.capabilities.prompts.open_review import (
    REVIEW_PROMPT, REVIEW_PROMPT_VERSION, REVIEW_RETRY_HINT,
)
from tagplatform.schemas import ContentJSON
from tagplatform.services.generation.open.mtg.pipeline import (
    _normalize_quote, _quote_ok,
)
from tagplatform.services.generation.open.mtg.prompts import render_content
from tagplatform.settings import Settings

logger = logging.getLogger(__name__)

# REVIEW_PROMPT_VERSION 从 capabilities/prompts.open_review 同驻 re-export：
# canonical 在 prompt 常量模块（文案与版本同驻），这里转出口给运行器记账
# （meta.review.prompt_version），import 路径与 make_review_client 对齐。
__all__ = [
    "REVIEW_PASS_FLOORS",
    "REVIEW_PROMPT_VERSION",
    "ReviewRequestError",
    "make_review_client",
    "review_calibre",
    "review_movie_tags",
]


# 评审通过阈值：与生产 judge 的保留阈值（pipeline._JUDGE_SCORE_FLOORS）
# 对齐——relevance/semantics ≥3、value ≥2。评审通过率即"独立评审认为达到
# 生产保留标准的标签占比"。null 分（无法判断）不算通过。
REVIEW_PASS_FLOORS = {"relevance": 3, "semantics": 3, "value": 2}


class ReviewRequestError(RuntimeError):
    """评审调用失败（请求失败 / 格式两连败 / 全部漏判）。

    上层（运行器）按「该片评审失败」处理：produce 产出完整保留、该片
    全部标签不计入通过率/标定（None≠0 纪律），绝不判 0。
    """


def make_review_client(settings: Settings, llm):
    """评审客户端工厂：未配独立评审端点或替身场景直接复用主客户端。

    - settings.use_mock_llm 或主客户端声明 zero_network 能力位（零网络
      替身）→ 返回 llm（能力探测而非类型判断，与 mtg 管线的
      _make_stage_client 同款——管线因此不 import 测试替身的类型）；
    - 评审三键全空（未配置独立评审端点）→ 返回 llm（同模评审，spec §6）；
    - 否则按评审模型 + env 名逐键回落另建真实客户端（model 必填，两个
      env 名空时用主 LLM 的 base_url/api_key 环境变量名）。温度恒 0
      （评审是判定不是创作），限速沿用主 LLM 设置。
    """
    if settings.use_mock_llm or getattr(llm, "zero_network", False):
        return llm
    if not settings.open_eval_review_model:
        return llm
    from tagplatform.capabilities.llm import OpenAIClient
    return OpenAIClient(
        model=settings.open_eval_review_model,
        base_url_env=settings.open_eval_review_base_url_env or settings.llm_base_url_env,
        api_key_env=settings.open_eval_review_api_key_env or settings.llm_api_key_env,
        temperature=0.0,
        request_interval=settings.llm_request_interval,
    )


def review_calibre(settings: Settings, llm) -> dict:
    """评审口径（meta 直用）：评审模型名 + 是否与生成同模。

    三键全空 = 回落主 LLM（同模评审），模型名取 settings.llm_model——
    刻意不读 llm.model：mock 替身的 model 属性是替身名（如 mock-1），
    写进 meta 会让同模警示误报。llm 参数仅为与 make_review_client 的
    调用形状对齐。
    """
    model = settings.open_eval_review_model or settings.llm_model
    return {"model": model, "same_model": model == settings.llm_model}


def review_movie_tags(client, content: ContentJSON, tag_rows: list[dict], *,
                      run_id: str, modalities=("script", "dialogue")) -> list[dict]:
    """对一片的全部产出标签做三维独立评审，同序回填三维分与推导的 pass。

    入参行 {"id","name","desc","level","scenes":[scene_id,...]，可选
    "judge_reference"}；返回同序行追加 {"relevance"/"semantics"/"value":
    {"score","reason"}, "pass": bool | None, "reason": str, "quote"}。送进
    模型的 id 一律按批次位置重编为 "t0".."tN"（防名称撞车——模型按 id
    回填，不按名称匹配），结果按位置映射回原行：入参行的 id 与其余字段
    原样保留；即便行里带了生成侧 reason 等多余字段也不会进提示（防同模
    锚定——清单只投影评审需要的字段）。

    抛 ReviewRequestError 的四种情况：材料为空（modalities 未含 script
    ——台词在 script 通道渲染，摘录核验无从进行，绝不产出假的全 fail
    报告）、请求失败（含传输异常）、同一批两次解析失败、全部行漏判。
    部分漏判不抛——漏判行 pass=None 占位，由指标层排除出分母（None≠0
    纪律）。
    """
    if not tag_rows:
        return []
    haystack = render_content(content, modalities=modalities)
    # render_content 对 script 关闭静默返回 ""（台词随 script 通道一起被
    # 关掉）——空材料下摘录核验无从进行，每个 pass 都会被拦成 fail，
    # 得到一份貌似合法的全 fail 报告（最坏的静默错误形状）。本层自防：
    # 按评审失败语义响亮抛出，而非依赖上层起跑校验。
    if not haystack.strip():
        raise ReviewRequestError("评审材料为空（modalities 未含 script），摘录核验无从进行")
    results: dict[int, dict] = {}
    errors: list[str] = []

    def batch(rows_slice: list[dict], offset: int) -> None:
        if errors:  # 已有硬失败（端点死亡/协议失败）：后续批次不再烧 token
            return
        messages = _build_messages(haystack, rows_slice)
        try:
            response = client.complete(messages, run_id)
        except Exception as exc:  # noqa: BLE001 —— 端点任何异常统一按调用失败记
            errors.append(f"评审调用失败：{exc}")
            return
        text, ok, transport_error = _unwrap(response)
        truncated = _truncated(response)
        if (truncated or (not ok and _input_limit(transport_error.lower()))) \
                and len(rows_slice) > 1:
            middle = len(rows_slice) // 2
            logger.info("评审请求超长，二分重试 run_id=%s batch_sizes=%d,%d",
                        run_id, middle, len(rows_slice) - middle)
            batch(rows_slice[:middle], offset)
            batch(rows_slice[middle:], offset + middle)
            return
        if not ok:
            errors.append(f"评审调用失败：{transport_error}")
            return
        if truncated:
            errors.append("评审输出超出长度限制")
            return
        verdicts = _parse_with_retry(client, messages, text, len(rows_slice),
                                     run_id, errors)
        if errors or verdicts is None:
            return
        for index, verdict in verdicts.items():
            results[offset + index] = verdict  # 批内 id 重编 → 全局位置回填

    batch(tag_rows, 0)
    if errors:
        raise ReviewRequestError("；".join(dict.fromkeys(errors)))
    if not results:
        raise ReviewRequestError("评审响应未包含任何有效判决（全部漏判）")
    # 摘录必核：高 relevance 分且摘录核不中 → 降档到 1（空口高分在这里拦下）。
    # 核验 haystack 归一化——对齐生产 judge 的判法（_require_quotes 对
    # render_content 结果先 _normalize_quote 再核）：quote 的 8 字窗跨过
    # 标点/换行时，raw 文本中间隔着标点会误杀真高分。
    quote_haystack = _normalize_quote(haystack)
    for verdict in results.values():
        relevance = verdict["relevance"]["score"]
        if isinstance(relevance, int) and relevance >= REVIEW_PASS_FLOORS["relevance"] \
                and not _quote_ok(verdict["quote"], quote_haystack):
            verdict["relevance"] = {"score": 1, "reason": "摘录未命中原文"}
    # 同序回填：漏判行占位 None（排除出分母，计 review_missing 由上层做）；
    # pass 由三维分按阈值推导——显式 null 分不算通过（计 undetermined）。
    for index, row in enumerate(tag_rows):
        verdict = results.get(index)
        if verdict is None:
            verdict = {axis: {"score": None, "reason": "评审未返回该条判定"}
                       for axis in REVIEW_PASS_FLOORS}
            verdict.update({"pass": None, "reason": "评审未返回该条判定",
                            "quote": None})
        else:
            verdict["pass"] = all(
                isinstance(verdict[axis]["score"], int)
                and verdict[axis]["score"] >= floor
                for axis, floor in REVIEW_PASS_FLOORS.items())
            verdict["reason"] = "；".join(
                verdict[axis]["reason"] for axis in REVIEW_PASS_FLOORS
                if verdict[axis]["reason"])[:200]
        row.update(verdict)
    return tag_rows


def _build_messages(haystack: str, rows: list[dict]) -> list[dict]:
    """评审消息：system = REVIEW_PROMPT（rubric + 补充 + 协议），user = 材料 + 清单。

    清单按批次位置重编 id（"t0".."tN"）并只投影评审字段——防名称撞车与
    生成侧 reason 锚定；judge_reference（生产 judge 三维分与理由）仅在
    行里有时附上，rubric 补充说明已把它定位为仅供参考。清单标题与 rubric
    的「待评估标签」一致（Mock 质量分支也按这个标题切分材料）。
    """
    listing = []
    for position, row in enumerate(rows):
        entry = {
            "id": f"t{position}",
            "name": row.get("name"),
            "desc": row.get("desc"),
            "level": row.get("level"),
            "scenes": list(row.get("scenes") or []),
        }
        if row.get("judge_reference") is not None:
            entry["judge_reference"] = row["judge_reference"]
        listing.append(entry)
    user_content = (
        haystack
        + "\n\n## 待评估标签\n"
        + json.dumps(listing, ensure_ascii=False)
    )
    return [
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_with_retry(client, messages: list[dict], text: str, total: int,
                      run_id: str, errors: list[str]) -> dict[int, dict] | None:
    """解析评审响应；格式重试一次（附格式要求重发），两连败记入 errors。

    返回 {批次内行序号: 判决}（可能为空 = 本批全漏判，由影片级统一裁决
    是否全漏）；硬失败返回 None（errors 已带上原因）。
    """
    last_error: Exception | None = None
    for attempt in range(2):
        if attempt:
            try:
                response = client.complete(
                    [messages[0],
                     {"role": "user",
                      "content": messages[1]["content"] + REVIEW_RETRY_HINT}],
                    run_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"评审调用失败：{exc}")
                return None
            retry_text, ok, transport_error = _unwrap(response)
            if not ok:
                errors.append(f"评审调用失败：{transport_error}")
                return None
            if _truncated(response):
                errors.append("评审输出超出长度限制")
                return None
            text = retry_text
        try:
            return _parse_reviews(text, total)
        except ValueError as exc:
            last_error = exc
    errors.append(f"评审响应无法解析：{last_error}")
    return None


def _parse_reviews(raw: str, total: int) -> dict[int, dict]:
    """响应文本 → {批次内行序号: {relevance/semantics/value, quote}}。

    畸形家族容错：首尾整圈 ``` 围栏剥除（tag_quality 同款）、直读失败按
    首尾大括号抠对象（verify._loads 同款）、reviews/tags 为按 id 索引的单
    对象时展平、裸列表容忍。维度对象缺 score 键 / 非法分值 / id 出界 /
    重复 id（镜像 tag_quality：重复即不可靠）的条目按漏判处理——不进
    结果，不抛错。显式 null 分有效（无法判断），与漏判不同。
    """
    text = raw.strip()
    first, separator, rest = text.partition("\n")
    if separator and first.strip().lower() in ("```json", "```") \
            and rest.rstrip().endswith("```"):
        text = rest.rstrip()[:-3].strip()
    data = _loads_with_prose_fallback(text)
    if isinstance(data, dict) and isinstance(data.get("reviews"), dict):
        items = [{"id": key, **value} for key, value in data["reviews"].items()
                 if isinstance(value, dict)]
    elif isinstance(data, dict) and isinstance(data.get("reviews"), list):
        items = [item for item in data["reviews"] if isinstance(item, dict)]
    elif isinstance(data, dict) and isinstance(data.get("tags"), list):
        # rubric 原输出形状（{"tags":[...]}）容忍：补充说明已要求 reviews，
        # 但模型照 rubric 末尾形状回也能解析。
        items = [item for item in data["tags"] if isinstance(item, dict)]
    elif isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
    else:
        raise ValueError("评审响应缺少 reviews 列表")
    results: dict[int, dict] = {}
    for item in items:
        index = _row_index(item.get("id"), total)
        if index is None:
            continue
        if index in results:
            results.pop(index)
            continue
        dimensions: dict[str, dict] = {}
        for axis in REVIEW_PASS_FLOORS:
            if axis not in item:  # 缺维度键 = 未完成（显式 null 才是无法判断）
                break
            score, reason = _clean_dimension(item[axis])
            if score is _MISSING:
                break
            dimensions[axis] = {"score": score, "reason": reason}
        if len(dimensions) != len(REVIEW_PASS_FLOORS):
            continue
        quote = item.get("quote")
        results[index] = {
            **dimensions,
            "quote": quote if isinstance(quote, str) else None,
        }
    return results


# 维度形状非法的哨兵（与有效 null 分区分：null 是"无法判断"，非法是漏判）
_MISSING = object()


def _clean_dimension(value) -> tuple:
    """维度值 → (score, reason)；形状非法返回 (_MISSING, "")（整行按漏判）。

    容忍 {"score","reason"} 对象与裸分值两种形状；score 只认 0-4 的整数
    （含整数值浮点）或显式 null，布尔/越界/缺 score 键都算未完成。
    """
    if isinstance(value, dict):
        if "score" not in value:
            return _MISSING, ""
        score = value["score"]
        reason = value.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""
    else:
        score, reason = value, ""
    if score is None:
        return None, reason
    if isinstance(score, bool):
        return _MISSING, ""
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    if isinstance(score, int) and 0 <= score <= 4:
        return score, reason
    return _MISSING, ""


def _loads_with_prose_fallback(text: str):
    """json.loads 直读失败时按首尾大括号抠对象（verify._loads 同款容错）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("评审响应中没有 JSON 对象")
    return json.loads(text[start:end + 1])


def _row_index(value, total: int) -> int | None:
    """响应 id → 批次内行序号：容忍 "t3" / 3 / "3"；出界或不可解析 → None。"""
    text = str(value).strip().lower()
    if text.startswith("t"):
        text = text[1:]
    if not text.isdigit():
        return None
    index = int(text)
    return index if index < total else None


def _unwrap(response) -> tuple[str, bool, str]:
    """评审客户端响应归一：真客户端 / mock 回 LLMResponse，简化替身回文本。

    返回 (text, ok, error)：纯文本替身按成功处理（测试替身契约的最小
    形状 .complete(messages, run_id) -> str）；LLMResponse 失败时给
    raw.error 错误串。
    """
    if isinstance(response, str):
        return response, True, ""
    ok = bool(getattr(response, "ok", True))
    if not ok:
        raw = getattr(response, "raw", None) or {}
        return "", False, str(raw.get("error") or "模型未返回成功响应")
    return getattr(response, "content", None) or "", True, ""


def _truncated(response) -> bool:
    """finish_reason == "length" 判定；输出截断同样走切分（镜像 tag_quality）。

    ok=False 信封不判截断（失败请求的 raw 里即便带着 length 也要走
    「评审调用失败」口径，tag_quality 同款 `response.ok and ...` 门）。
    纯文本替身没有 raw 信封，恒 False。
    """
    if not getattr(response, "ok", True):
        return False
    raw = getattr(response, "raw", None) or {}
    choices = raw.get("choices") or [{}]
    return choices[0].get("finish_reason") == "length"


def _input_limit(error: str) -> bool:
    """输入超长判别式（照抄 tag_quality 的判别式，本地实现——不跨模块
    import 私有函数）。"""
    return any(term in error for term in (
        "context_length_exceeded", "maximum context length", "context window",
        "input is too long", "input length", "payload too large", "request too large",
        "too many images", "413 request", "上下文长度", "输入过长",
    ))
